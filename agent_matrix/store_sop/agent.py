"""门店 SOP 督导智能体。

StoreSopAgent 结构化满足 eval_platform.ports.AgentSystemPort（`async def invoke(AgentInvocation) -> AgentResponse`），
可零适配接入 EvalRunner 跑 END_TO_END 回归评测；同时提供多轮对话入口 handle_turn 供 api.py / Dify 使用。

受控 Agent Loop（单轮；每次模型或工具调用计 1 步，步数 > config.max_steps 立即终止并返回 STEP_LIMIT_FALLBACK）：

    ① INTENT_SLOT   generate_structured(IntentSlotExtraction) → 合并槽位到会话状态
         ├─ OUT_OF_SCOPE / SMALL_TALK ───────────────────────────→ REFUSE（礼貌拒答，不检索）
         ├─ 安全隐患（模型判定 ∪ SAFETY_HAZARD_KEYWORDS） ─────────→ 安全措施优先，必要时直接 ESCALATE
         └─ 必填槽位缺失 且 clarification_rounds < max ─────────→ CLARIFY（确定性模板追问，结束本轮）
    ② DECIDE        decide_tool_call(白名单 = _allowed_actions(...)) → 非法决策按守卫兜底
    ③ RETRIEVE      RetrievalSystemPort.search（user_context 原样透传，ACL 由数据中台前置过滤）
         ├─ RetrievalError / 超时 ─────────────────────────────→ RETRIEVAL_UNAVAILABLE 风险提示（不编造）
         ├─ results 为空 ──────────────────────────────────────→ REFUSE(NO_EVIDENCE) 或 ESCALATE（设备故障）
         └─ degraded=True ────────────────────────────────────→ 继续，回答前插入降级风险提示
    ④ GENERATE      generate_structured(GroundedAnswerDraft)
    ⑤ VERIFY        引用核验：编号映射 + 数值逐字核验；不通过 → 拒答 NO_EVIDENCE（绝不放行未溯源回答）
    ⑥ ESCALATE      排查失败 / 命中升级条件 → RepairTicketDraft → TicketEscalationPort.submit（幂等）
    ⑦ PERSIST       追加对话、按需压缩摘要、CAS 写回 SessionStorePort

依赖的外部 I/O 全部 async；互不依赖的调用（如提交工单与压缩摘要）使用 asyncio.gather 并发。
"""

from __future__ import annotations

from collections.abc import Sequence
import asyncio
import hashlib
import re
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from agent_matrix.store_sop import prompts
from agent_matrix.store_sop.errors import (
    StoreAgentError, SessionNotFoundError, SessionOwnershipError, SessionStoreUnavailableError,
    ChatModelUnavailableError, StructuredOutputError, TicketEscalationError, UnsupportedScenarioError,
)
from agent_matrix.store_sop.models import (
    StoreIntent, StopReason, LoopStep, ToolCallStatus, TicketSeverity, TicketStatus,
    ConversationTurn, ChatRole, REQUIRED_SLOTS_BY_INTENT, TICKET_REQUIRED_SLOTS, ToolCallDecision,
)
from data_platform.retrieval_engine.models import SearchRequest
from data_platform.retrieval_engine.errors import RetrievalError, RetrievalUnavailableError
from eval_platform.models import EvalScenario, TokenUsage

from agent_matrix.store_sop.models import (
    AgentAction,
    AgentLoopOutcome,
    ExtractedSlots,
    GroundedAnswerDraft,
    IntentSlotExtraction,
    RefusalReason,
    RepairTicketDraft,
    SlotName,
    SopCitation,
    StoreAgentConfig,
    StoreAgentTurnRequest,
    StoreAgentTurnResponse,
    StoreSessionState,
    TicketReceipt,
    ToolSpec,
    ToolTrace,
)
from agent_matrix.store_sop.ports import ChatModelPort, SessionStorePort, TicketEscalationPort
from data_platform.retrieval_engine.models import RetrievedParentChunk, SearchResponse, UserContext
from eval_platform.models import AgentInvocation, AgentResponse
from eval_platform.ports import RetrievalSystemPort

EVAL_SESSION_PREFIX = "eval:"
"""invoke() 构造的一次性会话前缀；此类会话不写入 SessionStorePort。"""


