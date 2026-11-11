import httpx
import pytest

from web_console.app import create_console_app


@pytest.fixture
async def client():
    app = create_console_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console.test") as http:
            yield http
