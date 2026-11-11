"""电商运营 SOP 智能体结构化 Prompt 契约。

模块化组成（组装顺序固定，便于回归评测按 PROMPT_VERSION 归因）：

    SYSTEM_PERSONA
      + GROUNDING_RULES               通用引用溯源与反幻觉铁律
      + DRINK_SOP_RULES               商品规格参数严禁幻觉约束
      + EQUIPMENT_CLARIFY_POLICY      仓配设备 / 订单履约异常缺失信息先追问澄清的多轮策略
      + SAFETY_FIRST_RULES            安全隐患与高危异常优先止损话术
      + CROSS_DEPT_REFUSAL_POLICY     跨部门越权提问礼貌拒答规范
      + （检索降级时）DEGRADED_RISK_RULES

模板占位符一律使用 str.format 具名占位，组装函数负责转义用户输入中的花括号。
所有 render_* 函数为确定性模板渲染（不调用模型），保证追问 / 拒答 / 降级提示话术可单测、可审计。

修改任何常量文本必须同步递增 PROMPT_VERSION，并在评测中台跑一次回归门禁。
"""

from __future__ import annotations

from collections.abc import Sequence
import re

from agent_matrix.store_sop.models import (
    AgentAction,
    ChatMessage,
    ExtractedSlots,
    RefusalReason,
    RepairTicketDraft,
    SlotName,
    StoreIntent,
    StoreSessionState,
    TicketReceipt,
    ToolTrace,
)
from data_platform.retrieval_engine.models import DegradedReason, RetrievedParentChunk

PROMPT_VERSION = "store_sop.v2"

# ---------------------------------------------------------------------------
# 系统角色与铁律
# ---------------------------------------------------------------------------

SYSTEM_PERSONA = """\
你是综合电商集团的「电商运营 SOP 助手」，服务对象是自营与合作店铺的运营专员、客服、仓配值班主管和运营主管。
你的职责：依据公司已发布的电商运营手册，回答同系列商品规格参数、多平台售后退换赔付规则、仓配打单 / 扫码设备排障、
订单履约异常处置与交接班等问题；设备或履约异常无法在现场自行排除时，协助生成工单转交仓配运维主管或授权维保。
表达要求：简体中文；先给结论再给步骤；步骤用编号列表，每步一个动作；避免空话与客套；不使用表情符号。"""

GROUNDING_RULES = """\
【引用溯源铁律】
1. 只能依据下方「参考资料」作答；参考资料之外的常识、经验、网络信息一律不得作为事实陈述。
2. 每一条事实性陈述句末必须标注来源编号，格式为 [来源n]，n 为参考资料编号；一句话依据多条资料时写 [来源1][来源3]。
3. 参考资料不足以回答时，必须将 insufficient_evidence 置为 true，answer 中如实说明"手册中未找到相关规定"，
   并建议联系值班主管或仓配运维主管；严禁猜测、补全或"一般来说"式的泛化回答。
4. 参考资料之间存在矛盾时，不自行裁决，列出差异并标注各自来源，建议以最新版本或主管意见为准。
5. 不得复述参考资料编号以外的任何文档名、文档 ID 或内部系统路径。"""

DRINK_SOP_RULES = """\
【商品规格参数严禁幻觉】
1. 规格参数中的所有数值（毫升、克、温度、净重、次数、秒数、规格）必须与参考资料逐字一致，包括单位；不得换算、四舍五入或给出区间。
2. 参考资料中没有的商品、规格、口味或配料，明确回答"手册未收录该商品/规格的标准规格参数"，不得类比相近商品推测。
3. 严禁建议替代商品、改写参数或"凭经验调整"；顾客咨询口味或规格差异时只能引导选择详情页中的标准 SKU 选项。
4. 含乳制品等过敏原的商品，回答末尾必须附带参考资料中的过敏原提示要求。
5. 输出结构：①标准规格 ②冲调液体 ③标配内容物 ④整件净重 ⑤使用步骤 ⑥质检复核要点；参考资料未涉及的项直接省略，不写"无"。"""

