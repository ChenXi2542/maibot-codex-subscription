"""离线自测：不依赖 MaiBot 运行时，验证账号库、协议转换与 SSE 解析。

运行方式：
    python tests/test_protocol.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------------------
# 桩：maibot_sdk 与 httpx
# --------------------------------------------------------------------------------------


def install_sdk_stub() -> None:
    if "maibot_sdk" in sys.modules:
        return
    module = types.ModuleType("maibot_sdk")

    class PluginConfigBase:
        pass

    class MaiBotPlugin:
        def __init__(self) -> None:
            pass

    def _decorator(*args, **kwargs):
        def wrap(func):
            return func

        return wrap

    def Field(default=None, **kwargs):  # noqa: N802
        return default

    module.PluginConfigBase = PluginConfigBase
    module.MaiBotPlugin = MaiBotPlugin
    module.Field = Field
    module.Command = _decorator
    module.LLMProvider = _decorator
    module.CONFIG_RELOAD_SCOPE_SELF = "self"
    sys.modules["maibot_sdk"] = module


def install_httpx_stub() -> None:
    if "httpx" in sys.modules:
        return
    try:
        import httpx  # noqa: F401

        return
    except ImportError:
        pass

    module = types.ModuleType("httpx")

    class HTTPError(Exception):
        pass

    class TimeoutException(HTTPError):
        pass

    class Timeout:
        def __init__(self, *args, **kwargs):
            pass

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            self.is_closed = False

        async def aclose(self):
            self.is_closed = True

    module.HTTPError = HTTPError
    module.TimeoutException = TimeoutException
    module.Timeout = Timeout
    module.AsyncClient = AsyncClient
    sys.modules["httpx"] = module


def load_plugin():
    install_sdk_stub()
    install_httpx_stub()
    spec = importlib.util.spec_from_file_location("maibot_codex_plugin", PLUGIN_DIR / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


# --------------------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------------------


def make_jwt(claims: dict) -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def make_account_token(account_id: str = "acct_1", email: str = "user@example.com", plan: str = "plus") -> str:
    return make_jwt(
        {
            "email": email,
            "exp": 4102444800,
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_plan_type": plan},
        }
    )


def make_cfg(tmp: Path | None = None):
    return types.SimpleNamespace(
        plugin=types.SimpleNamespace(config_version="0.2.0", enabled=True),
        account=types.SimpleNamespace(
            accounts_file=str(tmp / "accounts.json") if tmp else "",
            active_account="",
            import_codex_auth=False,
            auto_refresh=False,
            prefer_api_key=False,
            issuer="https://auth.openai.com",
        ),
        network=types.SimpleNamespace(
            proxy="",
            base_url="https://chatgpt.com/backend-api",
            api_base_url="https://api.openai.com/v1",
            timeout=30.0,
            connect_timeout=5.0,
        ),
        model=types.SimpleNamespace(
            default_model="gpt-test",
            reasoning_effort="medium",
            reasoning_summary="auto",
            max_output_tokens=0,
            client_version="",
            originator="codex_cli_rs",
            catalog_ttl=60,
        ),
    )


# --------------------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------------------


def test_manifest_matches_decorator() -> None:
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    providers = manifest["llm_providers"]
    assert len(providers) == 1
    assert providers[0]["client_type"] == plugin.CLIENT_TYPE
    assert providers[0]["version"] == manifest["version"]
    assert manifest["version"] == plugin.__version__
    assert "." in manifest["id"]
    print("ok  manifest / client_type / 版本一致")


def test_normalize_call_id() -> None:
    assert plugin.normalize_call_id("call_abc-123") == "call_abc-123"
    weird = plugin.normalize_call_id("调用:1/2")
    assert weird.startswith("call_") and len(weird) <= plugin.MAX_CALL_ID_LENGTH
    assert plugin.normalize_call_id("调用:1/2") == weird
    print("ok  call_id 规范化")


def test_context_items_to_wire() -> None:
    request = {
        "context_items": [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "你是麦麦"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "看图"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "image", "image_base64": "AAAA", "image_format": "jpg"}]},
            {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "好的"}]},
            {"item_type": "FunctionCallItem", "tool_call": {"call_id": "c1", "func_name": "search", "args": {"q": "x"}}},
            {"item_type": "FunctionCallOutputItem", "call_id": "c1", "output": "结果", "success": True},
            {"item_type": "ReasoningItem", "representation": "summary", "summary_parts": ["想"], "text_parts": []},
        ],
        "tool_options": [
            {"type": "function", "function": {"name": "search", "description": "搜索", "parameters": {"type": "object"}}}
        ],
    }
    body = plugin.build_responses_body(request, model="gpt-test", effort="high", summary="auto")
    assert body["model"] == "gpt-test"
    assert body["instructions"] == "你是麦麦"
    assert body["store"] is False and body["stream"] is True
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    kinds = [item["type"] for item in body["input"]]
    assert kinds == ["message", "message", "message", "function_call", "function_call_output"], kinds
    assert body["input"][1]["content"][0]["image_url"].startswith("data:image/jpeg;base64,")
    assert json.loads(body["input"][3]["arguments"]) == {"q": "x"}
    assert body["tools"][0] == {
        "type": "function",
        "name": "search",
        "description": "搜索",
        "parameters": {"type": "object"},
    }
    print("ok  context_items -> Responses input")


def test_legacy_message_list() -> None:
    body = plugin.build_responses_body(
        {
            "message_list": [
                {"role": "system", "content": "系统"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": {"a": 1}}}]},
                {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            ]
        },
        model="m",
    )
    assert body["instructions"] == "系统"
    assert [item["type"] for item in body["input"]] == ["message", "function_call", "function_call_output"]
    print("ok  legacy message_list -> Responses input")


def test_chatgpt_mode_strips_params() -> None:
    body = plugin.build_responses_body(
        {"context_items": [], "temperature": 0.7, "max_tokens": 100}, model="m", include_temperature=True
    )
    assert body["temperature"] == 0.7
    removed = plugin.drop_unsupported(body)
    assert set(removed) == {"temperature"}
    assert "temperature" not in body
    print("ok  订阅模式参数裁剪")


def test_normalizers() -> None:
    assert plugin.normalize_effort("default") == ""
    assert plugin.normalize_effort("HIGH") == "high"
    assert plugin.normalize_summary("default") == ""
    assert plugin.normalize_summary("auto") == "auto"
    print("ok  推理参数归一化")


def test_account_identity() -> None:
    token = make_account_token()
    account_id, email, plan, expires_at = plugin.account_identity(token)
    assert account_id == "acct_1"
    assert email == "user@example.com"
    assert plan == "plus"
    assert expires_at == 4102444800
    account = plugin.Account.from_dict({"access_token": token})
    assert account.account_id == "acct_1" and account.label == "user@example.com"
    print("ok  JWT 账号解析")


def test_vault_roundtrip(tmp: Path) -> None:
    vault = plugin.AccountVault(lambda: tmp / "accounts.json")
    first = plugin.Account.from_dict({"access_token": make_account_token("acct_a", "a@x.com", "plus")})
    second = plugin.Account.from_dict({"access_token": make_account_token("acct_b", "b@x.com", "pro")})
    vault.upsert(first)
    vault.upsert(second)

    assert [a.account_id for a in vault.accounts()] == ["acct_a", "acct_b"]
    assert vault.resolve().account_id == "acct_b"

    import json as _json

    raw = _json.loads((tmp / "accounts.json").read_text(encoding="utf-8"))
    assert raw["active"] == "acct_b"
    assert raw["accounts"][0]["email"] == "a@x.com"

    assert vault.remove("acct_b") is True
    assert [a.account_id for a in vault.accounts()] == ["acct_a"]
    assert vault.resolve().account_id == "acct_a"
    print("ok  账号库读写/切换/移除")


def test_import_codex_auth(tmp: Path) -> None:
    token = make_account_token("acct_import", "imp@x.com", "plus")
    target = tmp / "auth.json"
    target.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": token, "refresh_token": "rt", "id_token": token},
            }
        ),
        encoding="utf-8",
    )
    account = plugin.import_codex_auth(target)
    assert account is not None
    assert account.account_id == "acct_import" and account.email == "imp@x.com"
    assert account.source == "import" and account.refresh_token == "rt"

    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert plugin.import_codex_auth(bad) is None
    print("ok  Codex auth.json 导入")


def test_sse_translation() -> None:
    events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "先想"},
        {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "你好"},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "呀"},
        {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message"}},
        {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "search"}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"q":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": ' "麦麦"}'},
        {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "search"}},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "input_tokens_details": {"cached_tokens": 20}},
            },
        },
    ]

    async def lines():
        for event in events:
            yield "event: " + event["type"]
            yield "data: " + json.dumps(event)
            yield ""

    result = asyncio.run(plugin.collect_sse(lines()))
    assert result.text == "你好呀"
    assert result.reasoning == "先想" and result.reasoning_kind == "summary"
    assert result.tool_calls[0].args_dict() == {"q": "麦麦"}
    assert result.usage["prompt_tokens"] == 100
    assert result.usage["prompt_cache_hit_tokens"] == 20

    items = plugin.build_output_items(result, 1)
    assert items["item_schema_version"] == 1
    assert [item["item_type"] for item in items["output_items"]] == [
        "ReasoningItem",
        "AssistantMessageItem",
        "FunctionCallItem",
    ]
    print("ok  SSE -> 文本/推理/工具调用/usage")


def test_catalog_helpers() -> None:
    entries = [
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna"},
        {"id": "gpt-5.5"},
        {"slug": "gpt-6-luna", "display_name": "dup"},
    ]
    catalog = plugin.normalize_catalog(entries)
    assert [item["slug"] for item in catalog] == ["gpt-6-luna", "gpt-5.5"]
    assert catalog[0]["name"] == "GPT-6 Luna"
    assert catalog[1]["name"] == "gpt-5.5"

    assert plugin.selected_slugs(catalog, []) == ["gpt-6-luna", "gpt-5.5"]
    assert plugin.selected_slugs(catalog, ["gpt-5.5"]) == ["gpt-5.5"]
    assert plugin.selected_slugs(catalog, ["gpt-5.5", "not-exist"]) == ["gpt-5.5"]
    print("ok  模型目录规范化与勾选过滤")


def test_sse_sniffing() -> None:
    assert plugin.looks_like_sse("event: response.created\ndata: {}\n")
    assert plugin.looks_like_sse("\n\ndata: {\"type\":\"response.completed\"}\n")
    assert not plugin.looks_like_sse('{"id":"resp_1","output":[]}')
    print("ok  SSE/JSON 内容嗅探")


def test_parse_json_fallback() -> None:
    payload = {
        "id": "resp_2",
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "想一想"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            {"type": "function_call", "call_id": "c9", "name": "f", "arguments": '{"k":1}'},
        ],
    }
    result = plugin.parse_json_response(json.dumps(payload).encode("utf-8"))
    assert result.text == "答案" and result.reasoning == "想一想"
    assert result.tool_calls[0].args_dict() == {"k": 1}
    print("ok  非流式 JSON 兜底")


# --------------------------------------------------------------------------------------
# Provider 端到端（假 httpx 客户端）
# --------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", lines=None, content_type: str = "text/event-stream"):
        self.status_code = status
        self._lines = lines or []
        self._body = body or ("\n".join(self._lines)).encode("utf-8")
        self.headers = {"content-type": content_type}

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStream:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.is_closed = False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "json": json or {}})
        return FakeStream(self._responses.pop(0))

    async def aclose(self):
        self.is_closed = True


def make_plugin(fake_client: FakeClient, tmp: Path):
    p = plugin.create_plugin()
    p._fallback_cfg = make_cfg(tmp)
    p._client = fake_client
    p._vault = plugin.AccountVault(lambda: tmp / "accounts.json")
    account = plugin.Account.from_dict({"access_token": make_account_token("acct_1", "u@x.com", "plus")})
    p._vault.upsert(account)

    async def fake_ensure(_client):
        return account

    async def fake_refresh(_client, acc):  # pragma: no cover - 仅 401 分支
        return acc

    p.ensure_account = fake_ensure
    p.refresh_and_store = fake_refresh
    return p


def test_provider_end_to_end(tmp: Path) -> None:
    sse = [
        'data: {"type":"response.output_text.delta","item_id":"m1","delta":"喵"}',
        'data: {"type":"response.output_text.delta","item_id":"m1","delta":"~"}',
        'data: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{"input_tokens":11,"output_tokens":2,"total_tokens":13}}}',
    ]
    client = FakeClient([FakeResponse(200, lines=sse)])
    p = make_plugin(client, tmp)

    request = {
        "item_schema_version": 1,
        "context_items": [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "系统"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "在吗"}]},
        ],
        "model_info": {"model_identifier": "gpt-test"},
        "temperature": 0.9,
        "max_tokens": 128,
    }
    result = asyncio.run(p._provider.respond(request))

    sent = client.requests[0]
    assert sent["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert sent["headers"]["chatgpt-account-id"] == "acct_1"
    assert sent["headers"]["OpenAI-Beta"] == "responses=experimental"
    assert sent["headers"]["Authorization"].startswith("Bearer ")
    body = sent["json"]
    assert body["instructions"] == "系统" and body["store"] is False
    assert "temperature" not in body and "max_output_tokens" not in body
    assert body["reasoning"]["effort"] == "medium"

    assert result["item_schema_version"] == 1
    assert result["output_items"][0]["parts"][0]["text"] == "喵~"
    print("ok  Provider 请求/响应端到端")


def test_provider_retries_without_unsupported_params(tmp: Path) -> None:
    error = json.dumps({"detail": "Unsupported parameter: temperature"}).encode("utf-8")
    sse = [
        'data: {"type":"response.output_text.delta","item_id":"m1","delta":"ok"}',
        'data: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{}}}',
    ]
    client = FakeClient([FakeResponse(400, body=error, content_type="application/json"), FakeResponse(200, lines=sse)])
    p = make_plugin(client, tmp)
    result = asyncio.run(
        p._provider.respond(
            {
                "context_items": [{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "hi"}]}],
                "model_info": {"model_identifier": "gpt-test"},
            }
        )
    )
    assert len(client.requests) == 2
    assert result["content"] == "ok"
    print("ok  400 Unsupported parameter 自动重试")


def test_http_error_message(tmp: Path) -> None:
    client = FakeClient(
        [FakeResponse(429, body=b'{"error":{"message":"quota exceeded"}}', content_type="application/json")]
    )
    p = make_plugin(client, tmp)
    try:
        asyncio.run(
            p._provider.respond(
                {
                    "context_items": [{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "hi"}]}],
                    "model_info": {"model_identifier": "gpt-test"},
                }
            )
        )
    except plugin.CodexError as exc:
        assert "429" in str(exc) and "quota exceeded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("429 应当抛 CodexError")
    print("ok  HTTP 错误信息")


def test_commands_helpers(tmp: Path) -> None:
    p = plugin.create_plugin()
    p._fallback_cfg = make_cfg(tmp)
    p._vault = plugin.AccountVault(lambda: tmp / "accounts.json")
    p._vault.upsert(plugin.Account.from_dict({"access_token": make_account_token("acct_a", "a@x.com", "plus")}))
    p._vault.upsert(plugin.Account.from_dict({"access_token": make_account_token("acct_b", "b@x.com", "pro")}))

    assert "login" in p._help_text()
    # Host 按文档下发 matched_groups（命名捕获组）
    assert p._parse_command({"matched_groups": {"action": "models"}}) == ("models", "")
    assert p._parse_command({"matched_groups": {"action": "use", "arg": "2"}}) == ("use", "2")
    # 旧 Host 只有原文时退化为文本解析
    assert p._parse_command({"raw_message": "/codex use 2"}) == ("use", "2")
    assert p._parse_command({"plain_text": "/codex models"}) == ("models", "")

    text = p._accounts_text()
    assert "a***@x.com" in text and "b***@x.com" in text
    assert "a@x.com" not in text
    assert "已切换到" in p._use_account("1")
    assert p._vault.active_id() == "acct_a"
    assert "已移除" in p._logout("1")
    assert p._vault.active_id() == "acct_b"
    assert plugin.mask_email("someone@example.com") == "s***@example.com"
    assert plugin.mask_id("1234567890abcdef") == "12345678…"
    print("ok  命令解析、脱敏与账号操作")


def main() -> int:
    test_manifest_matches_decorator()
    test_normalize_call_id()
    test_context_items_to_wire()
    test_legacy_message_list()
    test_chatgpt_mode_strips_params()
    test_normalizers()
    test_account_identity()

    tmp = Path(__file__).resolve().parent / "_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        test_vault_roundtrip(tmp)
        (tmp / "accounts.json").unlink(missing_ok=True)
        test_import_codex_auth(tmp)
        test_provider_end_to_end(tmp)
        (tmp / "accounts.json").unlink(missing_ok=True)
        test_provider_retries_without_unsupported_params(tmp)
        (tmp / "accounts.json").unlink(missing_ok=True)
        test_http_error_message(tmp)
        (tmp / "accounts.json").unlink(missing_ok=True)
        test_commands_helpers(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    test_sse_translation()
    test_catalog_helpers()
    test_sse_sniffing()
    test_parse_json_fallback()
    print("\n全部测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
