"""MCP 协议契约测试：mcp.Client 进程内直连 create_server，检索走真实发布的演示知识库。"""

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from agent_matrix.supply_chain_bi.semantic_layer import SUPPLY_CHAIN_CATALOG
from mcp_server.server import SERVER_NAME, SERVER_VERSION, create_server
from mcp_server.tools import KnowledgeSearchResult
from tests.mcp_adapter.conftest import FINANCE_STAFF, STORE_CLERK, STORE_MANAGER

pytestmark = pytest.mark.asyncio(loop_scope="module")

SEARCH = "search_enterprise_knowledge"
METRICS = "list_supply_chain_metrics"
PROMPT = "sop_citation_qa_prompt"

CLERK_VISIBLE = {"sop_sku_spec", "sop_equipment", "sop_scanner", "fin_expense_policy"}


def _client(kb, principal=STORE_CLERK) -> Client:
    return Client(create_server(retrieval=kb.retrieval, principal=principal))


async def _search(client, **arguments):
    return await client.call_tool(SEARCH, arguments)


def _error_text(result) -> str:
    assert result.is_error is True
    return result.content[0].text


def _rejected_fields(text: str) -> set[str]:
    """校验错误中报错字段所在行（错误文本还会回显完整入参，不能用子串判断）。"""
    lines = text.splitlines()
    return {lines[i].strip() for i in range(1, len(lines)) if lines[i] and not lines[i].startswith(" ")}


# ---------------------------------------------------------------------------
# 发现：server info / tools / prompts
# ---------------------------------------------------------------------------


async def test_server_identity_and_capabilities(demo_kb):
    async with _client(demo_kb) as client:
        assert client.server_info.name == SERVER_NAME
        assert client.server_info.version == SERVER_VERSION
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.prompts is not None
        assert "search_enterprise_knowledge" in client.instructions


async def test_tool_discovery_contract(demo_kb):
    async with _client(demo_kb) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {SEARCH, METRICS}
    search = tools[SEARCH].input_schema
    assert set(search["required"]) == {"query", "department", "role"}
    assert set(search["properties"]) == {"query", "department", "role", "top_k"}
    assert search["properties"]["department"]["pattern"]
    assert search["properties"]["top_k"]["maximum"] == 20
    assert "required" not in tools[METRICS].input_schema or not tools[METRICS].input_schema["required"]
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema is not None
    assert "evidence" in tools[SEARCH].output_schema["properties"]
    assert "metrics" in tools[METRICS].output_schema["properties"]


async def test_prompt_discovery_contract(demo_kb):
    async with _client(demo_kb) as client:
        prompts = (await client.list_prompts()).prompts
    assert [p.name for p in prompts] == [PROMPT]
    required = {a.name: a.required for a in prompts[0].arguments}
    assert required == {"question": True, "evidence": True, "scenario": False, "degraded": False}


# ---------------------------------------------------------------------------
# 正常带权限检索
# ---------------------------------------------------------------------------


async def test_clerk_search_returns_structured_evidence(demo_kb):
    async with _client(demo_kb) as client:
        result = await _search(client, query="热敏打单机不出纸怎么办", department="store_ops", role="clerk", top_k=3)
    assert result.is_error is False
    payload = KnowledgeSearchResult.model_validate(result.structured_content)
    assert 1 <= payload.total <= 3
    assert [e.rank for e in payload.evidence] == list(range(1, payload.total + 1))
    assert all(e.section_path and e.content and e.source.doc_id and e.source.version >= 1 for e in payload.evidence)
    assert {e.source.doc_id for e in payload.evidence} <= CLERK_VISIBLE
    assert json.loads(result.content[0].text) == result.structured_content


async def test_acl_hides_manager_only_document_from_clerk(demo_kb):
    query = {"query": "审批矩阵与数据可见范围", "department": "store_ops", "top_k": 10}
    async with _client(demo_kb) as clerk:
        clerk_docs = {e["source"]["doc_id"] for e in (await _search(clerk, role="clerk", **query)).structured_content["evidence"]}
    async with _client(demo_kb, STORE_MANAGER) as manager:
        manager_docs = {e["source"]["doc_id"] for e in
                        (await _search(manager, role="store_manager", **query)).structured_content["evidence"]}
    assert "fin_approval_matrix" not in clerk_docs
    assert clerk_docs <= CLERK_VISIBLE
    assert "fin_approval_matrix" in manager_docs


async def test_department_filter_isolates_other_departments(demo_kb):
    async with _client(demo_kb, FINANCE_STAFF) as client:
        result = await _search(client, query="报销 审批矩阵 付款", department="finance", role="finance", top_k=20)
    docs = {e["source"]["doc_id"] for e in result.structured_content["evidence"]}
    assert docs
    assert all(d.startswith(("finance_", "contract_")) for d in docs)
    assert not {"finance_private", "contract_private"} & docs