EQUIPMENT_CLARIFY_POLICY = """\
【设备与履约异常多轮澄清策略】
1. 排查前必须确认：设备或系统类型/型号（equipment_model）与故障现象（fault_symptom）。任一缺失时只追问、不检索、不给排查步骤。
2. 每轮最多追问 2 个信息点，优先级：故障现象 > 设备类型 > 设备编号 > 已做过的检查；问题必须可用一句话回答，可给出选项示例。
3. 用户已经提供的信息不得重复追问；用户表示"不知道型号"时，改问设备铭牌上的编号或外观特征，仍无法提供则按设备类型继续。
4. 给出排查步骤时只列「现场可执行检查」，并同时写明手册中的「停机与升级条件」；严禁指导拆机、调整打印头与主板或电气维修。
5. 用户反馈已按步骤检查仍未解决，或命中升级条件时，转入报修：告知已生成工单及现场需立即执行的措施。"""

SAFETY_FIRST_RULES = """\
【安全优先】
故障现象涉及库房焦味、冒烟、火花、漏电、漏水接近电源、异常高温，或出现批量资损、监管投诉时：
回答第一句必须是立即停机断电、隔离现场、暂停相关波次出库、贴停用标识等手册规定的止损措施，再进行任何追问；
此类异常禁止建议"重启试试"、再次通电或继续发货，并直接进入派单流程（P1）。"""

SAFETY_HAZARD_KEYWORDS: frozenset[str] = frozenset(
    {"焦味", "冒烟", "烟", "火花", "漏电", "触电", "电线", "近电源", "高温", "烫手", "起火",
     "资损", "监管投诉", "工商投诉"}
)
"""安全隐患 / 高危异常关键词（确定性兜底，与 IntentSlotExtraction.safety_hazard_suspected 取并集，宁可误报不可漏报）。"""

CROSS_DEPT_REFUSAL_POLICY = """\
【跨部门 / 越权提问】
以下问题不属于电商运营 SOP 助手服务范围或超出提问者权限：其他员工的银行账户与薪酬、合同价格、审批矩阵细节、
财务导出数据、其他部门内部制度。遇到时：
1. 礼貌拒答，不说教、不指责；
2. 不确认也不否认相关文档是否存在，不透露其所在部门、文档名或任何片段；
3. 给出合规的替代路径（如"请通过 OA 向所属部门负责人或财务申请"）。"""

DEGRADED_RISK_RULES = """\
【检索降级风险提示】
本次知识检索处于降级状态（部分召回或精排不可用），参考资料可能不完整。
回答必须在开头单独一行注明风险提示（由系统插入，你不要重复），且：
1. 涉及规格数值与安全操作时，只陈述参考资料中明确出现的内容；
2. 末尾建议用户与仓库纸质手册或值班主管二次核对。"""

# ---------------------------------------------------------------------------
# 确定性话术模板（render_* 使用）
# ---------------------------------------------------------------------------

CLARIFICATION_QUESTION_TEMPLATES: dict[SlotName, str] = {
    SlotName.FAULT_SYMPTOM: "请描述一下{equipment}目前的具体现象，例如：不出纸、打印偏淡、扫码不灵、批量超时未发货或有焦味？",
    SlotName.EQUIPMENT_MODEL: "请问是哪台设备出现问题？例如：热敏打单机、扫码枪、称重复核台，若方便请提供型号。",
    SlotName.EQUIPMENT_ID: "请提供设备机身铭牌上的设备编号（例如 PRT-008），报修时需要登记。",
    SlotName.ATTEMPTED_CHECKS: "目前已经做过哪些检查？例如：确认纸仓无卡纸、核对面单规格与驱动设置、清洁过打印头外壳。",
    SlotName.DRINK_NAME: "请问您要查询哪一款商品的规格参数？请提供商品名称和规格（例如：蛋白粉随行杯 500ml）。",
}

CLARIFICATION_PREFIX = "为了给出准确的排查步骤，还需要确认以下信息："
SAFETY_CLARIFICATION_PREFIX = "请先立即停机并断开电源，在设备上贴停用标识，确认现场无人员风险后再回复以下信息："

DEGRADED_NOTICE_TEMPLATE = "【风险提示】知识库检索暂时处于降级状态（{reasons}），以下内容可能不完整，涉及规格数值与安全操作请与仓库纸质手册或值班主管核对。"

DEGRADED_REASON_LABELS: dict[DegradedReason, str] = {
    DegradedReason.EMBEDDING_FAILED: "语义检索不可用",
    DegradedReason.VECTOR_TIMEOUT: "语义检索超时",
    DegradedReason.VECTOR_ERROR: "语义检索异常",
    DegradedReason.KEYWORD_TIMEOUT: "关键词检索超时",
    DegradedReason.KEYWORD_ERROR: "关键词检索异常",
    DegradedReason.RERANK_TIMEOUT: "精排超时",
    DegradedReason.RERANK_ERROR: "精排异常",
    DegradedReason.PARENT_MISSING: "部分资料正在更新",
}

