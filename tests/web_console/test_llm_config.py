"""在线模型配置：.env 解析、模式切换、连通性校验、外部调用计数与三大智能体适配。

全部请求经 httpx.MockTransport，不访问真实网络。
"""
import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from agent_matrix.finance_audit.errors import FinanceModelUnavailableError
from agent_matrix.finance_audit.ports import FinanceReviewModelPort
from agent_matrix.store_sop.errors import ChatModelUnavailableError, StructuredOutputError
from agent_matrix.store_sop.models import ChatMessage, ToolSpec
from agent_matrix.store_sop.ports import ChatModelPort
from agent_matrix.supply_chain_bi.errors import SqlModelUnavailableError
from agent_matrix.supply_chain_bi.models import AnalysisIntent, ScopeLevel
from agent_matrix.supply_chain_bi.ports import SqlGeneratorModelPort
from web_console.adapters import DEMO_MODEL_NAME, DemoSqlModel
from web_console.app import create_console_app
from web_console.llm_gateway import (
    CallMeter, ChatCompletionsClient, LlmOutputError, LlmUnavailableError, OnlineFinanceModel, OnlineSqlModel, OnlineStoreChatModel,
    parse_json_content,
)
from web_console.llm_settings import (
    DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_SECONDS, LlmConfigError, LlmSettings, load_env_file, load_llm_settings,
    normalize_base_url, parse_env_text,
)

SECRET = "sk-unit-test-secret-value"
GATEWAY = "https://gateway.test/v1"
CONFIG = "/api/console/llm/config"
TEST = "/api/console/llm/test"
STATUS = "/api/console/status"
ONLINE_BODY = {"mode": "online", "base_url": GATEWAY, "model": "unit-model", "api_key": SECRET, "timeout_seconds": 5}
EXPENSE = """单据类型：报销单
单据编号：EXP-LLM-001
门店编码：ST_0101
申请人：store_clerk_01 | store_ops | store_clerk
提交时间：2026-11-05 10:00
报销事由：门店日常清洁耗材采购
发票：INV20261104001 | 增值税普通发票 | 2026-11-04 | 杭州清洁用品有限公司 | 91330100MA12345678
明细：日常耗材 | 清洁用品 | 2 | 件 | 100.00 | 0.13 | INV20261104001 | 2026-11-04
附件：发票、采购清单、验收记录
申报总额：226.00"""


def completion(content, model="unit-model"):
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
                                     "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


