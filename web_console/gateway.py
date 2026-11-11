"""演示身份网关。

浏览器只提交人员档案编号；可信身份由服务端按档案生成，并以 X-User-Id / X-Dept-Id / X-User-Roles
注入下游。外部请求自带的同名头一律清洗，等同生产网关的认证与清洗职责。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

IDENTITY_HEADERS = (b"x-user-id", b"x-dept-id", b"x-user-roles")
PERSONA_HEADER = b"x-console-persona"
GATEWAY_PREFIX = "/gateway/"


@dataclass(frozen=True)
class Persona:
    persona_id: str
    label: str
    user_id: str | None
    dept_id: str | None
    roles: tuple[str, ...]
    modules: tuple[str, ...]


PERSONAS: tuple[Persona, ...] = (
    Persona("store_clerk", "门店店员", "store_clerk_01", "store_ops", ("clerk",), ("sop", "bi", "finance")),
    Persona("store_manager", "门店店长", "store_manager_01", "store_ops", ("store_manager",), ("sop",)),
    Persona("hq_analyst", "总部供应链分析师（全国）", "hq_analyst", "supply_chain", ("hq_supply_analyst",), ("bi",)),
    Persona("region_manager", "华东大区经理（大区）", "mgr_east", "supply_chain", ("supply_manager",), ("bi",)),
    Persona("warehouse_clerk", "杭州仓管员（单仓）", "wh_sup_e01", "supply_chain", ("warehouse",), ("sop", "bi")),
    Persona("finance_clerk", "财务专员", "fin_clerk_01", "finance", ("finance",), ("finance",)),
    Persona("finance_manager", "财务经理", "fin_mgr_01", "finance", ("finance_manager",), ("finance",)),
    Persona("legal_counsel", "法务专员", "legal_01", "finance", ("legal",), ("finance",)),
    Persona("anonymous", "未登录访客（无网关身份）", None, None, (), ("sop", "bi", "finance")),
)
PERSONA_INDEX = {p.persona_id: p for p in PERSONAS}


def persona_payload(persona: Persona) -> dict[str, Any]:
    payload = asdict(persona)
    payload["roles"] = list(persona.roles)
    payload["modules"] = list(persona.modules)
    return payload


def trusted_headers(persona_id: str | None) -> list[tuple[bytes, bytes]]:
    """返回可信身份头；未知档案或访客不产生任何身份，由下游按缺失身份 401 拒绝。"""
    persona = PERSONA_INDEX.get(persona_id or "")
    if persona is None or persona.user_id is None:
        return []
    return [(b"x-user-id", persona.user_id.encode()), (b"x-dept-id", persona.dept_id.encode()),
            (b"x-user-roles", ",".join(persona.roles).encode())]


class TrustedGatewayMiddleware:
    """对 /gateway/ 下挂载的业务应用清洗外部身份头，并按档案注入可信身份。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(GATEWAY_PREFIX):
            headers = list(scope["headers"])
            persona = next((v.decode("latin-1") for k, v in headers if k == PERSONA_HEADER), None)
            kept = [(k, v) for k, v in headers if k not in IDENTITY_HEADERS and k != PERSONA_HEADER]
            scope = {**scope, "headers": kept + trusted_headers(persona)}
        await self.app(scope, receive, send)


async def asgi_call(app, method: str, path: str, *, headers: list[tuple[bytes, bytes]],
                    body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """在进程内调用已挂载的业务应用，经过其真实的路由、身份解析与异常处理器。"""
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *headers],
        "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 8000),
    }
    delivered = False
    status, chunks = 500, []

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    try:
        await app(scope, receive, send)
    except Exception:
        # 兜底处理器发送响应后框架会继续上抛；响应已生成时以响应为准。
        if not chunks:
            raise
    raw = b"".join(chunks)
    return status, json.loads(raw) if raw else None