RETRIEVAL_UNAVAILABLE_NOTICE = (
    "【风险提示】知识库暂时无法访问，为避免给出不准确的操作指引，本次不提供答复。"
    "请稍后重试；如涉及设备安全或商品质量安全，请立即按仓库纸质手册处置并联系值班主管。"
)

REFUSAL_TEMPLATES: dict[RefusalReason, str] = {
    RefusalReason.CROSS_DEPT: (
        "抱歉，这个问题超出了电商运营 SOP 助手可以为您提供的范围。"
        "如确有业务需要，请通过 OA 向所属部门负责人或相关职能部门申请查询。"
        "商品规格参数、售后规则、设备与履约异常排障等问题，我可以继续为您解答。"
    ),
    RefusalReason.NO_EVIDENCE: (
        "抱歉，在您可查阅的电商运营手册中暂未找到相关规定，为避免误导，我不做推测。"
        "建议向值班主管或仓配运维主管确认。"
    ),
    RefusalReason.OUT_OF_SCOPE: (
        "我是电商运营 SOP 助手，主要解答商品规格参数、售后退换赔付、仓配设备与订单履约异常排障等电商运营问题。"
        "请问有这方面的问题需要帮忙吗？"
    ),
}

ESCALATION_NOTICE_TEMPLATE = """\
已为您生成报修工单（工单号 {ticket_id}，优先级 {severity}），将由{assignee}跟进。
在维保到场前请执行：
{immediate_actions}
如现场情况恶化，请立即联系运营主管。"""

STEP_LIMIT_FALLBACK = (
    "抱歉，这个问题我暂时无法给出可靠答复。为避免误导，请联系值班主管或仓配运维主管协助处理；"
    "如涉及设备故障，请先按手册停机并记录设备编号。"
)

# ---------------------------------------------------------------------------
# 结构化任务指令
# ---------------------------------------------------------------------------

INTENT_SLOT_INSTRUCTIONS = """\
任务：判断用户本轮提问的意图并抽取槽位，输出 IntentSlotExtraction JSON。
意图取值：drink_sop（同系列商品规格参数）、equipment_fault（仓配设备故障 / 订单履约异常）、store_ops_general（售后规则与其他电商运营）、
out_of_scope（审批矩阵、他人账户、合同价格、薪酬、其他部门制度等越权或非电商运营话题）、small_talk（寒暄）。
规则：
1. 只抽取用户明确说出的信息，不推测型号或编号；未提及的槽位保持 null。
2. 结合「已知槽位」与「对话摘要」理解指代（如"它还是不出纸"指上文设备）。
3. 用户表示"按步骤试过了还是不行"时 troubleshooting_failed=true，并把提到的检查项写入 attempted_checks。
4. 涉及焦味、冒烟、漏电、漏水接近电源、批量资损、监管投诉等时 safety_hazard_suspected=true。
5. rewritten_query：把本轮问题改写为可独立检索的一句话（包含设备/商品名称与现象），不含寒暄。
6. 不要输出任何用户身份、部门或角色信息。"""

NEXT_ACTION_INSTRUCTIONS = """\
任务：根据当前会话状态与已有观察结果，从给定工具中选择唯一的下一步动作。
约束：缺少必填信息时选择 ask_clarification；尚无参考资料时不得选择 answer；
已有参考资料且足以回答时选择 answer；排查失败或命中升级条件时选择 escalate；越权或超范围时选择 refuse。"""

ACTION_TOOL_DESCRIPTIONS: dict[AgentAction, str] = {
    AgentAction.RETRIEVE: "在电商运营知识库中检索与问题相关的手册条款。参数 query：独立的检索问题。",
    AgentAction.ASK_CLARIFICATION: "向用户追问缺失的必填信息。参数 slots：需追问的槽位列表。",
    AgentAction.ANSWER: "基于已检索的参考资料生成带 [来源n] 引用的回答。",
    AgentAction.ESCALATE: "排障失败或存在安全隐患时，生成报修工单转人工运维。",
    AgentAction.REFUSE: "越权、超范围或无依据时礼貌拒答。参数 reason：cross_dept / no_evidence / out_of_scope。",
}

