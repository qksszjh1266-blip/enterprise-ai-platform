"""MCP Tool 实现（与传输层解耦，便于单测直接调用）。

search_enterprise_knowledge
    薄封装 HybridRetrievalService.search。权限链路（缺一不可）：
      ① 入参 department / role 必填且格式合法（Schema 层校验）；
      ② 入参必须落在宿主注入的 CallerPrincipal 范围内，否则 PERMISSION_DENIED，且不触达检索服务；
      ③ 以 UserContext(dept_id=department, roles=[role]) 调用原有检索服务，由其执行台账 ACL 前置过滤。
    输出精简为证据列表：排名、章节路径、文档来源（doc_id / version / 页码）、正文（超长截断）。

list_supply_chain_metrics
    只读返回供应链语义层白名单指标的定义与计算口径。数据来自 SemanticLayer.public_catalog()（脱敏副本），
    且不输出 formula_sql / required_columns：本工具不提供任何 SQL 片段或执行入口。
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer
from data_platform.retrieval_engine.errors import RetrievalError
from data_platform.retrieval_engine.models import SearchRequest, UserContext
from data_platform.retrieval_engine.service import HybridRetrievalService
from mcp_server.identity import CallerPrincipal

MAX_EVIDENCE_CHARS = 1500
"""单条证据正文上限；超出截断并标记 truncated=True，避免单次工具结果挤占模型上下文。"""

MAX_TOP_K = 20

PERMISSION_DENIED_HINT = (
    "[PERMISSION_DENIED] 请求的部门或角色超出当前调用方的授权范围 | "
    "建议: 使用当前登录身份所属的部门与角色重试；如确需跨部门资料，请通过 OA 向对应部门申请，不要更换参数反复尝试。"
)


# ---------------------------------------------------------------------------
# 输出模型
# ---------------------------------------------------------------------------


class EvidenceSource(BaseModel):
    """证据的文档来源（用于引用溯源与审计）。"""

    doc_id: str
    version: int
    page_number: int | None = None


class KnowledgeEvidence(BaseModel):
    """单条检索证据（父块粒度）。"""

    rank: int = Field(..., ge=1, description="排名，从 1 开始；可直接作为 [来源n] 的编号")
    section_path: str = Field(..., description="标题层级路径")
    content: str
    truncated: bool = False
    score: float
    source: EvidenceSource


class KnowledgeSearchResult(BaseModel):
    """search_enterprise_knowledge 的结构化输出。"""

    query: str
    department: str
    role: str
    total: int
    degraded: bool = Field(..., description="true 表示部分召回或精排不可用，证据可能不完整")
    degraded_reasons: list[str] = Field(default_factory=list)
    evidence: list[KnowledgeEvidence] = Field(default_factory=list)


class MetricDefinitionView(BaseModel):
    """对外暴露的指标定义（不含任何 SQL）。"""

    metric_id: str
    display_name: str
    aliases: list[str]
    calculation_caliber: str = Field(..., description="标准计算口径说明")
    unit: str
    decimal_places: int
    source_tables: list[str]
    dimensions: list[str]
    default_lookback_days: int
    higher_is_better: bool | None
    owner_dept: str


class SupplyChainMetricCatalog(BaseModel):
    """list_supply_chain_metrics 的结构化输出。"""

    catalog_version: int
    dialect: str
    keyword: str | None = None
    total: int
    metrics: list[MetricDefinitionView]
    usage_note: str


METRIC_USAGE_NOTE = (
    "以上为供应链语义层白名单标准指标的只读定义与计算口径；本服务不执行 SQL。"
    "如需取数，请通过供应链 BI 智能体在授权的仓库 / 大区范围内查询。"
)


# ---------------------------------------------------------------------------
# Tool 逻辑
# ---------------------------------------------------------------------------


async def search_enterprise_knowledge(
    retrieval: HybridRetrievalService,
    principal: CallerPrincipal,
    *,
    query: str,
    department: str,
    role: str,
    top_k: int = 5,
) -> KnowledgeSearchResult:
    """校验调用方授权后，走原有 ACL 前置过滤的混合检索，返回精简证据列表。"""
    if not principal.permits(department=department, role=role):
        raise ToolError(PERMISSION_DENIED_HINT)
    try:
        request = SearchRequest(
            query=query, top_k=top_k,
            user_context=UserContext(user_id=principal.user_id, dept_id=department, roles=[role]))
    except ValueError as exc:
        raise ToolError(f"[INVALID_ARGUMENT] 检索参数无效 | 建议: 检查 query 是否为空或过长 ({exc.__class__.__name__})") from exc
    try:
        response = await retrieval.search(request)
    except RetrievalError as exc:
        retry = "可稍后重试" if exc.retryable else "不可重试"
        raise ToolError(f"{exc.to_hint()} | retryable: {retry}") from exc
    evidence = []
    for rank, chunk in enumerate(response.results, 1):
        truncated = len(chunk.full_content) > MAX_EVIDENCE_CHARS
        evidence.append(KnowledgeEvidence(
            rank=rank, section_path=chunk.section_path,
            content=chunk.full_content[:MAX_EVIDENCE_CHARS], truncated=truncated, score=round(chunk.score, 6),
            source=EvidenceSource(doc_id=chunk.doc_id, version=chunk.version, page_number=chunk.page_number)))
    return KnowledgeSearchResult(
        query=response.query, department=department, role=role, total=len(evidence),
        degraded=response.degraded, degraded_reasons=[r.value for r in response.degraded_reasons], evidence=evidence)


def list_supply_chain_metrics(semantic_layer: SemanticLayer, *, keyword: str | None = None) -> SupplyChainMetricCatalog:
    """返回白名单指标定义；keyword 非空时按指标名 / 同义词做确定性匹配。"""
    catalog = semantic_layer.public_catalog()
    keyword = (keyword or "").strip() or None
    if keyword is None:
        metrics = list(catalog.metrics)
    else:
        matched = {m.metric_id for m in semantic_layer.match_metrics(keyword)}
        metrics = [m for m in catalog.metrics if m.metric_id in matched or m.metric_id == keyword]
    views = [MetricDefinitionView(
        metric_id=m.metric_id, display_name=m.display_name, aliases=list(m.aliases),
        calculation_caliber=m.calculation_caliber, unit=m.unit.value, decimal_places=m.decimal_places,
        source_tables=list(m.source_tables), dimensions=list(m.dimensions),
        default_lookback_days=m.default_lookback_days, higher_is_better=m.higher_is_better,
        owner_dept=m.owner_dept) for m in metrics]
    return SupplyChainMetricCatalog(
        catalog_version=catalog.catalog_version, dialect=catalog.dialect.value, keyword=keyword,
        total=len(views), metrics=views, usage_note=METRIC_USAGE_NOTE)
