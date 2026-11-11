"""工作台统一 FastAPI 入口。

/gateway/*        真实挂载四个业务应用工厂，由可信网关中间件清洗并注入身份。
/api/console/*    面向页面的聚合接口，进程内调用上述应用，再补充引擎透视所需的数据。
/                 零构建单页工作台（本地静态资源，不依赖外部网络）。

注意：本模块不使用 `from __future__ import annotations`，避免 FastAPI 依赖解析时的前向引用问题。
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agent_matrix.finance_audit.api import create_app as create_finance_app
from agent_matrix.store_sop.api import create_app as create_store_app
from agent_matrix.supply_chain_bi.api import create_app as create_bi_app
from agent_matrix.supply_chain_bi.models import SqlExecutionStatus
from data_platform.mcp_server.app import create_app as create_knowledge_app
from web_console.adapters import DEMO_MODEL_NAME, _bigrams, search_trace
from web_console.eval_dashboard import build_dashboard
from web_console.finance_intake import IntakeError, parse_submission
from web_console.gateway import PERSONAS, TrustedGatewayMiddleware, asgi_call, persona_payload, trusted_headers
from web_console.runtime import SIMULATED_TABLES, ConsoleRuntime, build_runtime

STATIC_DIR = Path(__file__).with_name("static")
STORE_PREFIX = "/api/v1/agents/store-sop"


class SopTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    persona_id: str = Field(..., max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    message: str = Field(..., min_length=1, max_length=1000)


class SopResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_id: str = Field(..., max_length=64)
    session_id: str = Field(..., max_length=128)


class BiBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    persona_id: str = Field(..., max_length=64)
    question: str = Field(..., min_length=1, max_length=1000)


class FinanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_id: str = Field(..., max_length=64)
    text: str = Field(..., min_length=1, max_length=8000)


class KnowledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(..., min_length=1, max_length=1000)
    dept_id: str = Field(..., min_length=1, max_length=64)
    roles: list[str] = Field(..., min_length=1, max_length=8)
    top_k: int = Field(default=5, ge=1, le=10)
    enable_rerank: bool = True


def _trace_id() -> str:
    return "console-" + uuid4().hex[:16]


def _failure(status: int, error, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, **extra})


def _acl_view(rows, *, dept_id: str, roles: list[str]) -> list[dict]:
    view = []
    for row in rows:
        same_dept = row.dept_id == dept_id
        role_hit = bool(set(row.allowed_roles) & set(roles))
        reason = None if same_dept and role_hit else "跨部门隔离" if not same_dept else "角色未授权"
        view.append({"doc_id": row.doc_id, "title": row.title, "dept_id": row.dept_id, "allowed_roles": row.allowed_roles,
                     "version": row.current_version, "visible": reason is None, "reason": reason})
    return view


def _blocked_matches(runtime: ConsoleRuntime, query: str, acl: list[dict]) -> list[dict]:
    """统计被 ACL 前置过滤、否则可能命中的文档；只输出台账元数据，绝不输出正文。"""
    grams = _bigrams(query)
    blocked = {item["doc_id"]: item for item in acl if not item["visible"]}
    scores: dict[str, int] = {}
    for child in runtime.index.data.values():
        if child.doc_id in blocked:
            text = _bigrams(child.child_text)
            scores[child.doc_id] = max(scores.get(child.doc_id, 0), sum(min(c, text[g]) for g, c in grams.items()))
    ordered = sorted(((s, d) for d, s in scores.items() if s > 0), key=lambda p: (-p[0], p[1]))
    return [{**blocked[doc_id], "lexical_overlap": score} for score, doc_id in ordered[:6]]


def _children_of(runtime: ConsoleRuntime, parent_id: str, vector, keyword) -> list[dict]:
    ranks = {"vector": {c.child_id: c for c in vector}, "keyword": {c.child_id: c for c in keyword}}
    children = sorted((c for c in runtime.index.data.values() if c.parent_id == parent_id), key=lambda c: c.chunk_index)
    return [{"child_id": c.child_id, "chunk_index": c.chunk_index, "text": c.child_text,
             "vector_rank": ranks["vector"][c.child_id].rank if c.child_id in ranks["vector"] else None,
             "vector_score": ranks["vector"][c.child_id].raw_score if c.child_id in ranks["vector"] else None,
             "keyword_rank": ranks["keyword"][c.child_id].rank if c.child_id in ranks["keyword"] else None,
             "keyword_score": ranks["keyword"][c.child_id].raw_score if c.child_id in ranks["keyword"] else None}
            for c in children]


def create_console_app() -> FastAPI:
    """构造工作台应用；业务依赖在 lifespan 中装配后写入各子应用的 app.state。"""
    knowledge_app, store_app = create_knowledge_app(), create_store_app()
    bi_app, finance_app = create_bi_app(), create_finance_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = await build_runtime()
        knowledge_app.state.retrieval_service = runtime.retrieval
        knowledge_app.state.sync_manager = runtime.manager
        store_app.state.store_sop_agent = runtime.store_agent
        bi_app.state.supply_chain_bi_agent = runtime.bi_agent
        finance_app.state.finance_audit_agent = runtime.finance_agent
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(title="连锁餐饮企业 AI 平台统一管控与演练工作台", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedGatewayMiddleware)
    app.mount("/gateway/knowledge", knowledge_app)
    app.mount("/gateway/store-sop", store_app)
    app.mount("/gateway/supply-chain-bi", bi_app)
    app.mount("/gateway/finance-audit", finance_app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/console/status")
    async def status(request: Request) -> dict:
        runtime: ConsoleRuntime = request.app.state.runtime
        return {"mode": "离线演示", "model": DEMO_MODEL_NAME, "documents": runtime.documents, "parents": runtime.parents,
                "children": len(runtime.index.data), "tables": SIMULATED_TABLES, "external_calls": 0,
                "as_of_date": (await runtime.sandbox.latest_snapshot_date()).isoformat(),
                "personas": [persona_payload(p) for p in PERSONAS]}

    @app.post("/api/console/sop/turn")
    async def sop_turn(body: SopTurnBody):
        trace_id = _trace_id()
        headers = trusted_headers(body.persona_id)
        status_code, data = await asgi_call(store_app, "POST", f"{STORE_PREFIX}/turns", headers=headers, body={
            "session_id": body.session_id, "message": body.message, "trace_id": trace_id})
        if status_code != 200:
            return _failure(status_code, data, trace_id=trace_id)
        _, session = await asgi_call(store_app, "GET", f"{STORE_PREFIX}/sessions/{data['session_id']}", headers=headers)
        return {"trace_id": trace_id, "response": data, "session": session}

    @app.post("/api/console/sop/reset")
    async def sop_reset(body: SopResetBody):
        status_code, data = await asgi_call(store_app, "DELETE", f"{STORE_PREFIX}/sessions/{body.session_id}",
                                            headers=trusted_headers(body.persona_id))
        return {"reset": True} if status_code == 204 else _failure(status_code, data)

    @app.post("/api/console/bi/analyze")
    async def bi_analyze(body: BiBody, request: Request):
        trace_id = _trace_id()
        status_code, data = await asgi_call(bi_app, "POST", "/api/v1/agents/supply-chain-bi/analyze",
                                            headers=trusted_headers(body.persona_id),
                                            body={"question": body.question, "trace_id": trace_id})
        if status_code != 200:
            return _failure(status_code, data, trace_id=trace_id)
        fatal = any(a["validation"]["fatal"] for a in data["sql_attempts"])
        probe = None
        if fatal:
            # 第二道防线实证：绕过守卫直接对沙箱执行写操作，由连接层只读保护拒绝。
            result = await request.app.state.runtime.sandbox.execute(
                sql="DROP TABLE dim_material", bind_params={}, timeout_s=2, max_rows=1)
            probe = {"status": result.status.value, "blocked": result.status is SqlExecutionStatus.READ_ONLY_VIOLATION}
        return {"trace_id": trace_id, "response": data, "sandbox_probe": probe}

    @app.post("/api/console/finance/audit")
    async def finance_audit(body: FinanceBody, request: Request):
        runtime: ConsoleRuntime = request.app.state.runtime
        trace_id = _trace_id()
        result = runtime.redactor.redact_text(body.text)
        redaction = {"text": result.text, "matches": [m.model_dump(mode="json") for m in result.matches],
                     "counts": {k.value: v for k, v in result.counts.items()}}
        try:
            submission = parse_submission(body.text)
        except IntakeError as exc:
            return _failure(422, {"error_code": "INVALID_DOCUMENT", "message": str(exc), "retryable": False,
                                  "hint": "请按单据模板补全或修正字段后重新提交。", "details": []},
                            trace_id=trace_id, redaction=redaction)
        status_code, data = await asgi_call(finance_app, "POST", "/api/v1/agents/finance-audit/audit",
                                            headers=trusted_headers(body.persona_id),
                                            body={"submission": submission.model_dump(mode="json"), "trace_id": trace_id})
        if status_code != 200:
            return _failure(status_code, data, trace_id=trace_id, redaction=redaction)
        draft = runtime.approval_gateway.drafts.get(data.get("human_approval_ticket_id") or "")
        ticket = None if draft is None else {
            "ticket_id": data["human_approval_ticket_id"], "queue": draft.queue.value, "sla_hours": draft.sla_hours,
            "max_risk_level": draft.max_risk_level.value, "finding_codes": [c.value for c in draft.finding_codes],
            "summary": draft.summary, "requested_by": draft.requested_by_user_id, "created_at": draft.created_at.isoformat()}
        return {"trace_id": trace_id, "response": data, "redaction": redaction, "ticket": ticket}

    @app.post("/api/console/knowledge/search")
    async def knowledge_search(body: KnowledgeBody, request: Request):
        runtime: ConsoleRuntime = request.app.state.runtime
        trace = {}
        token = search_trace.set(trace)
        try:
            status_code, data = await asgi_call(knowledge_app, "POST", "/api/v1/knowledge/search", headers=[], body={
                "query": body.query, "top_k": body.top_k, "enable_rerank": body.enable_rerank,
                "user_context": {"user_id": "console_preview", "dept_id": body.dept_id, "roles": body.roles}})
        finally:
            search_trace.reset(token)
        if status_code != 200:
            return _failure(status_code, data)
        vector, keyword = trace.get("vector", []), trace.get("keyword", [])
        for item in data["results"]:
            item["children"] = _children_of(runtime, item["parent_id"], vector, keyword)
        acl = _acl_view(await runtime.ledger_rows(), dept_id=body.dept_id, roles=body.roles)
        return {"response": data, "acl": acl, "blocked_matches": _blocked_matches(runtime, body.query, acl),
                "visible_documents": sum(a["visible"] for a in acl), "total_documents": len(acl)}

    @app.get("/api/console/eval/dashboard")
    async def eval_dashboard() -> dict:
        return await asyncio.to_thread(build_dashboard)

    return app