class StoreSopAgent:
    """万店级加盟督导与门店 SOP 知识库智能体。"""

    def __init__(
        self,
        *,
        chat_model: ChatModelPort,
        retrieval: RetrievalSystemPort,
        session_store: SessionStorePort,
        ticket_port: TicketEscalationPort,
        config: StoreAgentConfig | None = None,
    ) -> None:
        """注入四个端口与运行参数（仅保存引用，不做任何 I/O）。"""
        self.chat_model = chat_model
        self.retrieval = retrieval
        self.session_store = session_store
        self.ticket_port = ticket_port
        self.config = config or StoreAgentConfig()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def invoke(self, invocation: AgentInvocation) -> AgentResponse:
        """AgentSystemPort 实现：评测中台端到端单轮调用。

        - scenario 非 STORE_SOP 抛 UnsupportedScenarioError（EvalRunner 归因 SYSTEM_ERROR）。
        - 构造一次性会话 session_id=f"{EVAL_SESSION_PREFIX}{trace_id}"，不读写 SessionStorePort。
        - invocation.user_context 原样透传，不得复制修改。
        - 业务异常直接抛出，不得吞掉后返回伪造结果（eval_platform.ports 约束 2）。
        - 映射规则见 _to_agent_response。
        """
        if invocation.scenario != EvalScenario.STORE_SOP:
            raise UnsupportedScenarioError("不支持此评测场景")
        session_id = EVAL_SESSION_PREFIX + invocation.trace_id
        if len(session_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", session_id):
            session_id = EVAL_SESSION_PREFIX + hashlib.sha256(invocation.trace_id.encode()).hexdigest()
        state = self._new_session(session_id=session_id, user_context=invocation.user_context)
        return self._to_agent_response(await self._run_loop(state, message=invocation.question, trace_id=invocation.trace_id))

    async def handle_turn(self, request: StoreAgentTurnRequest) -> StoreAgentTurnResponse:
        """多轮对话入口（api.py 与 Dify 工作流调用）。

        1. _load_or_create_session（含身份一致性校验）；
        2. _run_loop；
        3. _persist（CAS，expected_version = 读取时的 state_version）；
        4. 返回 outcome.response。
        """
        state, version = await self._load_or_create_session(request)
        outcome = await self._run_loop(state, message=request.message, trace_id=request.trace_id)
        await self._persist(outcome.final_state, expected_version=version)
        return outcome.response

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    async def _load_or_create_session(self, request: StoreAgentTurnRequest) -> tuple[StoreSessionState, int | None]:
        """返回 (state, expected_version)。

        - session_id 为 None：生成新 session_id，expected_version=None。
        - 指定 session_id 但不存在：抛 SessionNotFoundError。
        - 存在但 state.user_context != request.user_context：抛 SessionOwnershipError。
        """
        if request.session_id is None:
            return self._new_session(session_id=uuid4().hex, user_context=request.user_context), None
        # Reserved IDs cannot be used to bypass persistence through the public endpoint.
        if request.session_id.startswith(EVAL_SESSION_PREFIX):
            raise SessionNotFoundError("会话不存在", session_id=request.session_id)
        try:
            state = await self.session_store.load(request.session_id)
        except StoreAgentError:
            raise
        except Exception as exc:
            raise SessionStoreUnavailableError("读取会话失败", session_id=request.session_id) from exc
        if state is None:
            raise SessionNotFoundError("会话不存在", session_id=request.session_id)
        if state.user_context != request.user_context:
            raise SessionOwnershipError("会话身份不匹配", session_id=request.session_id)
        return state, state.state_version

    def _new_session(self, *, session_id: str, user_context: UserContext) -> StoreSessionState:
        """构造 turn_count=0、state_version=0 的空会话（UTC aware 时间戳）。"""
        now = datetime.now(timezone.utc)
        return StoreSessionState(session_id=session_id, user_context=user_context, created_at=now, updated_at=now)

    async def _persist(self, state: StoreSessionState, *, expected_version: int | None) -> StoreSessionState:
        """CAS 写回；EVAL_SESSION_PREFIX 会话直接返回原状态。SessionConflictError 向上抛出。"""
        if state.session_id.startswith(EVAL_SESSION_PREFIX):
            return state
        try:
            return await self.session_store.save(state, expected_version=expected_version, ttl_s=self.config.session_ttl_s)
        except StoreAgentError:
            raise
        except Exception as exc:
            raise SessionStoreUnavailableError("保存会话失败", session_id=state.session_id) from exc

    async def _maybe_summarize(self, state: StoreSessionState) -> tuple[StoreSessionState, ToolTrace | None]:
        """turn_count 达到 summary_trigger_turns 倍数时调用 build_summary_messages 压缩摘要，
        并将 recent_turns 截断至 recent_turns_window；失败不影响本轮回答（保留旧摘要，trace 记 ERROR）。"""
        trace = None
        if state.turn_count and state.turn_count % self.config.summary_trigger_turns == 0:
            start = perf_counter()
            try:
                result = await asyncio.wait_for(self.chat_model.generate_structured(
                    messages=prompts.build_summary_messages(state=state), output_model=_ConversationSummary),
                    self.config.model_timeout_s)
                summary = _ConversationSummary.model_validate(result.output).summary
                state = state.model_copy(update={"conversation_summary": summary})
                trace = _trace(LoopStep.SUMMARIZE, "chat_model.generate_structured", start, usage=result.token_usage)
            except Exception:
                trace = _trace(LoopStep.SUMMARIZE, "chat_model.generate_structured", start,
                    error=ChatModelUnavailableError("摘要压缩失败，保留原摘要"))
        return state.model_copy(update={"recent_turns": state.recent_turns[-self.config.recent_turns_window:]}), trace

    # ------------------------------------------------------------------
    # 受控 Agent Loop
    # ------------------------------------------------------------------

    async def _run_loop(
        self,
        state: StoreSessionState,
        *,
        message: str,
        trace_id: str | None,
    ) -> AgentLoopOutcome:
        """单轮受控循环（见模块文档 ①~⑦）。

        不变量：
        - steps_used <= max_steps；超限 stop_reason=MAX_STEPS_REACHED，answer=STEP_LIMIT_FALLBACK，citations=[]。
        - 检索调用次数 <= max_retrieval_calls，超出的 RETRIEVE 决策记 SKIPPED 并按守卫兜底。
        - generation_contexts 仅包含送入 GENERATE 的上下文；未生成（追问 / 拒答 / 不可用）时为 []。
        - 所有模型 / 工具调用都写入 tool_traces，token_usage 逐步累加。
        - 计时使用 time.perf_counter，latency_ms 不含会话持久化。
        """
        started = perf_counter()
        traces: list[ToolTrace] = []
        search = None
        contexts = []
        citations = []
        ticket = receipt = None
        clarification = []
        retrieval_calls = 0
        stop = StopReason.MAX_STEPS_REACHED
        answer = prompts.STEP_LIMIT_FALLBACK
        summarized = False

        def add(trace):
            traces.append(trace.model_copy(update={"step_index": len(traces) + 1}))

        extraction, trace = await self._classify_and_extract(state, message=message)
        add(trace)
        state = self._merge_extraction(state, extraction)
        safety = self._is_safety_hazard(extraction, state.slots) or any(k in message for k in prompts.SAFETY_HAZARD_KEYWORDS)
        missing = self._missing_required_slots(state)
        if extraction.confidence < self.config.intent_confidence_threshold:
            missing = list(REQUIRED_SLOTS_BY_INTENT.get(state.intent, (SlotName.DRINK_NAME,)))

        if state.intent in (StoreIntent.OUT_OF_SCOPE, StoreIntent.SMALL_TALK):
            stop = StopReason.REFUSED
            answer = self._refuse(RefusalReason.CROSS_DEPT if state.intent == StoreIntent.OUT_OF_SCOPE else RefusalReason.OUT_OF_SCOPE)
        elif missing:
            if state.clarification_rounds < self.config.max_clarification_rounds:
                stop = StopReason.CLARIFICATION_REQUESTED
                answer = self._build_clarification(state, missing=missing, safety_hazard=safety)
                clarification = state.pending_slots
            elif self._should_escalate(state, extraction=extraction, safety_hazard=safety, has_evidence=True):
                ticket = self._build_ticket_draft(state, safety_hazard=safety, citations=[])
            else:
                stop, answer = StopReason.REFUSED, self._refuse(RefusalReason.NO_EVIDENCE)
        elif self._should_escalate(state, extraction=extraction, safety_hazard=safety, has_evidence=True):
            ticket = self._build_ticket_draft(state, safety_hazard=safety, citations=[])
        elif state.escalated_ticket_id and state.intent == StoreIntent.EQUIPMENT_FAULT:
            ticket = self._build_ticket_draft(state, safety_hazard=safety, citations=[])
            receipt = TicketReceipt(ticket_id=state.escalated_ticket_id, status=TicketStatus.DUPLICATE_OPEN, created_at=state.updated_at)
        else:
            while self._check_step_budget(len(traces)):
                allowed = self._allowed_actions(state, retrieval_calls=retrieval_calls, has_contexts=bool(search and search.results))
                if not allowed:
                    break
                action, trace = await self._decide_next_action(state, message=message, allowed=allowed, observations=traces)
                add(trace)
                if action == AgentAction.REFUSE:
                    stop, answer = StopReason.REFUSED, self._refuse(RefusalReason.NO_EVIDENCE)
                    break
                if not self._check_step_budget(len(traces)):
                    break
                if action == AgentAction.RETRIEVE:
                    retrieval_calls += 1
                    search, trace = await self._retrieve(state, query=self._build_retrieval_query(state, message=message, extraction=extraction))
                    add(trace)
                    if search is None:
                        stop, answer = StopReason.RETRIEVAL_UNAVAILABLE, prompts.RETRIEVAL_UNAVAILABLE_NOTICE
                        break
                    if not search.results:
                        if self._should_escalate(state, extraction=extraction, safety_hazard=safety, has_evidence=False):
                            ticket = self._build_ticket_draft(state, safety_hazard=safety, citations=[])
                        else:
                            stop, answer = StopReason.REFUSED, self._refuse(RefusalReason.NO_EVIDENCE)
                        break
                elif action == AgentAction.ANSWER:
                    # Reserve the deterministic verification step before starting generation.
                    if len(traces) + 2 > self.config.max_steps:
                        add(_trace(LoopStep.GENERATE, "chat_model.generate_structured", perf_counter()).model_copy(
                            update={"status": ToolCallStatus.SKIPPED, "output_summary": "剩余步数不足以生成并核验引用"}))
                        break
                    draft, trace = await self._generate_grounded_answer(state, question=message, search=search)
                    add(trace)
                    if state.intent == StoreIntent.DRINK_SOP and not _drink_numbers_grounded(
                        draft.answer, state.slots.drink_name, search.results, draft.cited_context_indices
                    ):
                        draft = draft.model_copy(update={"insufficient_evidence": True})
                    citations, trace = self._verify_citations(draft, contexts=search.results)
                    add(trace)
                    if citations:
                        contexts = search.results
                        stop, answer = StopReason.ANSWERED, draft.answer
                    else:
                        stop, answer = StopReason.REFUSED, self._refuse(RefusalReason.NO_EVIDENCE)
                    break
                elif action == AgentAction.ESCALATE:
                    ticket = self._build_ticket_draft(state, safety_hazard=safety, citations=[])
                    break

        now = datetime.now(timezone.utc)
        state = state.model_copy(update={"turn_count": state.turn_count + 1, "updated_at": now,
            "recent_turns": [*state.recent_turns, ConversationTurn(turn_index=state.turn_count + 1,
                role=ChatRole.USER, content=message, created_at=now)]})
        if ticket is not None:
            if receipt is None and not self._check_step_budget(len(traces)):
                ticket = None
            else:
                if receipt is None:
                    # Summarize the available history concurrently; the final receipt is appended afterwards.
                    if len(traces) + 2 <= self.config.max_steps:
                        (receipt, trace), (state, summary_trace) = await asyncio.gather(self._escalate(ticket), self._maybe_summarize(state))
                        add(trace)
                        if summary_trace:
                            add(summary_trace)
                        summarized = True
                    else:
                        receipt, trace = await self._escalate(ticket)
                        add(trace)
                if receipt:
                    state = state.model_copy(update={"escalated_ticket_id": receipt.ticket_id})
                stop, answer = StopReason.ESCALATED, prompts.render_escalation_notice(draft=ticket, receipt=receipt)
        answer = self._compose_answer(body=answer, search=search)
        state = state.model_copy(update={"recent_turns": [*state.recent_turns,
            ConversationTurn(turn_index=state.turn_count, role=ChatRole.ASSISTANT, content=answer[:4000], created_at=now)]})
        if not summarized and self._check_step_budget(len(traces)):
            state, trace = await self._maybe_summarize(state)
            if trace:
                add(trace)
        state = state.model_copy(update={"recent_turns": state.recent_turns[-self.config.recent_turns_window:]})
        usage = TokenUsage(prompt_tokens=sum(t.token_usage.prompt_tokens for t in traces),
            completion_tokens=sum(t.token_usage.completion_tokens for t in traces))
        response = StoreAgentTurnResponse(session_id=state.session_id, turn_index=state.turn_count, answer=answer,
            citations=citations, needs_clarification=bool(clarification), clarification_slots=clarification,
            escalation_ticket=ticket, ticket_receipt=receipt, refused=stop == StopReason.REFUSED,
            degraded=bool(search and search.degraded), degraded_reasons=search.degraded_reasons if search else [],
            stop_reason=stop, steps_used=len(traces), tool_traces=traces, latency_ms=(perf_counter()-started)*1000,
            token_cost=usage.total_tokens, token_usage=usage, prompt_version=prompts.PROMPT_VERSION)
        return AgentLoopOutcome(response=response, generation_contexts=contexts, final_state=state)

    def _check_step_budget(self, steps_used: int) -> bool:
        """steps_used < max_steps 时返回 True；调用方在每次模型 / 工具调用前检查。"""
        return steps_used < self.config.max_steps

    async def _classify_and_extract(
        self, state: StoreSessionState, *, message: str
    ) -> tuple[IntentSlotExtraction, ToolTrace]:
        """① 意图与槽位判定（build_intent_slot_messages + generate_structured，带 model_timeout_s）。"""
        start = perf_counter()
        try:
            result = await asyncio.wait_for(self.chat_model.generate_structured(
                messages=prompts.build_intent_slot_messages(state=state, user_message=message),
                output_model=IntentSlotExtraction), self.config.model_timeout_s)
            extraction = IntentSlotExtraction.model_validate(result.output)
        except (ValidationError, StructuredOutputError) as exc:
            raise StructuredOutputError("意图槽位输出非法", session_id=state.session_id) from exc
        except StoreAgentError:
            raise
        except Exception as exc:
            raise ChatModelUnavailableError("意图槽位模型调用失败", session_id=state.session_id) from exc
        return extraction, _trace(LoopStep.INTENT_SLOT, "chat_model.generate_structured", start, usage=result.token_usage)

    def _merge_extraction(self, state: StoreSessionState, extraction: IntentSlotExtraction) -> StoreSessionState:
        """合并槽位（非 None 覆盖、attempted_checks 去重追加）；意图切换时清空旧意图专属槽位并将
        clarification_rounds 归零。返回新对象（model_copy），不得原地修改。"""
        switched = state.intent != extraction.intent
        values = (ExtractedSlots() if switched else state.slots).model_dump()
        for name, value in extraction.slots.model_dump().items():
            if name == "attempted_checks":
                values[name] = list(dict.fromkeys(values[name] + value))[-20:]
            elif value is not None:
                values[name] = value
        return state.model_copy(update={"intent": extraction.intent, "slots": ExtractedSlots(**values),
            "clarification_rounds": 0 if switched else state.clarification_rounds, "pending_slots": []})

    def _is_safety_hazard(self, extraction: IntentSlotExtraction, slots: ExtractedSlots) -> bool:
        """模型判定 safety_hazard_suspected 与 SAFETY_HAZARD_KEYWORDS 命中 fault_symptom 取并集。"""
        return extraction.safety_hazard_suspected or any(k in (slots.fault_symptom or "") for k in prompts.SAFETY_HAZARD_KEYWORDS)

    def _missing_required_slots(self, state: StoreSessionState) -> list[SlotName]:
        """按 REQUIRED_SLOTS_BY_INTENT 计算缺失槽位（保持定义顺序）。"""
        return [s for s in REQUIRED_SLOTS_BY_INTENT.get(state.intent, ()) if not getattr(state.slots, s.value)]

    def _allowed_actions(
        self,
        state: StoreSessionState,
        *,
        retrieval_calls: int,
        has_contexts: bool,
    ) -> list[AgentAction]:
        """本步动作白名单：
        - 必填槽位缺失 → 仅 ASK_CLARIFICATION / REFUSE；
        - retrieval_calls >= max_retrieval_calls → 去掉 RETRIEVE；
        - has_contexts=False → 去掉 ANSWER；
        - intent != EQUIPMENT_FAULT → 去掉 ESCALATE。
        白名单为空时调用方直接走 STEP_LIMIT_FALLBACK。"""
        if state.intent in (StoreIntent.OUT_OF_SCOPE, StoreIntent.SMALL_TALK):
            return [AgentAction.REFUSE]
        if self._missing_required_slots(state):
            return ([AgentAction.ASK_CLARIFICATION] if state.clarification_rounds < self.config.max_clarification_rounds else []) + [AgentAction.REFUSE]
        actions = []
        if retrieval_calls < self.config.max_retrieval_calls:
            actions.append(AgentAction.RETRIEVE)
        if has_contexts:
            actions.append(AgentAction.ANSWER)
        if state.intent == StoreIntent.EQUIPMENT_FAULT and not state.escalated_ticket_id:
            actions.append(AgentAction.ESCALATE)
        actions.append(AgentAction.REFUSE)
        return actions

    def _action_tools(self, actions: Sequence[AgentAction]) -> list[ToolSpec]:
        """将白名单动作转换为 ToolSpec（描述取 prompts.ACTION_TOOL_DESCRIPTIONS）。"""
        return [ToolSpec(name=a.value, description=prompts.ACTION_TOOL_DESCRIPTIONS[a]) for a in actions]

    async def _decide_next_action(
        self,
        state: StoreSessionState,
        *,
        message: str,
        allowed: Sequence[AgentAction],
        observations: Sequence[ToolTrace],
    ) -> tuple[AgentAction, ToolTrace]:
        """② 工具调用决策；模型返回不在 allowed 中的 tool_name 时按确定性优先级兜底：
        ASK_CLARIFICATION > RETRIEVE > ANSWER > ESCALATE > REFUSE（取 allowed 中第一个），trace 记录原始决策。"""
        start = perf_counter()
        try:
            result = await asyncio.wait_for(self.chat_model.decide_tool_call(
                messages=prompts.build_next_action_messages(state=state, user_message=message, observations=observations),
                tools=self._action_tools(allowed)), self.config.model_timeout_s)
            decision = ToolCallDecision.model_validate(result.output)
        except (ValidationError, StructuredOutputError) as exc:
            raise StructuredOutputError("动作决策输出非法", session_id=state.session_id) from exc
        except StoreAgentError:
            raise
        except Exception as exc:
            raise ChatModelUnavailableError("动作决策模型调用失败", session_id=state.session_id) from exc
        priority = [AgentAction.ASK_CLARIFICATION, AgentAction.RETRIEVE, AgentAction.ANSWER, AgentAction.ESCALATE, AgentAction.REFUSE]
        action = next((a for a in allowed if a.value == decision.tool_name), None)
        if action is None:
            action = next(a for a in priority if a in allowed)
        trace = _trace(LoopStep.DECIDE, "chat_model.decide_tool_call", start, usage=result.token_usage)
        # Keep the requested name for audit, never execute model arguments or identities.
        trace = trace.model_copy(update={"output_summary": f"requested={decision.tool_name[:100]}; selected={action.value}"})
        return action, trace

    def _build_clarification(self, state: StoreSessionState, *, missing: Sequence[SlotName], safety_hazard: bool) -> str:
        """调用 prompts.render_clarification_question；同步更新 pending_slots 与 clarification_rounds。"""
        state.pending_slots = list(missing[:2])
        state.clarification_rounds += 1
        return prompts.render_clarification_question(missing_slots=state.pending_slots, slots=state.slots, safety_hazard=safety_hazard)

    def _build_retrieval_query(self, state: StoreSessionState, *, message: str, extraction: IntentSlotExtraction) -> str:
        """优先 extraction.rewritten_query；否则由槽位（设备 + 现象 / 饮品名称）与原问题拼接，截断至 1000 字符。"""
        return (extraction.rewritten_query or " ".join(filter(None,
            [state.slots.equipment_model, state.slots.fault_symptom, state.slots.drink_name, message])))[:1000]

    async def _retrieve(
        self, state: StoreSessionState, *, query: str
    ) -> tuple[SearchResponse | None, ToolTrace]:
        """③ 调用数据中台混合检索：SearchRequest(query, user_context=state.user_context,
        top_k=config.retrieval_top_k, enable_rerank=config.enable_rerank)，带 retrieval_timeout_s。

        - RetrievalError：返回 (None, trace)，trace.error_code=exc.code、error_hint=exc.to_hint()；
        - 超时：error_code="RETRIEVAL_TIMEOUT"；
        - asyncio.CancelledError 必须重新抛出。
        """
        start = perf_counter()
        try:
            response = await asyncio.wait_for(self.retrieval.search(SearchRequest(query=query,
                user_context=state.user_context, top_k=self.config.retrieval_top_k, enable_rerank=self.config.enable_rerank)),
                self.config.retrieval_timeout_s)
        except RetrievalError as exc:
            return None, _trace(LoopStep.RETRIEVE, "knowledge.search", start, error=exc)
        except TimeoutError:
            trace = _trace(LoopStep.RETRIEVE, "knowledge.search", start, error=RetrievalUnavailableError("检索超时"))
            return None, trace.model_copy(update={"error_code": "RETRIEVAL_TIMEOUT", "error_hint": "[RETRIEVAL_TIMEOUT] 检索超时 | 建议: 请稍后重试。"})
        except Exception:
            return None, _trace(LoopStep.RETRIEVE, "knowledge.search", start, error=RetrievalUnavailableError("检索服务不可用"))
        trace = _trace(LoopStep.RETRIEVE, "knowledge.search", start)
        return response, trace.model_copy(update={"status": ToolCallStatus.DEGRADED if response.degraded else ToolCallStatus.OK,
            "output_summary": f"参考资料数量={len(response.results)}"})

    async def _generate_grounded_answer(
        self,
        state: StoreSessionState,
        *,
        question: str,
        search: SearchResponse,
    ) -> tuple[GroundedAnswerDraft, ToolTrace]:
        """④ 基于 search.results 生成回答（build_grounded_answer_messages + generate_structured）。"""
        start = perf_counter()
        try:
            result = await asyncio.wait_for(self.chat_model.generate_structured(
                messages=prompts.build_grounded_answer_messages(state=state, question=question,
                    contexts=search.results, degraded_reasons=search.degraded_reasons),
                output_model=GroundedAnswerDraft), self.config.model_timeout_s)
            draft = GroundedAnswerDraft.model_validate(result.output)
        except (ValidationError, StructuredOutputError) as exc:
            raise StructuredOutputError("引用回答输出非法", session_id=state.session_id) from exc
        except StoreAgentError:
            raise
        except Exception as exc:
            raise ChatModelUnavailableError("引用回答模型调用失败", session_id=state.session_id) from exc
        return draft, _trace(LoopStep.GENERATE, "chat_model.generate_structured", start, usage=result.token_usage)

    def _verify_citations(
        self,
        draft: GroundedAnswerDraft,
        *,
        contexts: Sequence[RetrievedParentChunk],
    ) -> tuple[list[SopCitation], ToolTrace]:
        """⑤ 确定性引用核验（不调用模型）：
        1. cited_context_indices 必须 ⊆ [1, len(contexts)]，越界即失败；
        2. answer 中出现的 [来源n] 必须 ⊆ cited_context_indices；
        3. DRINK_SOP 意图：answer 中每个「数字+单位」片段（ml/g/秒/次 等）必须是某条被引用 context.full_content 的子串；
        4. draft.insufficient_evidence=True 或 cited 为空 → 失败。
        失败返回 ([], trace(status=ERROR, error_code="CITATION_UNVERIFIED"))；
        成功按 cited 顺序构造 SopCitation（quote 取 context.full_content 中与回答最相关的 ≤200 字子串）。"""
        start = perf_counter()
        indices = list(dict.fromkeys(draft.cited_context_indices))
        markers = {int(n) for n in re.findall(r"\[来源(\d+)\]", draft.answer)}
        valid = (not draft.insufficient_evidence and bool(draft.answer.strip()) and bool(indices)
            and all(1 <= i <= len(contexts) for i in indices) and markers == set(indices))
        if valid:
            evidence = [contexts[i-1].full_content for i in indices]
            valid = _numbers_grounded(draft.answer, evidence)
        if not valid:
            return [], _trace(LoopStep.VERIFY_CITATIONS, "citations.verify", start).model_copy(update={
                "status": ToolCallStatus.ERROR, "error_code": "CITATION_UNVERIFIED",
                "error_hint": "[CITATION_UNVERIFIED] 引用或数值无法核验 | 建议: 请向值班主管确认。"})
        citations = []
        for i in indices:
            c = contexts[i-1]
            lines = [line for line in c.full_content.splitlines() if line.strip()]
            best = max(lines, key=lambda line: len(set(line) & set(draft.answer)), default="")
            citations.append(SopCitation(doc_id=c.doc_id, parent_id=c.parent_id, version=c.version,
                section_path=c.section_path, page_number=c.page_number, quote=best[:200]))
        return citations, _trace(LoopStep.VERIFY_CITATIONS, "citations.verify", start)

    def _should_escalate(
        self,
        state: StoreSessionState,
        *,
        extraction: IntentSlotExtraction,
        safety_hazard: bool,
        has_evidence: bool,
    ) -> bool:
        """EQUIPMENT_FAULT 且（troubleshooting_failed 或 safety_hazard 或 clarification_rounds 已达上限
        且 TICKET_REQUIRED_SLOTS 齐备 或 无检索依据）且 state.escalated_ticket_id 为 None 时返回 True。"""
        complete = all(getattr(state.slots, s.value) for s in TICKET_REQUIRED_SLOTS)
        return (state.intent == StoreIntent.EQUIPMENT_FAULT and complete and state.escalated_ticket_id is None
            and (extraction.troubleshooting_failed or safety_hazard or not has_evidence
                 or state.clarification_rounds >= self.config.max_clarification_rounds))

    def _build_ticket_draft(
        self,
        state: StoreSessionState,
        *,
        safety_hazard: bool,
        citations: Sequence[SopCitation],
    ) -> RepairTicketDraft:
        """⑥ 由会话槽位与 user_context 构造工单草稿（reporter_user_id / dept_id 只取 state.user_context）。
        severity：safety_hazard → P1_SAFETY；设备停机影响出品 → P2；其余 P3。
        immediate_actions 取自被引用 SOP 的「停机与升级条件」，无引用时为空。"""
        slots = state.slots
        key = f"{state.session_id}:{slots.equipment_id or slots.equipment_model}:{slots.fault_symptom}"
        if len(key) > 256:
            key = f"{state.session_id}:" + hashlib.sha256(key.encode()).hexdigest()
        summary = f"{slots.equipment_model}：{slots.fault_symptom}。"
        if not slots.equipment_id:
            summary += "设备编号待现场补录。"
        actions = []
        for citation in citations:
            if citation.quote.startswith("|"):
                # A verified fault-table row ends in its stop/escalation condition.
                cells = [cell.strip() for cell in citation.quote.split("|")[1:-1]]
                if cells and citation.quote.endswith("|"):
                    actions.append(cells[-1])
            elif "停机与升级条件" in citation.section_path:
                actions.append(citation.quote)
        severity = TicketSeverity.P1_SAFETY if safety_hazard else (
            TicketSeverity.P2_BUSINESS_BLOCKING if any(k in (slots.fault_symptom or "") for k in ("不", "停机", "漏水", "故障"))
            else TicketSeverity.P3_GENERAL)
        return RepairTicketDraft(idempotency_key=key, session_id=state.session_id,
            reporter_user_id=state.user_context.user_id, dept_id=state.user_context.dept_id,
            equipment_model=slots.equipment_model, equipment_id=slots.equipment_id, fault_symptom=slots.fault_symptom,
            attempted_checks=slots.attempted_checks, severity=severity, safety_hazard=safety_hazard,
            immediate_actions=actions, summary=summary, sop_citations=list(citations), created_at=datetime.now(timezone.utc))

    async def _escalate(self, draft: RepairTicketDraft) -> tuple[TicketReceipt | None, ToolTrace]:
        """提交工单（带 ticket_timeout_s）；TicketEscalationError / 超时返回 (None, trace)，
        回答中仍告知草稿已生成并给出电话升级路径，不得假称提交成功。"""
        start = perf_counter()
        try:
            receipt = await asyncio.wait_for(self.ticket_port.submit(draft), self.config.ticket_timeout_s)
            return receipt, _trace(LoopStep.ESCALATE, "ticket.submit", start)
        except Exception:
            return None, _trace(LoopStep.ESCALATE, "ticket.submit", start,
                error=TicketEscalationError("报修工单提交失败", session_id=draft.session_id))

    def _refuse(self, reason: RefusalReason) -> str:
        """调用 prompts.render_refusal；越权拒答不得附带任何检索结果或引用。"""
        return prompts.render_refusal(reason)

    def _compose_answer(self, *, body: str, search: SearchResponse | None) -> str:
        """search.degraded=True 时在正文首行插入 render_degraded_notice(search.degraded_reasons)。"""
        return (prompts.render_degraded_notice(search.degraded_reasons) + "\n" if search and search.degraded else "") + body

    # ------------------------------------------------------------------
    # 评测映射
    # ------------------------------------------------------------------

    @staticmethod
    def _to_agent_response(outcome: AgentLoopOutcome) -> AgentResponse:
        """StoreAgentTurnResponse → AgentResponse：

        answer              ← response.answer（追问 / 拒答 / 报修告知同样原样返回）
        retrieved_contexts  ← outcome.generation_contexts（实际送入生成的上下文，非全部检索结果）
        cited_doc_ids       ← 按 citations 顺序去重的 doc_id
        refused             ← response.refused
        latency_ms          ← response.latency_ms
        degraded / degraded_reasons ← 同名字段
        token_usage         ← response.token_usage
        """
        response = outcome.response
        return AgentResponse(answer=response.answer, retrieved_contexts=outcome.generation_contexts,
            cited_doc_ids=list(dict.fromkeys(c.doc_id for c in response.citations)), refused=response.refused,
            latency_ms=response.latency_ms, degraded=response.degraded, degraded_reasons=response.degraded_reasons,
            token_usage=response.token_usage)


class _ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=300)


