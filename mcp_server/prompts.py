"""MCP Prompt：电商运营 SOP 带引用防幻觉问答模板。

规则正文直接复用 agent_matrix.store_sop.prompts（compose_system_prompt 的铁律组合），
保证 MCP 客户端拿到的话术与电商运营 SOP 智能体、评测中台回归口径一致。

MCP Prompt 消息只允许 user / assistant 角色，因此渲染为两条 user 消息：
    [0] 作答规则：SOP 铁律 + 场景附加规则 +（降级时）风险提示要求 + MCP 输出要求
    [1] 参考资料（[来源n] 编号块）+ 用户问题

evidence 参数为 JSON 字符串，接受两种形态：
    - search_enterprise_knowledge 的完整结构化输出（含 evidence / degraded 字段）；
    - 证据对象数组，每项至少含 content，可选 section_path / page_number。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import UserMessage
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from agent_matrix.store_sop.models import StoreIntent
from agent_matrix.store_sop.prompts import PROMPT_VERSION as STORE_SOP_PROMPT_VERSION
from agent_matrix.store_sop.prompts import compose_system_prompt, render_degraded_notice

SOP_CITATION_PROMPT_VERSION = f"mcp.sop_citation_qa.v1+{STORE_SOP_PROMPT_VERSION}"

MAX_QUESTION_CHARS = 1000
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_ITEM_CHARS = 4000

SCENARIOS: dict[str, StoreIntent] = {
    "store_ops_general": StoreIntent.STORE_OPS_GENERAL,
    "drink_sop": StoreIntent.DRINK_SOP,
    "equipment_fault": StoreIntent.EQUIPMENT_FAULT,
}

MCP_ANSWER_FORMAT = """\
【输出要求】
1. 先给结论再给步骤；每条事实性陈述句末标注 [来源n]，n 只能取参考资料中实际存在的编号（1 至 {count}）。
2. 不得编造参考资料中不存在的编号、数值、步骤或文档名。
3. 参考资料不足以回答时，直接回答"手册中未找到相关规定"，并建议联系值班主管或仓配运维主管，不做推测。"""

DEGRADED_MCP_RULE = """\
【检索降级风险提示】
本次参考资料来自降级检索，可能不完整。回答开头必须单独一行原样输出：
{notice}
涉及规格数值与安全操作时只陈述参考资料中明确出现的内容。"""


def _invalid(message: str) -> MCPError:
    return MCPError(INVALID_PARAMS, f"[INVALID_ARGUMENT] {message}")


def _parse_bool(value: str | bool, name: str) -> bool:
    if isinstance(value, bool):
        return value
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no", ""}:
        return False
    raise _invalid(f"{name} 只能为 true / false")


def _parse_evidence(raw: str) -> tuple[list[dict[str, Any]], bool]:
    """解析证据 JSON，返回 (规范化证据列表, 是否来自降级检索)。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _invalid("evidence 必须是 JSON 字符串（search_enterprise_knowledge 的输出或证据数组）") from exc
    degraded = False
    if isinstance(data, dict):
        degraded = bool(data.get("degraded", False))
        data = data.get("evidence")
    if not isinstance(data, list) or not data:
        raise _invalid("evidence 不能为空：无参考资料时不得生成回答，请先检索或按无依据拒答处理")
    if len(data) > MAX_EVIDENCE_ITEMS:
        raise _invalid(f"evidence 最多 {MAX_EVIDENCE_ITEMS} 条")
    items = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise _invalid(f"evidence[{index}] 必须是对象")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise _invalid(f"evidence[{index}].content 必须是非空字符串")
        section = item.get("section_path", "")
        page = item.get("page_number")
        if page is None and isinstance(item.get("source"), dict):
            page = item["source"].get("page_number")
        if not isinstance(section, str):
            raise _invalid(f"evidence[{index}].section_path 必须是字符串")
        if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
            raise _invalid(f"evidence[{index}].page_number 必须是正整数")
        items.append({"section_path": section.strip(), "page_number": page,
                      "content": content.strip()[:MAX_EVIDENCE_ITEM_CHARS]})
    return items, degraded


def render_evidence_block(items: list[dict[str, Any]]) -> str:
    """与 store_sop.render_context_block 相同的 [来源n] 编号格式（不输出 doc_id）。"""
    blocks = []
    for index, item in enumerate(items, 1):
        page = f"（第 {item['page_number']} 页）" if item["page_number"] is not None else ""
        blocks.append(f"[来源{index}] 章节：{item['section_path'] or '未标注章节'}{page}\n{item['content']}")
    return "\n\n".join(blocks)


def render_sop_citation_qa(
    *,
    question: str,
    evidence: str,
    scenario: str = "store_ops_general",
    degraded: str | bool = "false",
) -> list[UserMessage]:
    """渲染结构化消息；任何入参问题抛 MCPError(INVALID_PARAMS)，消息可直接回灌给模型自纠错。"""
    question = (question or "").strip()
    if not question:
        raise _invalid("question 不能为空")
    if len(question) > MAX_QUESTION_CHARS:
        raise _invalid(f"question 不能超过 {MAX_QUESTION_CHARS} 字")
    intent = SCENARIOS.get((scenario or "").strip())
    if intent is None:
        raise _invalid(f"scenario 只能取 {' / '.join(SCENARIOS)}")
    items, evidence_degraded = _parse_evidence(evidence)
    is_degraded = _parse_bool(degraded, "degraded") or evidence_degraded

    rules = [compose_system_prompt(intent=intent, degraded=False)]
    if is_degraded:
        rules.append(DEGRADED_MCP_RULE.format(notice=render_degraded_notice([])))
    rules.append(MCP_ANSWER_FORMAT.format(count=len(items)))
    return [
        UserMessage("\n\n".join(rules)),
        UserMessage(f"参考资料：\n{render_evidence_block(items)}\n\n用户问题：{question}"),
    ]
