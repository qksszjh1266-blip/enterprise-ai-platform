"""sop_citation_qa_prompt 渲染单元测试。"""

import json

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from agent_matrix.store_sop.prompts import (
    CROSS_DEPT_REFUSAL_POLICY, DRINK_SOP_RULES, GROUNDING_RULES, SAFETY_FIRST_RULES, render_degraded_notice,
)
from mcp_server.prompts import MAX_EVIDENCE_ITEMS, render_sop_citation_qa

EVIDENCE = [
    {"section_path": "饮品出品标准/鲜柠檬茶", "page_number": 1, "content": "杯型 500ml，茶汤 300ml。"},
    {"section_path": "饮品出品标准/过敏原", "content": "含乳饮品须提示过敏原。"},
]


def _render(**overrides):
    args = {"question": "鲜柠檬茶怎么做？", "evidence": json.dumps(EVIDENCE, ensure_ascii=False)} | overrides
    return render_sop_citation_qa(**args)


def test_renders_rules_then_numbered_evidence_and_question():
    rules, context = _render()
    assert rules.role == context.role == "user"
    assert GROUNDING_RULES in rules.content.text
    assert CROSS_DEPT_REFUSAL_POLICY in rules.content.text
    assert "1 至 2" in rules.content.text
    body = context.content.text
    assert "[来源1] 章节：饮品出品标准/鲜柠檬茶（第 1 页）\n杯型 500ml，茶汤 300ml。" in body
    assert "[来源2] 章节：饮品出品标准/过敏原\n含乳饮品须提示过敏原。" in body
    assert body.rstrip().endswith("用户问题：鲜柠檬茶怎么做？")


def test_scenario_selects_store_sop_rule_blocks():
    general = _render()[0].content.text
    drink = _render(scenario="drink_sop")[0].content.text
    fault = _render(scenario="equipment_fault")[0].content.text
    assert DRINK_SOP_RULES not in general and SAFETY_FIRST_RULES not in general
    assert DRINK_SOP_RULES in drink
    assert SAFETY_FIRST_RULES in fault


def test_accepts_full_search_tool_output_and_inherits_degraded_flag():
    payload = {"query": "q", "degraded": True, "evidence": [
        {"rank": 1, "section_path": "设备/制冰机", "content": "停机断电。", "source": {"doc_id": "d", "version": 1, "page_number": 2}}]}
    rules, context = _render(evidence=json.dumps(payload, ensure_ascii=False))
    assert render_degraded_notice([]) in rules.content.text
    assert "[来源1] 章节：设备/制冰机（第 2 页）" in context.content.text
    assert "doc_id" not in context.content.text


@pytest.mark.parametrize("flag,expected", [("true", True), ("1", True), ("false", False), ("", False)])
def test_degraded_argument_controls_risk_notice(flag, expected):
    assert (render_degraded_notice([]) in _render(degraded=flag)[0].content.text) is expected


@pytest.mark.parametrize("overrides,fragment", [
    ({"question": "   "}, "question"),
    ({"question": "问" * 1001}, "question"),
    ({"scenario": "finance_audit"}, "scenario"),
    ({"degraded": "maybe"}, "degraded"),
    ({"evidence": "not json"}, "JSON"),
    ({"evidence": "[]"}, "不能为空"),
    ({"evidence": json.dumps({"evidence": []})}, "不能为空"),
    ({"evidence": json.dumps(["纯文本"])}, "evidence[1]"),
    ({"evidence": json.dumps([{"section_path": "x"}])}, "content"),
    ({"evidence": json.dumps([{"content": "x", "page_number": 0}])}, "page_number"),
    ({"evidence": json.dumps([{"content": "x", "section_path": 3}])}, "section_path"),
    ({"evidence": json.dumps([{"content": "x"}] * (MAX_EVIDENCE_ITEMS + 1))}, "最多"),
])
def test_invalid_arguments_raise_invalid_params(overrides, fragment):
    with pytest.raises(MCPError) as info:
        _render(**overrides)
    assert info.value.code == INVALID_PARAMS
    assert fragment in info.value.message
    assert info.value.message.startswith("[INVALID_ARGUMENT]")
