"""供应链数据分析场景的裁判实现（实现 eval_platform.ports.LLMJudgePort）。

为什么不能复用 eval_platform.offline.RuleBasedJudge：
- 其忠实度按"回答逐句是否为上下文子串"计算；BI 结论是对结果表的格式化归纳（0.0523 → 5.2%、
  "柠檬损耗最高"），几乎没有一句是 BI_RESULT_V1 原文子串 → faithfulness≈0，门禁 min_faithfulness 必然误阻断；
- 其满意度量表依赖"检查 / 审批 / 记录"等 SOP 动词，与数据分析的可执行性无关。

DataAnalysisJudge（离线、确定性、零 token；生产 LLM 裁判适配器可按相同端口另行实现）：

score_faithfulness（0~1）
    contexts 中解析出 BI_RESULT_V1 / BI_ALERTS_V1 的全部数值单元格为证据集 E。
    回答中抽取数字 token（剔除日期、含字母编码），每个 token 若能在 E 中找到满足容差的值即"有据"：
        |a − e| ≤ 0.5 × 10^-d（d = token 小数位数）；百分号 token 同时尝试 a/100 与 e 比较。
    score = 有据 token 数 / 总 token 数；无数字 token 时：有结果上下文 → 1.0，无上下文（拒答 / 追问）→ 1.0
    （拒答不添加事实）；有数字但无任何上下文 → 0.0。
score_accuracy（0~1）
    expected_keywords 中数值类关键词（可解析为数字 / 百分比）按上述容差匹配回答中的数字 token；
    文本类关键词按 eval_platform.evaluators 同一 norm（NFKC → casefold → 去空白）子串匹配。
    score = 命中关键词 / 关键词总数；reference_answer 仅在 expected_keywords 为空时作为单一文本关键词。
    二者皆空 → 抛 ValueError（EvalRunner 归因 JUDGE_ERROR）。
simulate_satisfaction（1~5，显式量表）
    1 空回答；2 拒答 / 追问但未说明原因；3 有结论但无范围或基准日说明；
    4 有结论 + 范围 / 基准日；5 在 4 的基础上含可执行建议（补货 / 调拨 / 先进先出 / 报损）且长度 ≤ 600 字。

数字抽取与容差比较统一使用 render.extract_numeric_tokens / render.numeric_match（与 agent 数值核验同口径）。
所有 JudgeScore.token_usage 为 0；reason 写明"规则裁判，非真人 / LLM 评价"。
"""

from __future__ import annotations

import re
import unicodedata
from agent_matrix.supply_chain_bi.render import (parse_result_context, extract_numeric_tokens, numeric_match, ALERT_CONTEXT_HEADER)

from data_platform.retrieval_engine.models import RetrievedParentChunk, UserContext
from eval_platform.models import EvalScenario, JudgeScore


class DataAnalysisJudge:
    """识别 BI_RESULT_V1 / BI_ALERTS_V1 数据分析格式的确定性裁判（结构化满足 LLMJudgePort）。"""

    def __init__(self, *, satisfaction_max_chars: int = 600) -> None:
        """保存量表参数；无 I/O。"""
        self.satisfaction_max_chars = satisfaction_max_chars

    async def score_faithfulness(
        self,
        *,
        question: str,
        answer: str,
        contexts: list[RetrievedParentChunk],
    ) -> JudgeScore:
        """见模块文档 score_faithfulness；contexts 中非 BI 格式的块忽略（不报错）。"""
        evidence = []
        for context in contexts:
            parsed = parse_result_context(context.full_content)
            if parsed:
                for row in parsed[1]:
                    for value in row:
                        try: evidence.append(float(value))
                        except ValueError: pass
                # Deterministic metadata shown in conclusions is also available evidence.
                for line in context.full_content.splitlines():
                    if line.startswith("scope: "):
                        evidence.extend(t[0] for t in extract_numeric_tokens(line))
            elif context.full_content.startswith(ALERT_CONTEXT_HEADER):
                evidence.extend(t[0] for t in extract_numeric_tokens(context.full_content))
                alert_lines = [l for l in context.full_content.splitlines() if l.startswith('alert: ')]
                evidence.append(float(len(alert_lines)))
                evidence.extend(float(sum(f' | {s} | ' in l for l in alert_lines)) for s in ('p1','p2','p3'))
        tokens = extract_numeric_tokens(answer)
        score = sum(any(numeric_match(t, e) for e in evidence) for t in tokens) / len(tokens) if tokens else 1.0
        return JudgeScore(score=score, reason="规则裁判，非真人 / LLM 评价；按结果单元格数值容差核验。")

    async def score_accuracy(
        self,
        *,
        question: str,
        answer: str,
        reference_answer: str | None,
        expected_keywords: list[str],
    ) -> JudgeScore:
        """见模块文档 score_accuracy。"""
        keywords = expected_keywords or ([reference_answer] if reference_answer else [])
        if not keywords:
            raise ValueError("accuracy requires keywords or reference_answer")
        normalize = lambda s: "".join(unicodedata.normalize("NFKC", s).casefold().split())
        answer_tokens = extract_numeric_tokens(answer)
        hits = 0
        for keyword in keywords:
            numeric = re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?%?", normalize(keyword))
            if numeric:
                token = extract_numeric_tokens(keyword)[0]
                expected = token[0] / 100 if token[2] else token[0]
                hits += any(
                    numeric_match(t, expected)
                    or numeric_match(token, t[0] / 100 if t[2] else t[0])
                    for t in answer_tokens
                )
            else:
                hits += normalize(keyword) in normalize(answer)
        return JudgeScore(score=hits / len(keywords), reason="规则裁判，非真人 / LLM 评价；文本与数值关键词命中率。")

    async def simulate_satisfaction(
        self,
        *,
        question: str,
        answer: str,
        scenario: EvalScenario,
        user_context: UserContext,
    ) -> JudgeScore:
        """见模块文档 simulate_satisfaction；scenario 非 SUPPLY_CHAIN 时仍按同一量表打分（不抛错）。"""
        score = 1
        if answer.strip():
            score = 3
            if any(t in answer for t in ("无法", "请说明", "权限", "终止")) and not any(t in answer for t in ("因为", "属于", "权限", "安全", "只能", "范围")):
                score = 2
            if any(t in answer for t in ("范围", "基准日", "大区", "全国")):
                score = 4
                if len(answer) <= self.satisfaction_max_chars and any(t in answer for t in ("补货", "调拨", "先进先出", "报损")):
                    score = 5
        return JudgeScore(score=score, reason="规则裁判，非真人 / LLM 评价；按数据分析可执行性量表。")

