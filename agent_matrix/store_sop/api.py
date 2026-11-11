"""电商运营 SOP 智能体 FastAPI 对话路由。

    POST   /api/v1/agents/store-sop/turns                 -> StoreSopAgent.handle_turn
    GET    /api/v1/agents/store-sop/sessions/{session_id} -> 读取本人会话状态（调试 / 工作流回显）
    DELETE /api/v1/agents/store-sop/sessions/{session_id} -> 结束本人会话

身份注入：生产环境由网关注入 `X-User-Id` / `X-Dept-Id` / `X-User-Roles`（逗号分隔）三个头，
get_trusted_user_context 据此构造 UserContext 并**覆盖** StoreAgentTurnPayload.user_context；三个头都缺失时
仅在 app.state.allow_body_identity=True（内网调试）下回退使用请求体，否则 401。

工具错误处理约定：任何异常都转为 data_platform ToolErrorResponse，禁止裸抛 500。

    异常                         HTTP   error_code                  retryable
    RequestValidationError       422    INVALID_ARGUMENT            False
    身份头缺失 / 非法             401    UNAUTHENTICATED             False
    StoreAgentError 子类         见 errors.py 模块文档映射表      {exc.retryable}
    未预期 Exception             503    INTERNAL_ERROR              True   (日志记录堆栈，响应不泄漏)

追问 / 拒答 / 报修 / 检索降级 / 数据中台不可用均为 HTTP 200 的正常 StoreAgentTurnResponse。

注意：本模块不使用 `from __future__ import annotations`，避免 FastAPI 依赖解析时的前向引用问题。
"""

import logging

from fastapi import APIRouter, FastAPI, Request
from pydantic import ValidationError
from agent_matrix.store_sop.errors import (AgentNotInitializedError, SessionNotFoundError, SessionOwnershipError, SessionStoreUnavailableError)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_matrix.store_sop.agent import StoreSopAgent
from agent_matrix.store_sop.errors import StoreAgentError
from agent_matrix.store_sop.models import (
    StoreAgentTurnPayload,
    StoreAgentTurnRequest,
    StoreAgentTurnResponse,
    StoreSessionState,
)
from data_platform.retrieval_engine.models import ToolErrorResponse, UserContext

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/agents/store-sop"

ERROR_STATUS_BY_CODE: dict[str, int] = {
    "SESSION_NOT_FOUND": 404,
    "SESSION_FORBIDDEN": 403,
    "SESSION_CONFLICT": 409,
    "SESSION_STORE_UNAVAILABLE": 503,
    "CHAT_MODEL_UNAVAILABLE": 503,
    "STRUCTURED_OUTPUT_INVALID": 502,
    "TICKET_ESCALATION_FAILED": 503,
    "AGENT_NOT_INITIALIZED": 503,
    "UNSUPPORTED_SCENARIO": 400,
}
"""StoreAgentError.code → HTTP 状态码；未登记的 code 一律 400。"""

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ToolErrorResponse, "description": "缺少可信身份头"},
    403: {"model": ToolErrorResponse, "description": "会话不属于当前身份"},
    404: {"model": ToolErrorResponse, "description": "会话不存在或已过期，按 hint 新建会话"},
    409: {"model": ToolErrorResponse, "description": "同一会话并发请求，稍后重试"},
    422: {"model": ToolErrorResponse, "description": "请求体校验失败，details 列出字段级错误"},
    502: {"model": ToolErrorResponse, "description": "模型结构化输出非法，可重试"},
    503: {"model": ToolErrorResponse, "description": "下游依赖暂不可用，retryable=True 时可稍后重试"},
}

router = APIRouter(prefix=API_PREFIX, tags=["store-sop-agent"])


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------


def get_store_sop_agent(request: Request) -> StoreSopAgent:
    """从 request.app.state.store_sop_agent 取出共享实例；为 None 时抛 AgentNotInitializedError（→ 503 结构化）。"""
    agent = getattr(request.app.state, "store_sop_agent", None)
    if agent is None:
        raise AgentNotInitializedError("智能体尚未初始化")
    return agent


def get_trusted_user_context(request: Request, body_user_context: UserContext | None) -> UserContext:
    """按模块文档规则从网关头构造 UserContext；头部与请求体同时存在时以头部为准。"""
    values = [request.headers.get(k) for k in ("X-User-Id", "X-Dept-Id", "X-User-Roles")]
    if all(value is not None for value in values):
        try:
            roles = [r.strip() for r in values[2].split(",")]
            if not all(roles):
                raise ValueError("empty role")
            return UserContext(user_id=values[0], dept_id=values[1], roles=roles)
        except (ValidationError, ValueError):
            pass
    elif all(value is None for value in values) and request.app.state.allow_body_identity and body_user_context is not None:
        return body_user_context
    raise _UnauthenticatedError("缺少或无效的可信身份头")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post(
    "/turns",
    operation_id="store_sop_chat_turn",
    summary="电商运营 SOP 助手单轮对话",
    description=(
        "多轮对话：首轮不传 session_id，后续轮次回传上一轮响应中的 session_id。"
        "needs_clarification=true 时 answer 为追问问题；escalation_ticket 非空表示已转人工报修；"
        "citations 为强制溯源引用。"
    ),
    response_model=StoreAgentTurnResponse,
    responses=_ERROR_RESPONSES,
)
async def chat_turn(body: StoreAgentTurnPayload, request: Request) -> StoreAgentTurnResponse:
    """user_context = get_trusted_user_context(request, body.user_context)；
    构造 StoreAgentTurnRequest(session_id, message, user_context, trace_id) 后调用 StoreSopAgent.handle_turn。"""
    user = get_trusted_user_context(request, body.user_context)
    return await get_store_sop_agent(request).handle_turn(StoreAgentTurnRequest(
        session_id=body.session_id, message=body.message, user_context=user, trace_id=body.trace_id))


