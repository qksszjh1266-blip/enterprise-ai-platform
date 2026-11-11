"""评测中台数据模型与传输契约。

本模块仅包含 Pydantic V2 模型与枚举，不含任何 I/O：

样本与配置
- EvalScenario          业务场景枚举（门店 SOP / 供应链 / 财务审批）
- EvalMode              评测模式（仅检索 / 端到端）
- EvalSample            黄金测试集单条样本
- EvalRunConfig         EvalRunner 运行参数（并发、超时、归因阈值）
- GateConfig            RegressionGate 门禁阈值

端口传输（ports.py 使用）
- AgentInvocation / AgentResponse   业务智能体端到端调用契约
- JudgeScore                        裁判模型单维度打分结果

评测结果
- FailureCategory       失败归因枚举
- RetrievalMetrics      单样本确定性检索指标
- AclCheckOutcome       红线越权检测结果
- SampleEvalResult      单样本评测结果
- SliceMetrics          分场景 / 分部门切片指标
- SuiteSummaryMetrics   评测集汇总指标
- EvalRunRecord         一次完整评测运行（元数据 + 明细 + 汇总），门禁与报告的唯一输入

回归门禁
- MetricDelta / GateViolation / BadCaseSummary / RegressionDiffReport

数值约定（实现与测试必须遵守）：
- 比例类指标（hit/recall/mrr/rate/faithfulness/accuracy）一律 0~1 浮点，报告层再格式化为百分比。
- satisfaction_score 为 1~5 Likert 分（模拟用户满意度）。
- 指标不适用或分母为 0 时取 None（N/A），严禁用 0 或 1 填充以免误导门禁。

兼容性要求：公共类名与模型字段保持稳定，变更需同步调用方与契约测试。
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_platform.retrieval_engine.models import (
    DegradedReason,
    RetrievedParentChunk,
    UserContext,
)

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class EvalScenario(str, enum.Enum):
    """业务场景（与 agent_matrix 三大智能体一一对应）。"""

    STORE_SOP = "store_sop"  # 门店 SOP 问答
    SUPPLY_CHAIN = "supply_chain"  # 供应链数据分析
    FINANCE_APPROVAL = "finance_approval"  # 财务合同 / 报销预审

    @property
    def label(self) -> str:
        """报告展示用中文名。"""
        return _SCENARIO_LABELS[self]


_SCENARIO_LABELS: dict[EvalScenario, str] = {
    EvalScenario.STORE_SOP: "门店SOP",
    EvalScenario.SUPPLY_CHAIN: "供应链",
    EvalScenario.FINANCE_APPROVAL: "财务审批",
}


class EvalMode(str, enum.Enum):
    """评测模式。"""

    RETRIEVAL_ONLY = "retrieval_only"  # 仅调用 RetrievalSystemPort，不跑裁判
    END_TO_END = "end_to_end"  # 调用 AgentSystemPort + LLMJudgePort


class FailureCategory(str, enum.Enum):
    """单样本失败归因（每个样本只取一个主因，优先级见 evaluators.FailureAttributor）。"""

    NONE = "none"  # 通过
    ACL_LEAK = "acl_leak"  # 越权泄露（最高优先级，红线）
    SYSTEM_ERROR = "system_error"  # 被测系统抛异常
    TIMEOUT = "timeout"  # 单样本超时
    RETRIEVAL_MISS = "retrieval_miss"  # hit@k == 0，期望文档未召回
    RANKING_LOW = "ranking_low"  # 召回命中但排序靠后（mrr < 阈值）
    HALLUCINATION = "hallucination"  # 忠实度低于阈值（回答未被上下文支撑）
    INCOMPLETE_ANSWER = "incomplete_answer"  # 关键词覆盖率低于阈值
    ANSWER_INCORRECT = "answer_incorrect"  # 准确率低于阈值
    LOW_SATISFACTION = "low_satisfaction"  # 模拟满意度低于阈值
    DEGRADED_SERVICE = "degraded_service"  # 指标通过但发生降级（仅用于提示，不计入失败）
    JUDGE_ERROR = "judge_error"  # 裁判模型调用失败，端到端指标不可用

    @property
    def is_failure(self) -> bool:
        """是否计为失败样本（NONE 与 DEGRADED_SERVICE 不计）。"""
        return self not in (FailureCategory.NONE, FailureCategory.DEGRADED_SERVICE)


class GateViolationCode(str, enum.Enum):
    """门禁阻断原因码（机器可读）。"""

    ACL_REDLINE_BREACHED = "acl_redline_breached"  # 候选越权拦截率 < 100% 或无红线样本
    SUITE_MISMATCH = "suite_mismatch"  # 两次运行样本集指纹不一致，不可比
    METRIC_REGRESSION = "metric_regression"  # 相对基线回退超过容忍度
    ABSOLUTE_FLOOR = "absolute_floor"  # 低于绝对下限
    LATENCY_REGRESSION = "latency_regression"  # P95 增幅超限
    ERROR_RATE_EXCEEDED = "error_rate_exceeded"  # 系统错误 / 超时率超限
    METRIC_UNAVAILABLE = "metric_unavailable"  # 基线有值而候选为 None


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------


class EvalSample(BaseModel):
    """黄金测试集单条样本。

    红线样本（is_acl_redline=True）语义：user_context 无权访问 forbidden_doc_ids，
    被测系统的检索结果与回答中均不得出现这些文档或 forbidden_keywords；
    此类样本不计算 hit/recall/mrr 与准确率，只计入越权拦截率。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    sample_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    scenario: EvalScenario
    question: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext = Field(..., description="提问者身份，原样透传给被测系统做 ACL 过滤")
    expected_doc_ids: list[str] = Field(default_factory=list, description="期望召回的文档 ID（文档粒度，与顺序无关）")
    expected_keywords: list[str] = Field(default_factory=list, description="标准回答必须覆盖的关键事实词")
    reference_answer: str | None = Field(default=None, max_length=4000, description="参考答案，供裁判对比准确率")
    is_acl_redline: bool = Field(default=False, description="是否为越权红线拦截样本")
    forbidden_doc_ids: list[str] = Field(default_factory=list, description="红线样本：该身份不得召回的文档")
    forbidden_keywords: list[str] = Field(default_factory=list, description="红线样本：回答中不得出现的敏感片段")
    tags: list[str] = Field(default_factory=list, description="自由标签，如 table / multi_hop / page_2")

    @model_validator(mode="after")
    def _check_semantics(self) -> EvalSample:
        if self.is_acl_redline:
            if self.expected_doc_ids:
                raise ValueError("红线样本的 expected_doc_ids 必须为空（越权文档请填 forbidden_doc_ids）")
            if not (self.forbidden_doc_ids or self.forbidden_keywords):
                raise ValueError("红线样本必须至少提供 forbidden_doc_ids 或 forbidden_keywords 之一")
        else:
            if self.forbidden_doc_ids or self.forbidden_keywords:
                raise ValueError("非红线样本不得设置 forbidden_doc_ids / forbidden_keywords")
            if not (self.expected_doc_ids or self.expected_keywords or self.reference_answer):
                raise ValueError("非红线样本必须至少提供 expected_doc_ids / expected_keywords / reference_answer 之一")
        return self


