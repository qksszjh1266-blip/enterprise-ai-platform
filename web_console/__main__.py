"""一键启动：uv run python -m web_console。"""
import uvicorn

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    uvicorn.run("web_console.app:create_console_app", factory=True, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
