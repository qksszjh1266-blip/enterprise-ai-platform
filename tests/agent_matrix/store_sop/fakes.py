"""Scripted model and fault-injectable session/ticket ports."""
from collections import defaultdict, deque
from agent_matrix.store_sop.offline import OfflineChatModel, InMemorySessionStore, InMemoryTicketPort
from agent_matrix.store_sop.models import ToolCallDecision


class ScriptedChatModel(OfflineChatModel):
    def __init__(self, scripts=None, *, by_question=None, decision=None, tamper_numbers=False):
        super().__init__(tamper_numbers=tamper_numbers)
        self.scripts = defaultdict(deque, {key: deque(values) for key, values in (scripts or {}).items()})
        self.by_question = by_question or {}
        self.decision = decision

    async def generate_structured(self, *, messages, output_model, temperature=0.0, max_output_tokens=1024):
        text = messages[-1].content
        label = "用户问题：" if "用户问题：" in text else "本轮提问："
        question = text.rsplit(label, 1)[-1]
        table = self.by_question.get(question, {})
        if self.scripts[output_model] or output_model in table:
            await self._before(messages, output_model)
            value = self.scripts[output_model].popleft() if self.scripts[output_model] else table[output_model]
            if isinstance(value, BaseException):
                raise value
            return self._result(output_model, value)
        return await super().generate_structured(messages=messages, output_model=output_model,
            temperature=temperature, max_output_tokens=max_output_tokens)

    async def decide_tool_call(self, *, messages, tools, temperature=0.0):
        if self.scripts[ToolCallDecision] or self.decision:
            await self._before(messages, ToolCallDecision, tools)
            value = self.scripts[ToolCallDecision].popleft() if self.scripts[ToolCallDecision] else {"tool_name": self.decision}
            return self._result(ToolCallDecision, value)
        return await super().decide_tool_call(messages=messages, tools=tools, temperature=temperature)


class FakeTicketPort(InMemoryTicketPort):
    pass