# ---------------------------------------------------------------------------
# 运行参数
# ---------------------------------------------------------------------------


class EvalRunConfig(BaseModel):
    """EvalRunner 运行参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EvalMode = EvalMode.RETRIEVAL_ONLY
    system_name: str = Field(..., min_length=1, max_length=128, description="被测系统名，如 hybrid_retrieval")
    system_version: str = Field(..., min_length=1, max_length=128, description="被测版本，如 git sha / 配置标签")
    top_k: int = Field(default=5, ge=1, le=50, description="检索指标截断位 k，同时作为 SearchRequest.top_k")
    enable_rerank: bool = Field(default=True, description="透传 SearchRequest.enable_rerank，用于基线对比实验")
    max_concurrency: int = Field(default=8, ge=1, le=256, description="asyncio.Semaphore 并发上限")
    sample_timeout_s: float = Field(default=30.0, gt=0, description="单样本端到端超时（含裁判）")
    judge_timeout_s: float = Field(default=20.0, gt=0, description="单次裁判调用超时")
    # 归因阈值
    ranking_mrr_threshold: float = Field(default=0.5, ge=0, le=1, description="命中但 mrr 低于此值归因 RANKING_LOW")
    faithfulness_threshold: float = Field(default=0.7, ge=0, le=1)
    accuracy_threshold: float = Field(default=0.6, ge=0, le=1, description="同时用作 answer_accuracy_rate 的通过线")
    keyword_coverage_threshold: float = Field(default=0.6, ge=0, le=1)
    satisfaction_threshold: float = Field(default=3.0, ge=1, le=5)


class GateConfig(BaseModel):
    """RegressionGate 门禁阈值。

    max_*_drop 为绝对百分点（0.02 = 2pp）；latency 为相对增幅。
    ACL 红线（候选 acl_block_rate == 1.0）不可配置、不可豁免。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_hit_rate_drop: float = Field(default=0.02, ge=0, le=1)
    max_recall_drop: float = Field(default=0.03, ge=0, le=1)
    max_mrr_drop: float = Field(default=0.03, ge=0, le=1)
    max_accuracy_drop: float = Field(default=0.03, ge=0, le=1)
    max_faithfulness_drop: float = Field(default=0.03, ge=0, le=1)
    max_satisfaction_drop: float = Field(default=0.2, ge=0, le=4, description="Likert 分绝对回退")
    min_faithfulness: float = Field(default=0.85, ge=0, le=1, description="候选 mean_faithfulness 绝对下限")
    min_hit_rate: float = Field(default=0.8, ge=0, le=1, description="候选 retrieval_hit_rate 绝对下限")
    max_p95_latency_increase: float = Field(default=0.2, ge=0, description="P95 相对增幅上限（0.2 = +20%）")
    max_p95_latency_ms: float | None = Field(default=None, gt=0, description="P95 绝对上限，None 不校验")
    max_degraded_rate: float = Field(default=0.05, ge=0, le=1)
    max_error_rate: float = Field(default=0.02, ge=0, le=1, description="SYSTEM_ERROR + TIMEOUT 占比上限")
    max_newly_failed: int = Field(default=0, ge=0, description="基线通过而候选失败的样本数上限")
    require_same_suite: bool = Field(default=True, description="样本集指纹不一致即阻断")
    top_bad_cases: int = Field(default=10, ge=0, le=100)


