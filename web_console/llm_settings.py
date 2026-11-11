"""在线模型配置：`.env` 解析、环境变量合并与字段校验。

配置优先级：系统环境变量 > 项目根目录 `.env` > 默认值；未提供 LLM_API_KEY 时保持离线确定性模式。
本模块不写文件，也不修改 os.environ；密钥只存在于 LlmSettings 实例中，且不进入 repr。
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("web_console.llm")

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 1.0, 300.0

GATEWAY_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "deepseek", "label": "DeepSeek", "base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL},
    {"id": "dashscope", "label": "通义千问 DashScope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"id": "custom", "label": "自定义网关", "base_url": "", "model": ""},
)

_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_API_KEY = re.compile(r"^[\x21-\x7e]{1,512}$")
_MODEL_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class LlmConfigError(ValueError):
    """配置字段不合法；message 可直接展示，且不包含密钥。"""


@dataclass(frozen=True)
class LlmSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    source: str = "env"


def parse_env_text(text: str) -> dict[str, str]:
    """解析 dotenv 文本：支持注释、export 前缀与单双引号；不做变量展开，无法识别的行直接跳过。"""
    values: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    """读取 `.env`；文件不存在或不可读时视为空配置。"""
    try:
        return parse_env_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def normalize_base_url(value: str) -> str:
    """规范化网关地址；允许用户粘贴带 /chat/completions 后缀的完整地址。"""
    url = value.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")].rstrip("/")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        raise LlmConfigError("Base URL 格式不正确") from None
    if parts.scheme not in ("http", "https") or not hostname:
        raise LlmConfigError("Base URL 必须是 http 或 https 地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise LlmConfigError("Base URL 不得包含账号、查询参数或片段")
    return url


def validate_model(value: str) -> str:
    model = value.strip()
    if not _MODEL_NAME.match(model):
        raise LlmConfigError("请填写有效的 Model 名称")
    return model


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not _API_KEY.match(key):
        raise LlmConfigError("API Key 为空或包含非法字符")
    return key


def validate_timeout(value: float) -> float:
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise LlmConfigError(f"超时时间须在 {MIN_TIMEOUT_SECONDS:.0f} 到 {MAX_TIMEOUT_SECONDS:.0f} 秒之间")
    return value


def _timeout_from(raw: str | None) -> float:
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return validate_timeout(float(raw))
    except (ValueError, LlmConfigError):
        logger.warning("LLM_TIMEOUT_SECONDS 无效，已使用默认值 %.0f 秒", DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS


def load_llm_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> LlmSettings | None:
    """合并 `.env` 与环境变量；未配置密钥或配置不合法时返回 None（离线确定性模式）。"""
    merged = load_env_file(env_file if env_file is not None else DEFAULT_ENV_FILE)
    system = os.environ if environ is None else environ
    merged.update({key: value for key, value in system.items() if key.startswith("LLM_") and value})
    if not merged.get("LLM_API_KEY"):
        return None
    try:
        return LlmSettings(
            base_url=normalize_base_url(merged.get("LLM_BASE_URL") or DEFAULT_BASE_URL),
            model=validate_model(merged.get("LLM_MODEL") or DEFAULT_MODEL),
            api_key=validate_api_key(merged["LLM_API_KEY"]),
            timeout_seconds=_timeout_from(merged.get("LLM_TIMEOUT_SECONDS")),
            source="env",
        )
    except LlmConfigError as exc:
        logger.warning("在线模型配置无效，已保持离线模式：%s", exc)
        return None
