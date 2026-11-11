"""标准 MCP（Model Context Protocol）Server 适配层。

以官方 mcp SDK 将企业知识库检索与只读业务能力暴露为 MCP Tool / Prompt，支持 stdio 启动：

    uv run python -m mcp_server

与 data_platform.mcp_server（FastAPI / OpenAPI 工具服务，供 Dify / HiAgent 导入）并存，互不依赖。
"""

from mcp_server.identity import CallerPrincipal
from mcp_server.server import create_server

__all__ = ["CallerPrincipal", "create_server"]
