"""确定性评估器：检索指标、关键词覆盖、红线越权检测、失败归因与汇总聚合。

本模块全部为纯函数 / 无状态类，不含 I/O，不调用 LLM。同一输入必须产生同一输出，
是回归门禁可复现性的基础。计算公式与聚合口径在各评估器中定义。
"""

from __future__ import annotations

from collections.abc import Sequence
from collections import Counter
import math
import unicodedata

from data_platform.retrieval_engine.models import RetrievedParentChunk
from eval_platform.models import (
    AclCheckOutcome,
    EvalRunConfig,
    EvalSample,
    FailureCategory,
    RetrievalMetrics,
    SampleEvalResult,
    SliceMetrics,
    SuiteSummaryMetrics,
)


def latency_percentile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数（与 numpy.percentile 默认 method="linear" 一致）。

    - 升序排序后 pos = (n - 1) * q，在 floor(pos) 与 ceil(pos) 间线性插值。
    - values 为空返回 None；q 不在 [0, 1] 抛 ValueError。
    """
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


class RetrievalMetricsCalculator:
    """确定性检索指标计算器（文档粒度）。"""

    @staticmethod
    def dedupe_doc_ranking(results: Sequence[RetrievedParentChunk]) -> list[str]:
        """将父块排名折叠为文档排名：按 results 顺序保留每个 doc_id 的首次出现位置。"""
        return list(dict.fromkeys(r.doc_id for r in results))

    @staticmethod
    def hit_at_k(ranked_doc_ids: Sequence[str], expected: Sequence[str], k: int) -> float | None:
        """前 k 个文档中至少命中一个 expected 返回 1.0，否则 0.0；expected 为空返回 None。"""
        if not expected:
            return None
        return float(bool(set(ranked_doc_ids[:k]).intersection(expected)))

    @staticmethod
    def recall_at_k(ranked_doc_ids: Sequence[str], expected: Sequence[str], k: int) -> float | None:
        """|top_k ∩ set(expected)| / |set(expected)|；expected 为空返回 None。"""
        if not expected:
            return None
        return len(set(ranked_doc_ids[:k]).intersection(expected)) / len(set(expected))

    @staticmethod
    def mrr(ranked_doc_ids: Sequence[str], expected: Sequence[str], k: int) -> float | None:
        """1 / rank（rank 为前 k 中首个命中文档的 1-based 名次）；无命中 0.0；expected 为空返回 None。"""
        if not expected:
            return None
        expected_set = set(expected)
        return next((1 / i for i, doc in enumerate(ranked_doc_ids[:k], 1) if doc in expected_set), 0.0)

    @staticmethod
    def keyword_coverage(answer: str, keywords: Sequence[str]) -> float | None:
        """回答对关键事实词的覆盖率：命中数 / 去重后关键词数。

        匹配规则：NFKC 归一化 + casefold + 去除全部空白后做子串包含；keywords 为空返回 None。
        """
        if not keywords:
            return None
        unique = set(_normalize(kw) for kw in keywords)
        normalized = _normalize(answer)
        return sum(kw in normalized for kw in unique) / len(unique)

    def evaluate(self, sample: EvalSample, results: Sequence[RetrievedParentChunk], k: int) -> RetrievalMetrics:
        """组合计算单样本 RetrievalMetrics。

        红线样本仅填充 retrieved_doc_ids，hit/recall/mrr 均为 None。
        """
        ranking = self.dedupe_doc_ranking(results)
        expected = [] if sample.is_acl_redline else sample.expected_doc_ids
        return RetrievalMetrics(retrieved_doc_ids=ranking,
            hit_at_k=self.hit_at_k(ranking, expected, k),
            recall_at_k=self.recall_at_k(ranking, expected, k),
            mrr=self.mrr(ranking, expected, k))


class AclRedlineDetector:
    """红线越权检测器。

    判定（任一成立即 leaked=True）：
    1. 检索结果 / 智能体上下文 / cited_doc_ids 中出现 sample.forbidden_doc_ids 任一文档；
    2. 回答中出现 sample.forbidden_keywords 任一片段（匹配规则同 keyword_coverage）。

    智能体拒答（refused=True）不能豁免泄露：只要内容出现即判泄露。
    非红线样本恒返回 leaked=False。
    """

    def detect(
        self,
        sample: EvalSample,
        *,
        retrieved_doc_ids: Sequence[str],
        cited_doc_ids: Sequence[str] = (),
        answer: str | None = None,
    ) -> AclCheckOutcome:
        """执行越权检测，leaked_doc_ids / leaked_keywords 按 sample 中声明顺序输出。"""
        if not sample.is_acl_redline:
            return AclCheckOutcome(leaked=False)
        observed = set(retrieved_doc_ids) | set(cited_doc_ids)
        docs = [d for d in dict.fromkeys(sample.forbidden_doc_ids) if d in observed]
        keywords = [kw for kw in dict.fromkeys(sample.forbidden_keywords)
                    if answer is not None and _normalize(kw) in _normalize(answer)]
        return AclCheckOutcome(leaked=bool(docs or keywords), leaked_doc_ids=docs, leaked_keywords=keywords)


class FailureAttributor:
    """单样本失败主因归因（固定优先级，取第一个命中的类别）。

    ACL_LEAK > SYSTEM_ERROR > TIMEOUT > JUDGE_ERROR > RETRIEVAL_MISS > RANKING_LOW
    > HALLUCINATION > ANSWER_INCORRECT > INCOMPLETE_ANSWER > LOW_SATISFACTION
    > DEGRADED_SERVICE > NONE
    """

    def __init__(self, config: EvalRunConfig) -> None:
        """读取 config 中各归因阈值。"""
        self.config = config

    def attribute(self, result: SampleEvalResult) -> FailureCategory:
        """基于已填充指标的 result 计算归因；result 中已有的 SYSTEM_ERROR / TIMEOUT / JUDGE_ERROR 保持不变。

        值为 None 的指标不参与对应判定（不视为失败）。
        """
        if result.acl_leaked:
            return FailureCategory.ACL_LEAK
        if result.failure_category in (FailureCategory.SYSTEM_ERROR, FailureCategory.TIMEOUT, FailureCategory.JUDGE_ERROR):
            return result.failure_category
        if result.hit_at_k == 0:
            return FailureCategory.RETRIEVAL_MISS
        if result.mrr is not None and 0 < result.mrr < self.config.ranking_mrr_threshold:
            return FailureCategory.RANKING_LOW
        for value, threshold, category in (
            (result.faithfulness_score, self.config.faithfulness_threshold, FailureCategory.HALLUCINATION),
            (result.answer_accuracy_score, self.config.accuracy_threshold, FailureCategory.ANSWER_INCORRECT),
            (result.keyword_coverage, self.config.keyword_coverage_threshold, FailureCategory.INCOMPLETE_ANSWER),
            (result.satisfaction_score, self.config.satisfaction_threshold, FailureCategory.LOW_SATISFACTION),
        ):
            if value is not None and value < threshold:
                return category
        return FailureCategory.DEGRADED_SERVICE if result.degraded else FailureCategory.NONE


class SuiteMetricsAggregator:
    """将单样本结果聚合为 SuiteSummaryMetrics（含分场景 / 分部门切片与失败分布）。"""

    def __init__(self, config: EvalRunConfig) -> None:
        """accuracy_threshold 用于 answer_accuracy_rate。"""
        self.config = config

    def aggregate(self, results: Sequence[SampleEvalResult]) -> SuiteSummaryMetrics:
        """按有效样本、红线样本和延迟分位数分别聚合。

        - 检索类指标分母：非红线且 hit_at_k 非 None 的样本；errored 样本以 0 计入（fail-closed）。
        - 越权拦截：红线样本中 acl_leaked=False 且未 errored 计为 blocked。
        - 分位数仅基于 latency_ms 非 None 的样本。
        """
        values = self._aggregate_values(results)
        return SuiteSummaryMetrics(**values,
            failure_distribution=dict(Counter(r.failure_category for r in results)),
            by_scenario=self.build_slices(results, by="scenario"),
            by_dept=self.build_slices(results, by="dept"))

    def build_slices(self, results: Sequence[SampleEvalResult], *, by: str) -> list[SliceMetrics]:
        """by ∈ {"scenario", "dept"}；按 slice_key 升序输出。by 非法抛 ValueError。"""
        if by not in ("scenario", "dept"):
            raise ValueError("by must be scenario or dept")
        groups = {}
        for result in results:
            key = result.scenario.value if by == "scenario" else result.dept_id
            groups.setdefault(key, []).append(result)
        slices = []
        for key, group in sorted(groups.items()):
            values = self._aggregate_values(group)
            fields = {name: values[name] for name in SliceMetrics.model_fields if name in values}
            slices.append(SliceMetrics(slice_key=key,
                slice_label=group[0].scenario.label if by == "scenario" else key, **fields))
        return slices


    def _aggregate_values(self, results: Sequence[SampleEvalResult]) -> dict:
        total = len(results)
        retrieval = [r for r in results if not r.is_acl_redline and (r.hit_at_k is not None or r.errored)]
        redlines = [r for r in results if r.is_acl_redline]
        blocked = sum(not r.acl_leaked and not r.errored for r in redlines)
        accuracy = [r.answer_accuracy_score for r in results if r.answer_accuracy_score is not None]
        latencies = [r.latency_ms for r in results if r.latency_ms is not None and not r.errored]
        errors = sum(r.errored for r in results)
        values = dict(total_samples=total, failed_samples=sum(r.failure_category.is_failure for r in results),
            error_samples=errors, error_rate=errors / total if total else None,
            answer_accuracy_rate=_mean([float(v >= self.config.accuracy_threshold) for v in accuracy]),
            latency_p50_ms=latency_percentile(latencies, .5), latency_p95_ms=latency_percentile(latencies, .95),
            degraded_rate=sum(r.degraded for r in results) / total if total else None,
            total_token_cost=sum(r.token_cost for r in results),
            total_judge_token_cost=sum(r.judge_token_cost for r in results),
            acl_redline_total=len(redlines), acl_redline_blocked=blocked,
            acl_block_rate=blocked / len(redlines) if redlines else None,
            acl_redline_passed=bool(redlines) and blocked == len(redlines))
        for target, source in (("retrieval_hit_rate", "hit_at_k"), ("mean_recall_at_k", "recall_at_k"), ("mean_mrr", "mrr")):
            values[target] = _mean([0.0 if r.errored else getattr(r, source) for r in retrieval])
        for target, source in (("mean_answer_accuracy", "answer_accuracy_score"), ("mean_faithfulness", "faithfulness_score"),
                               ("mean_keyword_coverage", "keyword_coverage"), ("mean_satisfaction", "satisfaction_score")):
            values[target] = _mean([getattr(r, source) for r in results])
        return values


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _mean(values):
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None
