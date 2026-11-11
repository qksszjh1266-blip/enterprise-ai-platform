"""供应链 BI 智能体 FastAPI 分析路由。

    POST /api/v1/agents/supply-chain-bi/analyze -> SupplyChainBIAgent.analyze
    GET  /api/v1/agents/supply-chain-bi/catalog -> SemanticLayer.public_catalog()（脱敏语义字典，供 Workflow / 前端展示口径）

刻意**不**暴露的端点：
- 裸 SQL 执行 / SQL dry-run 校验：守卫与行级范围只能在服务端闭环内生效；dry-run 会形成
  "敏感列 vs 不存在列"的存在性探测预言机（SENSITIVE_COLUMN 与 COLUMN_NOT_WHITELISTED 可区分）。

身份注入：与 门店 SOP store_sop.api 相同——网关注入 `X-User-Id` / `X-Dept-Id` / `X-User-Roles`（逗号分隔），
get_trusted_user_context 据此构造 UserContext 并**覆盖** SupplyChainQueryPayload.user_context；三个头都缺失时
仅在 app.state.allow_body_identity=True（内网调试）下回退使用请求体，否则 401。行级范围（WarehouseScopeFilter）
永远由 WarehouseScopeResolverPort 基于 UserContext 解析，请求体与头部均不得直接携带 region / warehouse 范围。

工具错误处理约定：任何异常都转为 data_platform ToolErrorResponse，禁止裸抛 500。

    异常                              HTTP   error_code                 retryable
    RequestValidationError            422    INVALID_ARGUMENT           False
    身份头缺失 / 非法                   401    UNAUTHENTICATED            False
    SupplyChainAgentError 子类         见 errors.py 模块文档映射表      {exc.retryable}
    未预期 Exception                   503    INTERNAL_ERROR             True   (日志记录堆栈，响应不泄漏)

追问 / 拒答 / SQL 重试耗尽 / 数据源不可用均为 HTTP 200 的正常 SupplyChainQueryResponse。

注意：本模块不使用 `from __future__ import annotations`，避免 FastAPI 依赖解析时的前向引用问题。
"""

import logging

from pydantic import ValidationError
from agent_matrix.supply_chain_bi.models import SupplyChainQueryRequest
from agent_matrix.supply_chain_bi.errors import AgentNotInitializedError

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_matrix.supply_chain_bi.agent import SupplyChainBIAgent
from agent_matrix.supply_chain_bi.errors import SupplyChainAgentError
from agent_matrix.supply_chain_bi.models import (
    SemanticCatalogSpec,
    SupplyChainQueryPayload,
    SupplyChainQueryResponse,
)
from data_platform.retrieval_engine.models import ToolErrorResponse, UserContext

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/agents/supply-chain-bi"

ERROR_STATUS_BY_CODE: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "SCOPE_FORBIDDEN": 403,
    "SCOPE_RESOLUTION_FAILED": 503,
    "SQL_MODEL_UNAVAILABLE": 503,
    "STRUCTURED_OUTPUT_INVALID": 502,
    "DATA_SOURCE_UNAVAILABLE": 503,
    "SEMANTIC_CATALOG_INVALID": 503,
    "AGENT_NOT_INITIALIZED": 503,
    "UNSUPPORTED_SCENARIO": 400,
}
"""SupplyChainAgentError.code → HTTP 状态码；未登记的 code 一律 400。"""

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ToolErrorResponse, "description": "缺少可信身份头"},
    403: {"model": ToolErrorResponse, "description": "当前身份无任何仓库 / 大区数据权限"},
    422: {"model": ToolErrorResponse, "description": "请求体校验失败，details 列出字段级错误"},
    502: {"model": ToolErrorResponse, "description": "模型结构化输出非法，可重试"},
    503: {"model": ToolErrorResponse, "description": "权限系统 / 模型 / 数据源暂不可用，retryable=True 时可稍后重试"},
}

router = APIRouter(prefix=API_PREFIX, tags=["supply-chain-bi-agent"])


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------


def get_supply_chain_agent(request: Request) -> SupplyChainBIAgent:
    """从 request.app.state.supply_chain_bi_agent 取出共享实例；为 None 时抛 AgentNotInitializedError（→ 503 结构化）。"""
    agent = request.app.state.supply_chain_bi_agent
    if agent is None:
        raise AgentNotInitializedError("智能体尚未装配")
    return agent