class Gateway:
    """记录请求并按脚本应答的 Mock 传输层处理器。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def payload(self, index=-1):
        return json.loads(self.requests[index].content)


def make_client(gateway, timeout=5.0):
    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway))
    settings = LlmSettings(base_url=GATEWAY, model="unit-model", api_key=SECRET, timeout_seconds=timeout, source="web")
    return ChatCompletionsClient(settings, http, CallMeter())


class app_client:
    """启动带 Mock 传输层的工作台。"""

    def __init__(self, gateway, env_file=None):
        self.app = create_console_app(env_file=env_file, llm_transport=httpx.MockTransport(gateway))

    async def __aenter__(self):
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://console.test")
        return self.http

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self.lifespan.__aexit__(*exc)


# ---------------- .env 解析与环境变量合并 ----------------


@pytest.mark.parametrize("text, expected", [
    ("A=1\nB=two", {"A": "1", "B": "two"}),
    ("# 注释\n\n  A = 1  ", {"A": "1"}),
    ("export A=1", {"A": "1"}),
    ("A=\"has # hash\" # tail", {"A": "has # hash"}),
    ("A='single'", {"A": "single"}),
    ("A=value # tail", {"A": "value"}),
    ("A=va#lue", {"A": "va#lue"}),
    ("A=", {"A": ""}),
    ("not a pair\n=novalue\n1A=x", {}),
    ("﻿A=1", {"A": "1"}),
    ("A=$HOME", {"A": "$HOME"}),
])
def test_parse_env_text(text, expected):
    assert parse_env_text(text) == expected


def test_load_env_file_missing_is_empty(tmp_path):
    assert load_env_file(tmp_path / "none.env") == {}


def test_no_key_stays_offline(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://gateway.test/v1\nLLM_MODEL=m\n", encoding="utf-8")
    assert load_llm_settings(env, {}) is None


def test_env_file_loaded_with_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"LLM_API_KEY={SECRET}\n", encoding="utf-8")
    settings = load_llm_settings(env, {})
    assert (settings.api_key, settings.base_url, settings.model, settings.timeout_seconds, settings.source) == (
        SECRET, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_SECONDS, "env")


def test_system_environment_overrides_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=from-file\nLLM_MODEL=file-model\nLLM_TIMEOUT_SECONDS=12\n", encoding="utf-8")
    settings = load_llm_settings(env, {"LLM_API_KEY": "from-system", "LLM_MODEL": "", "LLM_BASE_URL": GATEWAY + "/"})
    assert (settings.api_key, settings.model, settings.base_url, settings.timeout_seconds) == ("from-system", "file-model", GATEWAY, 12)


@pytest.mark.parametrize("raw", ["abc", "0", "999"])
def test_invalid_timeout_falls_back_to_default(tmp_path, raw):
    settings = load_llm_settings(tmp_path / "none", {"LLM_API_KEY": SECRET, "LLM_TIMEOUT_SECONDS": raw})
    assert settings.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_invalid_env_url_stays_offline(tmp_path):
    assert load_llm_settings(tmp_path / "none", {"LLM_API_KEY": SECRET, "LLM_BASE_URL": "ftp://x"}) is None


def test_settings_repr_hides_key():
    assert SECRET not in repr(LlmSettings(base_url=GATEWAY, model="m", api_key=SECRET))


@pytest.mark.parametrize("raw, expected", [
    ("https://a.test/v1/", "https://a.test/v1"),
    ("https://a.test/v1/chat/completions", "https://a.test/v1"),
    (" http://10.0.0.1:8000/v1 ", "http://10.0.0.1:8000/v1"),
])
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "gateway.test/v1", "file:///etc/passwd", "https://user:pw@a.test/v1", "https://a.test/v1?x=1"])
def test_normalize_base_url_rejects(raw):
    with pytest.raises(LlmConfigError):
        normalize_base_url(raw)


def test_env_template_and_ignore_rules():
    root = Path(__file__).resolve().parents[2]
    template = parse_env_text((root / ".env.example").read_text(encoding="utf-8"))
    assert template["LLM_API_KEY"] == "" and {"LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"} <= set(template)
    rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "!.env.example"} <= set(rules)


# ---------------- 模式切换接口 ----------------


async def test_default_is_offline_without_external_calls(client):
    status = (await client.get(STATUS)).json()
    assert (status["online"], status["mode"], status["model"], status["external_calls"]) == (False, "离线演示", DEMO_MODEL_NAME, 0)
    config = (await client.get(CONFIG)).json()
    assert config["mode"] == "offline" and config["key_configured"] is False
    assert [p["id"] for p in config["presets"]] == ["deepseek", "dashscope", "custom"]


async def test_env_key_starts_online(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"LLM_API_KEY={SECRET}\nLLM_BASE_URL={GATEWAY}\nLLM_MODEL=env-model\n", encoding="utf-8")
    async with app_client(Gateway(completion("ok")), env) as http:
        status = (await http.get(STATUS)).json()
        assert (status["online"], status["mode"], status["model"], status["gateway_host"]) == (True, "在线推理", "env-model", "gateway.test")
        config = await http.get(CONFIG)
        assert config.json()["key_source"] == "env" and SECRET not in config.text


async def test_save_switches_online_and_never_returns_key():
    gateway = Gateway(completion("ok"))
    async with app_client(gateway) as http:
        saved = await http.put(CONFIG, json=ONLINE_BODY)
        assert saved.status_code == 200 and saved.json()["mode"] == "online" and SECRET not in saved.text
        status = await http.get(STATUS)
        assert status.json()["online"] is True and status.json()["model"] == "unit-model" and SECRET not in status.text
        assert gateway.requests == []


async def test_web_key_is_not_written_to_disk(tmp_path):
    env = tmp_path / ".env"
    async with app_client(Gateway(completion("ok")), env) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
    assert not env.exists() and list(tmp_path.iterdir()) == []


async def test_clear_returns_offline_and_restart_forgets_web_key():
    gateway = Gateway(completion("ok"))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        cleared = (await http.delete(CONFIG)).json()
        assert cleared["mode"] == "offline" and cleared["key_configured"] is False
        assert (await http.get(STATUS)).json()["online"] is False
    async with app_client(gateway) as http:
        assert (await http.get(CONFIG)).json()["key_configured"] is False


async def test_offline_toggle_keeps_key_for_same_gateway():
    async with app_client(Gateway(completion("ok"))) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        assert (await http.put(CONFIG, json={"mode": "offline"})).json()["mode"] == "offline"
        again = await http.put(CONFIG, json={**ONLINE_BODY, "api_key": ""})
        assert again.status_code == 200 and again.json()["mode"] == "online"


async def test_online_requires_key(client):
    response = await client.put(CONFIG, json={**ONLINE_BODY, "api_key": ""})
    assert response.status_code == 422 and response.json()["error"]["error_code"] == "INVALID_LLM_CONFIG"
    assert (await client.get(STATUS)).json()["online"] is False


@pytest.mark.parametrize("patch", [{"base_url": "ftp://x"}, {"model": ""}, {"timeout_seconds": 0}, {"api_key": "has space"}])
async def test_save_rejects_invalid_fields(client, patch):
    assert (await client.put(CONFIG, json={**ONLINE_BODY, **patch})).status_code == 422


async def test_validation_error_does_not_echo_key(client):
    response = await client.put(CONFIG, json={**ONLINE_BODY, "unexpected": 1})
    assert response.status_code == 422 and SECRET not in response.text


async def test_stored_key_is_never_sent_to_another_gateway():
    gateway = Gateway(completion("ok"))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post(TEST, json={**ONLINE_BODY, "base_url": "https://other.test/v1", "api_key": ""})
        assert response.status_code == 422 and gateway.requests == []


@pytest.mark.parametrize("method, path", [("put", CONFIG), ("post", TEST), ("delete", CONFIG)])
async def test_cross_origin_requests_are_rejected(client, method, path):
    kwargs = {} if method == "delete" else {"json": ONLINE_BODY}
    response = await getattr(client, method)(path, headers={"Origin": "https://evil.test"}, **kwargs)
    assert response.status_code == 403


# ---------------- 连通性校验与外部调用计数 ----------------


async def test_connectivity_success_counts_one_call():
    gateway = Gateway(completion("pong"))
    async with app_client(gateway) as http:
        body = (await http.post(TEST, json=ONLINE_BODY)).json()
        assert body["ok"] is True and body["model"] == "unit-model" and body["external_calls"] == 1
        assert (await http.get(STATUS)).json()["external_calls"] == 1
        # 测试连通性不改变当前模式。
        assert (await http.get(STATUS)).json()["online"] is False
    request = gateway.requests[0]
    assert str(request.url) == f"{GATEWAY}/chat/completions" and request.headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize("reply, fragment", [
    (httpx.Response(401, text=f"bad key {SECRET}"), "API Key"),
    (httpx.Response(404, text="nope"), "Base URL"),
    (httpx.Response(429, text="slow"), "限流"),
    (httpx.Response(500, text="boom"), "HTTP 500"),
    (httpx.ReadTimeout("slow"), "超时"),
    (httpx.ConnectError("refused"), "无法连接"),
    (httpx.Response(200, text="<html>"), "协议"),
    (httpx.Response(200, json={"choices": []}), "协议"),
])
async def test_connectivity_failures_are_clear_counted_and_redacted(reply, fragment):
    async with app_client(Gateway(reply)) as http:
        response = await http.post(TEST, json=ONLINE_BODY)
        assert response.status_code == 502 and fragment in response.json()["error"]["message"] and SECRET not in response.text
        status = await http.get(STATUS)
        assert status.json()["external_calls"] == 1 and status.json()["last_error"] and SECRET not in status.text


async def test_offline_agents_never_touch_transport():
    gateway = Gateway(completion("ok"))
    async with app_client(gateway) as http:
        response = await http.post("/api/console/bi/analyze", json={"persona_id": "hq_analyst", "question": "各大区原料损耗率排行"})
        assert response.status_code == 200 and gateway.requests == []
        assert (await http.get(STATUS)).json()["external_calls"] == 0


# ---------------- 协议客户端 ----------------


class Sample(BaseModel):
    value: int


@pytest.mark.parametrize("content", ['{"value": 1}', '```json\n{"value": 1}\n```', '<think>推理</think>{"value": 1}', '结果如下：{"value": 1} 完毕'])
def test_parse_json_content_variants(content):
    assert parse_json_content(content) == {"value": 1}


@pytest.mark.parametrize("content", ["", "没有结构", "{broken"])
def test_parse_json_content_rejects(content):
    with pytest.raises(LlmOutputError):
        parse_json_content(content)


async def test_structured_sends_schema_and_json_mode():
    gateway = Gateway(completion({"value": 3}))
    output, usage, name = await make_client(gateway).structured([{"role": "user", "content": "q"}], Sample)
    assert output.value == 3 and usage.total_tokens == 18 and name == "unit-model"
    payload = gateway.payload()
    assert payload["response_format"] == {"type": "json_object"} and "JSON Schema" in payload["messages"][0]["content"]
    assert "value" in payload["messages"][0]["content"]


async def test_json_mode_unsupported_falls_back_and_counts_each_request():
    gateway = Gateway(httpx.Response(400, text="response_format is not supported"), completion({"value": 1}))
    client = make_client(gateway)
    await client.structured([{"role": "user", "content": "q"}], Sample)
    assert client.meter.count == 2 and "response_format" not in gateway.payload()
    await client.structured([{"role": "user", "content": "q"}], Sample)
    assert client.meter.count == 3 and "response_format" not in gateway.payload()


async def test_schema_mismatch_does_not_leak_output():
    client = make_client(Gateway(completion({"value": "not-int-private-text"})))
    with pytest.raises(LlmOutputError) as caught:
        await client.structured([{"role": "user", "content": "q"}], Sample)
    assert "private-text" not in str(caught.value) and caught.value.__cause__ is None


async def test_redirect_is_not_followed():
    gateway = Gateway(httpx.Response(302, headers={"location": "https://other.test/"}))
    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway), follow_redirects=False)
    client = ChatCompletionsClient(LlmSettings(base_url=GATEWAY, model="m", api_key=SECRET), http, CallMeter())
    with pytest.raises(LlmUnavailableError):
        await client.ping()
    assert len(gateway.requests) == 1


# ---------------- 智能体端口契约 ----------------


def test_adapters_satisfy_ports():
    assert isinstance(OnlineStoreChatModel(None), ChatModelPort)
    assert isinstance(OnlineSqlModel(None), SqlGeneratorModelPort)
    assert isinstance(OnlineFinanceModel(None), FinanceReviewModelPort)


async def test_store_tool_messages_become_protocol_safe():
    gateway = Gateway(completion({"tool_name": "retrieve", "arguments": {}, "reason": "r"}))
    model = OnlineStoreChatModel(make_client(gateway))
    result = await model.decide_tool_call(messages=[ChatMessage(role="system", content="s"), ChatMessage(role="tool", name="knowledge.search", content="观察")],
                                          tools=[ToolSpec(name="retrieve", description="检索")])
    assert result.output.tool_name == "retrieve" and result.model_name == "unit-model"
    messages = gateway.payload()["messages"]
    assert all(m["role"] in ("system", "user", "assistant") for m in messages)
    assert any("knowledge.search" in m["content"] and "观察" in m["content"] for m in messages)
    assert any('"retrieve"' in m["content"] for m in messages)


async def test_store_adapter_maps_errors():
    with pytest.raises(ChatModelUnavailableError):
        await OnlineStoreChatModel(make_client(Gateway(httpx.Response(503)))).generate_structured(messages=[ChatMessage(role="user", content="q")], output_model=Sample)
    with pytest.raises(StructuredOutputError):
        await OnlineStoreChatModel(make_client(Gateway(completion("不是结构化")))).generate_structured(messages=[ChatMessage(role="user", content="q")], output_model=Sample)


async def test_sql_and_finance_adapters_map_unavailable():
    from unittest.mock import MagicMock
    catalog = MagicMock(metrics=[])
    with pytest.raises(SqlModelUnavailableError):
        await OnlineSqlModel(make_client(Gateway(httpx.ConnectError("x")))).classify_intent(question="q", catalog=catalog)
    with pytest.raises(FinanceModelUnavailableError):
        await OnlineFinanceModel(make_client(Gateway(httpx.ReadTimeout("x")))).verify_citation_support(claims=[], policy_contexts=[])


async def test_agent_timeout_budget_follows_gateway_timeout():
    async with app_client(Gateway(completion("ok"))) as http:
        await http.put(CONFIG, json={**ONLINE_BODY, "timeout_seconds": 90})
        runtime = http._transport.app.state.runtime
        assert runtime.store_agent.config.model_timeout_s == runtime.bi_agent.config.model_timeout_s == runtime.finance_agent.config.model_timeout_s == 92


# ---------------- 端到端：在线调用、报错与降级 ----------------


async def test_sop_online_flow_counts_each_model_call():
    gateway = Gateway(
        completion({"intent": "small_talk", "confidence": 1}),
        completion({"tool_name": "refuse", "arguments": {"reason": "out_of_scope"}, "reason": "寒暄"}),
    )
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/sop/turn", json={"persona_id": "store_clerk", "session_id": None, "message": "你好"})
        assert response.status_code == 200
        status = (await http.get(STATUS)).json()
        assert status["external_calls"] == len(gateway.requests) >= 1


async def test_sop_gateway_failure_returns_clear_error():
    async with app_client(Gateway(httpx.Response(500))) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/sop/turn", json={"persona_id": "store_clerk", "session_id": None, "message": "制冰机不制冰"})
        assert response.status_code == 503 and response.json()["error"]["error_code"] == "CHAT_MODEL_UNAVAILABLE"
        status = (await http.get(STATUS)).json()
        assert status["external_calls"] == 1 and "HTTP 500" in status["last_error"]


async def test_bi_online_flow_runs_guard_sandbox_and_counts_calls():
    question = "各仓库安全库存缺口"
    intent = AnalysisIntent(intent_type="metric_query", confidence=1, metric_ids=["safety_stock_gap"])
    sql = DemoSqlModel()._template(question, intent, ScopeLevel.NATIONAL)
    gateway = Gateway(completion(intent.model_dump(mode="json")), completion({"sql": sql, "metric_ids": ["safety_stock_gap"]}),
                      completion({"conclusion": "授权范围内存在安全库存缺口，请安排补货。"}))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/bi/analyze", json={"persona_id": "hq_analyst", "question": question})
        assert response.status_code == 200
        body = response.json()["response"]
        assert body["sql_attempts"][-1]["execution"]["status"] == "success" and body["narrative_source"] == "llm_verified"
        assert (await http.get(STATUS)).json()["external_calls"] == 3
    assert "scope_instructions" in gateway.payload(1)["messages"][-1]["content"]


async def test_bi_gateway_timeout_returns_clear_error():
    async with app_client(Gateway(httpx.ReadTimeout("slow"))) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/bi/analyze", json={"persona_id": "hq_analyst", "question": "各大区原料损耗率排行"})
        assert response.status_code == 503 and response.json()["error"]["error_code"] == "SQL_MODEL_UNAVAILABLE"


async def test_finance_gateway_failure_degrades_to_human_review():
    gateway = Gateway(httpx.Response(500))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/finance/audit", json={"persona_id": "finance_clerk", "text": EXPENSE})
        assert response.status_code == 200
        body = response.json()["response"]
        assert body["model_review_status"] == "model_unavailable" and body["route"] == "ESCALATE_HUMAN"
        assert (await http.get(STATUS)).json()["external_calls"] == 1


async def test_finance_model_input_is_redacted():
    phone, card = "13800138000", "6222020200112233446"
    text = EXPENSE.replace("门店日常清洁耗材采购", f"门店耗材采购，手机 {phone}，垫付卡 {card}")
    gateway = Gateway(completion({"claims": [], "reason_matches_categories": True, "summary": "无风险"}))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        response = await http.post("/api/console/finance/audit", json={"persona_id": "finance_clerk", "text": text})
        assert response.status_code == 200 and gateway.requests
    for request in gateway.requests:
        sent = request.content.decode()
        assert phone not in sent and card not in sent


async def test_switching_back_offline_restores_deterministic_answers():
    gateway = Gateway(httpx.Response(500))
    async with app_client(gateway) as http:
        await http.put(CONFIG, json=ONLINE_BODY)
        await http.put(CONFIG, json={"mode": "offline"})
        response = await http.post("/api/console/bi/analyze", json={"persona_id": "hq_analyst", "question": "各大区原料损耗率排行"})
        assert response.status_code == 200 and gateway.requests == []


# ---------------- 前端 ----------------


def test_frontend_exposes_model_config_entry():
    static = Path(__file__).resolve().parents[2] / "web_console/static"
    dialog = (static / "js/modules/llm_config.js").read_text(encoding="utf-8")
    assert all(text in dialog for text in ("离线确定性演示", "在线 API 推理", "测试连通性", "保存生效", "type: 'password'"))
    main = (static / "js/main.js").read_text(encoding="utf-8")
    assert "llmDialog.open()" in main and "external-calls" in main and "nav-foot" in main
