"""stdio 启动入口契约测试：以子进程方式运行 `python -m mcp_server`，经真实 stdio JSON-RPC 通信。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from mcp_server.identity import ENV_DEPARTMENT, ENV_ROLES, ENV_USER_ID

ROOT = Path(__file__).resolve().parents[2]


def _env(**identity: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EAP_MCP_")}
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env | identity


async def test_stdio_server_serves_tools_with_injected_identity():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server"], cwd=ROOT,
        env=_env(**{ENV_USER_ID: "u-stdio", ENV_DEPARTMENT: "store_ops", ENV_ROLES: "clerk"}))
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        allowed = await client.call_tool("search_enterprise_knowledge",
                                         {"query": "制冰机不出冰", "department": "store_ops", "role": "clerk"})
        denied = await client.call_tool("search_enterprise_knowledge",
                                        {"query": "审批矩阵", "department": "store_ops", "role": "store_manager"})
    assert names == {"search_enterprise_knowledge", "list_supply_chain_metrics"}
    assert allowed.is_error is False and allowed.structured_content["total"] >= 1
    assert denied.is_error is True and "PERMISSION_DENIED" in denied.content[0].text


def test_stdio_server_refuses_to_start_with_partial_identity():
    completed = subprocess.run([sys.executable, "-m", "mcp_server"], cwd=ROOT, env=_env(**{ENV_DEPARTMENT: "finance"}),
                               input="", capture_output=True, text=True, timeout=60)
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert ENV_ROLES in completed.stderr