def get_trusted_user_context(request: Request, body_user_context: UserContext | None) -> UserContext:
    """规则同 agent_matrix.store_sop.api.get_trusted_user_context（不得 import 复用其私有异常类，需在本模块定义
    code="UNAUTHENTICATED" 的 SupplyChainAgentError 子类）：头部齐全 → 构造并覆盖；全缺失且 allow_body_identity → body；
    其余（部分缺失 / 非法 / 空角色）→ 401。"""
    values = [request.headers.get(k) for k in ('X-User-Id', 'X-Dept-Id', 'X-User-Roles')]
    if all(v is None for v in values) and request.app.state.allow_body_identity and body_user_context is not None:
        return body_user_context
    try:
        if not all(v and v.strip() for v in values):
            raise ValueError("missing identity")
        roles = [r.strip() for r in values[2].split(',') if r.strip()]
        if not roles: raise ValueError("empty roles")
        return UserContext(user_id=values[0].strip(), dept_id=values[1].strip(), roles=roles)
    except (ValueError, ValidationError) as exc:
        raise _UnauthenticatedError("需要有效的网关身份头") from exc


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post(
    "/analyze",
    operation_id="supply_chain_bi_analyze",
    summary="供应链库存预警与经营分析（自然语言 → 受控只读 SQL）",
    description=(
        "用自然语言询问库存、安全库存缺口、效期周转、原料损耗率、门店杯量等指标。"
        "结果严格限定在调用者授权的大区 / 仓库范围内；采购底价等敏感字段不可查询。"
        "stop_reason=clarification_requested 时 conclusion 为追问；refused=true 时 conclusion 为拒答说明；"
        "alerts 为规则引擎生成的补货 / 调拨 / 临期处置建议；final_sql 为脱敏后的最终执行 SQL。"
    ),
    response_model=SupplyChainQueryResponse,
    responses=_ERROR_RESPONSES,
)
async def analyze(body: SupplyChainQueryPayload, request: Request) -> SupplyChainQueryResponse:
    """user_context = get_trusted_user_context(request, body.user_context)；
    构造 SupplyChainQueryRequest(question, user_context, trace_id, as_of_date, include_alerts) 后调用 agent.analyze。"""
    user = get_trusted_user_context(request, body.user_context)
    agent = get_supply_chain_agent(request)
    return await agent.analyze(SupplyChainQueryRequest(question=body.question, user_context=user, trace_id=body.trace_id, as_of_date=body.as_of_date, include_alerts=body.include_alerts))


@router.get(
    "/catalog",
    operation_id="supply_chain_bi_get_catalog",
    summary="读取脱敏语义字典（标准指标口径与可查询表列）",
    response_model=SemanticCatalogSpec,
    responses=_ERROR_RESPONSES,
)
async def get_catalog(request: Request) -> SemanticCatalogSpec:
    """需可信身份（与 analyze 同规则，但不解析行级范围）；返回 agent.semantic_layer.public_catalog()。"""
    get_trusted_user_context(request, None)
    return get_supply_chain_agent(request).semantic_layer.public_catalog()


# ---------------------------------------------------------------------------
# 异常处理器（全部返回 ToolErrorResponse）
# ---------------------------------------------------------------------------


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 INVALID_ARGUMENT；details=[{"loc": "body.question", "msg": ...}]，hint 列出需修正字段。"""
    details = [{"loc": ".".join(str(p) for p in e['loc']), "msg": "字段值不符合接口约定，请检查类型、长度与允许字段"} for e in exc.errors()]
    body = ToolErrorResponse(error_code="INVALID_ARGUMENT", message="请求参数校验失败", hint="请修正字段: " + ", ".join(d['loc'] for d in details), details=details)
    return JSONResponse(status_code=422, content=body.model_dump())


async def handle_supply_chain_error(request: Request, exc: SupplyChainAgentError) -> JSONResponse:
    """按 ERROR_STATUS_BY_CODE 映射状态码；error_code=exc.code，hint=exc.to_hint()，retryable=exc.retryable。"""
    body = ToolErrorResponse(error_code=exc.code, message=exc.message, hint=exc.to_hint(), retryable=exc.retryable)
    return JSONResponse(status_code=ERROR_STATUS_BY_CODE.get(exc.code, 400), content=body.model_dump())


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """503 INTERNAL_ERROR；logger.exception 记录；响应体不含异常消息、堆栈、类名、文件路径与任何 SQL 片段。"""
    # Avoid logging exception text: upstream failures may contain SQL or secrets.
    logger.error("供应链请求发生未预期异常，已返回结构化错误")
    body = ToolErrorResponse(error_code="INTERNAL_ERROR", message="分析服务暂不可用", hint="[INTERNAL_ERROR] 请稍后重试；如持续失败请联系管理员。", retryable=True)
    return JSONResponse(status_code=503, content=body.model_dump())


def create_app(agent: SupplyChainBIAgent | None = None, *, allow_body_identity: bool = False) -> FastAPI:
    """装配应用：挂载 router、注册三个异常处理器、写入 app.state（装配已就位，仅为使 openapi() 可导出）。

    agent 为 None 时仍可调用 create_app().openapi() 导出 Schema（供平台 Workflow 导入）。
    """
    app = FastAPI(
        title="Supply Chain BI Agent API",
        version="0.4.0",
        description="餐饮供应链库存预警与自然语言经营分析：语义层受控 Text-to-SQL、行级大区 / 仓库权限、只读沙箱执行。",
    )
    app.state.supply_chain_bi_agent = agent
    app.state.allow_body_identity = allow_body_identity
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(SupplyChainAgentError, handle_supply_chain_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


class _UnauthenticatedError(SupplyChainAgentError):
    code = "UNAUTHENTICATED"
    suggestion = "请通过可信网关登录后重试。"
