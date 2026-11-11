"""供应链库存预警与自然语言经营分析智能体。

SupplyChainBIAgent 结构化满足 eval_platform.ports.AgentSystemPort（`async def invoke(AgentInvocation) -> AgentResponse`），
可零适配接入 EvalRunner 跑 END_TO_END 回归评测；同时提供 analyze 入口供 api.py / 平台 Workflow 调用。

受控 Text-to-SQL 自纠错循环（单次分析，模型调用至多 1 + max_sql_attempts + 1 次）：

    ⓪ SCOPE      WarehouseScopeResolverPort.resolve(user_context)  —— 失败 fail-closed（抛错，绝不降级为全国）
                 as_of_date = request.as_of_date or sandbox.latest_snapshot_date()
    ① INTENT     classify_intent(question, 全目录 SemanticContext)
         ├─ OUT_OF_SCOPE ────────────────────────────────→ REFUSED(OUT_OF_SCOPE)，不生成 SQL
         ├─ SENSITIVE_REQUEST ───────────────────────────→ REFUSED(SENSITIVE_FIELD)，不生成 SQL
         ├─ 明确要求授权范围外大区 / 仓库 / 全国 ──────────→ REFUSED(SCOPE_VIOLATION)，不生成 SQL
         └─ confidence < 阈值 或 metric_ids 过滤后为空 ───→ CLARIFICATION_REQUESTED（render_clarification）
    ② SEMANTIC   SemanticLayer.build_context(metric_ids, scope_level, as_of_date, max_rows)
    ③ 并发执行（asyncio.gather）：
         A. SQL 循环  attempt k = 1..N：
              k == 1 ? generate_sql : repair_sql(previous_attempts)
              → SqlStaticGuard.validate(candidate, scope=scope)
                   ├─ fatal ──────────────→ 立即终止：REFUSED(POLICY_VIOLATION)，不执行、不重试
                   └─ 可纠错违规 ─────────→ repair_feedback = validation.error_hint，进入 k+1
              → ReadOnlySqlSandboxPort.execute(normalized_sql, scope.bind_params(as_of_date), timeout, max_rows)
                   ├─ SUCCESS ───────────→ 跳出循环
                   ├─ READ_ONLY_VIOLATION → 立即终止：REFUSED(POLICY_VIOLATION) + logger.error 安全告警
                   ├─ 可重试状态 ─────────→ repair_feedback = execution.error_hint，进入 k+1
                   └─ SandboxUnavailableError → DATA_SOURCE_UNAVAILABLE
              k == N 仍失败 → SQL_RETRY_EXHAUSTED
         B. 预警扫描（include_alerts 且 enable_alert_scan）：两条内置模板经同一守卫 + 沙箱并发执行，
            规则引擎 _build_alerts 确定性生成 InventoryAlert；扫描失败只记日志、alerts=[]，不影响主结论。
    ④ VERIFY     summarize_analysis → _verify_numeric_consistency（KeyFigure 逐格核验 + 结论数字全覆盖）
                 不通过 / 模型失败 → render_template_conclusion（narrative_source=TEMPLATE_FALLBACK）
    ⑤ 组装响应   final_sql = mask_sql_for_display(normalized_sql)；token_cost = Σ 模型调用 token

评测接入说明（供应链 BI 端到端评测）：
- invoke 把结果表渲染为 BI_RESULT_V1 格式的 RetrievedParentChunk 作为 retrieved_contexts（见 _to_agent_response），
  因此**必须**配套使用 agent_matrix.supply_chain_bi.eval_judge.DataAnalysisJudge（实现 LLMJudgePort，按数值容差
  比对结果单元格），不得使用 eval_platform.offline.RuleBasedJudge：后者按"整句是否为上下文子串"判忠实度，
  会把格式化后的分析结论（百分比、四舍五入、归纳句）误判为幻觉，导致 faithfulness≈0 与门禁误阻断。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from contextvars import ContextVar
from time import perf_counter
from agent_matrix.supply_chain_bi.errors import (ScopeDeniedError, ScopeResolutionError, SemanticCatalogError, SandboxUnavailableError, SqlModelUnavailableError, StructuredOutputError, UnsupportedScenarioError)
from agent_matrix.supply_chain_bi.models import (ScopeLevel, AnalysisIntentType, LoopStage, AttemptOutcome, SqlExecutionStatus, RETRYABLE_EXECUTION_STATUSES, AlertType, AlertSeverity, AlertAction)
from agent_matrix.supply_chain_bi.guardrails import mask_sql_for_display
from agent_matrix.supply_chain_bi.render import (render_scope_summary, render_refusal, render_clarification, render_result_table, render_template_conclusion, render_alert_rationale, compose_final_answer, render_result_context, render_alert_context, extract_numeric_tokens, numeric_match, _metric_column, DATA_SOURCE_UNAVAILABLE_NOTICE, SQL_RETRY_EXHAUSTED_NOTICE)
from data_platform.retrieval_engine.models import RetrievedParentChunk, RetrievalBranch
from eval_platform.models import EvalScenario

logger = logging.getLogger(__name__)

class _ScanLatch:
    def __init__(self):
        self.ready = asyncio.Event()
        self.allowed = False
        self.timings = {}

_scan_latch = ContextVar("supply_chain_alert_latch", default=None)

def _record_stage(stage, started):
    latch = _scan_latch.get()
    if latch is not None:
        latch.timings[stage] = latch.timings.get(stage, 0.0) + (perf_counter() - started) * 1000


def _add_usage(a, b):
    return TokenUsage(prompt_tokens=a.prompt_tokens+b.prompt_tokens, completion_tokens=a.completion_tokens+b.completion_tokens)


from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from agent_matrix.supply_chain_bi.guardrails import SqlStaticGuard
from agent_matrix.supply_chain_bi.models import (
    AnalysisIntent,
    AnalysisNarrativeDraft,
    AnalysisOutcome,
    InventoryAlert,
    MetricDefinition,
    NarrativeSource,
    RefusalReason,
    ResultTable,
    SemanticContext,
    SqlAttempt,
    SqlCandidate,
    SqlExecutionResult,
    SqlModelResult,
    StopReason,
    SupplyChainAgentConfig,
    SupplyChainQueryRequest,
    SupplyChainQueryResponse,
    WarehouseScopeFilter,
)
from agent_matrix.supply_chain_bi.ports import (
    ReadOnlySqlSandboxPort,
    SqlGeneratorModelPort,
    WarehouseScopeResolverPort,
)
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer
from data_platform.retrieval_engine.models import UserContext
from eval_platform.models import AgentInvocation, AgentResponse, TokenUsage

RESULT_CONTEXT_DOC_PREFIX = "bi_metric:"
"""retrieved_contexts 中结果表上下文的 doc_id 前缀：f"bi_metric:{首个 matched_metric_id}"（无指标时 "bi_metric:adhoc"）。"""

ALERT_CONTEXT_DOC_ID = "bi_alerts"
"""预警上下文的 doc_id（有预警时追加一条）。"""

TABLE_DOC_PREFIX = "bi_table:"
"""cited_doc_ids 中物理表引用前缀：f"bi_table:{table_name}"；红线样本 forbidden_doc_ids 使用同一前缀
（如 "bi_table:dim_supplier_contract"）。"""


class SqlLoopResult(BaseModel):
    """_run_sql_loop 产物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_reason: StopReason = Field(..., description="ANSWERED / REFUSED / SQL_RETRY_EXHAUSTED / DATA_SOURCE_UNAVAILABLE")
    refusal_reason: RefusalReason | None = None
    attempts: list[SqlAttempt] = Field(default_factory=list)
    final_execution: SqlExecutionResult | None = None
    final_normalized_sql: str | None = None
    metric_ids: list[str] = Field(default_factory=list, description="最终成功 SqlCandidate.metric_ids ∩ 目录")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class SupplyChainBIAgent:
    """餐饮供应链库存预警与自然语言经营分析智能体。"""

    def __init__(
        self,
        *,
        sql_model: SqlGeneratorModelPort,
        sandbox: ReadOnlySqlSandboxPort,
        scope_resolver: WarehouseScopeResolverPort,
        semantic_layer: SemanticLayer | None = None,
        guard: SqlStaticGuard | None = None,
        config: SupplyChainAgentConfig | None = None,
    ) -> None:
        """注入三个端口与语义层 / 守卫 / 运行参数（仅保存引用，不做任何 I/O）。

        - semantic_layer 缺省为 SemanticLayer(SUPPLY_CHAIN_CATALOG)；guard 缺省为
          SqlStaticGuard(semantic_layer, max_rows=config.max_result_rows)。
        - 校验 sandbox.dialect == semantic_layer.catalog.dialect，否则抛 SemanticCatalogError。
        - 调用 semantic_layer.self_check(guard)：内置预警模板在全部 ScopeLevel 下必须通过守卫。
        """
        self.sql_model, self.sandbox, self.scope_resolver = sql_model, sandbox, scope_resolver
        self.config = config or SupplyChainAgentConfig()
        self.semantic_layer = semantic_layer or SemanticLayer()
        self.guard = guard or SqlStaticGuard(self.semantic_layer, max_rows=self.config.max_result_rows)
        if sandbox.dialect != self.semantic_layer.catalog.dialect:
            raise SemanticCatalogError("沙箱与语义目录方言不一致")
        self.semantic_layer.self_check(self.guard)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def invoke(self, invocation: AgentInvocation) -> AgentResponse:
        """AgentSystemPort 实现：评测中台端到端单次调用。

        - scenario 非 SUPPLY_CHAIN 抛 UnsupportedScenarioError（EvalRunner 归因 SYSTEM_ERROR）。
        - invocation.user_context 原样透传给 WarehouseScopeResolverPort，不得复制修改。
        - as_of_date 取 sandbox.latest_snapshot_date()（评测模拟库为固定快照，结果可复现）。
        - 业务异常直接抛出，不得吞掉后返回伪造结果（eval_platform.ports 约束 2）。
        - 映射规则见 _to_agent_response。
        """
        if invocation.scenario is not EvalScenario.SUPPLY_CHAIN:
            raise UnsupportedScenarioError("仅支持供应链评测")
        outcome = await self._run_pipeline(SupplyChainQueryRequest(question=invocation.question, user_context=invocation.user_context, trace_id=invocation.trace_id))
        return self._to_agent_response(outcome)

    async def analyze(self, request: SupplyChainQueryRequest) -> SupplyChainQueryResponse:
        """api.py / Workflow 入口：返回 (await self._run_pipeline(request)).response。"""
        return (await self._run_pipeline(request)).response

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------

    async def _run_pipeline(self, request: SupplyChainQueryRequest) -> AnalysisOutcome:
        """按模块文档 ⓪~⑤ 编排一次分析；记录 stage_latency_ms（LoopStage 键）与总 latency_ms。

        - ③ 中 SQL 循环与预警扫描使用 asyncio.gather 并发；SQL 循环非 ANSWERED 时丢弃预警结果（alerts=[]）。
        - asyncio.CancelledError 必须重新抛出。
        """
        started = perf_counter()
        stages = {s: 0.0 for s in LoopStage}
        scope = await self._resolve_scope(request.user_context, trace_id=request.trace_id)
        summary = render_scope_summary(scope)
        usage = TokenUsage()
        intent = None
        as_of = request.as_of_date
        attempts = []
        metric_ids = []
        def finish(stop, conclusion, *, refusal=None, source=NarrativeSource.NOT_APPLICABLE, execution=None, sql=None, table=None, alerts=()):
            response = SupplyChainQueryResponse(trace_id=request.trace_id, stop_reason=stop, refused=stop is StopReason.REFUSED, refusal_reason=refusal, conclusion=conclusion, narrative_source=source, matched_metric_ids=metric_ids, scope_summary=summary, as_of_date=as_of, final_sql=mask_sql_for_display(sql) if sql else None, result_table=table, alerts=list(alerts), sql_attempts=attempts, referenced_tables=attempts[-1].validation.referenced_tables if sql else [], latency_ms=(perf_counter()-started)*1000, stage_latency_ms=stages, token_usage=usage, token_cost=usage.total_tokens)
            return AnalysisOutcome(response=response, scope=scope, intent=intent, final_execution=execution)
        try:
            as_of = await self._resolve_as_of_date(request)
        except SandboxUnavailableError:
            return finish(StopReason.DATA_SOURCE_UNAVAILABLE, DATA_SOURCE_UNAVAILABLE_NOTICE)
        stages[LoopStage.SCOPE] = (perf_counter()-started)*1000
        context = self.semantic_layer.build_context(metric_ids=[], scope_level=scope.scope_level, as_of_date=as_of, max_rows=self.config.max_result_rows)
        t0 = perf_counter()
        classified = await self._classify_intent(request.question, context)
        intent, usage = classified.output, classified.token_usage
        metric_ids = intent.metric_ids
        stages[LoopStage.INTENT] = (perf_counter()-t0)*1000
        refusal = {AnalysisIntentType.OUT_OF_SCOPE: RefusalReason.OUT_OF_SCOPE, AnalysisIntentType.SENSITIVE_REQUEST: RefusalReason.SENSITIVE_FIELD}.get(intent.intent_type) or self._check_requested_scope(intent, scope)
        if refusal:
            return finish(StopReason.REFUSED, render_refusal(refusal), refusal=refusal)
        if intent.confidence < self.config.intent_confidence_threshold or not metric_ids:
            return finish(StopReason.CLARIFICATION_REQUESTED, render_clarification(self.semantic_layer.match_metrics(request.question), intent.clarification_question))
        t0 = perf_counter()
        context = self.semantic_layer.build_context(metric_ids=metric_ids, scope_level=scope.scope_level, as_of_date=as_of, max_rows=self.config.max_result_rows)
        stages[LoopStage.SEMANTIC] = (perf_counter()-t0)*1000
        # A per-request latch prevents alert I/O when the first candidate is fatal.
        latch = _ScanLatch()
        latch.timings = stages
        abort_alerts = False
        context_token = _scan_latch.set(latch)
        async def loop():
            nonlocal abort_alerts
            try:
                result = await self._run_sql_loop(question=request.question, intent=intent, context=context, scope=scope, as_of_date=as_of)
                if result.stop_reason is not StopReason.ANSWERED:
                    abort_alerts = True
                    latch.allowed = False
                    tasks[1].cancel()
                return result
            except BaseException:
                abort_alerts = True
                tasks[1].cancel()
                raise
            finally:
                latch.ready.set()
        async def scan():
            try:
                await latch.ready.wait()
                if not latch.allowed or not request.include_alerts or not self.config.enable_alert_scan:
                    return []
                t = perf_counter()
                try:
                    return await self._scan_inventory_alerts(scope=scope, as_of_date=as_of)
                finally:
                    stages[LoopStage.ALERT_SCAN] = (perf_counter()-t)*1000
            except asyncio.CancelledError:
                if abort_alerts:
                    return []
                raise
        tasks = [asyncio.create_task(loop()), asyncio.create_task(scan())]
        try:
            result, alerts = await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(result, BaseException):
                raise result
            if isinstance(alerts, BaseException):
                if abort_alerts and isinstance(alerts, asyncio.CancelledError):
                    alerts = []
                else:
                    raise alerts
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _scan_latch.reset(context_token)
        attempts = result.attempts
        usage = _add_usage(usage, result.token_usage)
        if result.stop_reason is not StopReason.ANSWERED:
            notice = render_refusal(result.refusal_reason) if result.refusal_reason else DATA_SOURCE_UNAVAILABLE_NOTICE if result.stop_reason is StopReason.DATA_SOURCE_UNAVAILABLE else SQL_RETRY_EXHAUSTED_NOTICE
            return finish(result.stop_reason, notice, refusal=result.refusal_reason)
        metric_ids = result.metric_ids or metric_ids
        table = render_result_table(result.final_execution, display_rows=self.config.display_rows)
        metrics = [self.semantic_layer.get_metric(m) for m in metric_ids]
        summary_token = _scan_latch.set(latch)
        try:
            conclusion, source, summary_usage = await self._summarize(question=request.question, intent=intent, context=context, table=table, alerts=alerts, scope_summary=summary, metrics=metrics, as_of_date=as_of)
        finally:
            _scan_latch.reset(summary_token)
        usage = _add_usage(usage, summary_usage)
        return finish(StopReason.ANSWERED, conclusion, source=source, execution=result.final_execution, sql=result.final_normalized_sql, table=table, alerts=alerts)

    async def _resolve_scope(self, user_context: UserContext, *, trace_id: str | None) -> WarehouseScopeFilter:
        """调用 scope_resolver.resolve；ScopeDeniedError 原样抛出，其他异常包装为 ScopeResolutionError。
        返回值 user_id 与 user_context.user_id 不一致时抛 ScopeResolutionError（防止适配器串号）。"""
        try:
            scope = await self.scope_resolver.resolve(user_context)
            if scope.user_id != user_context.user_id:
                raise ScopeResolutionError("权限身份不一致", trace_id=trace_id)
            return scope
        except ScopeDeniedError:
            raise
        except Exception as exc:
            raise ScopeResolutionError("权限范围解析失败", trace_id=trace_id) from exc

    async def _resolve_as_of_date(self, request: SupplyChainQueryRequest) -> date:
        """request.as_of_date 优先；否则 sandbox.latest_snapshot_date()（SandboxUnavailableError → DATA_SOURCE_UNAVAILABLE）。"""
        return request.as_of_date or await self.sandbox.latest_snapshot_date()

    async def _classify_intent(self, question: str, context: SemanticContext) -> SqlModelResult[AnalysisIntent]:
        """asyncio.wait_for(sql_model.classify_intent, model_timeout_s)；超时 / 异常 → SqlModelUnavailableError，
        StructuredOutputError 原样抛出。返回前用 semantic_layer.filter_metric_ids 清洗 metric_ids。"""
        try:
            result = await asyncio.wait_for(self.sql_model.classify_intent(question=question, catalog=context), self.config.model_timeout_s)
            return result.model_copy(update={"output": result.output.model_copy(update={"metric_ids": self.semantic_layer.filter_metric_ids(result.output.metric_ids)})})
        except StructuredOutputError:
            raise
        except Exception as exc:
            raise SqlModelUnavailableError("意图模型暂不可用") from exc

    def _check_requested_scope(self, intent: AnalysisIntent, scope: WarehouseScopeFilter) -> RefusalReason | None:
        """纯函数：明确提及的范围是否超出授权。

        - asks_national_view 且 scope_level != NATIONAL → SCOPE_VIOLATION；
        - mentioned_region_ids ⊄ {scope.region_id}（非 NATIONAL）→ SCOPE_VIOLATION；
        - WAREHOUSE 级且 mentioned_warehouse_ids ⊄ authorized_warehouse_ids → SCOPE_VIOLATION；
        - 否则 None。提及范围只用于拒答判定，不改变绑定参数：SQL 永远绑定完整授权范围，用户提及的子集由模型以
          "强制范围谓词 AND warehouse_id = '<提及仓>'" 的合取收窄表达（守卫校验字面量 ⊆ 授权范围）。
        """
        if scope.scope_level is ScopeLevel.NATIONAL:
            return None
        if intent.asks_national_view or not set(intent.mentioned_region_ids) <= {scope.region_id}:
            return RefusalReason.SCOPE_VIOLATION
        if scope.scope_level is ScopeLevel.WAREHOUSE and not set(intent.mentioned_warehouse_ids) <= set(scope.authorized_warehouse_ids):
            return RefusalReason.SCOPE_VIOLATION
        return None

    # ------------------------------------------------------------------
    # Text-to-SQL 自纠错循环
    # ------------------------------------------------------------------

    async def _run_sql_loop(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
        scope: WarehouseScopeFilter,
        as_of_date: date,
    ) -> SqlLoopResult:
        """模块文档 ③A。硬性约束：

        - len(attempts) <= config.max_sql_attempts；attempt_index 从 1 连续递增；
        - 只有 validation.is_valid 的 normalized_sql 会被送入沙箱；
        - FATAL / READ_ONLY_VIOLATION 之后不再调用模型与沙箱；
        - repair_sql 收到的 previous_attempts 中不含 FATAL 尝试（FATAL 即终止，不存在后续）；
        - 每次模型调用 token 计入 SqlAttempt.token_usage 与 SqlLoopResult.token_usage。
        """
        attempts = []
        usage = TokenUsage()
        for index in range(1, self.config.max_sql_attempts + 1):
            candidate = await self._generate_candidate(question=question, intent=intent, context=context, previous_attempts=attempts)
            usage = _add_usage(usage, candidate.token_usage)
            try:
                attempt = await self._guard_and_execute(attempt_index=index, candidate=candidate, scope=scope, as_of_date=as_of_date)
            except SandboxUnavailableError:
                return SqlLoopResult(stop_reason=StopReason.DATA_SOURCE_UNAVAILABLE, attempts=attempts, token_usage=usage)
            attempts.append(attempt)
            if attempt.validation.fatal or (attempt.execution and attempt.execution.status is SqlExecutionStatus.READ_ONLY_VIOLATION):
                latch = _scan_latch.get()
                if latch: latch.allowed = False
                return SqlLoopResult(stop_reason=StopReason.REFUSED, refusal_reason=RefusalReason.POLICY_VIOLATION, attempts=attempts, token_usage=usage)
            if attempt.outcome is AttemptOutcome.SUCCEEDED:
                return SqlLoopResult(stop_reason=StopReason.ANSWERED, attempts=attempts, final_execution=attempt.execution, final_normalized_sql=attempt.validation.normalized_sql, metric_ids=self.semantic_layer.filter_metric_ids(candidate.output.metric_ids), token_usage=usage)
            if attempt.execution and attempt.execution.status not in RETRYABLE_EXECUTION_STATUSES:
                break
        return SqlLoopResult(stop_reason=StopReason.SQL_RETRY_EXHAUSTED, attempts=attempts, token_usage=usage)

    async def _generate_candidate(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
        previous_attempts: Sequence[SqlAttempt],
    ) -> SqlModelResult[SqlCandidate]:
        """previous_attempts 为空调用 generate_sql，否则 repair_sql；wait_for(model_timeout_s)；
        超时 / 异常 → SqlModelUnavailableError（整次分析失败，HTTP 503）。"""
        started = perf_counter()
        try:
            kwargs = dict(question=question, intent=intent, context=context)
            call = self.sql_model.repair_sql(**kwargs, previous_attempts=tuple(previous_attempts)) if previous_attempts else self.sql_model.generate_sql(**kwargs)
            return await asyncio.wait_for(call, self.config.model_timeout_s)
        except StructuredOutputError:
            raise
        except Exception as exc:
            raise SqlModelUnavailableError("查询模型暂不可用") from exc
        finally:
            _record_stage(LoopStage.REPAIR if previous_attempts else LoopStage.GENERATE, started)

    async def _guard_and_execute(
        self,
        *,
        attempt_index: int,
        candidate: SqlModelResult[SqlCandidate],
        scope: WarehouseScopeFilter,
        as_of_date: date,
    ) -> SqlAttempt:
        """守卫 → （通过时）沙箱执行，构造 SqlAttempt（outcome / repair_feedback / elapsed_ms）。

        - 沙箱调用：sql=validation.normalized_sql，bind_params=scope.bind_params(as_of_date=...)，
          timeout_s=config.sql_timeout_s，max_rows=config.max_result_rows；外层再套
          asyncio.timeout(sql_timeout_s + 1) 兜底，触发时构造 status=TIMEOUT 的执行结果。
        - SandboxUnavailableError 原样抛出，由 _run_sql_loop 转为 DATA_SOURCE_UNAVAILABLE。
        """
        t0 = perf_counter()
        validation = self.guard.validate(candidate.output.sql, scope=scope)
        _record_stage(LoopStage.GUARD, t0)
        execution = None
        feedback = validation.error_hint
        if not validation.is_valid:
            outcome = AttemptOutcome.GUARD_FATAL if validation.fatal else AttemptOutcome.GUARD_REJECTED
        else:
            latch = _scan_latch.get()
            if latch:
                latch.allowed = True
                latch.ready.set()
            execute_started = perf_counter()
            try:
                async with asyncio.timeout(self.config.sql_timeout_s + 1):
                    execution = await self.sandbox.execute(sql=validation.normalized_sql, bind_params=scope.bind_params(as_of_date=as_of_date), timeout_s=self.config.sql_timeout_s, max_rows=self.config.max_result_rows)
            except TimeoutError:
                execution = SqlExecutionResult(status=SqlExecutionStatus.TIMEOUT, elapsed_ms=(perf_counter()-t0)*1000, error_hint="[TIMEOUT] 请缩小查询范围后重试")
            finally:
                _record_stage(LoopStage.EXECUTE, execute_started)
            # LIMIT prevents observing a sentinel row; a full capped result is conservatively incomplete.
            if execution.status is SqlExecutionStatus.SUCCESS and execution.row_count >= self.config.max_result_rows:
                execution = execution.model_copy(update={"truncated": True})
            outcome = AttemptOutcome.SUCCEEDED if execution.status is SqlExecutionStatus.SUCCESS else AttemptOutcome.EXECUTION_FAILED
            feedback = execution.error_hint
            if execution.status is SqlExecutionStatus.READ_ONLY_VIOLATION:
                logger.error("只读沙箱触发安全保护；终止查询")
        # Fatal SQL must never be retained in the response or subsequent model inputs.
        return SqlAttempt(attempt_index=attempt_index, candidate_sql="[REDACTED]" if validation.fatal else mask_sql_for_display(candidate.output.sql), validation=validation, execution=execution, outcome=outcome, repair_feedback=feedback, token_usage=candidate.token_usage, elapsed_ms=(perf_counter()-t0)*1000)

    # ------------------------------------------------------------------
    # 库存预警
    # ------------------------------------------------------------------

    async def _scan_inventory_alerts(self, *, scope: WarehouseScopeFilter, as_of_date: date) -> list[InventoryAlert]:
        """模块文档 ③B：render_alert_sql → guard.validate（不通过即 SemanticCatalogError 级别的配置错误，记 error 日志并返回 []）
        → 两条模板 asyncio.gather 执行 → _build_alerts。任一模板执行失败返回 []（不部分生成）。"""
        try:
            validations = [self.guard.validate(self.semantic_layer.render_alert_sql(t, scope.scope_level), scope=scope) for t in self.semantic_layer.catalog.alert_templates]
            if len(validations) != 2 or any(not v.is_valid for v in validations):
                raise SemanticCatalogError("预警模板守卫校验失败")
            async with asyncio.timeout(self.config.sql_timeout_s + 1):
                results = await asyncio.gather(*(self.sandbox.execute(sql=v.normalized_sql, bind_params=scope.bind_params(as_of_date=as_of_date), timeout_s=self.config.sql_timeout_s, max_rows=self.config.max_result_rows) for v in validations))
            if any(r.status is not SqlExecutionStatus.SUCCESS or r.truncated or r.row_count >= self.config.max_result_rows for r in results):
                return []  # incomplete scans cannot safely produce transfer recommendations
            return self._build_alerts(position=results[0], batches=results[1], scope=scope, as_of_date=as_of_date)
        except Exception:
            logger.error("预警扫描未完成，已隔离；不输出部分预警")
            return []

    def _build_alerts(
        self,
        *,
        position: SqlExecutionResult,
        batches: SqlExecutionResult,
        scope: WarehouseScopeFilter,
        as_of_date: date,
    ) -> list[InventoryAlert]:
        """纯函数规则引擎（AlertRuleConfig）：

        SAFETY_STOCK_BREACH（position 每行）：available_qty < safety_stock_qty
            gap = safety_stock_qty − available_qty；dos = available_qty / avg_daily_outbound_7d（分母 0 → dos=0）
            severity: dos ≤ p1_days_of_supply → P1；≤ p2 → P2；否则 P3
            action: 同原料存在授权范围内（position 中出现的其他仓）且调出 gap 后 dos ≥ transfer_min_surplus_days 的仓
                    → TRANSFER_IN（counterpart = 满足条件中 dos 最大者，同分按 warehouse_id 升序）；否则 REPLENISH
            suggested_qty = gap
        NEAR_EXPIRY_OVERSTOCK（batches 每行，days_to_expiry ≤ near_expiry_days）：
            turnover = remaining_qty / avg_daily_outbound_7d（分母 0 或 NULL → +inf）
            积压 ⇔ turnover > days_to_expiry × overstock_ratio；overstock = remaining_qty − avg × days_to_expiry（≥ 0）
            severity: days_to_expiry ≤ p1_days_to_expiry → P1；否则 P2
            action: days_to_expiry ≤ 0 → INSPECT_AND_DISPOSE；授权范围内存在同原料 dos 更低（消耗更快）的仓
                    → TRANSFER_OUT（counterpart 取 dos 最小者）；否则 PRIORITIZE_CONSUMPTION
        - counterpart_warehouse_id 必须 ∈ position 结果中的仓库（即已受行级范围约束），永不跨范围建议调拨；
        - alert_id = sha256("|".join([type, warehouse, material, batch or "-", as_of_date.isoformat()]))[:16]；
        - 排序：severity → alert_type → warehouse_id → material_id → batch_no；截断 max_alerts。
        """
        rules = self.config.alert_rules
        positions = [dict(zip([c.name for c in position.columns], r)) for r in position.rows]
        batch_rows = [dict(zip([c.name for c in batches.columns], r)) for r in batches.rows]
        def permitted(r):
            return scope.scope_level is ScopeLevel.NATIONAL or (r['region_id'] == scope.region_id and (scope.scope_level is ScopeLevel.REGION or r['warehouse_id'] in scope.authorized_warehouse_ids))
        positions = [r for r in positions if permitted(r)]
        alerts = []
        def dos(r):
            return r['available_qty'] / r['avg_daily_outbound_7d'] if r.get('avg_daily_outbound_7d') else 0.0
        def make(row, kind, severity, current, threshold, gap, action, counterpart=None, **extra):
            fields = dict(alert_type=kind, severity=severity, region_id=row['region_id'], warehouse_id=row['warehouse_id'], material_id=row['material_id'], material_name=row['material_name'], metric_id='safety_stock_gap' if kind is AlertType.SAFETY_STOCK_BREACH else 'shelf_life_turnover_days', current_value=current, threshold_value=threshold, gap_qty=gap, quantity_unit=row['base_unit'], recommended_action=action, suggested_qty=gap, counterpart_warehouse_id=counterpart, as_of_date=as_of_date, **extra)
            identity = '|'.join([kind.value, row['warehouse_id'], row['material_id'], extra.get('batch_no') or '-', as_of_date.isoformat()])
            fields['alert_id'] = hashlib.sha256(identity.encode()).hexdigest()[:16]
            fields['rationale'] = render_alert_rationale(fields, rules)
            alerts.append(InventoryAlert(**fields))
        for row in positions:
            available, safety = row['available_qty'], row['safety_stock_qty']
            if available >= safety: continue
            gap = safety - available
            severity = AlertSeverity.P1 if dos(row) <= rules.p1_days_of_supply else AlertSeverity.P2 if dos(row) <= rules.p2_days_of_supply else AlertSeverity.P3
            donors = [r for r in positions if r['material_id'] == row['material_id'] and r['warehouse_id'] != row['warehouse_id'] and r['avg_daily_outbound_7d'] > 0 and (r['available_qty'] - gap) / r['avg_daily_outbound_7d'] >= rules.transfer_min_surplus_days]
            donors.sort(key=lambda r: (-dos(r), r['warehouse_id']))
            make(row, AlertType.SAFETY_STOCK_BREACH, severity, available, safety, gap, AlertAction.TRANSFER_IN if donors else AlertAction.REPLENISH, donors[0]['warehouse_id'] if donors else None)
        for row in batch_rows:
            if not permitted(row): continue
            days, avg = int(row['days_to_expiry']), row['avg_daily_outbound_7d'] or 0
            if days > rules.near_expiry_days or row['remaining_qty'] <= 0: continue
            turnover = row['remaining_qty'] / avg if avg > 0 else float('inf')
            if turnover <= days * rules.overstock_ratio: continue
            gap = max(0.0, row['remaining_qty'] - avg * max(days, 0))
            own = next((r for r in positions if r['warehouse_id'] == row['warehouse_id'] and r['material_id'] == row['material_id']), None)
            own_dos = dos(own) if own else turnover
            receivers = sorted([r for r in positions if r['material_id'] == row['material_id'] and r['warehouse_id'] != row['warehouse_id'] and r['avg_daily_outbound_7d'] > 0 and dos(r) < own_dos], key=lambda r: (dos(r), r['warehouse_id']))
            counterpart = receivers[0]['warehouse_id'] if receivers and days > 0 else None
            action = AlertAction.INSPECT_AND_DISPOSE if days <= 0 else AlertAction.TRANSFER_OUT if counterpart else AlertAction.PRIORITIZE_CONSUMPTION
            make(row, AlertType.NEAR_EXPIRY_OVERSTOCK, AlertSeverity.P1 if days <= rules.p1_days_to_expiry else AlertSeverity.P2, turnover, days, gap, action, counterpart, batch_no=row['batch_no'], expiry_date=date.fromisoformat(row['expiry_date']), days_to_expiry=days)
        return sorted(alerts, key=lambda a: (a.severity.value, a.alert_type.value, a.warehouse_id, a.material_id, a.batch_no or ''))[:rules.max_alerts]

    # ------------------------------------------------------------------
    # 结论与核验
    # ------------------------------------------------------------------

    async def _summarize(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
        table: ResultTable,
        alerts: Sequence[InventoryAlert],
        scope_summary: str,
        metrics: Sequence[MetricDefinition],
        as_of_date: date,
    ) -> tuple[str, NarrativeSource, TokenUsage]:
        """summarize_analysis（wait_for model_timeout_s）→ _verify_numeric_consistency。

        - 通过 → (compose_final_answer(draft), LLM_VERIFIED, usage)；
        - 核验失败 / 模型超时 / 异常 / 结构化失败 → (render_template_conclusion(...), TEMPLATE_FALLBACK, 已消耗 usage)。
          摘要阶段失败**不**抛 SqlModelUnavailableError（数据已查出，降级为模板结论）。
        - 空结果表不调用模型，直接模板结论。
        """
        usage = TokenUsage()
        if table.rows:
            try:
                started = perf_counter()
                try:
                    result = await asyncio.wait_for(self.sql_model.summarize_analysis(question=question, intent=intent, context=context, result=table, alerts=alerts, scope_summary=scope_summary), self.config.model_timeout_s)
                finally:
                    _record_stage(LoopStage.SUMMARIZE, started)
                usage, draft = result.token_usage, result.output
                started = perf_counter()
                errors = self._verify_numeric_consistency(draft, table, metrics, alerts, as_of_date)
                _record_stage(LoopStage.VERIFY, started)
                if not errors:
                    return compose_final_answer(conclusion=draft.conclusion, draft=draft, truncated=table.source_truncated, max_rows=self.config.max_result_rows), NarrativeSource.LLM_VERIFIED, usage
            except Exception:
                pass
        return render_template_conclusion(metrics=metrics, table=table, alerts=alerts, scope_summary=scope_summary, as_of_date=as_of_date, max_rows=self.config.max_result_rows), NarrativeSource.TEMPLATE_FALLBACK, usage

    def _verify_numeric_consistency(
        self,
        draft: AnalysisNarrativeDraft,
        table: ResultTable,
        metrics: Sequence[MetricDefinition],
        alerts: Sequence[InventoryAlert],
        as_of_date: date,
    ) -> list[str]:
        """纯函数，返回不一致说明列表（空列表 = 通过）。

        1. 每个 KeyFigure：row_index < len(table.rows)、column ∈ table.columns、单元格为数值，且
           |cell − value| ≤ 0.5 × 10^-dp（dp 取列名匹配的 metric.decimal_places；无匹配取 2）。
        2. 结论正文中的每个数字 token（含小数、百分号、千分位）必须能对应到：某 KeyFigure 的原值或其
           format_metric_value 展示值（RATIO 允许 ×100 后按 dp-2 位比较）；或 len(alerts) / 各 severity 计数；
           或 as_of_date / 意图时间范围的年月日组成部分。仓库 / 原料编码等含字母 token 不参与。
        3. 结论中不得出现 table.columns 以外的列名（防止模型编造口径字段）。
        """
        errors = []
        columns = [c.name for c in table.columns]
        evidence = []
        for figure in draft.key_figures:
            if figure.row_index >= len(table.rows) or figure.column not in columns:
                errors.append("关键数值单元格不存在")
                continue
            value = table.rows[figure.row_index][columns.index(figure.column)]
            metric = next((m for m in metrics if _metric_column(figure.column, m.metric_id)), None)
            dp = metric.decimal_places if metric else 2
            if not isinstance(value, (int,float)) or isinstance(value,bool) or not math.isfinite(value) or not math.isfinite(figure.value) or abs(value-figure.value) > 0.5 * 10**-dp + 1e-12 or (metric and figure.unit is not metric.unit):
                errors.append("关键数值与单元格不一致")
            else:
                evidence.append(float(value))
        evidence += [float(len(alerts)), *[float(sum(a.severity is s for a in alerts)) for s in AlertSeverity]]
        text = draft.conclusion + "\n" + "\n".join(draft.follow_up_suggestions)
        for token in extract_numeric_tokens(text):
            if not any(numeric_match(token, value) for value in evidence):
                errors.append("结论存在未锚定数值")
        for identifier in re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", text):
            if identifier not in columns:
                errors.append("结论出现结果表之外的字段")
        if any(term in text.casefold() for term in self.semantic_layer.catalog.global_sensitive_columns + self.semantic_layer.catalog.forbidden_tables):
            errors.append("结论包含受控字段")
        return errors

    # ------------------------------------------------------------------
    # 评测映射
    # ------------------------------------------------------------------

    def _to_agent_response(self, outcome: AnalysisOutcome) -> AgentResponse:
        """SupplyChainQueryResponse → AgentResponse：

        | AgentResponse 字段    | 来源                                                                     |
        |----------------------|--------------------------------------------------------------------------|
        | answer               | response.conclusion                                                      |
        | retrieved_contexts   | ANSWERED：RetrievedParentChunk(doc_id=f"{RESULT_CONTEXT_DOC_PREFIX}{metric}", |
        |                      |   parent_id=f"{doc_id}:{trace_id or '-'}", version=catalog_version,       |
        |                      |   section_path="supply_chain_bi/result", full_content=render_result_context, |
        |                      |   score=1.0, rrf_score=0.0, source_branches=[RetrievalBranch.KEYWORD]（占位，|
        |                      |   无检索语义）) + 有预警时同构的 ALERT_CONTEXT_DOC_ID 块；其余 stop_reason 为 [] |
        | cited_doc_ids        | [f"{TABLE_DOC_PREFIX}{t}" for t in referenced_tables] + 结果上下文 doc_id，保序去重 |
        | refused              | response.refused                                                          |
        | latency_ms           | response.latency_ms                                                       |
        | degraded / degraded_reasons | narrative_source == TEMPLATE_FALLBACK 或结果被截断 → degraded=True；   |
        |                      |   degraded_reasons 恒为 []（DegradedReason 为检索专用枚举，不得复用）       |
        | token_usage          | response.token_usage                                                      |
        """
        response = outcome.response
        contexts = []
        if response.stop_reason is StopReason.ANSWERED:
            metric = response.matched_metric_ids[0] if response.matched_metric_ids else 'adhoc'
            doc_id = RESULT_CONTEXT_DOC_PREFIX + metric
            def chunk(doc, content, section):
                parent = f"{doc}:{response.trace_id or '-'}"
                if len(parent) > 192:
                    parent = f"{doc}:{hashlib.sha256(parent.encode()).hexdigest()}"
                return RetrievedParentChunk(doc_id=doc, parent_id=parent, version=self.semantic_layer.catalog.catalog_version, section_path=section, full_content=content, score=1.0, rrf_score=0.0, source_branches=[RetrievalBranch.KEYWORD])
            contexts.append(chunk(doc_id, render_result_context(catalog_version=self.semantic_layer.catalog.catalog_version, metric_ids=response.matched_metric_ids, as_of_date=response.as_of_date, scope_summary=response.scope_summary, final_sql=response.final_sql, table=response.result_table), 'supply_chain_bi/result'))
            if response.alerts:
                contexts.append(chunk(ALERT_CONTEXT_DOC_ID, render_alert_context(response.alerts), 'supply_chain_bi/alerts'))
        return AgentResponse(answer=response.conclusion, retrieved_contexts=contexts, cited_doc_ids=list(dict.fromkeys([TABLE_DOC_PREFIX+t for t in response.referenced_tables] + [c.doc_id for c in contexts])), refused=response.refused, latency_ms=response.latency_ms, degraded=response.narrative_source is NarrativeSource.TEMPLATE_FALLBACK or bool(response.result_table and response.result_table.source_truncated), token_usage=response.token_usage)
