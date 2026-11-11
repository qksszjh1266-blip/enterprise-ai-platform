"""检索读链路数据模型与传输契约。

本模块仅包含 Pydantic V2 模型，不含任何 I/O：
- UserContext            调用方身份（强制前置 ACL 过滤的唯一依据）
- SearchRequest          检索请求
- RetrievalFilter        由台账推导出的 ACL + 有效版本过滤条件（下推至 Milvus / ES）
- RetrievalCandidate     单路召回候选（子块粒度）
- RetrievedParentChunk   回表后的父块结果（对外输出粒度）
- SearchResponse         检索响应（含降级标记与耗时统计，供 eval_platform 消费）
- RetrievalConfig        HybridRetrievalService 运行参数
- ToolErrorResponse      暴露给 Agent 的结构化自纠错错误体

版本语义沿用 知识发布模块：检索侧只读 version == kb_documents.current_version，
且 current_version >= 1（0 表示从未发布）。

兼容性要求：公共类名与模型字段保持稳定，变更需同步调用方与契约测试。
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field

from data_platform.storage_sync.models import DocumentStatus

# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------

SERVABLE_STATUSES: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.ACTIVE, DocumentStatus.SYNCING, DocumentStatus.FAILED}
)
"""可对外服务 current_version 的台账状态。

依据 知识发布模块 DocumentStatus 语义：SYNCING（正在写影子版本）与 FAILED（影子版本已回滚）
期间 current_version 仍完整可用，必须继续服务，否则文档会在每次重新同步时"闪断"。
PENDING（current_version == 0，从未发布）与 DELETED（逻辑下线）必须过滤。
影子版本（current_version + 1）在任何状态下都不可见。
"""


class RetrievalBranch(str, enum.Enum):
    """召回分支。"""

    VECTOR = "vector"  # Milvus 稠密向量召回
    KEYWORD = "keyword"  # Elasticsearch BM25 召回


class DegradedReason(str, enum.Enum):
    """降级原因码（机器可读；SearchResponse.degraded_reason 为其人类可读拼接）。"""

    EMBEDDING_FAILED = "embedding_failed"  # 查询向量化失败 -> 仅 BM25
    VECTOR_TIMEOUT = "vector_timeout"
    VECTOR_ERROR = "vector_error"
    KEYWORD_TIMEOUT = "keyword_timeout"
    KEYWORD_ERROR = "keyword_error"
    RERANK_TIMEOUT = "rerank_timeout"  # 精排超时 -> 按 RRF 分数截断
    RERANK_ERROR = "rerank_error"
    PARENT_MISSING = "parent_missing"  # 回表时部分父块已被 GC（版本切换竞态），已丢弃


# ---------------------------------------------------------------------------
# 请求侧
# ---------------------------------------------------------------------------


class UserContext(BaseModel):
    """调用方身份上下文。

    ACL 规则（前置过滤，召回前下推到索引引擎）：
        可见 ⇔ chunk.dept_id == user.dept_id  AND  (chunk.allowed_roles ∩ user.roles) ≠ ∅

    allowed_roles 为空的文档对任何人不可见（fail-closed）。
    生产环境中 UserContext 必须由可信网关 / JWT 注入，不得信任 LLM 生成的参数。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(..., min_length=1, max_length=128, description="调用用户 ID（审计与评测追踪）")
    dept_id: str = Field(..., min_length=1, max_length=64, description="所属部门，ACL 标量过滤字段")
    roles: list[str] = Field(..., min_length=1, description="用户角色列表，至少 1 个；与 allowed_roles 求交集")


class SearchRequest(BaseModel):
    """知识检索请求。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(..., min_length=1, max_length=1000, description="自然语言检索问题")
    user_context: UserContext
    top_k: int = Field(default=5, ge=1, le=50, description="最终返回父块数（精排截断后）")
    enable_rerank: bool = Field(default=True, description="False 时跳过交叉编码器，直接按 RRF 截断（不视为降级）")


class RetrievalFilter(BaseModel):
    """下推至 Milvus / ES 的前置过滤条件（由 HybridRetrievalService 从台账构造，禁止调用方直传）。

    适配器必须将三项条件全部翻译为引擎内 pre-filter：
        dept_id == {dept_id}
        AND allowed_roles 包含 roles 中任一
        AND (doc_id, version) ∈ doc_versions
    doc_versions 为空时适配器必须返回空结果（严禁退化为"无过滤"）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dept_id: str = Field(..., min_length=1, max_length=64)
    roles: list[str] = Field(..., min_length=1)
    doc_versions: dict[str, int] = Field(
        default_factory=dict,
        description="doc_id -> current_version（仅含 SERVABLE_STATUSES 且 current_version >= 1 且 ACL 命中的文档）",
    )


