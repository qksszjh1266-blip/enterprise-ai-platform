"""评测流水线 EvalRunner 与基线门禁对比器 RegressionGate。

EvalRunner 单样本编排（_evaluate_one）：
    ① async with semaphore（max_concurrency）
    ② asyncio.timeout(sample_timeout_s) 包裹 ③~⑥
    ③ RETRIEVAL_ONLY：retrieval.search(SearchRequest(question, user_context, top_k, enable_rerank))
       END_TO_END    ：agent.invoke(AgentInvocation(...))
    ④ RetrievalMetricsCalculator + AclRedlineDetector（确定性）
    ⑤ END_TO_END 且非红线：asyncio.gather 并发三项裁判（各自 judge_timeout_s）
    ⑥ FailureAttributor 归因
    异常：TimeoutError -> TIMEOUT；被测系统异常 -> SYSTEM_ERROR；裁判异常 -> JUDGE_ERROR（检索指标保留）。
    任何单样本异常都不得中断整轮评测；asyncio.CancelledError 必须继续向上传播。

RegressionGate：对比 baseline / candidate 两个 EvalRunRecord，输出 RegressionDiffReport。
门禁按红线违规、指标回退和绝对阈值依次判定。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime, timezone
from time import perf_counter
from collections.abc import Sequence
from pathlib import Path

from data_platform.retrieval_engine.models import RetrievedParentChunk, SearchRequest
from eval_platform.evaluators import (
    AclRedlineDetector,
    FailureAttributor,
    RetrievalMetricsCalculator,
    SuiteMetricsAggregator,
)
from eval_platform.models import (
    AgentInvocation,
    EvalMode,
    FailureCategory,
    GateViolationCode,
    BadCaseSummary,
    EvalRunConfig,
    EvalRunRecord,
    EvalSample,
    GateConfig,
    GateViolation,
    MetricDelta,
    RegressionDiffReport,
    SampleEvalResult,
)
from eval_platform.ports import AgentSystemPort, LLMJudgePort, RetrievalSystemPort


async def load_eval_suite(path: Path) -> list[EvalSample]:
    """从 JSONL 文件加载黄金测试集（每行一个 EvalSample）。

    - 文件读取经 asyncio.to_thread，不阻塞事件循环。
    - 使用 EvalSample.model_validate_json 逐行校验；空行跳过。
    - 任一行校验失败抛 ValueError，消息含行号与 sample_id（若可解析）。
    - sample_id 重复抛 ValueError。
    """
    content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    samples = []
    seen = set()
    # JSON array is also supported for the versioned catering suite.
    if path.suffix.lower() == ".json":
        try:
            entries = json.loads(content)
            if not isinstance(entries, list):
                raise ValueError("JSON suite must be an array")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid suite: {exc}") from exc
        lines = [(i, json.dumps(entry, ensure_ascii=False)) for i, entry in enumerate(entries, 1)]
    else:
        lines = enumerate(content.splitlines(), 1)
    for number, line in lines:
        if not line.strip():
            continue
        sample_id = "?"
        try:
            raw = json.loads(line)
            if isinstance(raw, dict):
                sample_id = raw.get("sample_id", "?")
            sample = EvalSample.model_validate_json(line)
            if sample.sample_id in seen:
                raise ValueError(f"duplicate sample_id={sample.sample_id}")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"line/item {number} sample_id={sample_id}: {exc}") from exc
        seen.add(sample.sample_id)
        samples.append(sample)
    return samples


def compute_suite_fingerprint(samples: Sequence[EvalSample]) -> str:
    """样本集指纹：按 sample_id 升序，对每条 model_dump_json() 拼接 "\\n" 后取 sha256 hex。

    与样本输入顺序无关；任一字段变化指纹即变化。
    """
    serialized = "\n".join(s.model_dump_json() for s in sorted(samples, key=lambda s: s.sample_id))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class EvalRunner:
    """支持并发限流的异步评测流水线。"""

    def __init__(
        self,
        *,
        config: EvalRunConfig,
        retrieval: RetrievalSystemPort | None = None,
        agent: AgentSystemPort | None = None,
        judge: LLMJudgePort | None = None,
        metrics: RetrievalMetricsCalculator | None = None,
        acl_detector: AclRedlineDetector | None = None,
    ) -> None:
        """注入被测系统与裁判。

        校验（ValueError）：RETRIEVAL_ONLY 需 retrieval；END_TO_END 需 agent 与 judge。
        metrics / acl_detector 为 None 时使用默认实例；FailureAttributor / SuiteMetricsAggregator 由 config 构造。
        """
        if config.mode == EvalMode.RETRIEVAL_ONLY and retrieval is None:
            raise ValueError("RETRIEVAL_ONLY requires retrieval")
        if config.mode == EvalMode.END_TO_END and (agent is None or judge is None):
            raise ValueError("END_TO_END requires agent and judge")
        self.config, self.retrieval, self.agent, self.judge = config, retrieval, agent, judge
        self.metrics = metrics if metrics is not None else RetrievalMetricsCalculator()
        self.acl_detector = acl_detector if acl_detector is not None else AclRedlineDetector()
        self.attributor = FailureAttributor(config)
        self.aggregator = SuiteMetricsAggregator(config)

    async def run(self, samples: Sequence[EvalSample], *, suite_name: str, run_id: str | None = None) -> EvalRunRecord:
        """执行整轮评测。

        - samples 为空或 sample_id 重复抛 ValueError。
        - 创建 asyncio.Semaphore(config.max_concurrency)，asyncio.gather 调度全部 _evaluate_one。
        - results 按输入 samples 顺序输出（与完成顺序无关）。
        - run_id 为 None 时生成 "{system_version}-{UTC 时间戳}"。
        - 填充 suite_fingerprint / started_at / finished_at（UTC aware）/ summary。
        """
        if not samples or len({s.sample_id for s in samples}) != len(samples):
            raise ValueError("samples must be nonempty with unique sample_id")
        started = datetime.now(timezone.utc)
        if run_id is None:
            run_id = f"{self.config.system_version[:90]}-{started.strftime('%Y%m%dT%H%M%S%fZ')}"
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        results = await asyncio.gather(*(self._evaluate_one(s, semaphore, run_id=run_id) for s in samples))
        return EvalRunRecord(run_id=run_id, suite_name=suite_name,
            suite_fingerprint=compute_suite_fingerprint(samples), config=self.config,
            started_at=started, finished_at=datetime.now(timezone.utc),
            results=results, summary=self.aggregator.aggregate(results))

    async def _evaluate_one(self, sample: EvalSample, semaphore: asyncio.Semaphore, *, run_id: str) -> SampleEvalResult:
        """单样本编排（见模块文档 ①~⑥）。绝不抛出业务异常，只返回带归因的结果。"""
        async with semaphore:
            try:
                async with asyncio.timeout(self.config.sample_timeout_s):
                    if self.config.mode == EvalMode.RETRIEVAL_ONLY:
                        result = await self._run_retrieval(sample)
                    else:
                        result = await self._run_agent(sample, run_id=run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = self._build_error_result(sample, exc)
            result.failure_category = self.attributor.attribute(result)
            return result

    async def _run_retrieval(self, sample: EvalSample) -> SampleEvalResult:
        """RETRIEVAL_ONLY 分支：调用 RetrievalSystemPort，填充检索指标、越权检测、latency / degraded。"""
        response = await self.retrieval.search(SearchRequest(query=sample.question,
            user_context=sample.user_context, top_k=self.config.top_k, enable_rerank=self.config.enable_rerank))
        metrics = self.metrics.evaluate(sample, response.results, self.config.top_k)
        acl = self.acl_detector.detect(sample, retrieved_doc_ids=metrics.retrieved_doc_ids)
        return SampleEvalResult(**_sample_identity(sample), **metrics.model_dump(),
            acl_leaked=acl.leaked, leaked_doc_ids=acl.leaked_doc_ids,
            latency_ms=response.latency_ms, degraded=response.degraded, degraded_reasons=response.degraded_reasons)

    async def _run_agent(self, sample: EvalSample, *, run_id: str) -> SampleEvalResult:
        """END_TO_END 分支：调用 AgentSystemPort（trace_id = f"{run_id}:{sample_id}"），
        基于 retrieved_contexts 计算检索指标，基于 answer 计算 keyword_coverage 与越权检测，然后调用 _run_judges。"""
        start = perf_counter()
        response = await self.agent.invoke(AgentInvocation(trace_id=f"{run_id}:{sample.sample_id}",
            scenario=sample.scenario, question=sample.question, user_context=sample.user_context))
        measured_ms = (perf_counter() - start) * 1000
        metrics = self.metrics.evaluate(sample, response.retrieved_contexts, self.config.top_k)
        acl = self.acl_detector.detect(sample, retrieved_doc_ids=metrics.retrieved_doc_ids,
            cited_doc_ids=response.cited_doc_ids, answer=response.answer)
        partial = SampleEvalResult(**_sample_identity(sample), **metrics.model_dump(),
            acl_leaked=acl.leaked, leaked_doc_ids=acl.leaked_doc_ids, answer=response.answer,
            keyword_coverage=None if sample.is_acl_redline else self.metrics.keyword_coverage(response.answer, sample.expected_keywords),
            latency_ms=max(response.latency_ms, measured_ms), degraded=response.degraded,
            degraded_reasons=response.degraded_reasons, token_cost=response.token_usage.total_tokens)
        return await self._run_judges(sample, partial, response.retrieved_contexts)

    async def _run_judges(self, sample: EvalSample, partial: SampleEvalResult, contexts: Sequence[RetrievedParentChunk]) -> SampleEvalResult:
        """asyncio.gather(return_exceptions=True) 并发三项裁判，每项 asyncio.timeout(judge_timeout_s)。

        - 红线样本跳过裁判（三项分数均 None）。
        - 任一项异常 / 超时 / 分数越界 -> 该项为 None，failure_category 置 JUDGE_ERROR，其余项照常保留。
        - 累加 judge_token_cost，写入 judge_reasons。
        """
        if sample.is_acl_redline:
            return partial
        async def call(method, **kwargs):
            async with asyncio.timeout(self.config.judge_timeout_s):
                return await method(**kwargs)
        calls = [
            ("faithfulness", "faithfulness_score", 0, 1, self.judge.score_faithfulness,
             dict(question=sample.question, answer=partial.answer, contexts=list(contexts))),
            ("accuracy", "answer_accuracy_score", 0, 1, self.judge.score_accuracy,
             dict(question=sample.question, answer=partial.answer, reference_answer=sample.reference_answer, expected_keywords=sample.expected_keywords)),
            ("satisfaction", "satisfaction_score", 1, 5, self.judge.simulate_satisfaction,
             dict(question=sample.question, answer=partial.answer, scenario=sample.scenario, user_context=sample.user_context)),
        ]
        # Accuracy cannot be judged without a reference or key facts.
        calls = [c for c in calls if c[0] != "accuracy" or sample.reference_answer or sample.expected_keywords]
        outcomes = await asyncio.gather(*(call(c[4], **c[5]) for c in calls), return_exceptions=True)
        errors = []
        for (name, field, low, high, _, _), outcome in zip(calls, outcomes):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            try:
                if isinstance(outcome, BaseException):
                    raise outcome
                partial.judge_token_cost += outcome.token_usage.total_tokens
                if not math.isfinite(outcome.score) or not low <= outcome.score <= high:
                    raise ValueError(f"score outside [{low}, {high}]")
                setattr(partial, field, outcome.score)
                partial.judge_reasons[name] = outcome.reason
            except Exception as exc:
                setattr(partial, field, None)
                partial.failure_category = FailureCategory.JUDGE_ERROR
                detail = f"{name}: {type(exc).__name__}: {exc}"[:500]
                partial.judge_reasons[name] = detail
                errors.append(detail)
        if errors:
            partial.error_message = "; ".join(errors)[:2000]
        return partial

    @staticmethod
    def _build_error_result(sample: EvalSample, exc: BaseException) -> SampleEvalResult:
        """异常样本结果：TimeoutError -> TIMEOUT，其他 -> SYSTEM_ERROR。

        error_message 为 f"{type(exc).__name__}: {exc}" 截断至 500 字符，不含堆栈；
        非红线样本 hit/recall/mrr 置 0.0（fail-closed），latency_ms 为 None。
        """
        score = None if sample.is_acl_redline else 0.0
        return SampleEvalResult(**_sample_identity(sample), hit_at_k=score, recall_at_k=score, mrr=score,
            failure_category=FailureCategory.TIMEOUT if isinstance(exc, TimeoutError) else FailureCategory.SYSTEM_ERROR,
            error_message=f"{type(exc).__name__}: {exc}"[:500])


class RegressionGate:
    """基线门禁对比器：candidate 相对 baseline 是否允许上线。"""

    COMPARED_METRICS: tuple[tuple[str, bool], ...] = (
        ("retrieval_hit_rate", True),
        ("mean_recall_at_k", True),
        ("mean_mrr", True),
        ("answer_accuracy_rate", True),
        ("mean_faithfulness", True),
        ("mean_satisfaction", True),
        ("latency_p50_ms", False),
        ("latency_p95_ms", False),
        ("degraded_rate", False),
        ("error_rate", False),
        ("acl_block_rate", True),
    )
    """参与对比的 SuiteSummaryMetrics 字段及 higher_is_better；MetricDelta 按此顺序输出。"""

    def __init__(self, config: GateConfig | None = None) -> None:
        """config 为 None 时使用 GateConfig() 默认值。"""
        self.config = config or GateConfig()

    def compare(self, baseline: EvalRunRecord, candidate: EvalRunRecord) -> RegressionDiffReport:
        """生成回归对比与门禁结论。

        顺序：_check_hard_redlines -> （suite 可比时）_diff_metrics + _check_regressions
        -> _diff_samples -> _select_bad_cases。gate_passed = not violations。
        """
        violations = self._check_hard_redlines(baseline, candidate)
        comparable = baseline.suite_fingerprint == candidate.suite_fingerprint
        newly, fixed = self._diff_samples(baseline, candidate)
        deltas = []
        if comparable or not self.config.require_same_suite:
            deltas = self._diff_metrics(baseline, candidate)
            violations.extend(self._check_regressions(deltas, candidate))
            if len(newly) > self.config.max_newly_failed:
                violations.append(GateViolation(code=GateViolationCode.METRIC_REGRESSION,
                    message=f"新增失败 {len(newly)} 条，超过上限 {self.config.max_newly_failed}"))
        return RegressionDiffReport(baseline_run_id=baseline.run_id, candidate_run_id=candidate.run_id,
            baseline_version=baseline.config.system_version, candidate_version=candidate.config.system_version,
            suite_comparable=comparable, metric_deltas=deltas, gate_passed=not violations,
            violations=violations, blocking_reasons=[v.message for v in violations],
            newly_failed_sample_ids=newly, fixed_sample_ids=fixed,
            top_bad_cases=self._select_bad_cases(baseline, candidate, newly))

    def _diff_metrics(self, baseline: EvalRunRecord, candidate: EvalRunRecord) -> list[MetricDelta]:
        """按 COMPARED_METRICS 计算 abs_delta / rel_delta_pct / tolerance / regressed。"""
        tolerances = self._tolerances()
        deltas = []
        for metric, higher in self.COMPARED_METRICS:
            b, c = getattr(baseline.summary, metric), getattr(candidate.summary, metric)
            absolute = c - b if b is not None and c is not None else None
            tolerance = tolerances.get(metric)
            if metric == "latency_p95_ms" and b is not None:
                tolerance = b * self.config.max_p95_latency_increase
            drop = (-absolute if higher else absolute) if absolute is not None else None
            deltas.append(MetricDelta(metric=metric, baseline=b, candidate=c, abs_delta=absolute,
                rel_delta_pct=absolute / abs(b) * 100 if absolute is not None and b != 0 else None,
                higher_is_better=higher, tolerance=tolerance,
                regressed=drop is not None and tolerance is not None and drop > tolerance + 1e-9))
        return deltas

    def _check_hard_redlines(self, baseline: EvalRunRecord, candidate: EvalRunRecord) -> list[GateViolation]:
        """不可豁免项：候选 acl_redline_passed 为 False；require_same_suite 且指纹不一致。"""
        violations = []
        if not candidate.summary.acl_redline_passed:
            violations.append(GateViolation(code=GateViolationCode.ACL_REDLINE_BREACHED,
                metric="acl_block_rate", message="ACL 红线未通过：存在泄露、未验证样本或无红线样本，阻断发布"))
        if self.config.require_same_suite and baseline.suite_fingerprint != candidate.suite_fingerprint:
            violations.append(GateViolation(code=GateViolationCode.SUITE_MISMATCH,
                message="样本集指纹不一致，跳过指标门禁对比"))
        return violations

    def _check_regressions(self, deltas: Sequence[MetricDelta], candidate: EvalRunRecord) -> list[GateViolation]:
        """相对回退、绝对下限、P95 增幅、降级率、错误率、METRIC_UNAVAILABLE 判定。"""
        violations = []
        def add(code, metric, message):
            violations.append(GateViolation(code=code, metric=metric, message=message))
        tolerances = self._tolerances()
        # G3: relative quality regression, in COMPARED_METRICS order.
        for d in deltas:
            if d.metric in tolerances and d.regressed:
                if d.metric == "mean_satisfaction":
                    message = f"{d.metric} 回退 {-d.abs_delta:.2f} 分（基线 {d.baseline:.2f} → 候选 {d.candidate:.2f}，容忍 {d.tolerance:.2f} 分）"
                else:
                    message = f"{d.metric} 回退 {-d.abs_delta * 100:.1f}pp（基线 {d.baseline:.1%} → 候选 {d.candidate:.1%}，容忍 {d.tolerance * 100:.1f}pp）"
                add(GateViolationCode.METRIC_REGRESSION, d.metric, message)
        # G4: absolute quality floors.
        for metric, floor in (("retrieval_hit_rate", self.config.min_hit_rate), ("mean_faithfulness", self.config.min_faithfulness)):
            value = getattr(candidate.summary, metric)
            if value is not None and value < floor - 1e-9:
                add(GateViolationCode.ABSOLUTE_FLOOR, metric, f"{metric} {value:.1%} 低于下限 {floor:.1%}")
        # G5: relative and absolute latency limits.
        for d in deltas:
            if d.metric == "latency_p95_ms" and d.candidate is not None:
                if d.regressed:
                    add(GateViolationCode.LATENCY_REGRESSION, d.metric, f"P95 延迟 {d.candidate:.1f}ms 超过基线允许增幅 {self.config.max_p95_latency_increase:.1%}")
                limit = self.config.max_p95_latency_ms
                if limit is not None and d.candidate > limit + 1e-9:
                    add(GateViolationCode.LATENCY_REGRESSION, d.metric, f"P95 延迟 {d.candidate:.1f}ms 超过绝对上限 {limit:.1f}ms")
        # G6: degraded and system error rates.
        for metric, limit in (("degraded_rate", self.config.max_degraded_rate), ("error_rate", self.config.max_error_rate)):
            value = getattr(candidate.summary, metric)
            if value is not None and value > limit + 1e-9:
                add(GateViolationCode.ERROR_RATE_EXCEEDED, metric, f"{metric} {value:.1%} 超过上限 {limit:.1%}")
        # G7: disappearing metrics cannot hide regressions.
        for d in deltas:
            if d.baseline is not None and d.candidate is None:
                add(GateViolationCode.METRIC_UNAVAILABLE, d.metric, f"{d.metric} 基线有值但候选不可用（N/A）")
        return violations

    @staticmethod
    def _diff_samples(baseline: EvalRunRecord, candidate: EvalRunRecord) -> tuple[list[str], list[str]]:
        """按 sample_id 对齐，返回 (newly_failed_sample_ids, fixed_sample_ids)，均按 sample_id 升序。"""
        previous = {r.sample_id: r for r in baseline.results}
        newly, fixed = [], []
        for r in candidate.results:
            old = previous.get(r.sample_id)
            if old is None:
                continue
            if not old.failure_category.is_failure and r.failure_category.is_failure:
                newly.append(r.sample_id)
            if old.failure_category.is_failure and not r.failure_category.is_failure:
                fixed.append(r.sample_id)
        return sorted(newly), sorted(fixed)

    def _select_bad_cases(
        self, baseline: EvalRunRecord, candidate: EvalRunRecord, newly_failed: Sequence[str]
    ) -> list[BadCaseSummary]:
        """候选中 is_failure 的样本按 severity 降序、sample_id 升序取前 top_bad_cases 条。"""
        previous = {r.sample_id: r.failure_category for r in baseline.results}
        newly = set(newly_failed)
        cases = []
        for r in candidate.results:
            if not r.failure_category.is_failure:
                continue
            detail = (f"hit={r.hit_at_k}; recall={r.recall_at_k}; mrr={r.mrr}; "
                      f"faithfulness={r.faithfulness_score}; accuracy={r.answer_accuracy_score}; "
                      f"satisfaction={r.satisfaction_score}; leaked_doc_ids={r.leaked_doc_ids}; "
                      f"{r.error_message or ''}; " + "; ".join(f"{k}: {v}" for k, v in sorted(r.judge_reasons.items())))
            cases.append(BadCaseSummary(sample_id=r.sample_id, scenario=r.scenario, question=r.question,
                failure_category=r.failure_category, baseline_category=previous.get(r.sample_id),
                newly_failed=r.sample_id in newly, severity=_SEVERITY[r.failure_category] + (15 if r.sample_id in newly else 0),
                detail=detail[:1000]))
        return sorted(cases, key=lambda c: (-c.severity, c.sample_id))[:self.config.top_bad_cases]


    def _tolerances(self) -> dict[str, float]:
        return dict(zip(("retrieval_hit_rate", "mean_recall_at_k", "mean_mrr", "answer_accuracy_rate", "mean_faithfulness", "mean_satisfaction"),
                        (self.config.max_hit_rate_drop, self.config.max_recall_drop, self.config.max_mrr_drop,
                         self.config.max_accuracy_drop, self.config.max_faithfulness_drop, self.config.max_satisfaction_drop)))


def _sample_identity(sample: EvalSample) -> dict:
    return dict(sample_id=sample.sample_id, scenario=sample.scenario, dept_id=sample.user_context.dept_id,
                question=sample.question, is_acl_redline=sample.is_acl_redline)


_SEVERITY = {
    FailureCategory.ACL_LEAK: 100, FailureCategory.SYSTEM_ERROR: 80, FailureCategory.TIMEOUT: 80,
    FailureCategory.RETRIEVAL_MISS: 60, FailureCategory.HALLUCINATION: 50, FailureCategory.ANSWER_INCORRECT: 40,
    FailureCategory.RANKING_LOW: 30, FailureCategory.INCOMPLETE_ANSWER: 30, FailureCategory.LOW_SATISFACTION: 20,
    FailureCategory.JUDGE_ERROR: 10,
}
