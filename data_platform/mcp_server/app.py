"""FastAPI 工具服务层。

对外暴露两个工具端点，生成标准 OpenAPI 3.1 Schema，可直接导入 Dify「自定义工具」或 HiAgent 工作流：

    POST /api/v1/knowledge/ingest   -> VersionedSyncManager.sync_document
    POST /api/v1/knowledge/search   -> HybridRetrievalService.search

工具错误处理约定：任何异常都必须转为 ToolErrorResponse，禁止裸抛 HTTP 500。

    异常                         HTTP   error_code              retryable
    RequestValidationError       422    INVALID_ARGUMENT        False   (details 列出字段级错误)
    FilterTooBroadError          400    FILTER_TOO_BROAD        False
    LedgerUnavailableError       503    LEDGER_UNAVAILABLE      True
    RetrievalUnavailableError    503    RETRIEVAL_UNAVAILABLE   True
    其他 RetrievalError          400    {exc.code}              {exc.retryable}
    StorageSyncError             503    {exc.code}              True
    未预期 Exception             503    INTERNAL_ERROR          True    (日志记录堆栈，响应不泄漏堆栈)

ingest 的同步失败由 sync_document 以 SyncResult(outcome=FAILED, error_hint=...) 返回，HTTP 200。
search 的降级以 SearchResponse.degraded=True 返回，HTTP 200。

注意：本模块不使用 `from __future__ import annotations`，避免 FastAPI 依赖解析时的前向引用问题。
"""

import logging
from contextlib import asynccontextmanager
from data_platform.retrieval_engine.errors import RetrievalUnavailableError, LedgerUnavailableError

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from data_platform.retrieval_engine.errors import RetrievalError
from data_platform.retrieval_engine.models import SearchRequest, SearchResponse, ToolErrorResponse
from data_platform.retrieval_engine.service import HybridRetrievalService
from data_platform.storage_sync.errors import StorageSyncError
from data_platform.storage_sync.models import DocumentIngestRequest, SyncResult
from data_platform.storage_sync.sync_manager import VersionedSyncManager

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/knowledge"

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ToolErrorResponse, "description": "参数或权限范围错误，按 hint 修正后重试"},
    422: {"model": ToolErrorResponse, "description": "请求体校验失败，details 列出字段级错误"},
    503: {"model": ToolErrorResponse, "description": "下游依赖暂不可用，retryable=True 时可稍后重试"},
}

router = APIRouter(prefix=API_PREFIX, tags=["knowledge"])


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------


def get_retrieval_service(request: Request) -> HybridRetrievalService:
    """从 request.app.state.retrieval_service 取出共享实例。"""
    service = getattr(request.app.state, "retrieval_service", None)
    if service is None:
        raise RetrievalUnavailableError("服务未初始化")
    return service


def get_sync_manager(request: Request) -> VersionedSyncManager:
    """从 request.app.state.sync_manager 取出共享实例。"""
    manager = getattr(request.app.state, "sync_manager", None)
    if manager is None:
        raise RetrievalUnavailableError("服务未初始化")
    return manager


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    operation_id="ingest_knowledge_document",
    summary="入库或更新一篇知识文档（蓝绿版本发布）",
    description=(
        "提交 Markdown 原文及 ACL 字段。内容未变化时返回 skipped_unchanged；"
        "写入失败时返回 outcome=failed 与 error_hint，旧版本继续对外服务。"
    ),
    response_model=SyncResult,
    responses=_ERROR_RESPONSES,
)
async def ingest_document(body: DocumentIngestRequest, request: Request) -> SyncResult:
    """调用 sync_document 并原样返回 SyncResult（HTTP 200）。"""
    return await get_sync_manager(request).sync_document(body)


@router.post(
    "/search",
    operation_id="search_knowledge_base",
    summary="在用户有权限的知识库中混合检索",
    description=(
        "向量 + BM25 双路召回、RRF 融合、交叉编码器精排，返回带章节路径的父块原文。"
        "degraded=true 表示部分召回或精排不可用，结果仍可引用但可能不完整。"
    ),
    response_model=SearchResponse,
    responses=_ERROR_RESPONSES,
)
async def search_knowledge(body: SearchRequest, request: Request) -> SearchResponse:
    """调用 HybridRetrievalService.search 并返回 SearchResponse（HTTP 200）。"""
    return await get_retrieval_service(request).search(body)


# ---------------------------------------------------------------------------
# 异常处理器（全部返回 ToolErrorResponse）
# ---------------------------------------------------------------------------


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 INVALID_ARGUMENT；details = [{"loc": "body.top_k", "msg": ...}]，hint 指出需修正的字段。"""
    details = [{"loc": ".".join(map(str, e["loc"])), "msg": e["msg"]} for e in exc.errors()]
    body = ToolErrorResponse(error_code="INVALID_ARGUMENT", message="请求参数校验失败",
        hint="请修正以下字段后重试: " + ", ".join(d["loc"] for d in details), details=details)
    return JSONResponse(status_code=422, content=body.model_dump())


async def handle_retrieval_error(request: Request, exc: RetrievalError) -> JSONResponse:
    """按模块文档映射表转换 RetrievalError 子类；hint = exc.to_hint()。"""
    status = 503 if isinstance(exc, (LedgerUnavailableError, RetrievalUnavailableError)) else 400
    body = ToolErrorResponse(error_code=exc.code, message=exc.message, hint=exc.to_hint(), retryable=exc.retryable)
    return JSONResponse(status_code=status, content=body.model_dump())


async def handle_storage_sync_error(request: Request, exc: StorageSyncError) -> JSONResponse:
    """503 + exc.code；hint = exc.to_hint()。"""
    body = ToolErrorResponse(error_code=exc.code, message=exc.message, hint=exc.to_hint(), retryable=True)
    return JSONResponse(status_code=503, content=body.model_dump())


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：503 INTERNAL_ERROR；logging.exception 记录，响应体不得包含堆栈或内部路径。"""
    logger.exception("工具服务发生未预期异常", exc_info=(type(exc), exc, exc.__traceback__))
    body = ToolErrorResponse(error_code="INTERNAL_ERROR", message="服务暂时不可用",
        hint="请稍后重试；如持续失败请联系管理员。", retryable=True)
    return JSONResponse(status_code=503, content=body.model_dump())


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(
    *,
    retrieval_service: HybridRetrievalService | None = None,
    sync_manager: VersionedSyncManager | None = None,
) -> FastAPI:
    """构造 FastAPI 应用（依赖为 None 时仍可生成 OpenAPI Schema，便于离线导出给 Dify / HiAgent）。

    - 依赖挂到 app.state.retrieval_service / app.state.sync_manager。
    - lifespan 关闭阶段调用 sync_manager.aclose()（若已注入）。
    - 注册路由与全部异常处理器（Exception 兜底处理器必须注册，确保无裸 500）。
    - 依赖为 None 而请求到达时，返回 503 + ToolErrorResponse（而非 AttributeError）。
    """
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if app.state.sync_manager is not None:
                await app.state.sync_manager.aclose()

    app = FastAPI(
        lifespan=lifespan,
        title="Enterprise Knowledge Data Platform",
        version="0.1.0",
        description="多源知识库数据中台：ACL 前置过滤的混合检索与蓝绿版本入库工具。",
    )
    app.state.retrieval_service = retrieval_service
    app.state.sync_manager = sync_manager
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(RetrievalError, handle_retrieval_error)
    app.add_exception_handler(StorageSyncError, handle_storage_sync_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app
