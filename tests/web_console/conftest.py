import httpx
import pytest

from web_console.app import create_console_app


@pytest.fixture(autouse=True)
def isolated_llm_env(monkeypatch, tmp_path):
    """测试不读取开发者本机的 .env 与 LLM_* 环境变量，避免默认用例发出真实外部请求。"""
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("web_console.llm_settings.DEFAULT_ENV_FILE", tmp_path / "absent.env")


@pytest.fixture
async def client():
    app = create_console_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console.test") as http:
            yield http
