"""Offline port adapters. Rule extraction is a reproducible fixture, not a production LLM.

No evaluation labels, reference answers, or document IDs are available to the model.
TokenUsage is a fixed synthetic accounting fixture (10 input + 5 output per call).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from time import monotonic
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from agent_matrix.store_sop import prompts
from agent_matrix.store_sop.errors import SessionConflictError, StructuredOutputError
from agent_matrix.store_sop.models import (
    ChatMessage, ChatModelResult, ExtractedSlots, GroundedAnswerDraft, IntentSlotExtraction,
    RepairTicketDraft, StoreSessionState, TicketReceipt, TicketStatus, ToolCallDecision, ToolSpec,
)
from eval_platform.models import TokenUsage

T = TypeVar("T", bound=BaseModel)


class InMemorySessionStore:
    """Copy-isolated sessions with monotonic TTL and atomic process-local CAS."""

    def __init__(self):
        self.states: dict[str, StoreSessionState] = {}
        self.expires: dict[str, float] = {}
        self.fail: Exception | None = None
        self.save_calls = 0
        self.load_calls = 0
        self._lock = asyncio.Lock()

    def _check(self):
        if self.fail:
            raise self.fail

    def _expire(self, session_id):
        if self.expires.get(session_id, float("inf")) <= monotonic():
            self.states.pop(session_id, None)
            self.expires.pop(session_id, None)

    async def load(self, session_id: str) -> StoreSessionState | None:
        self._check()
        self.load_calls += 1
        async with self._lock:
            self._expire(session_id)
            state = self.states.get(session_id)
            return state.model_copy(deep=True) if state else None

    async def save(self, state: StoreSessionState, *, expected_version: int | None, ttl_s: int) -> StoreSessionState:
        self._check()
        self.save_calls += 1
        async with self._lock:
            self._expire(state.session_id)
            old = self.states.get(state.session_id)
            if (expected_version is None and old is not None) or (expected_version is not None and (old is None or old.state_version != expected_version)):
                raise SessionConflictError("会话版本已更新", session_id=state.session_id)
            saved = state.model_copy(deep=True, update={"state_version": (old.state_version if old else 0) + 1})
            self.states[state.session_id] = saved
            self.expires[state.session_id] = monotonic() + ttl_s
            return saved.model_copy(deep=True)

    async def delete(self, session_id: str) -> bool:
        self._check()
        async with self._lock:
            self._expire(session_id)
            self.expires.pop(session_id, None)
            return self.states.pop(session_id, None) is not None


class InMemoryTicketPort:
    """Idempotent offline repair queue; no real ticket or notification is sent."""

    def __init__(self):
        self.receipts: dict[str, TicketReceipt] = {}
        self.submitted: list[RepairTicketDraft] = []
        self.fail: Exception | None = None
        self.delay = 0.0
        self._lock = asyncio.Lock()

    async def submit(self, draft: RepairTicketDraft) -> TicketReceipt:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        async with self._lock:
            self.submitted.append(draft)
            if draft.idempotency_key in self.receipts:
                return self.receipts[draft.idempotency_key].model_copy(update={"status": TicketStatus.DUPLICATE_OPEN})
            receipt = TicketReceipt(ticket_id="OFFLINE-" + hashlib.sha256(draft.idempotency_key.encode()).hexdigest()[:16],
                status=TicketStatus.CREATED, assignee_role="区域督导", created_at=datetime.now(timezone.utc))
            self.receipts[draft.idempotency_key] = receipt
            return receipt


class OfflineChatModel:
    """Extract exact source rows; optionally change lemon 40g to 45g for fault injection."""

    def __init__(self, *, tamper_numbers: bool = False):
        self.tamper_numbers = tamper_numbers
        self.calls: list[dict] = []
        self.fail: Exception | None = None
        self.delay = 0.0

    async def _before(self, messages, output_model, tools=()):
        self.calls.append({"messages": list(messages), "output_model": output_model, "tools": list(tools)})
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

    def _result(self, output_model, value):
        try:
            output = output_model.model_validate(value)
        except ValidationError as exc:
            raise StructuredOutputError("离线模型输出校验失败") from exc
        return ChatModelResult(output=output, token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5), model_name="offline-rules-v1")

    async def generate_structured(self, *, messages: Sequence[ChatMessage], output_model: type[T],
                                  temperature: float = 0.0, max_output_tokens: int = 1024) -> ChatModelResult[T]:
        await self._before(messages, output_model)
        text = messages[-1].content.replace("{{", "{").replace("}}", "}")
        if output_model is IntentSlotExtraction:
            value = self._extract(text)
        elif output_model is GroundedAnswerDraft:
            value = self._answer(text)
        else:
            # Summaries are a fourth call site, with an internal schema containing only summary.
            value = {"summary": text[-300:]}
        return self._result(output_model, value)

    async def decide_tool_call(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
                               temperature: float = 0.0) -> ChatModelResult[ToolCallDecision]:
        await self._before(messages, ToolCallDecision, tools)
        names = {tool.name for tool in tools}
        action = next(name for name in ("ask_clarification", "answer", "retrieve", "escalate", "refuse") if name in names)
        return self._result(ToolCallDecision, {"tool_name": action})

    def _extract(self, text):
        question = text.rsplit("本轮提问：", 1)[-1]
        found = re.search(r"已知槽位\(JSON\)：([^\n]+)", text)
        known = json.loads(found.group(1)) if found else {}
        slots = ExtractedSlots()
        if any(k in question for k in ("审批", "银行账户", "合同价格", "薪酬", "财务", "采购底价", "供应商价格")):
            intent = "out_of_scope"
        else:
            equipment = next((k for k in ("制冰机", "封口机", "冷藏柜") if k in question), None)
            drink = next((k for k in ("鲜柠檬茶", "珍珠奶茶", "草莓酸奶") if k in question), None)
            symptoms = [k for k in ("不进水", "冰块偏薄", "漏水", "异响", "焦味", "冒烟", "封口不牢", "不加热", "漏电", "火花") if k in question]
            if drink or "饮品" in question or "配方" in question:
                intent = "drink_sop"
                slots.drink_name = drink
            elif equipment or symptoms or "机器" in question or known.get("equipment_model"):
                intent = "equipment_fault"
                slots.equipment_model = equipment
                slots.fault_symptom = "、".join(symptoms) or None
                equipment_id = re.search(r"\b[A-Z]{2,8}-\d+\b", question)
                slots.equipment_id = equipment_id.group() if equipment_id else None
                if "检查过" in question or "试过" in question:
                    slots.attempted_checks = [question[:200]]
            elif question in ("你好", "谢谢", "再见"):
                intent = "small_talk"
            else:
                intent = "store_ops_general"
        return IntentSlotExtraction(intent=intent, confidence=1, slots=slots,
            safety_hazard_suspected=any(k in question for k in prompts.SAFETY_HAZARD_KEYWORDS),
            troubleshooting_failed=any(k in question for k in ("还是", "仍然", "仍不", "未解决")))

    def _answer(self, text):
        question = text.rsplit("用户问题：", 1)[-1]
        known = re.search(r"已知槽位：([^\n]+)", text)
        slots = json.loads(known.group(1)) if known else {}
        subject = slots.get("drink_name") or slots.get("equipment_model") or ""
        symptom = slots.get("fault_symptom") or ""
        blocks = re.findall(r"\[来源(\d+)\] 章节：([^\n]+)\n(.*?)(?=\n\n\[来源|\n已知槽位：|\Z)", text, re.S)
        choices = []
        for index, section, content in blocks:
            for line in content.splitlines():
                if not line.strip() or line.startswith("| ---"):
                    continue
                score = len(set(question) & set(line))
                if subject:
                    score += 50 * (subject in line) + 20 * (subject in section)
                if symptom:
                    score += 50 * (symptom in line)
                choices.append((score, int(index), line, content))
        if not choices:
            return GroundedAnswerDraft(answer="手册中未找到相关规定", insufficient_evidence=True)
        _, index, line, content = max(choices, key=lambda item: item[0])
        if self.tamper_numbers and "柠檬" in line:
            line = line.replace("40g", "45g")
        answer = f"{line} [来源{index}]"
        if subject in ("珍珠奶茶", "草莓酸奶"):
            for source_line in content.splitlines():
                if "过敏" in source_line:
                    answer += f"\n{source_line} [来源{index}]"
        return GroundedAnswerDraft(answer=answer, cited_context_indices=[index])
