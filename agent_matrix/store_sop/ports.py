"""电商运营 SOP 智能体异步端口（Ports & Adapters）。

- 单元测试注入离线确定性替身（FakeChatModel 按脚本返回结构化结果）；生产注入真实大模型 / Redis / 工单系统适配器。
- 知识检索不在此重复定义：直接复用 eval_platform.ports.RetrievalSystemPort
  （数据中台 HybridRetrievalService 原样满足；生产可换为调用数据中台 OpenAPI `search_knowledge_base` 的 HTTP 适配器，
  签名同为 `async def search(SearchRequest) -> SearchResponse`）。

适配器硬性约束：
1. 全部方法为 async；不得在适配器内阻塞事件循环（同步 SDK 必须换用异步客户端）。
2. 只抛异常，不自行吞错或伪造结果；超时、重试与降级由 StoreSopAgent 统一控制。
3. ChatModelPort 返回的 output 必须已通过 output_model.model_validate；解析失败抛 StructuredOutputError。
4. 任何端口不得接收或返回由 LLM 生成的 UserContext。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from agent_matrix.store_sop.models import (
    ChatMessage,
    ChatModelResult,
    RepairTicketDraft,
    StoreSessionState,
    TicketReceipt,
    ToolCallDecision,
    ToolSpec,
)

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class ChatModelPort(Protocol):
    """大模型异步接口（结构化输出 + 工具调用决策）。"""

    async def generate_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        output_model: type[T],
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
    ) -> ChatModelResult[T]:
        """按 output_model 的 JSON Schema 约束生成结构化结果。

        - 生产适配器优先使用原生 structured output / JSON mode；退化为文本时必须自行 model_validate。
        - 校验失败抛 StructuredOutputError；服务不可用 / 超时抛 ChatModelUnavailableError。
        """
        ...

    async def decide_tool_call(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> ChatModelResult[ToolCallDecision]:
        """在给定 tools 白名单中选择下一步动作。

        - 必须恰好返回一个决策；tool_name 不在 tools 中时适配器不做纠正，由 agent 判定为非法决策并兜底。
        """
        ...


@runtime_checkable
class SessionStorePort(Protocol):
    """多轮会话状态异步读写端口（生产：Redis / PostgreSQL；测试：内存 dict）。"""

    async def load(self, session_id: str) -> StoreSessionState | None:
        """读取会话；不存在或已过期返回 None。存储不可用抛 SessionStoreUnavailableError。"""
        ...

    async def save(
        self,
        state: StoreSessionState,
        *,
        expected_version: int | None,
        ttl_s: int,
    ) -> StoreSessionState:
        """乐观并发写入（CAS）。

        - expected_version=None 表示新建：已存在同 session_id 抛 SessionConflictError。
        - 否则存储中 state_version 必须等于 expected_version，写入后返回 state_version + 1 的新状态；
          不一致抛 SessionConflictError。
        """
        ...

    async def delete(self, session_id: str) -> bool:
        """删除会话，返回是否确有删除。"""
        ...


@runtime_checkable
class TicketEscalationPort(Protocol):
    """店铺设备故障转人工运维 / 报修工单端口（生产：内部工单系统 / 企业 IM 机器人）。"""

    async def submit(self, draft: RepairTicketDraft) -> TicketReceipt:
        """提交工单，按 draft.idempotency_key 幂等。

        - 已存在未关闭的同 key 工单时返回原工单，status=DUPLICATE_OPEN。
        - 失败抛 TicketEscalationError。
        """
        ...
