"""电商运营 SOP 智能体数据模型与传输契约。

本模块仅包含 Pydantic V2 模型、枚举与策略常量，不含任何 I/O：

枚举与策略常量
- StoreIntent / SlotName / AgentAction / LoopStep / StopReason / RefusalReason
- ToolCallStatus / TicketSeverity / TicketStatus / ChatRole
- REQUIRED_SLOTS_BY_INTENT        各意图进入检索前必须补齐的槽位
- TICKET_REQUIRED_SLOTS           生成报修工单的最低槽位

多轮会话
- ExtractedSlots / ConversationTurn / StoreSessionState

溯源与报修
- SopCitation / RepairTicketDraft / TicketReceipt

大模型端口传输（ports.ChatModelPort 使用）
- ChatMessage / ToolSpec / ToolCallDecision / ChatModelResult[T]
- IntentSlotExtraction / GroundedAnswerDraft   （结构化输出 schema）

对话传输
- ToolTrace / StoreAgentTurnRequest / StoreAgentTurnPayload（HTTP 体）/ StoreAgentTurnResponse

运行参数与内部结果
- StoreAgentConfig / AgentLoopOutcome

复用（只读，不得复制或改写）：
- data_platform.retrieval_engine.models.UserContext / RetrievedParentChunk / DegradedReason
- eval_platform.models.TokenUsage

身份铁律：UserContext 只能来自可信网关 / 评测 AgentInvocation，绝不能由 LLM 结构化输出产生；
因此所有 LLM 结构化输出模型均 extra="forbid" 且不含任何身份字段。

兼容性要求：公共类名与模型字段保持稳定，变更需同步调用方与契约测试。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_platform.retrieval_engine.models import (
    DegradedReason,
    RetrievedParentChunk,
    UserContext,
)
from eval_platform.models import TokenUsage

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class StoreIntent(str, enum.Enum):
    """运营侧提问意图。"""

    DRINK_SOP = "drink_sop"  # 商品规格参数 / 售后规则（严禁幻觉，数值必须逐字来自上下文）
    EQUIPMENT_FAULT = "equipment_fault"  # 设备故障排查（缺信息先追问，失败转报修）
    STORE_OPS_GENERAL = "store_ops_general"  # 开仓检查、交接、质量安全等通用电商运营
    OUT_OF_SCOPE = "out_of_scope"  # 跨部门 / 越权领域（审批矩阵、他人账户、合同价格、薪酬等）
    SMALL_TALK = "small_talk"  # 寒暄 / 与业务无关，不检索


class SlotName(str, enum.Enum):
    """可提取槽位（与 ExtractedSlots 字段名一一对应）。"""

    EQUIPMENT_MODEL = "equipment_model"  # 设备类型 / 型号，如"热敏打单机"、"PRT-XX 系列"
    EQUIPMENT_ID = "equipment_id"  # 设备编号，如 PRT-008（报修必填，可在追问后补录）
    FAULT_SYMPTOM = "fault_symptom"  # 故障现象，如"不出纸"、"异响或焦味"
    ATTEMPTED_CHECKS = "attempted_checks"  # 现场已执行的检查项
    DRINK_NAME = "drink_name"  # 商品名称，如"蛋白粉随行杯"


REQUIRED_SLOTS_BY_INTENT: dict[StoreIntent, tuple[SlotName, ...]] = {
    StoreIntent.DRINK_SOP: (SlotName.DRINK_NAME,),
    StoreIntent.EQUIPMENT_FAULT: (SlotName.EQUIPMENT_MODEL, SlotName.FAULT_SYMPTOM),
}
"""进入检索前必须补齐的槽位；未列出的意图无必填槽位。缺失即触发追问（受 max_clarification_rounds 限制）。"""

TICKET_REQUIRED_SLOTS: tuple[SlotName, ...] = (SlotName.EQUIPMENT_MODEL, SlotName.FAULT_SYMPTOM)
"""生成 RepairTicketDraft 的最低槽位。EQUIPMENT_ID 缺失时工单仍可提交，但 summary 必须标注"设备编号待现场补录"。"""


class AgentAction(str, enum.Enum):
    """受控 Agent Loop 中 LLM 可选择的动作（白名单；每一步由 agent 按状态裁剪可选集合）。"""

    RETRIEVE = "retrieve"  # 调用数据中台混合检索
    ASK_CLARIFICATION = "ask_clarification"  # 追问缺失槽位
    ANSWER = "answer"  # 基于已检索上下文生成带引用的回答
    ESCALATE = "escalate"  # 排障失败 / 安全隐患，转人工报修
    REFUSE = "refuse"  # 越权 / 超范围 / 无依据，礼貌拒答


class LoopStep(str, enum.Enum):
    """工具调用轨迹中的阶段标识。"""

    INTENT_SLOT = "intent_slot"
    DECIDE = "decide"
    CLARIFY = "clarify"
    RETRIEVE = "retrieve"
    GENERATE = "generate"
    VERIFY_CITATIONS = "verify_citations"
    ESCALATE = "escalate"
    REFUSE = "refuse"
    SUMMARIZE = "summarize"
    PERSIST = "persist"


class StopReason(str, enum.Enum):
    """单轮 Agent Loop 终止原因。"""

    ANSWERED = "answered"  # 正常回答（citations 必须非空）
    CLARIFICATION_REQUESTED = "clarification_requested"  # 本轮以追问结束
    ESCALATED = "escalated"  # 已生成（并提交）报修工单
    REFUSED = "refused"  # 越权 / 超范围 / 无依据拒答
    MAX_STEPS_REACHED = "max_steps_reached"  # 触发 max_steps 防死循环保护，返回安全兜底话术
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"  # 数据中台不可用，返回结构化风险提示（不编造）


class RefusalReason(str, enum.Enum):
    """拒答原因（决定 prompts.render_refusal 的话术分支）。"""

    CROSS_DEPT = "cross_dept"  # 跨部门 / 越权提问
    NO_EVIDENCE = "no_evidence"  # 有权限范围内检索无依据
    OUT_OF_SCOPE = "out_of_scope"  # 非电商运营业务


class ToolCallStatus(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"  # 调用成功但下游降级（如 SearchResponse.degraded=True）
    ERROR = "error"  # 调用失败，error_code / error_hint 必填
    SKIPPED = "skipped"  # 因守卫条件未执行（如超出 max_retrieval_calls）


class TicketSeverity(str, enum.Enum):
    P1_SAFETY = "p1_safety"  # 焦味 / 冒烟 / 漏水接近电源等人身与质量安全风险
    P2_BUSINESS_BLOCKING = "p2_business_blocking"  # 影响出库（如热敏打单机停机）
    P3_GENERAL = "p3_general"


class TicketStatus(str, enum.Enum):
    CREATED = "created"
    DUPLICATE_OPEN = "duplicate_open"  # 同一 idempotency_key 已存在未关闭工单，返回原工单


class ChatRole(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# ---------------------------------------------------------------------------
# 多轮会话状态
# ---------------------------------------------------------------------------


class ExtractedSlots(BaseModel):
    """已提取槽位。合并规则：新值非 None 才覆盖旧值；attempted_checks 去重追加。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    equipment_model: str | None = Field(default=None, max_length=64)
    equipment_id: str | None = Field(default=None, max_length=64)
    fault_symptom: str | None = Field(default=None, max_length=200)
    attempted_checks: list[str] = Field(default_factory=list, max_length=20)
    drink_name: str | None = Field(default=None, max_length=64)


