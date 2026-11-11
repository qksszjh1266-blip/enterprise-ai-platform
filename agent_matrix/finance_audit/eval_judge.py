"""财务预审场景的裁判实现（实现 eval_platform.ports.LLMJudgePort）。

为什么不能复用 eval_platform.offline.RuleBasedJudge / supply_chain_bi.eval_judge.DataAnalysisJudge：
- RuleBasedJudge 按"回答逐句是否为上下文子串"判忠实度；预审结论是确定性模板（"审批路由：ESCALATE_HUMAN"、
  "¥3,800.00"），几乎没有一句是制度原文子串 → faithfulness≈0，门禁误阻断。
- DataAnalysisJudge 按 0.5×10^-d 数值容差比较；财务金额必须**分毫不差**（Decimal 精确相等），
  容差比较会把 3,200.00 与 3,200.004 视为一致，掩盖勾稽错误。
- 二者都不理解"审批路由"这一离散决策：路由判错（如把拆单判为 AUTO_APPROVE）是最严重的错误，
  不能被关键词命中率稀释。

FinanceAuditJudge（离线、确定性、零 token；reason 写明"规则裁判，非真人 / LLM 评价"）：

score_faithfulness（0~1）
    证据集：contexts 中 FIN_RULE_V1 块经 rule_engine.parse_rule_context 解析出的全部金额（Decimal）与
    finding 编码；制度父块的 section_path 集合。
    回答中可核验单元：rule_engine.extract_money_tokens 抽取的金额 token、"风险编码："行中的每个编码、
    "制度依据："行中的每个章节路径。
    金额 token 与证据金额 **Decimal 精确相等**才算有据；编码必须出现在 FIN_RULE_V1 finding 行或为
    MODEL_REVIEW / SYSTEM_GUARD 来源编码（这些编码不出现在规则上下文中，按"制度依据"行至少一个有据视为有据）；
    章节路径必须等于某个制度父块的 section_path。
    score = 有据单元数 / 总单元数；无可核验单元 → 1.0；有单元但 contexts 为空 → 0.0。
score_accuracy（0~1）
    expected_keywords 采用带前缀的结构化关键词，其余按 eval_platform.evaluators 同一 norm 文本子串匹配：
        "route:{ApprovalRoute.value}"   回答的 CONCLUSION_ROUTE_LINE 解析值必须精确相等
        "code:{ViolationCode.value}"    "风险编码："行中必须出现该编码（金额异常 / 重复 / 拆单检出）
        "nocode:{ViolationCode.value}"  "风险编码："行中不得出现该编码（误报检查）
        "cite:{section 末级名}"          "制度依据："行中某个 section_path 末级名精确相等
        "amount:{两位小数}"              回答中存在与之 Decimal 精确相等的金额 token
    **路由一票否决**：存在 route: 关键词且不匹配 → score = 0.0（reason 写明期望 / 实际路由）；
    否则 score = 命中数 / 关键词总数。reference_answer 仅在 expected_keywords 为空时作为单一文本关键词；
    二者皆空 → 抛 ValueError（EvalRunner 归因 JUDGE_ERROR）。
simulate_satisfaction（1~5，财务复核岗视角显式量表）
    1 空回答或无法解析路由行；2 有路由但无风险编码行；3 有路由 + 编码但无制度依据（且存在 finding）；
    4 路由 + 编码 + 制度依据齐全；5 在 4 的基础上：NEEDS_SUPPLEMENT 含补正事项 / ESCALATE_HUMAN 含人工复核单号 /
    AUTO_APPROVE 与 REJECT 含审批层级，且总长度 <= satisfaction_max_chars。

所有 JudgeScore.token_usage 为 0。
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal, InvalidOperation
from agent_matrix.finance_audit.agent import CONCLUSION_ROUTE_LINE, CONCLUSION_CODES_PREFIX, CONCLUSION_POLICY_PREFIX
from agent_matrix.finance_audit.models import ApprovalRoute, VIOLATION_POLICIES, FindingSource
from agent_matrix.finance_audit.rule_engine import extract_money_tokens, parse_rule_context

from data_platform.retrieval_engine.models import RetrievedParentChunk, UserContext
from eval_platform.models import EvalScenario, JudgeScore

ROUTE_KEYWORD_PREFIX = "route:"
CODE_KEYWORD_PREFIX = "code:"
NO_CODE_KEYWORD_PREFIX = "nocode:"
CITE_KEYWORD_PREFIX = "cite:"
AMOUNT_KEYWORD_PREFIX = "amount:"


class FinanceAuditJudge:
    """识别 FIN_RULE_V1 规则上下文与预审结论模板的确定性裁判（结构化满足 LLMJudgePort）。"""

    def __init__(self, *, satisfaction_max_chars: int = 800) -> None:
        """保存量表参数；无 I/O。"""
        self.satisfaction_max_chars = satisfaction_max_chars

    async def score_faithfulness(
        self,
        *,
        question: str,
        answer: str,
        contexts: list[RetrievedParentChunk],
    ) -> JudgeScore:
        """见模块文档 score_faithfulness。"""
        amounts, codes, sections = set(), set(), set()
        for context in contexts:
            parsed = parse_rule_context(context.full_content)
            if parsed is not None:
                for key in ('declared_total', 'computed_total', 'total_deviation', 'approval_amount', 'split_window_total'):
                    for value in parsed.get(key, []):
                        try:
                            amounts.add(Decimal(value))
                        except InvalidOperation:
                            pass
                for finding in parsed.get('finding', []):
                    fields = finding.split(' | ')
                    codes.add(fields[0])
                    if len(fields) == 4 and fields[3] != '-':
                        try:
                            amounts.add(Decimal(fields[3]))
                        except InvalidOperation:
                            pass
            else:
                sections.add(context.section_path)
        answer_sections = self._parse_policy_sections(answer)
        has_policy = any(s in sections for s in answer_sections)
        checks = [value in amounts for value in extract_money_tokens(answer)]
        for code in self._parse_codes(answer):
            policy = VIOLATION_POLICIES.get(code)
            nonrule = policy is not None and bool(policy.allowed_sources & {FindingSource.MODEL_REVIEW, FindingSource.SYSTEM_GUARD})
            checks.append(code in codes or (nonrule and has_policy))
        checks.extend(section in sections for section in answer_sections)
        score = sum(checks)/len(checks) if checks else 1.0
        return JudgeScore(score=score, reason='规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验')

    async def score_accuracy(
        self,
        *,
        question: str,
        answer: str,
        reference_answer: str | None,
        expected_keywords: list[str],
    ) -> JudgeScore:
        """见模块文档 score_accuracy（含路由一票否决）。"""
        keywords = expected_keywords or ([reference_answer] if reference_answer else [])
        if not keywords:
            raise ValueError('准确率需要期望关键词或参考答案')
        route, codes = self._parse_route(answer), self._parse_codes(answer)
        sections = self._parse_policy_sections(answer)
        amounts = extract_money_tokens(answer)
        hits = []
        for keyword in keywords:
            prefix, _, value = keyword.partition(':')
            if keyword.startswith(ROUTE_KEYWORD_PREFIX):
                if route != value:
                    return JudgeScore(score=0, reason=f'规则裁判，非真人 / LLM 评价；路由一票否决：期望 {value}，实际 {route}')
                hit = True
            elif keyword.startswith(CODE_KEYWORD_PREFIX):
                hit = value in codes
            elif keyword.startswith(NO_CODE_KEYWORD_PREFIX):
                hit = value not in codes
            elif keyword.startswith(CITE_KEYWORD_PREFIX):
                hit = any(s.split(' > ')[-1].split('/')[-1] == value for s in sections)
            elif keyword.startswith(AMOUNT_KEYWORD_PREFIX):
                hit = Decimal(value) in amounts
            else:
                hit = _normalize(keyword) in _normalize(answer)
            hits.append(hit)
        return JudgeScore(score=sum(hits)/len(hits), reason='规则裁判，非真人 / LLM 评价；结构化期望逐项核验')

    async def simulate_satisfaction(
        self,
        *,
        question: str,
        answer: str,
        scenario: EvalScenario,
        user_context: UserContext,
    ) -> JudgeScore:
        """见模块文档 simulate_satisfaction；scenario 非 FINANCE_APPROVAL 时仍按同一量表打分（不抛错）。"""
        route = self._parse_route(answer)
        if not answer.strip() or route is None:
            score = 1
        elif not any(line.startswith(CONCLUSION_CODES_PREFIX) for line in answer.splitlines()):
            score = 2
        elif self._parse_codes(answer) and not self._parse_policy_sections(answer):
            score = 3
        else:
            score = 4
            required = {'NEEDS_SUPPLEMENT': '补正事项：', 'ESCALATE_HUMAN': '人工复核单号：'}.get(route, '审批层级：')
            if required in answer and len(answer) <= self.satisfaction_max_chars:
                score = 5
        return JudgeScore(score=score, reason='规则裁判，非真人 / LLM 评价；财务复核岗五级量表')

    # ------------------------------------------------------------------
    # 解析辅助（纯函数）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_route(answer: str) -> str | None:
        """按 agent.CONCLUSION_ROUTE_LINE 解析路由值；未找到或值不属于 ApprovalRoute 返回 None。"""
        prefix = CONCLUSION_ROUTE_LINE.split('{route}')[0]
        for line in answer.splitlines():
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                return value if value in {r.value for r in ApprovalRoute} else None
        return None

    @staticmethod
    def _parse_codes(answer: str) -> list[str]:
        """解析 "风险编码：" 行中的编码列表（去掉括号内风险等级）；"无" → []。"""
        for line in answer.splitlines():
            if line.startswith(CONCLUSION_CODES_PREFIX):
                value = line[len(CONCLUSION_CODES_PREFIX):].strip()
                return [] if value == '无' else [part.split('(')[0].strip() for part in value.split('、')]
        return []

    @staticmethod
    def _parse_policy_sections(answer: str) -> list[str]:
        """解析 "制度依据：" 行中以 "；" 分隔的 section_path 列表。"""
        for line in answer.splitlines():
            if line.startswith(CONCLUSION_POLICY_PREFIX):
                return [p.strip() for p in line[len(CONCLUSION_POLICY_PREFIX):].split('；') if p.strip()]
        return []


def _normalize(text):
    return ''.join(unicodedata.normalize('NFKC', text).casefold().split())
