"""综合电商财务费用报销与商品采购合同合规预审智能体。

FinanceAuditAgent 结构化满足 eval_platform.ports.AgentSystemPort（`async def invoke(AgentInvocation) -> AgentResponse`），
可零适配接入 EvalRunner 跑 END_TO_END 回归评测；同时提供 audit 入口供 api.py / 平台 Workflow 调用。

受控闭环（单次预审；模型调用至多 2 次：1 次审查 + 1 次引用支撑性核验）：

    ⓪ AUTHZ      user_context.roles ∩ config.allowed_caller_roles = ∅ → CallerForbiddenError（不做任何 I/O）
    ① REDACT     view = PiiRedactor.redact_expense / redact_contract(submission)
                 redactor.ensure_clean(view.model_dump_json(), sink="model_input")   —— 纵深防御，命中即 PiiLeakError
    ② 并发 I/O（asyncio.gather）：
         A. LEDGER_LOOKUP   _load_ledger_snapshot：find_invoice_usages ∥ list_recent_claims（或 list_recent_contracts）
                            任一失败 / 超时 → LedgerUnavailableError（fail-closed：无法排除重复与拆单时不出结论；
                            同时取消 B）
         B. RETRIEVE_POLICY _retrieve_policy：query 由 _build_policy_query 按 类别 / 合同类型 模板确定性构造
                            （**不含任何单据自由文本**），RetrievalSystemPort.search(user_context 原样透传)；
                            异常 / 超时 → PolicyRetrievalResult(unavailable=True)，不抛出
    ③ RULE_CHECK DeterministicFinanceRuleEngine.evaluate_*(submission, ledger, caller)   —— Decimal，零浮点
                 _bind_rule_citations：按 RULE_POLICY_SECTIONS 在 B 的 contexts 中确定性绑定 SopCitation
    ④ MODEL_REVIEW
         ├─ 规则已产生 REJECT 级 finding ───────→ SKIPPED_BY_REJECT（红线一票否决，跳过模型，节省 token）
         ├─ contexts 为空（不可用 / ACL 无可见条款）→ NO_POLICY_CONTEXT + SYSTEM_GUARD(POLICY_CONTEXT_UNAVAILABLE)
         └─ review_expense_reason / review_contract_clauses(view, contexts)
              模型异常 / 超时 / 结构化非法 → MODEL_UNAVAILABLE + SYSTEM_GUARD(MODEL_REVIEW_UNAVAILABLE)
    ⑤ VERIFY_CITATIONS（闭锁引用校验，见 _verify_citations）
         确定性：citation_parent_id ∈ contexts 且 citation_quote 为该父块 full_content 子串（非空）
                 且 evidence_excerpt 为空或为视图脱敏文本子串
         可选：  通过确定性校验的 claim 再调 verify_citation_support（config.verify_citation_support）
         处置：  通过 → MODEL_REVIEW finding（citation_verified=True）
                 未通过且风险 >= HIGH → 保留 finding（citation_verified=False）+ SYSTEM_GUARD(CITATION_UNVERIFIED)
                 未通过且风险 < HIGH → 丢弃（discarded_claim_count += 1）
                 编码不允许 MODEL_REVIEW 来源 → 丢弃
         合同额外：ContractTermExtraction 与结构化字段确定性比对（账期 / 预付比例）→ CLAUSE_FIELD_MISMATCH
         injection_suspected=True → PROMPT_INJECTION_SUSPECTED（CRITICAL，无需引用即保留）
    ⑥ ROUTE      _synthesize_route：route = max(f.min_route)（空 findings → AUTO_APPROVE）；
                 AUTO_APPROVE 另需 reconciliation.passed ∧ citation_check_passed ∧ status == COMPLETED，否则至少 ESCALATE_HUMAN。
                 **模型只能把路由抬高，永远不能降低；REJECT 只能来自 RULE_ENGINE。**
    ⑦ ESCALATE   route == ESCALATE_HUMAN → HumanApprovalGatewayPort.create_review_ticket（幂等键见 _build_ticket_draft）；
                 失败 → HumanApprovalUnavailableError（503，可安全重试）
    ⑧ 组装       conclusion / audit_summary 确定性渲染；redactor.ensure_clean(response.model_dump_json(), sink="response")；
                 审计日志只记录 AuditTrailSummary（经 PiiLoggingFilter）；token_cost = Σ 模型调用 token

评测接入说明（财务预审 端到端评测）：
- invoke 的 question 必须为 `FIN_AUDIT:{expense|contract}:{reference_id}`（AgentInvocation.question ≤ 1000 字，
  无法内联完整单据），单据经 ClaimHistoryLedgerPort.fetch_submission 按调用人身份加载。
- retrieved_contexts = 本轮实际使用的制度父块（按排名）+ 末尾追加一条 FIN_RULE_V1 规则上下文（见 _to_agent_response）；
  因此**必须**配套使用 agent_matrix.finance_audit.eval_judge.FinanceAuditJudge，不得使用 RuleBasedJudge
  （后者按整句子串判忠实度，会把模板化结论 / 金额格式化误判为幻觉）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import Counter
from datetime import timedelta
from time import perf_counter
from agent_matrix.finance_audit.errors import (
    CallerForbiddenError, InvalidAuditInvocationError, SubmissionNotFoundError,
    LedgerUnavailableError, HumanApprovalUnavailableError, UnsupportedScenarioError,
)
from agent_matrix.finance_audit.models import (
    AuditSubjectType, AuditStage, ModelReviewStatus, FindingSource, ViolationCode, VIOLATION_POLICIES,
    RiskLevel, ExpenseCategory, ContractType, ExpenseReasonReviewDraft, ContractClauseReviewDraft, CitationSupportDraft,
)
from agent_matrix.finance_audit.redaction import PiiLoggingFilter
from agent_matrix.finance_audit.rule_engine import RULE_POLICY_SECTIONS, CONTRACT_POLICY_SECTIONS, format_money, render_rule_context
from agent_matrix.store_sop.models import SopCitation
from data_platform.retrieval_engine.models import SearchRequest, RetrievalBranch
from eval_platform.models import EvalScenario, TokenUsage

from collections.abc import Sequence

from agent_matrix.finance_audit.models import (
    AmountReconciliationResult,
    ApprovalRoute,
    AuditTrailSummary,
    CitationCheckResult,
    ComplianceFinding,
    ContractAuditSubmission,
    ContractTermExtraction,
    ExpenseAuditSubmission,
    FinanceAuditConfig,
    FinanceAuditOutcome,
    FinanceAuditRequest,
    FinanceAuditResponse,
    HumanApprovalTicketDraft,
    HumanApprovalTicketReceipt,
    LedgerSnapshot,
    ModelReviewOutcome,
    ModelRiskClaim,
    PiiType,
    PolicyRetrievalResult,
    RedactedContractView,
    RedactedExpenseView,
    ReviewerQueue,
    RouteDecision,
    RuleEvaluation,
)
from agent_matrix.finance_audit.ports import (
    ClaimHistoryLedgerPort,
    FinanceReviewModelPort,
    HumanApprovalGatewayPort,
)
from agent_matrix.finance_audit.redaction import PiiRedactor
from agent_matrix.finance_audit.rule_engine import DeterministicFinanceRuleEngine
from data_platform.retrieval_engine.models import RetrievedParentChunk, UserContext
from eval_platform.models import AgentInvocation, AgentResponse
from eval_platform.ports import RetrievalSystemPort

FIN_AUDIT_REF_PATTERN = r"^FIN_AUDIT:(expense|contract):([A-Za-z0-9_.:-]{1,128})$"
"""invoke 的 question 格式（re.fullmatch）；不匹配 → InvalidAuditInvocationError。"""

RULE_CONTEXT_DOC_PREFIX = "fin_rules:"
"""retrieved_contexts 中规则上下文的 doc_id：f"fin_rules:{ruleset_version}"；parent_id = f"fin_rules:{reference_id}"。"""

CONCLUSION_ROUTE_LINE = "审批路由：{route}"
"""conclusion 中的路由行（{route} 为 ApprovalRoute.value）；FinanceAuditJudge 据此精确解析路由，修改须同步裁判。"""

CONCLUSION_CODES_PREFIX = "风险编码："
"""conclusion 中的编码行前缀：风险编码：FIN_SUSPECTED_SPLIT_CLAIM(CRITICAL)、…；无 finding 时为"风险编码：无"。"""

CONCLUSION_POLICY_PREFIX = "制度依据："
"""conclusion 中的引用行前缀：制度依据：{section_path}；…（按 citations 顺序，去重）。"""

AUDIT_RENDER_VERSION = "finance_audit.v1"
"""conclusion / audit_summary 模板版本；修改任何话术常量或模板必须递增。"""


class FinanceAuditAgent:
    """财务费用报销与商品采购合同合规预审智能体。"""

    def __init__(
        self,
        *,
        review_model: FinanceReviewModelPort,
        ledger: ClaimHistoryLedgerPort,
        approval_gateway: HumanApprovalGatewayPort,
        retrieval: RetrievalSystemPort,
        redactor: PiiRedactor | None = None,
        rule_engine: DeterministicFinanceRuleEngine | None = None,
        config: FinanceAuditConfig | None = None,
    ) -> None:
        """注入四个端口与脱敏器 / 规则引擎 / 运行参数（仅保存引用，不做任何 I/O）。

        - redactor 缺省为 PiiRedactor()；rule_engine 缺省为 DeterministicFinanceRuleEngine(config.rules)
          （其构造函数执行 self_check，配置非法抛 FinanceRuleConfigError）。
        - 为 logging.getLogger("agent_matrix.finance_audit") 安装 PiiLoggingFilter（幂等，不重复安装）。
        """
        self.review_model, self.ledger = review_model, ledger
        self.approval_gateway, self.retrieval = approval_gateway, retrieval
        self.config = config or FinanceAuditConfig()
        self.redactor = redactor or PiiRedactor()
        self.rule_engine = rule_engine or DeterministicFinanceRuleEngine(self.config.rules)
        logger = logging.getLogger('agent_matrix.finance_audit')
        if not any(isinstance(f, PiiLoggingFilter) for f in logger.filters):
            logger.addFilter(PiiLoggingFilter(PiiRedactor()))

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def invoke(self, invocation: AgentInvocation) -> AgentResponse:
        """AgentSystemPort 实现：评测中台端到端单次调用。

        - scenario 非 FINANCE_APPROVAL 抛 UnsupportedScenarioError（EvalRunner 归因 SYSTEM_ERROR）。
        - question 按 FIN_AUDIT_REF_PATTERN 解析；不匹配抛 InvalidAuditInvocationError。
        - ledger.fetch_submission(subject_type, reference_id, user_context=invocation.user_context)；None → SubmissionNotFoundError。
        - invocation.user_context 原样透传，不得复制修改；业务异常直接抛出，不得吞掉后返回伪造结果。
        - 映射规则见 _to_agent_response。
        """
        if invocation.scenario is not EvalScenario.FINANCE_APPROVAL:
            raise UnsupportedScenarioError('不支持的评测场景')
        self._authorize_caller(invocation.user_context, trace_id=invocation.trace_id)
        match = re.fullmatch(FIN_AUDIT_REF_PATTERN, invocation.question)
        if match is None:
            raise InvalidAuditInvocationError('单据引用格式无效')
        subject, reference = match.groups()
        try:
            submission = await asyncio.wait_for(self.ledger.fetch_submission(
                subject_type=AuditSubjectType(subject), reference_id=reference, user_context=invocation.user_context),
                self.config.ledger_timeout_s)
        except Exception:
            raise LedgerUnavailableError('加载单据失败') from None
        if submission is None or submission.subject_type != subject or _reference(submission) != reference:
            raise SubmissionNotFoundError('单据不存在或当前身份无权访问')
        outcome = await self._run_pipeline(FinanceAuditRequest(submission=submission,
            user_context=invocation.user_context, trace_id=invocation.trace_id))
        return self._to_agent_response(outcome)

    async def audit(self, request: FinanceAuditRequest) -> FinanceAuditResponse:
        """api.py / Workflow 入口：返回 (await self._run_pipeline(request)).response。"""
        return (await self._run_pipeline(request)).response

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------

    async def _run_pipeline(self, request: FinanceAuditRequest) -> FinanceAuditOutcome:
        """按模块文档 ⓪~⑧ 编排一次预审；记录 stage_latency_ms（AuditStage 键）与总 latency_ms。

        - ② 中台账与制度检索使用 asyncio.gather 并发；台账失败时取消检索任务后抛 LedgerUnavailableError。
        - asyncio.CancelledError 必须重新抛出。
        """
        started = perf_counter()
        self._authorize_caller(request.user_context, trace_id=request.trace_id)
        stages = {}
        t0 = perf_counter()
        view = self._redact(request.submission)
        stages[AuditStage.REDACT] = (perf_counter()-t0)*1000
        async def timed(stage, coro):
            tick = perf_counter()
            try:
                return await coro
            finally:
                stages[stage] = (perf_counter()-tick)*1000
        tasks = [asyncio.create_task(timed(AuditStage.LEDGER_LOOKUP, self._load_ledger_snapshot(request.submission))),
                 asyncio.create_task(timed(AuditStage.RETRIEVE_POLICY, self._retrieve_policy(view, user_context=request.user_context)))]
        try:
            ledger, policy = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        t0 = perf_counter()
        evaluation = self._bind_rule_citations(self._evaluate_rules(request.submission, ledger=ledger, caller=request.user_context), policy.contexts)
        stages[AuditStage.RULE_CHECK] = (perf_counter()-t0)*1000
        review = await timed(AuditStage.MODEL_REVIEW, self._review_with_model(view, evaluation=evaluation, policy=policy))
        t0 = perf_counter()
        decision = self._synthesize_route(evaluation=evaluation, review=review, is_contract=isinstance(view, RedactedContractView))
        stages[AuditStage.ROUTE] = (perf_counter()-t0)*1000
        findings = sorted([*evaluation.findings, *review.findings], key=lambda f: (-f.risk_level.rank, f.finding_id))
        supplements = list(dict.fromkeys(f.supplement_request for f in findings if f.supplement_request))
        summary = self._build_audit_summary(request, decision=decision, evaluation=evaluation,
            review=review, findings=findings, pii_counts=view.pii_counts)
        receipt = None
        if decision.route is ApprovalRoute.ESCALATE_HUMAN:
            draft = self._build_ticket_draft(request, decision=decision, findings=findings,
                reconciliation=evaluation.reconciliation, summary=summary)
            receipt = await timed(AuditStage.ESCALATE, self._escalate(draft))
        citations = list({c.parent_id: c for f in reversed(findings) for c in reversed(f.citations)}.values())
        citations.reverse()
        response = FinanceAuditResponse(trace_id=request.trace_id, subject_type=request.submission.subject_type,
            reference_id=_reference(request.submission),
            conclusion=self._render_conclusion(decision=decision, reconciliation=evaluation.reconciliation,
                findings=findings, supplement_requests=supplements, ticket_id=receipt.ticket_id if receipt else None),
            route=decision.route, approver_tier=decision.approver_tier, reconciliation=evaluation.reconciliation,
            findings=findings, max_risk_level=decision.max_risk_level, supplement_requests=supplements,
            citations=citations, citation_check_passed=not any(f.source is FindingSource.MODEL_REVIEW and not f.citation_verified for f in review.findings),
            model_review_status=review.status, audit_summary=summary,
            human_approval_ticket_id=receipt.ticket_id if receipt else None, retrieval_degraded=policy.degraded,
            latency_ms=(perf_counter()-started)*1000, stage_latency_ms=stages,
            token_usage=review.token_usage, token_cost=review.token_usage.total_tokens)
        self.redactor.ensure_clean(response.model_dump_json(), sink='response')
        self.redactor.ensure_clean(summary.model_dump_json(), sink='log')
        logging.getLogger('agent_matrix.finance_audit').info('%s', summary.model_dump_json())
        return FinanceAuditOutcome(response=response, rule_evaluation=evaluation, policy_retrieval=policy,
            model_review=review, route_decision=decision, ticket_receipt=receipt)

    def _authorize_caller(self, user_context: UserContext, *, trace_id: str | None) -> None:
        """⓪ 调用人角色校验；不通过抛 CallerForbiddenError（消息不回显角色列表以外的身份信息）。"""
        if not set(user_context.roles) & self.config.allowed_caller_roles:
            raise CallerForbiddenError('调用人角色不满足预审要求', trace_id=trace_id)

    def _redact(
        self, submission: ExpenseAuditSubmission | ContractAuditSubmission
    ) -> RedactedExpenseView | RedactedContractView:
        """① 按 subject_type 分派到 redactor.redact_expense / redact_contract，并对视图 JSON 执行 ensure_clean(sink="model_input")。"""
        view = self.redactor.redact_expense(submission) if isinstance(submission, ExpenseAuditSubmission) else self.redactor.redact_contract(submission)
        self.redactor.ensure_clean(view.model_dump_json(), sink='model_input')
        return view

    async def _load_ledger_snapshot(
        self, submission: ExpenseAuditSubmission | ContractAuditSubmission
    ) -> LedgerSnapshot:
        """② A：并发查询台账并组装 LedgerSnapshot。

        - 报销：asyncio.gather(find_invoice_usages(全部 invoice_no, exclude_claim_id),
          list_recent_claims(applicant, store, 明细类别去重, [submitted_at − split_window_days, submitted_at]))。
        - 合同：list_recent_contracts(counterparty.unified_credit_code, [submitted_at − contract_split_window_days, submitted_at])。
        - 整体受 config.ledger_timeout_s 约束；任何异常 / 超时统一包装为 LedgerUnavailableError（不泄露底层消息）。
        """
        is_expense = isinstance(submission, ExpenseAuditSubmission)
        days = self.config.rules.split_window_days if is_expense else self.config.rules.contract_split_window_days
        start, end = submission.submitted_at-timedelta(days=days), submission.submitted_at
        async def load():
            if is_expense:
                usages, claims = await _gather_cancel(
                    self.ledger.find_invoice_usages(invoice_nos=[i.invoice_no for i in submission.invoices], exclude_claim_id=submission.claim_id),
                    self.ledger.list_recent_claims(applicant_user_id=submission.applicant.user_id, store_code=submission.store_code,
                        categories=list(dict.fromkeys(line.category for line in submission.lines)), window_start=start, window_end=end, exclude_claim_id=submission.claim_id))
                return LedgerSnapshot(subject_type=submission.subject_type, window_start=start, window_end=end, invoice_usages=usages, recent_claims=claims)
            rows = await self.ledger.list_recent_contracts(counterparty_credit_code=submission.counterparty.unified_credit_code,
                window_start=start, window_end=end, exclude_contract_id=submission.contract_id)
            return LedgerSnapshot(subject_type=submission.subject_type, window_start=start, window_end=end, recent_contracts=rows)
        try:
            return await asyncio.wait_for(load(), self.config.ledger_timeout_s)
        except Exception:
            raise LedgerUnavailableError('历史台账查询失败') from None

    def _build_policy_query(self, view: RedactedExpenseView | RedactedContractView) -> str:
        """确定性构造制度检索 query：

        - 报销："财务费用报销制度 " + 各类别中文名 + " 费用审批矩阵 发票 重复发票 拆单 报销时限"；含 TRAVEL_LODGING 时追加 "差旅标准 超标住宿"。
        - 合同："合同管理制度 " + 合同类型中文名 + " 付款与账期 违约责任 合同审批权限"。
        - **不得**拼接 reason / 条款等自由文本；返回前 ensure_clean(sink="retrieval_query")。
        """
        if isinstance(view, RedactedExpenseView):
            query = '财务费用报销制度 ' + ' '.join(_CATEGORY_NAMES[c] for c in view.categories) + ' 费用审批矩阵 发票 重复发票 拆单 报销时限'
            if ExpenseCategory.TRAVEL_LODGING in view.categories:
                query += ' 差旅标准 超标住宿'
        else:
            query = '合同管理制度 ' + _CONTRACT_NAMES[view.contract_type] + ' 付款与账期 违约责任 合同审批权限'
        self.redactor.ensure_clean(query, sink='retrieval_query')
        return query

    async def _retrieve_policy(
        self,
        view: RedactedExpenseView | RedactedContractView,
        *,
        user_context: UserContext,
    ) -> PolicyRetrievalResult:
        """② B：RetrievalSystemPort.search(SearchRequest(query, user_context, top_k=config.policy_top_k))。

        - 受 config.retrieval_timeout_s 约束；异常 / 超时 → unavailable=True（不抛出）。
        - SearchResponse.degraded 透传为 degraded / degraded_reasons。
        - contexts 为 ACL 前置过滤后的结果，agent 不做任何权限放宽或补检索。
        """
        query = self._build_policy_query(view)
        try:
            result = await asyncio.wait_for(self.retrieval.search(SearchRequest(query=query,
                user_context=user_context, top_k=self.config.policy_top_k)), self.config.retrieval_timeout_s)
            # Relevance narrowing only: never retrieve again or broaden caller ACL.
            sections = (set(RULE_POLICY_SECTIONS.values()) | {'薪酬账户权限'} if isinstance(view, RedactedExpenseView)
                        else set(CONTRACT_POLICY_SECTIONS.values()) | {'合同底价保密'})
            contexts = [p for p in result.results if _section_tail(p.section_path) in sections]
            return PolicyRetrievalResult(query=query, contexts=contexts, degraded=result.degraded, degraded_reasons=result.degraded_reasons)
        except Exception:
            return PolicyRetrievalResult(query=query, unavailable=True)

    def _evaluate_rules(
        self,
        submission: ExpenseAuditSubmission | ContractAuditSubmission,
        *,
        ledger: LedgerSnapshot,
        caller: UserContext,
    ) -> RuleEvaluation:
        """③ 按 subject_type 调用 rule_engine.evaluate_expense / evaluate_contract（同步、纯 CPU）。"""
        method = self.rule_engine.evaluate_expense if isinstance(submission, ExpenseAuditSubmission) else self.rule_engine.evaluate_contract
        return method(submission, ledger=ledger, caller=caller)

    def _bind_rule_citations(
        self,
        evaluation: RuleEvaluation,
        contexts: Sequence[RetrievedParentChunk],
    ) -> RuleEvaluation:
        """③ 为 RULE_ENGINE finding 确定性绑定制度引用。

        - 按 finding.policy_section 与父块 section_path 末级（" > " 分隔）精确相等匹配，取排名最高的一个；
          SopCitation(doc_id, section_path, version, parent_id, page_number, quote="")；
          quote 取 full_content 中包含该编码关键词的首句（≤ 200 字，必须为子串），找不到则为空串。
        - 绑定成功 → citation_verified=True；未检索到 → citations=[]、citation_verified=False（规则 finding 依然成立）。
        - 返回新的 RuleEvaluation（模型均 frozen，使用 model_copy(update=...)）。
        """
        findings = []
        for f in evaluation.findings:
            parent = next((p for p in contexts if _section_tail(p.section_path) == f.policy_section), None)
            if parent:
                f = f.model_copy(update={'citations': [_citation(parent, '')], 'citation_verified': True})
            findings.append(f)
        return evaluation.model_copy(update={'findings': findings})

    async def _review_with_model(
        self,
        view: RedactedExpenseView | RedactedContractView,
        *,
        evaluation: RuleEvaluation,
        policy: PolicyRetrievalResult,
    ) -> ModelReviewOutcome:
        """④ + ⑤：按模块文档分支执行模型审查、闭锁引用校验与 claim → finding 转换。

        - 单次模型调用受 config.model_timeout_s 约束；FinanceModelUnavailableError / FinanceStructuredOutputError /
          TimeoutError → MODEL_UNAVAILABLE（不抛出）。
        - verify_citation_support 失败时视为全部 support_verified=None（不否决，也不放行为 AUTO_APPROVE：
          此时 citation_check_passed 仍由确定性校验决定）。
        - 模型 rationale 写入 finding.detail 前必须经 redactor.redact_text（模型可能复述占位符以外的数字）。
        """
        if self._max_route(evaluation.findings) is ApprovalRoute.REJECT:
            return ModelReviewOutcome(status=ModelReviewStatus.SKIPPED_BY_REJECT)
        if not policy.contexts:
            return ModelReviewOutcome(status=ModelReviewStatus.NO_POLICY_CONTEXT, findings=[_guard(ViolationCode.POLICY_CONTEXT_UNAVAILABLE)])
        # Check the complete outbound arguments, including retrieved policy text.
        for parent in policy.contexts:
            self.redactor.ensure_clean(parent.model_dump_json(), sink='model_input')
        self.redactor.ensure_clean(view.model_dump_json(), sink='model_input')
        usage, names = TokenUsage(), []
        try:
            is_contract = isinstance(view, RedactedContractView)
            method = self.review_model.review_contract_clauses if is_contract else self.review_model.review_expense_reason
            result = await asyncio.wait_for(method(view=view, policy_contexts=policy.contexts), self.config.model_timeout_s)
            usage = _add_usage(usage, result.token_usage)
            names.append(result.model_name)
            schema = ContractClauseReviewDraft if is_contract else ExpenseReasonReviewDraft
            draft = schema.model_validate(result.output.model_dump())
        except Exception:
            return ModelReviewOutcome(status=ModelReviewStatus.MODEL_UNAVAILABLE,
                findings=[_guard(ViolationCode.MODEL_REVIEW_UNAVAILABLE)], token_usage=usage, model_names=names)
        claims = draft.claims
        checks = self._verify_citations(claims, contexts=policy.contexts, view=view)
        indices = [i for i, c in enumerate(checks) if c.passed and FindingSource.MODEL_REVIEW in VIOLATION_POLICIES[claims[i].code].allowed_sources]
        if indices and self.config.verify_citation_support:
            safe_claims = [claims[i].model_copy(update={'rationale': self.redactor.redact_text(claims[i].rationale).text}) for i in indices]
            for claim in safe_claims:
                self.redactor.ensure_clean(claim.model_dump_json(), sink='model_input')
            try:
                support = await asyncio.wait_for(self.review_model.verify_citation_support(claims=safe_claims,
                    policy_contexts=policy.contexts), self.config.model_timeout_s)
                usage = _add_usage(usage, support.token_usage)
                names.append(support.model_name)
                verdicts = CitationSupportDraft.model_validate(support.output.model_dump()).verdicts
                for local_index, original_index in enumerate(indices):
                    values = [v.supported for v in verdicts if v.claim_index == local_index]
                    verified = False if False in values else True if values else None
                    old = checks[original_index]
                    checks[original_index] = CitationCheckResult.model_validate({**old.model_dump(),
                        'support_verified': verified, 'passed': old.passed and verified is not False,
                        'citation': old.citation if verified is not False else None})
            except Exception:
                pass
        findings, discarded = self._claims_to_findings(claims, checks)
        if is_contract:
            # Only the structured fields used by this comparison are needed; the redacted view has all of them.
            term_findings = self._cross_check_contract_terms(view, draft.extracted_terms, evaluation=evaluation)
            parent = next((p for p in policy.contexts if _section_tail(p.section_path) == '付款与账期'), None)
            if parent is not None:
                # This finding is a deterministic structured-field comparison, not a model claim.
                term_findings = [f.model_copy(update={'citations': [_citation(parent, '')],
                    'citation_verified': True, 'policy_section': '付款与账期'}) for f in term_findings]
            findings.extend(term_findings)
        if draft.injection_suspected and not any(f.code is ViolationCode.PROMPT_INJECTION_SUSPECTED for f in findings):
            findings.append(_model_finding(ViolationCode.PROMPT_INJECTION_SUSPECTED, '语义审查识别到审批操纵指令'))
        return ModelReviewOutcome(status=ModelReviewStatus.COMPLETED, findings=findings, citation_checks=checks,
            discarded_claim_count=discarded, model_names=list(dict.fromkeys(names)), token_usage=usage)

    def _verify_citations(
        self,
        claims: Sequence[ModelRiskClaim],
        *,
        contexts: Sequence[RetrievedParentChunk],
        view: RedactedExpenseView | RedactedContractView,
    ) -> list[CitationCheckResult]:
        """⑤ 确定性闭锁引用校验（不调用模型；support_verified 一律 None，由 _review_with_model 回填）。

        - 子串比较前对双方做 NFKC 归一化与连续空白折叠，不做大小写折叠。
        - 通过时 citation 由父块元数据构造（doc_id / section_path / version / parent_id / page_number 取自父块，
          quote = claim.citation_quote），绝不信任模型给出的 doc_id / 章节名。
        """
        parents = {p.parent_id: p for p in contexts}
        texts = ([view.reason, *(line.description for line in view.lines)] if isinstance(view, RedactedExpenseView)
                 else [view.title, view.counterparty_name, view.payment_terms_text, view.breach_clause_text, view.other_key_clauses, *(p.description for p in view.payment_schedule)])
        checks = []
        for index, claim in enumerate(claims):
            parent = parents.get(claim.citation_parent_id)
            quote = _norm(claim.citation_quote)
            quoted = parent is not None and bool(quote) and quote in _norm(parent.full_content)
            evidence = not claim.evidence_excerpt or any(_norm(claim.evidence_excerpt) in _norm(text) for text in texts)
            passed = parent is not None and quoted and evidence
            checks.append(CitationCheckResult(claim_index=index, code=claim.code, parent_id=claim.citation_parent_id,
                parent_in_contexts=parent is not None, quote_is_substring=quoted, evidence_is_substring=evidence,
                passed=passed, citation=_citation(parent, claim.citation_quote) if passed else None))
        return checks

    def _claims_to_findings(
        self,
        claims: Sequence[ModelRiskClaim],
        checks: Sequence[CitationCheckResult],
    ) -> tuple[list[ComplianceFinding], int]:
        """⑤ 按模块文档"处置"规则把 claim 转为 finding；返回 (findings, discarded_claim_count)。

        - risk_level = max(claim.risk_level, VIOLATION_POLICIES[code].min_risk_level)；min_route 取策略表下限
          （HIGH / CRITICAL 自动抬到 ESCALATE_HUMAN）；is_redline 取策略表。
        - 同一编码多条 claim 保留全部，finding_id 按编码分别从 1 计数（与规则 finding 的计数相互独立，前缀
          f"{code.value}:m{序号}" 以避免冲突）。
        """
        findings, discarded, counts = [], 0, Counter()
        check_map = {c.claim_index: c for c in checks}
        for index, claim in enumerate(claims):
            policy = VIOLATION_POLICIES[claim.code]
            if FindingSource.MODEL_REVIEW not in policy.allowed_sources:
                discarded += 1
                continue
            check = check_map.get(index)
            verified = check is not None and check.passed
            risk = max(claim.risk_level, policy.min_risk_level, key=lambda r: r.rank)
            if not verified and risk.rank < RiskLevel.HIGH.rank:
                discarded += 1
                continue
            counts[claim.code] += 1
            finding = _model_finding(claim.code, self.redactor.redact_text(claim.rationale).text,
                risk=risk, finding_id=f'{claim.code.value}:m{counts[claim.code]}',
                citations=[check.citation] if verified else [], citation_verified=verified)
            findings.append(finding)
            if not verified:
                guard = _guard(ViolationCode.CITATION_UNVERIFIED)
                if not any(f.code is guard.code for f in findings):
                    findings.append(guard)
        return findings, discarded

    def _cross_check_contract_terms(
        self,
        submission: ContractAuditSubmission,
        extracted: ContractTermExtraction,
        *,
        evaluation: RuleEvaluation,
    ) -> list[ComplianceFinding]:
        """⑤ 合同：模型抽取值与结构化字段的确定性比对（Decimal 精确比较）。

        - stated_payment_term_days 非 None 且 != submission.payment_term_days → CLAUSE_FIELD_MISMATCH。
        - stated_prepayment_ratio 非 None 且与 PaymentScheduleCheck.prepayment_ratio 不等 → CLAUSE_FIELD_MISMATCH。
        - 抽取值为 None 不产出 finding（模型未读到不等于不一致）。
        """
        mismatch = extracted.stated_payment_term_days is not None and extracted.stated_payment_term_days != submission.payment_term_days
        schedule = evaluation.reconciliation.payment_schedule_check
        if extracted.stated_prepayment_ratio is not None and schedule is not None:
            mismatch = mismatch or extracted.stated_prepayment_ratio != schedule.prepayment_ratio
        if mismatch:
            return [_model_finding(ViolationCode.CLAUSE_FIELD_MISMATCH, '条款抽取值与结构化账期或预付比例不一致')]
        return []

    def _synthesize_route(
        self,
        *,
        evaluation: RuleEvaluation,
        review: ModelReviewOutcome,
        is_contract: bool,
    ) -> RouteDecision:
        """⑥ 确定性路由合成。

        - findings = 规则 findings ∪ review.findings；route = max(f.min_route)，空集为 AUTO_APPROVE。
        - route == AUTO_APPROVE 但 (¬reconciliation.passed ∨ ¬citation_check_passed ∨ status ≠ COMPLETED) → ESCALATE_HUMAN
          （理论上前两者已由 finding 覆盖，此处为纵深防御）。
        - reviewer_queue（仅 ESCALATE_HUMAN）：报销 → FINANCE_SUPERVISOR；合同 → LEGAL_COUNSEL，
          若同时存在 PAYMENT_SCHEDULE_MISMATCH / SUSPECTED_SPLIT_CONTRACT / SELF_REVIEW_CONFLICT → FINANCE_AND_LEGAL。
        - approver_tier = evaluation.approver_tier（拆单时已按窗口合计升级）。
        """
        findings = [*evaluation.findings, *review.findings]
        route = self._max_route(findings)
        retained_unverified = any(f.source is FindingSource.MODEL_REVIEW and not f.citation_verified for f in review.findings)
        if route is ApprovalRoute.AUTO_APPROVE and (not evaluation.reconciliation.passed or retained_unverified or review.status is not ModelReviewStatus.COMPLETED):
            route = ApprovalRoute.ESCALATE_HUMAN
        return RouteDecision(route=route, approver_tier=evaluation.approver_tier,
            reviewer_queue=self._queue_for(findings, is_contract=is_contract) if route is ApprovalRoute.ESCALATE_HUMAN else None,
            max_risk_level=max((f.risk_level for f in findings), key=lambda r: r.rank, default=None),
            decisive_finding_ids=[f.finding_id for f in findings if f.min_route is route],
            reasons=[f.title for f in findings if f.min_route is route])

    def _build_ticket_draft(
        self,
        request: FinanceAuditRequest,
        *,
        decision: RouteDecision,
        findings: Sequence[ComplianceFinding],
        reconciliation: AmountReconciliationResult,
        summary: AuditTrailSummary,
    ) -> HumanApprovalTicketDraft:
        """⑦ 构造工单草稿。

        - idempotency_key = f"{subject_type}:{reference_id}:{fingerprint}"，fingerprint = sha256(按 finding_id 排序的
          "code|risk_level" 串)前 16 位——同一单据同一结论重试不重复建单，结论变化（如补正后重审）生成新工单。
        - requested_by_* 取自 request.user_context；sla_hours 取 config.sla_hours_by_risk[max_risk_level]。
        - created_at 取 submission.submitted_at（确定性，可复现）。
        - 构造后 ensure_clean(draft.model_dump_json(), sink="ticket")。
        """
        fingerprint = hashlib.sha256('\n'.join(f'{f.code.value}|{f.risk_level.value}' for f in sorted(findings, key=lambda f: f.finding_id)).encode()).hexdigest()[:16]
        submission = request.submission
        reference = _reference(submission)
        draft = HumanApprovalTicketDraft(idempotency_key=f'{submission.subject_type}:{reference}:{fingerprint}',
            subject_type=submission.subject_type, reference_id=reference, queue=decision.reviewer_queue,
            max_risk_level=decision.max_risk_level, finding_codes=list(dict.fromkeys(f.code for f in findings)), findings=list(findings),
            approval_amount=reconciliation.approval_amount, approver_tier=decision.approver_tier,
            summary=summary.summary_text, requested_by_user_id=request.user_context.user_id, requested_by_dept_id=request.user_context.dept_id,
            trace_id=request.trace_id, sla_hours=self.config.sla_hours_by_risk[decision.max_risk_level], created_at=submission.submitted_at)
        self.redactor.ensure_clean(draft.model_dump_json(), sink='ticket')
        return draft

    async def _escalate(self, draft: HumanApprovalTicketDraft) -> HumanApprovalTicketReceipt:
        """⑦ 调用 approval_gateway.create_review_ticket（config.approval_timeout_s）；失败包装为 HumanApprovalUnavailableError。"""
        try:
            return await asyncio.wait_for(self.approval_gateway.create_review_ticket(draft), self.config.approval_timeout_s)
        except Exception:
            raise HumanApprovalUnavailableError('人工复核工单创建失败') from None

    def _render_conclusion(
        self,
        *,
        decision: RouteDecision,
        reconciliation: AmountReconciliationResult,
        findings: Sequence[ComplianceFinding],
        supplement_requests: Sequence[str],
        ticket_id: str | None,
    ) -> str:
        """⑧ 确定性渲染预审结论（不调用模型，不含任何单据自由文本）。逐行：

            预审结论：{路由中文名}
            审批路由：{route.value}                         （CONCLUSION_ROUTE_LINE）
            审批层级：{approver_tier.value}
            定档金额：{format_money(approval_amount)}
            金额勾稽：通过 | 偏差 {format_money(total_deviation)}
            风险编码：{code}({risk})、…                     （CONCLUSION_CODES_PREFIX）
            补正事项：{…}；{…}                              （仅非空）
            制度依据：{section_path}；…                     （CONCLUSION_POLICY_PREFIX，仅非空）
            人工复核单号：{ticket_id}                        （仅 ESCALATE_HUMAN）
        """
        labels = {ApprovalRoute.AUTO_APPROVE: '预审通过', ApprovalRoute.NEEDS_SUPPLEMENT: '退回补正', ApprovalRoute.ESCALATE_HUMAN: '转人工复核', ApprovalRoute.REJECT: '预审驳回'}
        lines = [f'预审结论：{labels[decision.route]}', CONCLUSION_ROUTE_LINE.format(route=decision.route.value),
            f'审批层级：{decision.approver_tier.value}', f'定档金额：{format_money(reconciliation.approval_amount)}',
            '金额勾稽：' + ('通过' if reconciliation.passed else f'偏差 {format_money(reconciliation.total_deviation)}'),
            CONCLUSION_CODES_PREFIX + ('、'.join(f'{f.code.value}({f.risk_level.value})' for f in findings) or '无')]
        if supplement_requests:
            lines.append('补正事项：' + '；'.join(supplement_requests))
        sections = list(dict.fromkeys(c.section_path for f in findings for c in f.citations))
        if sections:
            lines.append(CONCLUSION_POLICY_PREFIX + '；'.join(sections))
        if ticket_id:
            lines.append(f'人工复核单号：{ticket_id}')
        return '\n'.join(lines)

    def _build_audit_summary(
        self,
        request: FinanceAuditRequest,
        *,
        decision: RouteDecision,
        evaluation: RuleEvaluation,
        review: ModelReviewOutcome,
        findings: Sequence[ComplianceFinding],
        pii_counts: dict[PiiType, int],
    ) -> AuditTrailSummary:
        """⑧ 构造脱敏审计摘要：applicant_ref = "app_" + sha256(applicant.user_id)[:12]；summary_text 为结论前 3 行；
        policy_refs 按 citations 去重；整体 ensure_clean(sink="log")。"""
        text = self._render_conclusion(decision=decision, reconciliation=evaluation.reconciliation,
            findings=findings, supplement_requests=[], ticket_id=None)
        summary = AuditTrailSummary(subject_type=request.submission.subject_type, reference_id=_reference(request.submission),
            applicant_ref='app_'+hashlib.sha256(request.submission.applicant.user_id.encode()).hexdigest()[:12],
            store_code=request.submission.store_code, approval_amount=evaluation.reconciliation.approval_amount,
            route=decision.route, approver_tier=decision.approver_tier, finding_codes=list(dict.fromkeys(f.code for f in findings)),
            max_risk_level=decision.max_risk_level, pii_redaction_counts=pii_counts,
            policy_refs=list(dict.fromkeys(f'{c.doc_id}@v{c.version}#{c.section_path}' for f in findings for c in f.citations)),
            ruleset_version=evaluation.ruleset_version, model_names=review.model_names, summary_text='\n'.join(text.splitlines()[:3]))
        self.redactor.ensure_clean(summary.model_dump_json(), sink='log')
        return summary

    def _to_agent_response(self, outcome: FinanceAuditOutcome) -> AgentResponse:
        """FinanceAuditOutcome → AgentResponse（评测映射）：

            AgentResponse 字段      取值
            answer                  response.conclusion
            retrieved_contexts      policy_retrieval.contexts（按排名，即实际用于引用绑定 / 送入模型的父块）
                                    + 末尾追加 FIN_RULE_V1 规则上下文 RetrievedParentChunk：
                                      doc_id=f"fin_rules:{ruleset_version}", parent_id=f"fin_rules:{reference_id}",
                                      version=1, section_path="确定性规则核验", full_content=render_rule_context(...),
                                      score=0.0, rrf_score=0.0, source_branches=[KEYWORD]
                                    （置于末尾，避免挤占制度文档的 hit@k / MRR 名次）
            cited_doc_ids           response.citations 的 doc_id 去重（保持顺序）
            refused                 False（REJECT 是预审结论，不是拒答；红线样本不以 refused 为判据）
            latency_ms              response.latency_ms
            degraded                retrieval_degraded ∨ policy.unavailable ∨ status ∈ {NO_POLICY_CONTEXT, MODEL_UNAVAILABLE}
            degraded_reasons        policy_retrieval.degraded_reasons
            token_usage             response.token_usage
        """
        r, p = outcome.response, outcome.policy_retrieval
        rules = RetrievedParentChunk(doc_id=RULE_CONTEXT_DOC_PREFIX+outcome.rule_evaluation.ruleset_version,
            parent_id=RULE_CONTEXT_DOC_PREFIX+r.reference_id, version=1, section_path='确定性规则核验',
            full_content=render_rule_context(reference_id=r.reference_id, evaluation=outcome.rule_evaluation),
            score=0.0, rrf_score=0.0, source_branches=[RetrievalBranch.KEYWORD])
        response = AgentResponse(answer=r.conclusion, retrieved_contexts=[*p.contexts, rules],
            cited_doc_ids=list(dict.fromkeys(c.doc_id for c in r.citations)), refused=False, latency_ms=r.latency_ms,
            degraded=r.retrieval_degraded or p.unavailable or r.model_review_status in {ModelReviewStatus.NO_POLICY_CONTEXT, ModelReviewStatus.MODEL_UNAVAILABLE},
            degraded_reasons=p.degraded_reasons, token_usage=r.token_usage)
        self.redactor.ensure_clean(response.model_dump_json(), sink='response')
        return response

    @staticmethod
    def _queue_for(findings: Sequence[ComplianceFinding], *, is_contract: bool) -> ReviewerQueue:
        """reviewer_queue 选择规则（见 _synthesize_route）。"""
        if not is_contract:
            return ReviewerQueue.FINANCE_SUPERVISOR
        if any(f.code in {ViolationCode.PAYMENT_SCHEDULE_MISMATCH, ViolationCode.SUSPECTED_SPLIT_CONTRACT, ViolationCode.SELF_REVIEW_CONFLICT} for f in findings):
            return ReviewerQueue.FINANCE_AND_LEGAL
        return ReviewerQueue.LEGAL_COUNSEL

    @staticmethod
    def _max_route(findings: Sequence[ComplianceFinding]) -> ApprovalRoute:
        """max(f.min_route，按 ApprovalRoute.rank)；空序列返回 AUTO_APPROVE。"""
        return max((f.min_route for f in findings), key=lambda r: r.rank, default=ApprovalRoute.AUTO_APPROVE)


_CATEGORY_NAMES = dict(zip(ExpenseCategory, ('日常耗材', '设备维修', '装修改造', '差旅住宿', '市内交通', '其他费用')))
_CONTRACT_NAMES = dict(zip(ContractType, ('商品采购', '设备采购', '仓库租赁', '装修服务', '物流服务', '其他合同')))


def _reference(submission):
    return submission.claim_id if isinstance(submission, ExpenseAuditSubmission) else submission.contract_id


def _norm(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def _section_tail(path):
    return path.split(' > ')[-1].split('/')[-1].strip()


def _citation(parent, quote):
    return SopCitation(doc_id=parent.doc_id, parent_id=parent.parent_id, version=parent.version,
        section_path=parent.section_path, page_number=parent.page_number, quote=quote)


def _add_usage(left, right):
    return TokenUsage(prompt_tokens=left.prompt_tokens+right.prompt_tokens,
        completion_tokens=left.completion_tokens+right.completion_tokens)


def _guard(code):
    policy = VIOLATION_POLICIES[code]
    labels = {ViolationCode.POLICY_CONTEXT_UNAVAILABLE: '缺少可引用制度，转人工核验',
        ViolationCode.MODEL_REVIEW_UNAVAILABLE: '语义审查不可用，转人工核验',
        ViolationCode.CITATION_UNVERIFIED: '高风险主张未通过引用核验，转人工核验'}
    return ComplianceFinding(finding_id=f'{code.value}:s1', code=code, source=FindingSource.SYSTEM_GUARD,
        risk_level=policy.min_risk_level, min_route=policy.min_route, is_redline=policy.is_redline,
        title=labels[code], detail=labels[code])


def _model_finding(code, detail, *, risk=None, finding_id=None, **kwargs):
    policy = VIOLATION_POLICIES[code]
    risk = risk or policy.min_risk_level
    route = max(policy.min_route, ApprovalRoute.ESCALATE_HUMAN if risk.rank >= RiskLevel.HIGH.rank else policy.min_route, key=lambda r: r.rank)
    return ComplianceFinding(finding_id=finding_id or f'{code.value}:m1', code=code, source=FindingSource.MODEL_REVIEW,
        risk_level=risk, min_route=route, is_redline=policy.is_redline, title='语义合规审查', detail=detail,
        supplement_request='请补充或修正条款、事由与结构化字段' if route is ApprovalRoute.NEEDS_SUPPLEMENT else None, **kwargs)


async def _gather_cancel(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
