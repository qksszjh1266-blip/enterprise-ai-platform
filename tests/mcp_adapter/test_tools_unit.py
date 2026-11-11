"""Tool 逻辑单元测试（直接调用，不经传输层）。"""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from agent_matrix.supply_chain_bi.semantic_layer import SUPPLY_CHAIN_CATALOG, SemanticLayer
from data_platform.retrieval_engine.errors import FilterTooBroadError, LedgerUnavailableError, RetrievalUnavailableError
from data_platform.retrieval_engine.models import DegradedReason
from mcp_server.tools import MAX_EVIDENCE_CHARS, list_supply_chain_metrics, search_enterprise_knowledge
from tests.mcp_adapter.conftest import STORE_CLERK, STORE_MANAGER, StubRetrieval, make_chunk


# ---------------------------------------------------------------------------
# search_enterprise_knowledge
# ---------------------------------------------------------------------------


async def test_search_narrows_user_context_to_single_requested_role():
    stub = StubRetrieval(results=[make_chunk()])
    await search_enterprise_knowledge(stub, STORE_MANAGER, query="热敏打单机", department="store_ops", role="clerk")
    (request,) = stub.requests
    assert request.user_context.dept_id == "store_ops"
    assert request.user_context.roles == ["clerk"]
    assert request.user_context.user_id == STORE_MANAGER.user_id


@pytest.mark.parametrize("department,role", [("finance", "clerk"), ("store_ops", "store_manager"), ("store_ops", "finance")])
async def test_out_of_scope_department_or_role_is_denied_before_retrieval(department, role):
    stub = StubRetrieval(results=[make_chunk()])
    with pytest.raises(ToolError, match="PERMISSION_DENIED"):
        await search_enterprise_knowledge(stub, STORE_CLERK, query="审批矩阵", department=department, role=role)
    assert stub.requests == []


async def test_evidence_is_compact_and_carries_section_and_source():
    stub = StubRetrieval(results=[make_chunk("sop_a", section="设备/热敏打单机", page=2, score=0.91234567),
                                  make_chunk("sop_b", section="商品", page=None)])
    result = await search_enterprise_knowledge(stub, STORE_CLERK, query="q", department="store_ops", role="clerk")
    assert result.total == 2
    first, second = result.evidence
    assert (first.rank, first.section_path, first.source.doc_id, first.source.page_number) == (1, "设备/热敏打单机", "sop_a", 2)
    assert first.score == 0.912346
    assert second.rank == 2 and second.source.page_number is None
    dumped = result.model_dump()
    assert "parent_id" not in json.dumps(dumped)
    assert "rrf_score" not in json.dumps(dumped)


async def test_long_evidence_is_truncated_and_flagged():
    stub = StubRetrieval(results=[make_chunk(content="长" * (MAX_EVIDENCE_CHARS + 10))])
    result = await search_enterprise_knowledge(stub, STORE_CLERK, query="q", department="store_ops", role="clerk")
    assert len(result.evidence[0].content) == MAX_EVIDENCE_CHARS
    assert result.evidence[0].truncated is True


async def test_degraded_reasons_are_exposed_as_codes():
    stub = StubRetrieval(results=[make_chunk()], degraded_reasons=[DegradedReason.RERANK_TIMEOUT])
    result = await search_enterprise_knowledge(stub, STORE_CLERK, query="q", department="store_ops", role="clerk")
    assert result.degraded is True
    assert result.degraded_reasons == ["rerank_timeout"]


async def test_top_k_is_forwarded():
    stub = StubRetrieval(results=[make_chunk(f"d{i}") for i in range(8)])
    result = await search_enterprise_knowledge(stub, STORE_CLERK, query="q", department="store_ops", role="clerk", top_k=3)
    assert stub.requests[0].top_k == 3
    assert result.total == 3


@pytest.mark.parametrize("error,code,retry", [
    (RetrievalUnavailableError("两路召回均失败"), "RETRIEVAL_UNAVAILABLE", "可稍后重试"),
    (LedgerUnavailableError("台账不可用"), "LEDGER_UNAVAILABLE", "可稍后重试"),
    (FilterTooBroadError("范围过大"), "FILTER_TOO_BROAD", "不可重试"),
])
async def test_retrieval_errors_become_self_correcting_tool_errors(error, code, retry):
    with pytest.raises(ToolError) as info:
        await search_enterprise_knowledge(StubRetrieval(error=error), STORE_CLERK, query="q",
                                          department="store_ops", role="clerk")
    assert f"[{code}]" in str(info.value)
    assert retry in str(info.value)


async def test_overlong_query_is_rejected_as_invalid_argument():
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await search_enterprise_knowledge(StubRetrieval(), STORE_CLERK, query="问" * 1001,
                                          department="store_ops", role="clerk")


# ---------------------------------------------------------------------------
# list_supply_chain_metrics
# ---------------------------------------------------------------------------


def test_lists_every_whitelisted_metric_in_catalog_order():
    result = list_supply_chain_metrics(SemanticLayer())
    assert [m.metric_id for m in result.metrics] == [m.metric_id for m in SUPPLY_CHAIN_CATALOG.metrics]
    assert result.total == len(SUPPLY_CHAIN_CATALOG.metrics)
    assert result.catalog_version == SUPPLY_CHAIN_CATALOG.catalog_version
    assert all(m.calculation_caliber for m in result.metrics)


def test_metric_output_never_contains_sql_or_sensitive_schema():
    text = list_supply_chain_metrics(SemanticLayer()).model_dump_json()
    for forbidden in ("formula_sql", "required_columns", "NULLIF(", "SUM(", "SELECT"):
        assert forbidden not in text
    for name in SUPPLY_CHAIN_CATALOG.global_sensitive_columns + SUPPLY_CHAIN_CATALOG.forbidden_tables:
        assert name not in text


@pytest.mark.parametrize("keyword,expected", [
    ("报损率", ["material_loss_rate"]),
    ("material_loss_rate", ["material_loss_rate"]),
    ("  安全库存缺口 ", ["safety_stock_gap"]),
    ("不存在的指标", []),
])
def test_keyword_filters_by_name_alias_or_id(keyword, expected):
    result = list_supply_chain_metrics(SemanticLayer(), keyword=keyword)
    assert [m.metric_id for m in result.metrics] == expected
    assert result.keyword == keyword.strip()


def test_blank_keyword_means_no_filter():
    assert list_supply_chain_metrics(SemanticLayer(), keyword="   ").total == len(SUPPLY_CHAIN_CATALOG.metrics)