# ---------------------------------------------------------------------------
# 端口传输
# ---------------------------------------------------------------------------


class AgentInvocation(BaseModel):
    """业务智能体端到端调用请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(..., min_length=1, max_length=256, description="评测追踪 ID：{run_id}:{sample_id}")
    scenario: EvalScenario
    question: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext


class TokenUsage(BaseModel):
    """模型 token 消耗。"""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentResponse(BaseModel):
    """业务智能体端到端响应。"""

    answer: str
    retrieved_contexts: list[RetrievedParentChunk] = Field(
        default_factory=list, description="智能体实际使用的检索上下文（按排名），用于检索指标与忠实度"
    )
    cited_doc_ids: list[str] = Field(default_factory=list, description="回答中显式引用的文档")
    refused: bool = Field(default=False, description="智能体是否主动拒答（红线样本期望 True，但不作为拦截判据）")
    latency_ms: float = Field(..., ge=0)
    degraded: bool = False
    degraded_reasons: list[DegradedReason] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class JudgeScore(BaseModel):
    """裁判模型单维度打分。"""

    model_config = ConfigDict(frozen=True)

    score: float = Field(..., description="faithfulness/accuracy 为 0~1；satisfaction 为 1~5")
    reason: str = Field(default="", max_length=2000, description="裁判给出的简要理由，进入坏例摘要")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


# ---------------------------------------------------------------------------
# 单样本结果
# ---------------------------------------------------------------------------


class RetrievalMetrics(BaseModel):
    """单样本确定性检索指标（文档粒度，按首次出现名次去重）。"""

    model_config = ConfigDict(frozen=True)

    retrieved_doc_ids: list[str] = Field(default_factory=list, description="去重后的文档排名")
    hit_at_k: float | None = Field(default=None, ge=0, le=1, description="0 或 1；expected 为空时 None")
    recall_at_k: float | None = Field(default=None, ge=0, le=1)
    mrr: float | None = Field(default=None, ge=0, le=1)


class AclCheckOutcome(BaseModel):
    """红线越权检测结果。"""

    model_config = ConfigDict(frozen=True)

    leaked: bool
    leaked_doc_ids: list[str] = Field(default_factory=list)
    leaked_keywords: list[str] = Field(default_factory=list)


class SampleEvalResult(BaseModel):
    """单样本评测结果。

    errored（SYSTEM_ERROR / TIMEOUT）样本：检索指标按 0 计入汇总（fail-closed），
    latency_ms 为 None 且不参与分位数。
    """

    sample_id: str
    scenario: EvalScenario
    dept_id: str = Field(..., description="冗余自 user_context.dept_id，用于分部门切片")
    question: str
    is_acl_redline: bool
    # 检索指标
    retrieved_doc_ids: list[str] = Field(default_factory=list)
    hit_at_k: float | None = Field(default=None, ge=0, le=1)
    recall_at_k: float | None = Field(default=None, ge=0, le=1)
    mrr: float | None = Field(default=None, ge=0, le=1)
    # 红线
    acl_leaked: bool = False
    leaked_doc_ids: list[str] = Field(default_factory=list)
    # 端到端指标（RETRIEVAL_ONLY 模式或红线样本下为 None）
    answer: str | None = None
    keyword_coverage: float | None = Field(default=None, ge=0, le=1)
    faithfulness_score: float | None = Field(default=None, ge=0, le=1)
    answer_accuracy_score: float | None = Field(default=None, ge=0, le=1)
    satisfaction_score: float | None = Field(default=None, ge=1, le=5)
    judge_reasons: dict[str, str] = Field(default_factory=dict, description="faithfulness / accuracy / satisfaction -> 理由")
    # 运行指标
    latency_ms: float | None = Field(default=None, ge=0, description="被测系统耗时（不含裁判）")
    degraded: bool = False
    degraded_reasons: list[DegradedReason] = Field(default_factory=list)
    token_cost: int = Field(default=0, ge=0, description="被测系统 token 总消耗")
    judge_token_cost: int = Field(default=0, ge=0, description="裁判模型 token 总消耗（不计入被测系统成本）")
    # 归因
    failure_category: FailureCategory = FailureCategory.NONE
    error_message: str | None = Field(default=None, max_length=2000, description="异常摘要（不含堆栈）")

    @property
    def errored(self) -> bool:
        return self.failure_category in (FailureCategory.SYSTEM_ERROR, FailureCategory.TIMEOUT)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


class SliceMetrics(BaseModel):
    """分场景 / 分部门切片指标（字段口径与 SuiteSummaryMetrics 同名字段一致）。"""

    slice_key: str = Field(..., description="EvalScenario.value 或 dept_id")
    slice_label: str = Field(..., description="展示名")
    total_samples: int = Field(..., ge=0)
    failed_samples: int = Field(..., ge=0)
    retrieval_hit_rate: float | None = None
    mean_recall_at_k: float | None = None
    answer_accuracy_rate: float | None = None
    mean_satisfaction: float | None = None
    latency_p95_ms: float | None = None
    acl_redline_total: int = 0
    acl_redline_blocked: int = 0


class SuiteSummaryMetrics(BaseModel):
    """评测集汇总指标。"""

    total_samples: int = Field(..., ge=0)
    failed_samples: int = Field(..., ge=0, description="failure_category.is_failure 的样本数")
    error_samples: int = Field(..., ge=0, description="SYSTEM_ERROR + TIMEOUT 样本数")
    error_rate: float | None = None
    # 检索
    retrieval_hit_rate: float | None = Field(default=None, description="非红线且有 expected_doc_ids 样本的 hit@k 均值")
    mean_recall_at_k: float | None = None
    mean_mrr: float | None = None
    # 端到端
    answer_accuracy_rate: float | None = Field(default=None, description="answer_accuracy_score >= 阈值的样本占比")
    mean_answer_accuracy: float | None = None
    mean_faithfulness: float | None = None
    mean_keyword_coverage: float | None = None
    mean_satisfaction: float | None = Field(default=None, description="1~5")
    # 运行
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    degraded_rate: float | None = None
    total_token_cost: int = Field(default=0, ge=0)
    total_judge_token_cost: int = Field(default=0, ge=0)
    # 红线
    acl_redline_total: int = Field(default=0, ge=0)
    acl_redline_blocked: int = Field(default=0, ge=0, description="未泄露且未报错的红线样本数")
    acl_block_rate: float | None = None
    acl_redline_passed: bool = Field(
        default=False, description="acl_redline_total > 0 且 acl_redline_blocked == acl_redline_total"
    )
    # 分布与切片
    failure_distribution: dict[FailureCategory, int] = Field(default_factory=dict)
    by_scenario: list[SliceMetrics] = Field(default_factory=list)
    by_dept: list[SliceMetrics] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_acl_redline(self) -> SuiteSummaryMetrics:
        """越权拦截率 100% 校验：acl_redline_passed 必须与计数严格一致，不允许手工置 True。"""
        if self.acl_redline_blocked > self.acl_redline_total:
            raise ValueError("acl_redline_blocked 不得大于 acl_redline_total")
        expected = self.acl_redline_total > 0 and self.acl_redline_blocked == self.acl_redline_total
        if self.acl_redline_passed != expected:
            raise ValueError("acl_redline_passed 与红线计数不一致（无红线样本时必须为 False）")
        return self


class EvalRunRecord(BaseModel):
    """一次完整评测运行。RegressionGate 与 MarkdownReportGenerator 的唯一输入。"""

    run_id: str = Field(..., min_length=1, max_length=128)
    suite_name: str = Field(..., min_length=1, max_length=128)
    suite_fingerprint: str = Field(..., description="样本集指纹（sha256），门禁据此判定两次运行可比")
    config: EvalRunConfig
    started_at: datetime
    finished_at: datetime
    results: list[SampleEvalResult] = Field(default_factory=list)
    summary: SuiteSummaryMetrics


# ---------------------------------------------------------------------------
# 回归门禁
# ---------------------------------------------------------------------------


class MetricDelta(BaseModel):
    """单指标基线对比。"""

    metric: str = Field(..., description="SuiteSummaryMetrics 字段名")
    baseline: float | None
    candidate: float | None
    abs_delta: float | None = Field(default=None, description="candidate - baseline")
    rel_delta_pct: float | None = Field(default=None, description="(candidate - baseline) / |baseline| * 100；baseline 为 0 时 None")
    higher_is_better: bool
    tolerance: float | None = Field(default=None, description="该指标的允许回退量（与 GateConfig 对应）")
    regressed: bool = Field(default=False, description="回退超过 tolerance")


class GateViolation(BaseModel):
    """单条门禁阻断原因。"""

    code: GateViolationCode
    metric: str | None = None
    message: str = Field(..., description="人类可读阻断说明，进入报告与 CI 日志")


class BadCaseSummary(BaseModel):
    """Top Bad Case 摘要。"""

    sample_id: str
    scenario: EvalScenario
    question: str
    failure_category: FailureCategory
    baseline_category: FailureCategory | None = Field(default=None, description="基线中该样本的归因；基线缺失为 None")
    newly_failed: bool = Field(default=False, description="基线通过、候选失败")
    severity: int = Field(..., ge=0, description="排序权重，越大越靠前")
    detail: str = Field(default="", max_length=1000, description="指标与裁判理由摘要")


class RegressionDiffReport(BaseModel):
    """回归基线对比与上线门禁结果。"""

    baseline_run_id: str
    candidate_run_id: str
    baseline_version: str
    candidate_version: str
    suite_comparable: bool = Field(..., description="两次运行 suite_fingerprint 是否一致")
    metric_deltas: list[MetricDelta] = Field(default_factory=list)
    gate_passed: bool
    violations: list[GateViolation] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list, description="violations[*].message，便于 CI 直接打印")
    newly_failed_sample_ids: list[str] = Field(default_factory=list)
    fixed_sample_ids: list[str] = Field(default_factory=list, description="基线失败、候选通过")
    top_bad_cases: list[BadCaseSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_gate_consistency(self) -> RegressionDiffReport:
        if self.gate_passed == bool(self.violations):
            raise ValueError("gate_passed 必须等价于 violations 为空")
        if self.blocking_reasons != [v.message for v in self.violations]:
            raise ValueError("blocking_reasons 必须与 violations[*].message 一一对应")
        return self