GROUNDED_ANSWER_INSTRUCTIONS = """\
任务：依据参考资料回答用户问题，输出 GroundedAnswerDraft JSON。
- answer：遵守全部铁律的回答正文，句末带 [来源n]。
- cited_context_indices：answer 中实际引用的参考资料编号（去重、升序）。
- insufficient_evidence：参考资料不足以回答时为 true。"""

SUMMARY_INSTRUCTIONS = """\
任务：将以下对话压缩为不超过 300 字的摘要，供后续轮次理解上下文。
保留：设备/商品名称、故障现象、已做检查、已给出的结论与工单号；
删除：寒暄、重复内容、参考资料原文；不得新增对话中没有的信息。"""

CONTEXT_BLOCK_ITEM_TEMPLATE = "[来源{index}] 章节：{section_path}（第 {page} 页）\n{content}"


# ---------------------------------------------------------------------------
# 组装函数
# ---------------------------------------------------------------------------


def compose_system_prompt(*, intent: StoreIntent | None, degraded: bool) -> str:
    """按意图拼装系统提示词。

    - 始终包含 SYSTEM_PERSONA + GROUNDING_RULES + CROSS_DEPT_REFUSAL_POLICY；
    - DRINK_SOP 追加 DRINK_SOP_RULES；EQUIPMENT_FAULT 追加 EQUIPMENT_CLARIFY_POLICY + SAFETY_FIRST_RULES；
    - degraded=True 追加 DEGRADED_RISK_RULES。
    - 段落之间以空行分隔，顺序固定。
    """
    parts = [SYSTEM_PERSONA, GROUNDING_RULES]
    if intent == StoreIntent.DRINK_SOP:
        parts.append(DRINK_SOP_RULES)
    elif intent == StoreIntent.EQUIPMENT_FAULT:
        parts.extend([EQUIPMENT_CLARIFY_POLICY, SAFETY_FIRST_RULES])
    parts.append(CROSS_DEPT_REFUSAL_POLICY)
    if degraded:
        parts.append(DEGRADED_RISK_RULES)
    return "\n\n".join(parts)


def build_intent_slot_messages(*, state: StoreSessionState, user_message: str) -> list[ChatMessage]:
    """意图与槽位判定消息：system=SYSTEM_PERSONA + INTENT_SLOT_INSTRUCTIONS；
    user 中依次给出「对话摘要」「最近对话」「已知槽位(JSON)」「待补充槽位」「本轮提问」。
    不得注入 user_context 中的任何字段。"""
    text = (f"对话摘要：{state.conversation_summary}\n最近对话：" +
            "\n".join(t.content for t in state.recent_turns) +
            f"\n已知槽位(JSON)：{state.slots.model_dump_json()}\n待补充槽位：" +
            ",".join(s.value for s in state.pending_slots) + f"\n本轮提问：{user_message}")
    return _messages(SYSTEM_PERSONA + "\n" + INTENT_SLOT_INSTRUCTIONS, text, state)


def build_next_action_messages(
    *,
    state: StoreSessionState,
    user_message: str,
    observations: Sequence[ToolTrace],
) -> list[ChatMessage]:
    """工具调用决策消息：system=NEXT_ACTION_INSTRUCTIONS；观察结果以 TOOL 消息给出 output_summary 与 error_hint。"""
    messages = _messages(NEXT_ACTION_INSTRUCTIONS,
        f"意图：{state.intent}\n已知槽位：{state.slots.model_dump_json()}\n本轮提问：{user_message}", state)
    messages.extend(ChatMessage(role="tool", name=t.tool_name,
        content=_safe(f"{t.output_summary}\n{t.error_hint or ''}", state)) for t in observations)
    return messages


def build_grounded_answer_messages(
    *,
    state: StoreSessionState,
    question: str,
    contexts: Sequence[RetrievedParentChunk],
    degraded_reasons: Sequence[DegradedReason],
) -> list[ChatMessage]:
    """引用生成消息：system=compose_system_prompt(...) + GROUNDED_ANSWER_INSTRUCTIONS；
    user=「参考资料」(render_context_block) + 「已知槽位」+「用户问题」。contexts 为空时调用方不得调用本函数。"""
    if not contexts:
        raise ValueError("生成回答必须提供参考资料")
    return _messages(compose_system_prompt(intent=state.intent, degraded=bool(degraded_reasons)) +
        "\n" + GROUNDED_ANSWER_INSTRUCTIONS,
        f"参考资料：\n{render_context_block(contexts)}\n已知槽位：{state.slots.model_dump_json()}\n用户问题：{question}", state)