@router.get(
    "/sessions/{session_id}",
    operation_id="store_sop_get_session",
    summary="读取本人会话状态",
    response_model=StoreSessionState,
    responses=_ERROR_RESPONSES,
)
async def get_session(session_id: str, request: Request) -> StoreSessionState:
    """读取会话；不存在 → SessionNotFoundError，非本人 → SessionOwnershipError。"""
    user = get_trusted_user_context(request, None)
    agent = get_store_sop_agent(request)
    state = await _load_session(agent, session_id)
    if state is None:
        raise SessionNotFoundError("会话不存在", session_id=session_id)
    if state.user_context != user:
        raise SessionOwnershipError("会话身份不匹配", session_id=session_id)
    return state


@router.delete(
    "/sessions/{session_id}",
    operation_id="store_sop_end_session",
    summary="结束本人会话",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
async def end_session(session_id: str, request: Request) -> None:
    """校验归属后删除会话；不存在时幂等返回 204。"""
    user = get_trusted_user_context(request, None)
    agent = get_store_sop_agent(request)
    state = await _load_session(agent, session_id)
    if state is None:
        return
    if state.user_context != user:
        raise SessionOwnershipError("会话身份不匹配", session_id=session_id)
    try:
        await agent.session_store.delete(session_id)
    except StoreAgentError:
        raise
    except Exception as exc:
        raise SessionStoreUnavailableError("删除会话失败", session_id=session_id) from exc


# ---------------------------------------------------------------------------
# 异常处理器（全部返回 ToolErrorResponse）
# ---------------------------------------------------------------------------


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 INVALID_ARGUMENT；details=[{"loc": "body.message", "msg": ...}]，hint 列出需修正字段。"""
    details = [{"loc": ".".join(map(str, e["loc"])), "msg": e["msg"]} for e in exc.errors()]
    body = ToolErrorResponse(error_code="INVALID_ARGUMENT", message="请求参数校验失败",
        hint="请修正以下字段后重试: " + ", ".join(d["loc"] for d in details), details=details)
    return JSONResponse(status_code=422, content=body.model_dump())


async def handle_store_agent_error(request: Request, exc: StoreAgentError) -> JSONResponse:
    """按 ERROR_STATUS_BY_CODE 映射状态码；error_code=exc.code，hint=exc.to_hint()，retryable=exc.retryable。"""
    body = ToolErrorResponse(error_code=exc.code, message=exc.message, hint=exc.to_hint(), retryable=exc.retryable)
    return JSONResponse(status_code=ERROR_STATUS_BY_CODE.get(exc.code, 400), content=body.model_dump())


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """503 INTERNAL_ERROR；logger.exception 记录；响应体不含异常消息、堆栈、类名与文件路径。"""
    logger.exception("智能体服务发生未预期异常", exc_info=(type(exc), exc, exc.__traceback__))
    body = ToolErrorResponse(error_code="INTERNAL_ERROR", message="服务暂时不可用",
        hint="请稍后重试；如持续失败请联系管理员。", retryable=True)
    return JSONResponse(status_code=503, content=body.model_dump())


def create_app(agent: StoreSopAgent | None = None, *, allow_body_identity: bool = False) -> FastAPI:
    """装配应用：挂载 router、注册三个异常处理器、写入 app.state。

    agent 为 None 时仍可调用 create_app().openapi() 导出 Schema（供 Dify / HiAgent 导入）。
    """
    app = FastAPI(
        title="Store SOP Agent API",
        version="0.3.0",
        description="电商运营 SOP 助手：商品规格参数 / 设备排障多轮问答，强制溯源引用，排障失败转人工报修。",
    )
    app.state.store_sop_agent = agent
    app.state.allow_body_identity = allow_body_identity
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StoreAgentError, handle_store_agent_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


class _UnauthenticatedError(StoreAgentError):
    code = "UNAUTHENTICATED"
    suggestion = "请通过登录网关提供完整有效的身份头。"


ERROR_STATUS_BY_CODE["UNAUTHENTICATED"] = 401


async def _load_session(agent: StoreSopAgent, session_id: str) -> StoreSessionState | None:
    try:
        return await agent.session_store.load(session_id)
    except StoreAgentError:
        raise
    except Exception as exc:
        raise SessionStoreUnavailableError("读取会话失败", session_id=session_id) from exc