class ConversationTurn(BaseModel):
    """单条对话记录（仅保留最近窗口，更早内容压缩进 conversation_summary）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_index: int = Field(..., ge=1)
    role: ChatRole = Field(..., description="仅允许 USER / ASSISTANT")
    content: str = Field(..., max_length=4000)
    created_at: datetime


class StoreSessionState(BaseModel):
    """多轮对话状态（SessionStorePort 的读写单元）。

    - user_context 在会话创建时绑定；后续任何一轮请求的 user_context 与之不一致必须拒绝
      （SessionOwnershipError），防止会话劫持与越权续聊。
    - state_version 用于乐观并发控制：save 时比对 expected_version，不一致抛 SessionConflictError。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    user_context: UserContext
    intent: StoreIntent | None = None
    slots: ExtractedSlots = Field(default_factory=ExtractedSlots)
    pending_slots: list[SlotName] = Field(default_factory=list, description="上一轮追问、尚待用户补充的槽位")
    conversation_summary: str = Field(default="", max_length=2000, description="历史对话压缩摘要（不含原文引用）")
    recent_turns: list[ConversationTurn] = Field(default_factory=list, description="最近 N 条原文，N=recent_turns_window")
    turn_count: int = Field(default=0, ge=0, description="已完成的用户轮次")
    clarification_rounds: int = Field(default=0, ge=0, description="当前意图下已连续追问的轮数，意图切换时清零")
    escalated_ticket_id: str | None = Field(default=None, description="本会话已提交的工单号；存在时不得重复提交")
    state_version: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# 溯源与报修
# ---------------------------------------------------------------------------