# ---------------------------------------------------------------------------
# 召回 / 输出侧
# ---------------------------------------------------------------------------


class RetrievalCandidate(BaseModel):
    """单路召回候选（子块粒度）。由 VectorSearchPort / KeywordSearchPort 返回。"""

    model_config = ConfigDict(frozen=True)

    child_id: str = Field(..., max_length=224)
    parent_id: str = Field(..., max_length=192)
    doc_id: str = Field(..., max_length=128)
    version: int = Field(..., ge=1)
    branch: RetrievalBranch
    raw_score: float = Field(..., description="引擎原始分（向量相似度 / BM25），仅用于调试，不参与 RRF")
    rank: int = Field(..., ge=1, description="该分支内的名次，从 1 开始")


class FusedCandidate(BaseModel):
    """RRF 融合并按 parent_id 去重后的中间结果（尚未回表）。"""

    model_config = ConfigDict(frozen=True)

    parent_id: str = Field(..., max_length=192)
    doc_id: str = Field(..., max_length=128)
    version: int = Field(..., ge=1)
    rrf_score: float = Field(..., ge=0)
    source_branches: list[RetrievalBranch] = Field(..., min_length=1)


class RetrievedParentChunk(BaseModel):
    """对外输出的父块结果（回表 kb_parent_chunks 后）。"""

    model_config = ConfigDict(frozen=True)

    parent_id: str = Field(..., max_length=192)
    doc_id: str = Field(..., max_length=128)
    version: int = Field(..., ge=1)
    section_path: str = Field(default="", description="标题层级路径，用于引用溯源")
    full_content: str
    page_number: int | None = Field(default=None, ge=1)
    score: float = Field(..., description="最终排序分：精排生效时为 rerank_score，否则为 rrf_score")
    rrf_score: float = Field(..., ge=0)
    rerank_score: float | None = None
    source_branches: list[RetrievalBranch] = Field(..., min_length=1, description="命中该父块的召回分支")


class SearchResponse(BaseModel):
    """检索响应。degraded=True 时结果仍可用，但召回或精排不完整。"""

    query: str
    results: list[RetrievedParentChunk] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = Field(default=None, description="人类可读降级说明；degraded=False 时为 None")
    degraded_reasons: list[DegradedReason] = Field(default_factory=list, description="机器可读降级原因码")
    latency_ms: float = Field(..., ge=0, description="端到端耗时（毫秒）")
    stage_latency_ms: dict[str, float] = Field(
        default_factory=dict,
        description="分阶段耗时：filter / embedding / vector / keyword / fusion / fetch / rerank",
    )
    candidate_counts: dict[str, int] = Field(
        default_factory=dict, description="各分支召回条数与融合后父块数：vector / keyword / fused"
    )


class RetrievalConfig(BaseModel):
    """HybridRetrievalService 运行参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vector_top_k: int = Field(default=50, ge=1, le=500, description="向量分支召回子块数")
    keyword_top_k: int = Field(default=50, ge=1, le=500, description="BM25 分支召回子块数")
    rrf_k: int = Field(default=60, ge=1, description="RRF 平滑常数 k：score = Σ 1/(k + rank)")
    rerank_candidates: int = Field(default=20, ge=1, le=200, description="送入精排的父块上限")
    embedding_timeout_s: float = Field(default=2.0, gt=0)
    branch_timeout_s: float = Field(default=1.5, gt=0, description="单路召回超时，超时即降级")
    rerank_timeout_s: float = Field(default=2.0, gt=0)
    max_filter_docs: int = Field(default=10_000, ge=1, description="doc_versions 上限，超出抛 FilterTooBroadError")


# ---------------------------------------------------------------------------
# 错误响应（FastAPI / MCP 工具层）
# ---------------------------------------------------------------------------


class ToolErrorResponse(BaseModel):
    """暴露给 Agent 的结构化错误体（禁止裸抛 500）。

    hint 必须可直接回灌给 LLM 用于自纠错（说明哪里错了、应如何修改参数或是否可重试）。
    """

    error_code: str = Field(..., description="稳定错误码，如 INVALID_ARGUMENT / RETRIEVAL_UNAVAILABLE")
    message: str
    hint: str = Field(..., description="面向 Agent 的修正建议")
    retryable: bool = False
    details: list[dict[str, str]] = Field(default_factory=list, description="字段级校验错误：loc / msg")