def _trace(step, tool, start, *, usage=None, error=None):
    return ToolTrace(step_index=1, step=step, tool_name=tool,
        status=ToolCallStatus.ERROR if error else ToolCallStatus.OK,
        latency_ms=(perf_counter()-start)*1000, token_usage=usage or TokenUsage(),
        error_code=error.code if error else None, error_hint=error.to_hint() if error else None)


# Apply the strict numeric check to every answer, a conservative superset of DRINK_SOP.
# No mutable intent flag is needed on an agent shared by concurrent sessions.
_NUMBER_UNIT = re.compile(r"(?:[+-]?\d+(?:\.\d+)?|[零一二三四五六七八九十百半两]+)\s*(?:毫升|千克|公斤|分钟|小时|摄氏度|ml|kg|g|L|克|升|秒|次|度|℃|°C|%|％)", re.IGNORECASE)


def _drink_numbers_grounded(answer, drink_name, contexts, indices):
    """Bind quantities to the requested drink, even if the answer omits its name.

    A shared parent table may contain valid quantities for several different drinks.
    Preserve non-table text for handling/allergen statements, but exclude other recipes.
    """
    if not drink_name:
        return False
    cited = [contexts[i-1].full_content for i in indices if 1 <= i <= len(contexts)]
    rows = [line for text in cited for line in text.splitlines() if line.startswith("|") and drink_name in line]
    if not rows:
        return _numbers_grounded(answer, cited)
    narrative = [line for text in cited for line in text.splitlines() if not line.startswith("|")]
    return _numbers_grounded(answer, rows + narrative)


def _numbers_grounded(answer: str, evidence: list[str]) -> bool:
    for match in _NUMBER_UNIT.finditer(answer):
        value = match.group()
        # A numeric substring inside a larger number is not the same quantity.
        pattern = re.compile(r"(?<![\d.])" + re.escape(value))
        if not any(pattern.search(text) for text in evidence):
            return False
        prefix = answer[max(0, match.start()-16):match.start()]
        ingredient = re.search(r"([\u4e00-\u9fff]{2,})\s*$", prefix)
        rows = [line for text in evidence for line in text.splitlines() if line.startswith("|")]
        # Restrict values to rows containing the stated drink (if present).
        named_rows = []
        for row in rows:
            name = row.split("|")[1].strip().split()[0] if row.split("|")[1].strip() else ""
            if name and name in answer and re.search(r"\d", row.split("|")[1]):
                named_rows.append(row)
        if named_rows and not any(pattern.search(row) for row in named_rows):
            return False
        # Catch 45g for lemon even though another drink in the same parent contains 45g.
        if ingredient:
            label = ingredient.group(1)
            matching = [row for row in rows if label in row]
            if matching and not any(pattern.search(row) for row in matching):
                return False
    return True