# ---------------------------------------------------------------------------
# 缺省 / 越权 / 无效入参
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["query", "department", "role"])
async def test_missing_required_argument_is_rejected(demo_kb, missing):
    arguments = {"query": "热敏打单机", "department": "store_ops", "role": "clerk"}
    arguments.pop(missing)
    async with _client(demo_kb) as client:
        text = _error_text(await client.call_tool(SEARCH, arguments))
    assert _rejected_fields(text) == {missing}
    assert "Field required" in text


@pytest.mark.parametrize("department,role", [
    ("finance", "clerk"),        # 越权部门
    ("supply_chain", "warehouse"),
    ("store_ops", "store_manager"),  # 越权角色
    ("store_ops", "finance_manager"),
])
async def test_out_of_scope_department_or_role_is_denied(demo_kb, department, role):
    async with _client(demo_kb) as client:
        text = _error_text(await _search(client, query="审批矩阵", department=department, role=role))
    assert "PERMISSION_DENIED" in text
    assert "finance" not in text.split("|")[0]


@pytest.mark.parametrize("arguments,field", [
    ({"query": ""}, "query"),
    ({"query": "问" * 1001}, "query"),
    ({"query": 123}, "query"),
    ({"department": ""}, "department"),
    ({"department": "store_ops' OR '1'='1"}, "department"),
    ({"role": "Clerk"}, "role"),
    ({"role": ["clerk", "store_manager"]}, "role"),
    ({"top_k": 0}, "top_k"),
    ({"top_k": 21}, "top_k"),
])
async def test_invalid_arguments_are_rejected(demo_kb, arguments, field):
    base = {"query": "热敏打单机", "department": "store_ops", "role": "clerk"}
    async with _client(demo_kb) as client:
        text = _error_text(await client.call_tool(SEARCH, base | arguments))
    assert _rejected_fields(text) == {field}


async def test_unknown_tool_is_reported_as_error(demo_kb):
    async with _client(demo_kb) as client:
        result = await client.call_tool("execute_sql", {"sql": "SELECT 1"})
    assert result.is_error is True


# ---------------------------------------------------------------------------
# list_supply_chain_metrics
# ---------------------------------------------------------------------------


async def test_metrics_tool_lists_whitelist_without_sql(demo_kb):
    async with _client(demo_kb) as client:
        result = await client.call_tool(METRICS, {})
        filtered = await client.call_tool(METRICS, {"keyword": "损耗率"})
    assert result.is_error is False
    assert result.structured_content["total"] == len(SUPPLY_CHAIN_CATALOG.metrics)
    assert "formula_sql" not in result.content[0].text
    assert [m["metric_id"] for m in filtered.structured_content["metrics"]] == ["material_loss_rate"]


async def test_metrics_keyword_length_is_bounded(demo_kb):
    async with _client(demo_kb) as client:
        assert (await client.call_tool(METRICS, {"keyword": "x" * 65})).is_error is True


# ---------------------------------------------------------------------------
# Prompt 渲染
# ---------------------------------------------------------------------------


async def test_search_output_feeds_citation_prompt_end_to_end(demo_kb):
    async with _client(demo_kb) as client:
        search = await _search(client, query="扫码枪扫码不灵", department="store_ops", role="clerk", top_k=3)
        prompt = await client.get_prompt(PROMPT, {
            "question": "扫码枪扫码不灵怎么处理？", "scenario": "equipment_fault",
            "evidence": json.dumps(search.structured_content, ensure_ascii=False)})
    assert [m.role for m in prompt.messages] == ["user", "user"]
    rules, context = (m.content.text for m in prompt.messages)
    assert "【引用溯源铁律】" in rules and "【安全优先】" in rules
    assert f"1 至 {search.structured_content['total']}" in rules
    first = search.structured_content["evidence"][0]
    assert f"[来源1] 章节：{first['section_path']}" in context
    assert context.rstrip().endswith("用户问题：扫码枪扫码不灵怎么处理？")
    assert first["source"]["doc_id"] not in context


async def test_prompt_rejects_empty_evidence_with_invalid_params(demo_kb):
    async with _client(demo_kb) as client:
        with pytest.raises(MCPError) as info:
            await client.get_prompt(PROMPT, {"question": "怎么做？", "evidence": "[]"})
    assert info.value.code == INVALID_PARAMS
    assert "evidence" in info.value.message


async def test_prompt_rejects_missing_required_argument(demo_kb):
    async with _client(demo_kb) as client:
        with pytest.raises(MCPError):
            await client.get_prompt(PROMPT, {"question": "怎么做？"})


async def test_unknown_prompt_is_rejected(demo_kb):
    async with _client(demo_kb) as client:
        with pytest.raises(MCPError):
            await client.get_prompt("free_form_prompt", {})
