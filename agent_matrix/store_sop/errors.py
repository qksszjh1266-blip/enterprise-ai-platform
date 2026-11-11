"""门店 SOP 智能体异常体系。

风格与 data_platform.retrieval_engine.errors 一致：每个异常携带稳定 code、retryable 与 suggestion，
to_hint() 输出可直接回灌给 LLM / Dify 工作流的结构化修正提示。

    异常                          HTTP   error_code               retryable
    SessionNotFoundError          404    SESSION_NOT_FOUND        False
    SessionOwnershipError         403    SESSION_FORBIDDEN        False
    SessionConflictError          409    SESSION_CONFLICT         True
    SessionStoreUnavailableError  503    SESSION_STORE_UNAVAILABLE True
    ChatModelUnavailableError     503    CHAT_MODEL_UNAVAILABLE   True
    StructuredOutputError         502    STRUCTURED_OUTPUT_INVALID True
    TicketEscalationError         503    TICKET_ESCALATION_FAILED True
    AgentNotInitializedError      503    AGENT_NOT_INITIALIZED    True
    UnsupportedScenarioError      400    UNSUPPORTED_SCENARIO     False

注意：数据中台 RetrievalError 不在此重复定义；agent 内部捕获后转为 ToolTrace(status=ERROR) 与
StopReason.RETRIEVAL_UNAVAILABLE 的风险提示回答，不向上抛出。
"""

from __future__ import annotations


class StoreAgentError(Exception):
    """门店 SOP 智能体异常基类。"""

    code: str = "STORE_AGENT_ERROR"
    retryable: bool = False
    suggestion: str = "请检查请求参数后重试。"

    def __init__(self, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.session_id = session_id

    def to_hint(self) -> str:
        """输出固定格式：`[{code}] session_id={session_id or '-'} | {message} | 建议: {suggestion}`。"""
        return f"[{self.code}] session_id={self.session_id or '-'} | {self.message} | 建议: {self.suggestion}"


class SessionNotFoundError(StoreAgentError):
    code = "SESSION_NOT_FOUND"
    suggestion = "会话已过期或不存在，请不传 session_id 新建会话后重新描述问题。"


class SessionOwnershipError(StoreAgentError):
    code = "SESSION_FORBIDDEN"
    suggestion = "该会话不属于当前身份，请使用本人会话或新建会话。"


class SessionConflictError(StoreAgentError):
    code = "SESSION_CONFLICT"
    retryable = True
    suggestion = "同一会话存在并发请求，请等待上一轮回复后再发送。"


class SessionStoreUnavailableError(StoreAgentError):
    code = "SESSION_STORE_UNAVAILABLE"
    retryable = True
    suggestion = "会话存储暂不可用，请稍后重试。"


class ChatModelUnavailableError(StoreAgentError):
    code = "CHAT_MODEL_UNAVAILABLE"
    retryable = True
    suggestion = "大模型服务暂不可用，请稍后重试；紧急设备故障请直接联系区域督导。"


class StructuredOutputError(StoreAgentError):
    code = "STRUCTURED_OUTPUT_INVALID"
    retryable = True
    suggestion = "模型输出不符合约定 schema，已终止本轮，请重试。"


class TicketEscalationError(StoreAgentError):
    code = "TICKET_ESCALATION_FAILED"
    retryable = True
    suggestion = "报修工单提交失败，请稍后重试或电话联系区域督导，并记录设备编号。"


class AgentNotInitializedError(StoreAgentError):
    code = "AGENT_NOT_INITIALIZED"
    retryable = True
    suggestion = "智能体服务正在启动或未完成装配，请稍后重试。"


class UnsupportedScenarioError(StoreAgentError):
    code = "UNSUPPORTED_SCENARIO"
    suggestion = "StoreSopAgent 仅支持 EvalScenario.STORE_SOP，请检查评测样本 scenario。"