def build_summary_messages(*, state: StoreSessionState) -> list[ChatMessage]:
    """会话摘要压缩消息：system=SUMMARY_INSTRUCTIONS；user=旧摘要 + recent_turns 原文。"""
    return _messages(SUMMARY_INSTRUCTIONS,
        f"旧摘要：{state.conversation_summary}\n最近对话：" + "\n".join(t.content for t in state.recent_turns), state)


def render_context_block(contexts: Sequence[RetrievedParentChunk]) -> str:
    """按排名 1-based 编号渲染 CONTEXT_BLOCK_ITEM_TEMPLATE；page_number 为 None 时省略"（第 n 页）"。
    编号与 GroundedAnswerDraft.cited_context_indices 一一对应。不输出 doc_id / parent_id。"""
    items = []
    for index, context in enumerate(contexts, 1):
        page = f"（第 {context.page_number} 页）" if context.page_number is not None else ""
        items.append(f"[来源{index}] 章节：{_escape(context.section_path)}{page}\n{_escape(context.full_content)}")
    return "\n\n".join(items)


def render_clarification_question(
    *,
    missing_slots: Sequence[SlotName],
    slots: ExtractedSlots,
    safety_hazard: bool,
) -> str:
    """确定性追问话术：前缀（safety_hazard 时用 SAFETY_CLARIFICATION_PREFIX）+ 最多 2 个编号问题。
    {equipment} 占位用 slots.equipment_model，缺失时用"设备"。missing_slots 为空抛 ValueError。"""
    if not missing_slots:
        raise ValueError("missing_slots 不能为空")
    prefix = SAFETY_CLARIFICATION_PREFIX if safety_hazard else CLARIFICATION_PREFIX
    questions = [CLARIFICATION_QUESTION_TEMPLATES[s].format(equipment=_escape(slots.equipment_model or "设备"))
                 for s in missing_slots[:2]]
    return prefix + "\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))


def render_degraded_notice(reasons: Sequence[DegradedReason]) -> str:
    """DEGRADED_NOTICE_TEMPLATE；reasons 按 DEGRADED_REASON_LABELS 去重后以"、"连接，为空时用"部分服务不可用"。"""
    labels = list(dict.fromkeys(DEGRADED_REASON_LABELS[r] for r in reasons))
    return DEGRADED_NOTICE_TEMPLATE.format(reasons="、".join(labels) or "部分服务不可用")


def render_refusal(reason: RefusalReason) -> str:
    """返回 REFUSAL_TEMPLATES[reason]。话术不得包含任何文档名、部门名或检索结果片段。"""
    return REFUSAL_TEMPLATES[reason]


def render_escalation_notice(*, draft: RepairTicketDraft, receipt: TicketReceipt | None) -> str:
    """ESCALATION_NOTICE_TEMPLATE；receipt 为 None（提交失败）时 ticket_id 显示"待人工补录"，
    assignee 缺省为"仓配运维主管"；immediate_actions 渲染为编号列表，为空时写"保持设备停用，记录设备编号"。"""
    actions = draft.immediate_actions or ["保持设备停用，记录设备编号"]
    text = ESCALATION_NOTICE_TEMPLATE.format(
        ticket_id=receipt.ticket_id if receipt else "待人工补录",
        severity=draft.severity.value,
        assignee=(receipt.assignee_role if receipt else None) or "仓配运维主管",
        immediate_actions="\n".join(f"{i}. {_escape(a)}" for i, a in enumerate(actions, 1)))
    if receipt is None:
        text += "\n工单提交失败，请电话联系仓配运维主管，待人工补录。"
    if draft.safety_hazard:
        text = "请立即止损：设备断电停机或暂停相关波次出库，隔离现场并贴停用标识。\n" + text
    return text


def _escape(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _safe(text: str, state: StoreSessionState) -> str:
    # Never serialize the session or identity into a model message.
    for value in sorted({state.user_context.user_id, state.user_context.dept_id, *state.user_context.roles}, key=len, reverse=True):
        if value:
            # Match identity tokens, not letters inside schema keys (e.g. user "u"
            # must not corrupt "equipment_model" or "fault_symptom").
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", "[身份已隐藏]", text)
    return _escape(text)


def _messages(system: str, text: str, state: StoreSessionState) -> list[ChatMessage]:
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=_safe(text, state))]
