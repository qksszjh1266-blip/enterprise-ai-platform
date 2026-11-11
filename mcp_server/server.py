"""MCP Server 装配：基于官方 mcp SDK（MCPServer）注册 Tool 与 Prompt。

    Tool   search_enterprise_knowledge   ACL 前置过滤的企业知识库混合检索（只读）
    Tool   list_supply_chain_metrics     供应链语义层白名单指标定义与口径（只读，不执行 SQL）
    Prompt sop_citation_qa_prompt        门店 SOP 带引用防幻觉问答模板

所有依赖（检索服务、可信身份、语义层）均由调用方注入，create_server 本身无 I/O，
单测可用 mcp.Client(server) 进程内直连，stdio 入口见 mcp_server.__main__。
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer, UserMessage
from mcp.types import ToolAnnotations
from pydantic import Field

from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer
from data_platform.retrieval_engine.service import HybridRetrievalService
from mcp_server import prompts, tools
from mcp_server.identity import IDENTIFIER_PATTERN, CallerPrincipal

SERVER_NAME = "enterprise-ai-platform"
SERVER_VERSION = "0.1.0"

SERVER_INSTRUCTIONS = """\
企业知识与只读业务能力服务。
- 回答门店 / 部门制度类问题前，先调用 search_enterprise_knowledge 获取证据；department 与 role 必须填写当前登录用户自己的部门与角色。
- 拿到证据后可使用 sop_citation_qa_prompt 渲染带 [来源n] 引用的问答消息；无证据时不得作答。
- 供应链指标口径问题调用 list_supply_chain_metrics；本服务不执行任何 SQL。"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

SEARCH_DESCRIPTION = (
    "在调用方有权限的企业知识库中做混合检索（向量 + BM25 + 精排），返回带章节路径与文档来源的证据列表。"
    "department 与 role 必填，且必须属于当前调用方的授权范围，否则返回 PERMISSION_DENIED；"
    "检索内部强制执行部门与角色 ACL 前置过滤。degraded=true 表示证据可能不完整。"
)

METRICS_DESCRIPTION = (
    "只读列出供应链语义层白名单标准指标的定义、计算口径、单位与推荐维度；可按指标名或同义词过滤。"
    "不返回 SQL，也不执行任何查询。"
)

PROMPT_DESCRIPTION = (
    "门店 SOP 带引用防幻觉问答模板：传入 search_enterprise_knowledge 的输出（或证据数组）与用户问题，"
    "渲染为要求逐句标注 [来源n]、资料不足即拒答的结构化消息。"
)

Department = Annotated[str, Field(pattern=IDENTIFIER_PATTERN, description="调用用户所属部门 ID，如 store_ops")]
Role = Annotated[str, Field(pattern=IDENTIFIER_PATTERN, description="调用用户的角色 ID（单个），如 clerk")]


def create_server(
    *,
    retrieval: HybridRetrievalService,
    principal: CallerPrincipal,
    semantic_layer: SemanticLayer | None = None,
) -> MCPServer:
    """构造 MCPServer。principal 为宿主注入的可信身份，工具入参只能在其范围内收窄。"""
    layer = semantic_layer or SemanticLayer()
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION, instructions=SERVER_INSTRUCTIONS)

    @server.tool(name="search_enterprise_knowledge", title="企业知识库检索",
                 description=SEARCH_DESCRIPTION, annotations=READ_ONLY)
    async def search_enterprise_knowledge(
        query: Annotated[str, Field(min_length=1, max_length=1000, description="自然语言检索问题")],
        department: Department,
        role: Role,
        top_k: Annotated[int, Field(ge=1, le=tools.MAX_TOP_K, description="返回证据条数")] = 5,
    ) -> tools.KnowledgeSearchResult:
        return await tools.search_enterprise_knowledge(
            retrieval, principal, query=query, department=department, role=role, top_k=top_k)

    @server.tool(name="list_supply_chain_metrics", title="供应链标准指标目录",
                 description=METRICS_DESCRIPTION, annotations=READ_ONLY)
    def list_supply_chain_metrics(
        keyword: Annotated[str | None, Field(max_length=64, description="可选：指标名、同义词或 metric_id")] = None,
    ) -> tools.SupplyChainMetricCatalog:
        return tools.list_supply_chain_metrics(layer, keyword=keyword)

    @server.prompt(name="sop_citation_qa_prompt", title="门店 SOP 引用问答", description=PROMPT_DESCRIPTION)
    def sop_citation_qa_prompt(
        question: Annotated[str, Field(description="用户问题")],
        evidence: Annotated[str, Field(description="JSON：search_enterprise_knowledge 的输出或证据数组")],
        scenario: Annotated[str, Field(description="store_ops_general / drink_sop / equipment_fault")] = "store_ops_general",
        degraded: Annotated[str, Field(description="true 表示检索降级，需输出风险提示")] = "false",
    ) -> list[UserMessage]:
        return prompts.render_sop_citation_qa(question=question, evidence=evidence, scenario=scenario, degraded=degraded)

    return server