class SopCitation(BaseModel):
    """强制溯源引用。每条引用必须能映射回本轮实际送入生成的某个 RetrievedParentChunk。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(..., max_length=128)
    section_path: str = Field(..., description="标题层级路径，如「电商自营商品与仓配运营手册 > 设备巡检与维护 > 热敏打单机故障排查表」")
    version: int = Field(..., ge=1, description="引用时的 current_version，便于发现引用过期")
    parent_id: str = Field(..., max_length=192)
    page_number: int | None = Field(default=None, ge=1)
    quote: str = Field(default="", max_length=200, description="支撑回答的原文短摘录，必须是 full_content 的子串")


class RepairTicketDraft(BaseModel):
    """设备排障失败 / 安全隐患时转人工运维与报修的工单草稿。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(..., min_length=1, max_length=256, description="{session_id}:{equipment_id or equipment_model}:{fault_symptom}")
    session_id: str = Field(..., max_length=128)
    reporter_user_id: str = Field(..., max_length=128, description="取自 UserContext.user_id，不得取自 LLM 输出")
    dept_id: str = Field(..., max_length=64)
    equipment_model: str = Field(..., min_length=1, max_length=64)
    equipment_id: str | None = Field(default=None, max_length=64)
    fault_symptom: str = Field(..., min_length=1, max_length=200)
    attempted_checks: list[str] = Field(default_factory=list)
    severity: TicketSeverity
    safety_hazard: bool = False
    immediate_actions: list[str] = Field(default_factory=list, description="SOP 要求现场立即执行的措施，如断电、贴停用标识")
    summary: str = Field(..., max_length=1000, description="给运维主管 / 维保的故障摘要")
    sop_citations: list[SopCitation] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def _check_severity(self) -> RepairTicketDraft:
        if self.safety_hazard and self.severity is not TicketSeverity.P1_SAFETY:
            raise ValueError("safety_hazard=True 的工单 severity 必须为 P1_SAFETY")
        return self


class TicketReceipt(BaseModel):
    """TicketEscalationPort.submit 的回执。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1, max_length=128)
    status: TicketStatus
    assignee_role: str | None = Field(default=None, description="如 仓配运维主管 / 授权维保")
    created_at: datetime


# ---------------------------------------------------------------------------
# 大模型端口传输
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ChatRole
    content: str
    name: str | None = Field(default=None, description="TOOL 消息对应的工具名")


class ToolSpec(BaseModel):
    """暴露给模型的可选工具 / 动作描述（JSON Schema 参数）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    parameters_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolCallDecision(BaseModel):
    """模型的工具调用决策。tool_name 必须属于本步传入的 tools，否则 agent 视为非法决策。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500, description="决策理由，进入 tool_traces，不展示给运营用户")


class ChatModelResult(BaseModel, Generic[T]):
    """ChatModelPort 统一返回包装：结构化结果 + token 消耗 + 模型标识。"""

    output: T
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_name: str = Field(default="", description="实际调用的模型 ID，进入轨迹审计")


class IntentSlotExtraction(BaseModel):
    """意图与槽位判定的结构化输出 schema（只含本轮新识别信息，合并由 agent 负责）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: StoreIntent
    confidence: float = Field(..., ge=0, le=1)
    slots: ExtractedSlots = Field(default_factory=ExtractedSlots)
    safety_hazard_suspected: bool = Field(default=False, description="焦味 / 冒烟 / 漏电 / 漏水近电源等")
    troubleshooting_failed: bool = Field(default=False, description="用户明确表示已按 SOP 检查仍未解决")
    rewritten_query: str | None = Field(default=None, max_length=500, description="结合槽位改写后的独立检索问题")


class GroundedAnswerDraft(BaseModel):
    """引用核验前的回答草稿 schema。cited_context_indices 为 1-based，对应 render_context_block 的编号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(..., max_length=4000)
    cited_context_indices: list[int] = Field(default_factory=list)
    insufficient_evidence: bool = Field(default=False, description="上下文不足以回答时模型必须置 True，而非编造")


# ---------------------------------------------------------------------------
# 对话传输
# ---------------------------------------------------------------------------


class ToolTrace(BaseModel):
    """单步工具调用轨迹（审计 + 评测归因）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_index: int = Field(..., ge=1, description="本轮内第几步，<= max_steps")
    step: LoopStep
    tool_name: str = Field(..., description="如 chat_model.generate_structured / knowledge.search / ticket.submit")
    status: ToolCallStatus
    latency_ms: float = Field(..., ge=0)
    input_summary: str = Field(default="", max_length=500, description="脱敏摘要，不含完整上下文与身份信息")
    output_summary: str = Field(default="", max_length=500)
    error_code: str | None = None
    error_hint: str | None = Field(default=None, description="结构化修正提示（ToolErrorResponse.hint 或 to_hint()）")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class StoreAgentTurnRequest(BaseModel):
    """单轮对话请求。

    user_context 在请求体中仅供内网 / 工作流调试；生产由 api.get_trusted_user_context 从网关头注入并覆盖。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$", description="None 表示新建会话")
    message: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext
    trace_id: str | None = Field(default=None, max_length=256)


class StoreAgentTurnPayload(BaseModel):
    """HTTP 请求体（api.py 使用）。user_context 可省略：生产由网关头注入，
    路由层据此构造 StoreAgentTurnRequest 后再调用 handle_turn。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$", description="None 表示新建会话")
    message: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext | None = Field(default=None, description="仅内网调试使用；存在网关头时被覆盖")
    trace_id: str | None = Field(default=None, max_length=256)


