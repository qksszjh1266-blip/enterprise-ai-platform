"""stdio 启动入口：uv run python -m mcp_server

stdout 专用于 MCP JSON-RPC 报文，所有日志只写 stderr。
可信身份读取 EAP_MCP_* 环境变量（见 mcp_server.identity），未设置时使用最低权限演示身份。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp_server.identity import CallerPrincipal, PrincipalConfigError
from mcp_server.knowledge import build_demo_knowledge_base
from mcp_server.server import create_server

logger = logging.getLogger("mcp_server")


async def serve(principal: CallerPrincipal) -> None:
    knowledge = await build_demo_knowledge_base()
    try:
        server = create_server(retrieval=knowledge.retrieval, principal=principal)
        logger.info("MCP server ready: %d documents, caller department=%s", knowledge.documents, principal.department)
        await server.run_stdio_async()
    finally:
        await knowledge.aclose()


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        principal = CallerPrincipal.from_env()
    except PrincipalConfigError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(principal))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