class StoreAgentTurnResponse(BaseModel):
    """单轮对话响应。"""

    session_id: str
    turn_index: int = Field(..., ge=1)
    answer: str = Field(..., description="回答正文 / 追问问题 / 拒答话术 / 报修告知")
    citations: list[SopCitation] = Field(default_factory=list, description="核验通过的溯源引用")
    needs_clarification: bool = False
    clarification_slots: list[SlotName] = Field(default_factory=list)
    escalation_ticket: RepairTicketDraft | None = None
    ticket_receipt: TicketReceipt | None = None
    refused: bool = False
    degraded: bool = False
    degraded_reasons: list[DegradedReason] = Field(default_factory=list)
    stop_reason: StopReason
    steps_used: int = Field(..., ge=0)
    tool_traces: list[ToolTrace] = Field(default_factory=list)
    latency_ms: float = Field(..., ge=0)
    token_cost: int = Field(default=0, ge=0, description="被测系统 token 总消耗，口径同 SampleEvalResult.token_cost")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    prompt_version: str = Field(default="", description="prompts.PROMPT_VERSION，用于回归评测归因")

    @model_validator(mode="after")
    def _check_invariants(self) -> StoreAgentTurnResponse:
        if self.needs_clarification != (self.stop_reason is StopReason.CLARIFICATION_REQUESTED):
            raise ValueError("needs_clarification 必须等价于 stop_reason == CLARIFICATION_REQUESTED")
        if self.needs_clarification != bool(self.clarification_slots):
            raise ValueError("needs_clarification=True 时 clarification_slots 必须非空，反之必须为空")
        if self.needs_clarification and self.escalation_ticket is not None:
            raise ValueError("追问澄清与报修工单不得同时出现")
        if (self.stop_reason is StopReason.ESCALATED) != (self.escalation_ticket is not None):
            raise ValueError("escalation_ticket 非空必须等价于 stop_reason == ESCALATED")
        if self.ticket_receipt is not None and self.escalation_ticket is None:
            raise ValueError("ticket_receipt 必须伴随 escalation_ticket")
        if self.stop_reason is StopReason.ANSWERED and not self.citations:
            raise ValueError("ANSWERED 必须携带至少 1 条 SopCitation（强制溯源）")
        if self.refused != (self.stop_reason is StopReason.REFUSED):
            raise ValueError("refused 必须等价于 stop_reason == REFUSED")
        if self.token_cost != self.token_usage.total_tokens:
            raise ValueError("token_cost 必须等于 token_usage.total_tokens")
        return self


# ---------------------------------------------------------------------------
# 运行参数与内部结果
# ---------------------------------------------------------------------------


class StoreAgentConfig(BaseModel):
    """StoreSopAgent 运行参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = Field(default=8, ge=3, le=20, description="单轮 Agent Loop 步数上限（每次模型/工具调用计 1 步）")
    max_clarification_rounds: int = Field(default=2, ge=0, le=5, description="同一意图连续追问上限，超出后按兜底策略处理")
    max_retrieval_calls: int = Field(default=2, ge=1, le=5, description="单轮检索调用上限")
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    enable_rerank: bool = True
    intent_confidence_threshold: float = Field(default=0.6, ge=0, le=1, description="低于阈值视为意图不明，走追问")
    recent_turns_window: int = Field(default=6, ge=2, le=50)
    summary_trigger_turns: int = Field(default=10, ge=2, description="turn_count 达到该值的倍数时压缩摘要")
    model_timeout_s: float = Field(default=15.0, gt=0)
    retrieval_timeout_s: float = Field(default=5.0, gt=0)
    ticket_timeout_s: float = Field(default=5.0, gt=0)
    session_ttl_s: int = Field(default=7200, ge=60)


class AgentLoopOutcome(BaseModel):
    """_run_loop 的内部结果：handle_turn 取 response，invoke 额外取 generation_contexts 映射为 AgentResponse。"""

    response: StoreAgentTurnResponse
    generation_contexts: list[RetrievedParentChunk] = Field(
        default_factory=list, description="本轮实际送入生成阶段的上下文（按排名），即 AgentResponse.retrieved_contexts"
    )
    final_state: StoreSessionState
