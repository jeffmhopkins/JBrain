"""Unit tests for the provider-agnostic LLM layer (app/services/llm.py).

This module is the LLM PROVIDER ABSTRACTION: an Anthropic adapter and an
OpenAI-compatible xAI/Grok adapter, plus provider/model routing, credential
checks, message translation/wiring, and usage recording. These tests exercise
the PURE routing/translation/selection logic directly, and for the provider
SDK methods they monkeypatch the provider's client factory (`_sync_client` /
`_client`) with a fake that returns canned response objects — no real network
and no real SDK calls. Token usage is recorded through a monkeypatched sink so
the tests stay pure (no DB).
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("openai")

from app.services import llm  # noqa: E402
from app.services.llm import (  # noqa: E402
    AnthropicProvider,
    ToolCall,
    ToolDef,
    ToolResult,
    XAIProvider,
)

pytestmark = pytest.mark.unit


# --- helpers ----------------------------------------------------------------

def _settings(**over):
    base = dict(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="claude-sonnet-4-6",
        xai_api_key="",
        xai_base_url="https://api.x.ai/v1",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def patch_settings(monkeypatch):
    """Return a setter that installs a fake Settings object on llm.get_settings."""
    def _set(**over):
        s = _settings(**over)
        monkeypatch.setattr(llm, "get_settings", lambda: s)
        return s
    return _set


@pytest.fixture(autouse=True)
def _no_usage_sink(monkeypatch):
    """Keep tests pure: capture every usage record instead of touching the DB."""
    recorded = []
    import app.services.usage as usage

    def _fake_record(model, **kw):
        recorded.append({"model": model, **kw})

    monkeypatch.setattr(usage, "record", _fake_record)
    return recorded


# Canned SDK response shapes -------------------------------------------------

def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _anthropic_msg(text="", *, tool_use=None, stop_reason="end_turn", usage=None):
    content = []
    if text:
        content.append(_block("text", text=text))
    for tu in (tool_use or []):
        content.append(_block("tool_use", id=tu["id"], name=tu["name"], input=tu["args"]))
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


def _anthropic_usage(i=10, o=5, cr=0, cw=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


class FakeAnthropicClient:
    """Stub for anthropic.Anthropic with .messages.create capturing kwargs."""
    def __init__(self, response):
        self._response = response
        self.kwargs = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


def _oa_resp(content="", *, tool_calls=None, finish_reason="stop", usage=None):
    fn_calls = []
    for tc in (tool_calls or []):
        fn_calls.append(SimpleNamespace(
            id=tc["id"], type="function",
            function=SimpleNamespace(name=tc["name"], arguments=tc["args"])))
    message = SimpleNamespace(content=content, tool_calls=fn_calls or None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _oa_usage(p=10, c=5):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c)


class FakeOpenAIClient:
    def __init__(self, response):
        self._response = response
        self.kwargs = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


# ===========================================================================
# Provider selection / model routing
# ===========================================================================

def test_provider_for_model_grok():
    assert llm._provider_for_model("grok-4.3") == "xai"
    assert llm._provider_for_model("GROK-2") == "xai"


def test_provider_for_model_claude():
    assert llm._provider_for_model("claude-sonnet-4-6") == "anthropic"
    assert llm._provider_for_model("CLAUDE-opus") == "anthropic"


def test_provider_for_model_unknown_and_none():
    assert llm._provider_for_model("gpt-4o") is None
    assert llm._provider_for_model(None) is None
    assert llm._provider_for_model("") is None


def test_get_provider_infers_from_model_id(patch_settings):
    patch_settings(llm_provider="anthropic")
    assert isinstance(llm.get_provider("grok-4.3"), XAIProvider)
    assert isinstance(llm.get_provider("claude-sonnet-4-6"), AnthropicProvider)


def test_get_provider_falls_back_to_configured_default(patch_settings):
    patch_settings(llm_provider="xai")
    # No model id -> use the LLM_PROVIDER default.
    assert isinstance(llm.get_provider(), XAIProvider)
    patch_settings(llm_provider="anthropic")
    assert isinstance(llm.get_provider(), AnthropicProvider)


def test_get_provider_grok_alias(patch_settings):
    patch_settings(llm_provider="grok")
    assert isinstance(llm.get_provider(), XAIProvider)


def test_get_provider_unknown_default_falls_back_to_anthropic(patch_settings):
    patch_settings(llm_provider="totally-unknown")
    assert isinstance(llm.get_provider(), AnthropicProvider)


# ===========================================================================
# model_for(tier)
# ===========================================================================

def test_model_for_tier_specific(monkeypatch):
    import app.services.prompts as prompts

    def _get(key, default=""):
        return {"models.summary": "claude-haiku-4", "models.default": "claude-sonnet-4-6"}.get(key, default)

    monkeypatch.setattr(prompts, "get", _get)
    assert llm.model_for("summary") == "claude-haiku-4"


def test_model_for_falls_back_to_default(monkeypatch):
    import app.services.prompts as prompts

    def _get(key, default=""):
        return {"models.default": "claude-sonnet-4-6"}.get(key, default)

    monkeypatch.setattr(prompts, "get", _get)
    assert llm.model_for("summary") == "claude-sonnet-4-6"


def test_model_for_none_when_nothing_configured(monkeypatch):
    import app.services.prompts as prompts
    monkeypatch.setattr(prompts, "get", lambda key, default="": "")
    assert llm.model_for("summary") is None


# ===========================================================================
# has_credentials per provider
# ===========================================================================

def test_anthropic_has_credentials(patch_settings):
    patch_settings(llm_api_key="")
    assert AnthropicProvider().has_credentials() is False
    patch_settings(llm_api_key="sk-ant-123")
    assert AnthropicProvider().has_credentials() is True


def test_xai_has_credentials_uses_xai_key_then_llm_key(patch_settings):
    patch_settings(xai_api_key="", llm_api_key="")
    assert XAIProvider().has_credentials() is False
    # Explicit xAI key wins.
    patch_settings(xai_api_key="xai-key", llm_api_key="")
    assert XAIProvider().has_credentials() is True
    # Legacy single-key path: falls back to llm_api_key.
    patch_settings(xai_api_key="", llm_api_key="shared")
    assert XAIProvider().has_credentials() is True
    assert XAIProvider()._key() == "shared"


def test_module_has_credentials_routes_to_default_provider(patch_settings):
    patch_settings(llm_provider="anthropic", llm_api_key="sk-ant")
    assert llm.has_credentials() is True
    patch_settings(llm_provider="anthropic", llm_api_key="")
    assert llm.has_credentials() is False


def test_default_model(patch_settings):
    patch_settings(llm_model="claude-sonnet-4-6")
    assert AnthropicProvider().default_model() == "claude-sonnet-4-6"
    # xAI keeps a grok model, else falls back to grok-4.3.
    assert XAIProvider().default_model() == "grok-4.3"
    patch_settings(llm_model="grok-4.3")
    assert XAIProvider().default_model() == "grok-4.3"
    patch_settings(llm_model="Grok-Beta")
    assert XAIProvider().default_model() == "Grok-Beta"


def test_supports_tools():
    assert AnthropicProvider().supports_tools() is True
    assert XAIProvider().supports_tools() is True


# ===========================================================================
# xAI _translate / _wire message shaping
# ===========================================================================

def test_xai_translate_plain_string_passthrough():
    p = XAIProvider()
    m = {"role": "user", "content": "hello"}
    assert p._translate(m) == m


def test_xai_translate_text_block():
    p = XAIProvider()
    out = p._translate({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    assert out["content"] == [{"type": "text", "text": "hi"}]


def test_xai_translate_image_block():
    p = XAIProvider()
    block = {"type": "image", "source": {"media_type": "image/jpeg", "data": "BASE64"}}
    out = p._translate({"role": "user", "content": [block]})
    assert out["content"] == [{
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,BASE64"}}]


def test_xai_translate_image_block_defaults_media_type():
    p = XAIProvider()
    out = p._translate({"role": "user", "content": [{"type": "image", "source": {"data": "X"}}]})
    assert out["content"][0]["image_url"]["url"] == "data:image/png;base64,X"


def test_xai_translate_tool_result_to_text():
    p = XAIProvider()
    out = p._translate({"role": "user", "content": [{"type": "tool_result", "content": 42}]})
    assert out["content"] == [{"type": "text", "text": "42"}]


def test_xai_translate_unknown_block_passthrough():
    p = XAIProvider()
    block = {"type": "weird", "x": 1}
    out = p._translate({"role": "user", "content": [block]})
    assert out["content"] == [block]


def test_xai_wire_prepends_system():
    p = XAIProvider()
    wired = p._wire([{"role": "user", "content": "q"}], "be terse")
    assert wired[0] == {"role": "system", "content": "be terse"}
    assert wired[1] == {"role": "user", "content": "q"}


def test_xai_wire_without_system():
    p = XAIProvider()
    wired = p._wire([{"role": "user", "content": "q"}], None)
    assert wired == [{"role": "user", "content": "q"}]


# ===========================================================================
# Anthropic complete / complete_with_meta
# ===========================================================================

def test_anthropic_complete_with_meta_text_and_stop_reason(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(llm_api_key="sk", llm_model="claude-sonnet-4-6")
    resp = _anthropic_msg(text="hello world", stop_reason="end_turn", usage=_anthropic_usage(20, 7))
    client = FakeAnthropicClient(resp)
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: client)

    text, stop = p.complete_with_meta([{"role": "user", "content": "hi"}], system="sys", max_tokens=256)
    assert text == "hello world"
    assert stop == "end_turn"
    # system + model + max_tokens wired through
    assert client.kwargs["system"] == "sys"
    assert client.kwargs["model"] == "claude-sonnet-4-6"
    assert client.kwargs["max_tokens"] == 256
    # usage recorded
    assert _no_usage_sink[0]["input_tokens"] == 20
    assert _no_usage_sink[0]["output_tokens"] == 7
    assert _no_usage_sink[0]["context"] == "action"


def test_anthropic_complete_truncation_meta(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk")
    resp = _anthropic_msg(text="partial", stop_reason="max_tokens", usage=None)
    client = FakeAnthropicClient(resp)
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: client)
    text, stop = p.complete_with_meta([{"role": "user", "content": "hi"}])
    assert (text, stop) == ("partial", "max_tokens")
    # no system passed -> not in kwargs
    assert "system" not in client.kwargs


def test_anthropic_complete_returns_text_only(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk")
    resp = _anthropic_msg(text="answer", stop_reason="end_turn")
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: FakeAnthropicClient(resp))
    assert p.complete([{"role": "user", "content": "hi"}]) == "answer"


def test_anthropic_complete_with_explicit_model(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk", llm_model="claude-sonnet-4-6")
    client = FakeAnthropicClient(_anthropic_msg(text="x"))
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: client)
    p.complete([{"role": "user", "content": "hi"}], model="claude-opus-4-8")
    assert client.kwargs["model"] == "claude-opus-4-8"


# ===========================================================================
# Anthropic complete_with_tools
# ===========================================================================

def test_anthropic_complete_with_tools_parses_calls(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(llm_api_key="sk", llm_model="claude-sonnet-4-6")
    resp = _anthropic_msg(
        text="let me look",
        tool_use=[{"id": "tu_1", "name": "search", "args": {"q": "x"}}],
        usage=_anthropic_usage(30, 12))
    client = FakeAnthropicClient(resp)
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: client)

    messages = [{"role": "user", "content": "find x"}]
    tools = [ToolDef(name="search", description="d", json_schema={"type": "object"})]
    text, calls, usage = p.complete_with_tools(messages, system="s", tools=tools, max_tokens=512)

    assert text == "let me look"
    assert len(calls) == 1
    assert calls[0] == ToolCall(id="tu_1", name="search", args={"q": "x"})
    assert usage == {"input_tokens": 30, "output_tokens": 12}
    # assistant turn appended with opaque content blocks
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] is resp.content
    # wire_tools shaped correctly
    assert client.kwargs["tools"] == [
        {"name": "search", "description": "d", "input_schema": {"type": "object"}}]
    assert _no_usage_sink[0]["context"] == "agent"


def test_anthropic_complete_with_tools_no_usage(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk")
    resp = _anthropic_msg(text="just text", usage=None)
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_sync_client", lambda: FakeAnthropicClient(resp))
    text, calls, usage = p.complete_with_tools(
        [{"role": "user", "content": "hi"}], tools=[], max_tokens=128)
    assert text == "just text"
    assert calls == []
    assert usage is None


# ===========================================================================
# Anthropic append_tool_results
# ===========================================================================

def test_anthropic_append_tool_results():
    p = AnthropicProvider()
    messages = []
    p.append_tool_results(messages, [
        ToolResult(tool_call_id="tu_1", content="result-a"),
        ToolResult(tool_call_id="tu_2", content="result-b"),
    ])
    assert messages == [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "result-a"},
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "result-b"},
        ],
    }]


# ===========================================================================
# xAI complete / complete_with_meta
# ===========================================================================

def test_xai_complete_with_meta(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")
    resp = _oa_resp(content="  grok says hi  ", finish_reason="stop", usage=_oa_usage(11, 4))
    client = FakeOpenAIClient(resp)
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)

    text, finish = p.complete_with_meta([{"role": "user", "content": "q"}], system="sys", max_tokens=200)
    assert text == "grok says hi"  # stripped
    assert finish == "stop"
    assert client.kwargs["model"] == "grok-4.3"
    assert client.kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert _no_usage_sink[0]["input_tokens"] == 11
    assert _no_usage_sink[0]["output_tokens"] == 4
    assert _no_usage_sink[0]["context"] == "action"


def test_xai_complete_truncation_meta(patch_settings, monkeypatch):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")
    resp = _oa_resp(content="cut", finish_reason="length")
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: FakeOpenAIClient(resp))
    text, finish = p.complete_with_meta([{"role": "user", "content": "q"}])
    assert (text, finish) == ("cut", "length")


def test_xai_complete_handles_none_content(patch_settings, monkeypatch):
    patch_settings(xai_api_key="xai")
    resp = _oa_resp(content=None, finish_reason="stop")
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: FakeOpenAIClient(resp))
    assert p.complete([{"role": "user", "content": "q"}]) == ""


# ===========================================================================
# xAI complete_with_tools
# ===========================================================================

def test_xai_complete_with_tools_parses_calls(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")
    resp = _oa_resp(
        content="working",
        tool_calls=[{"id": "call_1", "name": "search", "args": '{"q": "y"}'}],
        usage=_oa_usage(8, 3))
    client = FakeOpenAIClient(resp)
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)

    messages = [{"role": "user", "content": "find y"}]
    tools = [ToolDef(name="search", description="d", json_schema={"type": "object"})]
    text, calls, usage = p.complete_with_tools(messages, system="s", tools=tools, max_tokens=300)

    assert text == "working"
    assert calls == [ToolCall(id="call_1", name="search", args={"q": "y"})]
    assert usage == {"input_tokens": 8, "output_tokens": 3}
    # assistant turn appended in OpenAI shape with tool_calls
    amsg = messages[-1]
    assert amsg["role"] == "assistant"
    assert amsg["content"] == "working"
    assert amsg["tool_calls"][0]["function"]["name"] == "search"
    # wire_tools shaped as OpenAI function tools
    assert client.kwargs["tools"][0]["type"] == "function"
    assert client.kwargs["tools"][0]["function"]["name"] == "search"


def test_xai_complete_with_tools_malformed_args_become_empty(patch_settings, monkeypatch):
    patch_settings(xai_api_key="xai")
    resp = _oa_resp(
        content=None,
        tool_calls=[{"id": "call_1", "name": "search", "args": "not json{"}])
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: FakeOpenAIClient(resp))
    messages = [{"role": "user", "content": "go"}]
    text, calls, usage = p.complete_with_tools(messages, tools=[], max_tokens=100)
    assert calls[0].args == {}  # malformed blob -> empty dict
    # content None -> assistant content is None
    assert messages[-1]["content"] is None


def test_xai_complete_with_tools_no_tools_sends_none(patch_settings, monkeypatch):
    patch_settings(xai_api_key="xai")
    resp = _oa_resp(content="hi", usage=_oa_usage())
    client = FakeOpenAIClient(resp)
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)
    p.complete_with_tools([{"role": "user", "content": "q"}], tools=[], max_tokens=100)
    assert client.kwargs["tools"] is None  # empty tool list -> None


# ===========================================================================
# xAI append_tool_results
# ===========================================================================

def test_xai_append_tool_results():
    p = XAIProvider()
    messages = []
    p.append_tool_results(messages, [
        ToolResult(tool_call_id="call_1", content="a"),
        ToolResult(tool_call_id="call_2", content="b"),
    ])
    assert messages == [
        {"role": "tool", "tool_call_id": "call_1", "content": "a"},
        {"role": "tool", "tool_call_id": "call_2", "content": "b"},
    ]


# ===========================================================================
# Usage recording sinks (monkeypatched usage.record)
# ===========================================================================

def test_record_usage_anthropic_fields(monkeypatch):
    captured = {}
    import app.services.usage as usage
    monkeypatch.setattr(usage, "record", lambda model, **kw: captured.update({"model": model, **kw}))
    llm._record_usage("claude-x", _anthropic_usage(i=100, o=50, cr=10, cw=5), "action")
    assert captured == {
        "model": "claude-x", "input_tokens": 100, "output_tokens": 50,
        "cache_read": 10, "cache_write": 5, "context": "action"}


def test_record_usage_none_is_noop(monkeypatch):
    called = []
    import app.services.usage as usage
    monkeypatch.setattr(usage, "record", lambda *a, **k: called.append(1))
    llm._record_usage("claude-x", None, "action")
    assert called == []


def test_record_usage_swallows_errors(monkeypatch):
    import app.services.usage as usage

    def _boom(*a, **k):
        raise RuntimeError("sink down")

    monkeypatch.setattr(usage, "record", _boom)
    # must not raise — metering is best-effort
    llm._record_usage("claude-x", _anthropic_usage(), "action")


def test_record_openai_usage_fields(monkeypatch):
    captured = {}
    import app.services.usage as usage
    monkeypatch.setattr(usage, "record", lambda model, **kw: captured.update({"model": model, **kw}))
    llm._record_openai_usage("grok-4.3", _oa_usage(p=200, c=80), "agent")
    assert captured == {
        "model": "grok-4.3", "input_tokens": 200, "output_tokens": 80, "context": "agent"}


def test_record_openai_usage_none_is_noop(monkeypatch):
    called = []
    import app.services.usage as usage
    monkeypatch.setattr(usage, "record", lambda *a, **k: called.append(1))
    llm._record_openai_usage("grok", None, "agent")
    assert called == []


def test_record_openai_usage_swallows_errors(monkeypatch):
    import app.services.usage as usage
    monkeypatch.setattr(usage, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    llm._record_openai_usage("grok", _oa_usage(), "agent")  # no raise


# ===========================================================================
# Module-level facade routes through get_provider
# ===========================================================================

def test_facade_complete_routes_by_model(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk", xai_api_key="xai")
    seen = {}

    class Stub:
        def complete(self, messages, *, system=None, model=None, max_tokens=1024):
            seen["complete"] = (system, model, max_tokens)
            return "ok"

        def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024):
            seen["meta"] = (system, model, max_tokens)
            return "ok", "end_turn"

        def complete_with_tools(self, messages, *, system=None, tools, model=None, max_tokens=1024):
            seen["tools"] = (tools, model, max_tokens)
            return "ok", [], None

        def append_tool_results(self, messages, results):
            seen["append"] = results

    stub = Stub()
    monkeypatch.setattr(llm, "get_provider", lambda model=None: stub)

    assert llm.complete([{"role": "user", "content": "q"}], system="s", max_tokens=99) == "ok"
    assert seen["complete"] == ("s", None, 99)
    assert llm.complete_with_meta([], model="m") == ("ok", "end_turn")
    assert seen["meta"][1] == "m"
    assert llm.complete_with_tools([], tools=[], model="m2") == ("ok", [], None)
    assert seen["tools"][1] == "m2"
    res = [ToolResult(tool_call_id="1", content="c")]
    llm.append_tool_results([], res)
    assert seen["append"] is res


def test_cached_client_builds_once_and_reuses():
    llm._client_cache.clear()
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = llm._cached_client(("k",), factory)
    b = llm._cached_client(("k",), factory)
    assert a is b
    assert calls == [1]  # factory invoked exactly once
    llm._client_cache.clear()


# ===========================================================================
# Client factories: SDK class is constructed once and cached per key.
# We stub the SDK constructors (no real network) and assert the cache key wiring.
# ===========================================================================

def test_anthropic_client_factories_cache(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk-ant")
    import anthropic
    built = []

    def _fake_ctor(name):
        def _ctor(**kw):
            built.append((name, kw))
            return SimpleNamespace(kind=name, **kw)
        return _ctor

    monkeypatch.setattr(anthropic, "Anthropic", _fake_ctor("sync"))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_ctor("async"))
    p = AnthropicProvider()
    c1 = p._sync_client()
    c2 = p._sync_client()
    a1 = p._async_client()
    assert c1 is c2  # cached
    assert c1.kind == "sync" and a1.kind == "async"
    assert c1.api_key == "sk-ant"
    assert c1.timeout == llm._LLM_TIMEOUT
    # sync built once, async once
    assert [b[0] for b in built] == ["sync", "async"]


def test_xai_client_factory_passes_base_url(patch_settings, monkeypatch):
    patch_settings(xai_api_key="xai-key", xai_base_url="https://example/v1")
    import openai
    built = []

    def _fake_ctor(name):
        def _ctor(**kw):
            built.append((name, kw))
            return SimpleNamespace(kind=name, **kw)
        return _ctor

    monkeypatch.setattr(openai, "OpenAI", _fake_ctor("sync"))
    monkeypatch.setattr(openai, "AsyncOpenAI", _fake_ctor("async"))
    p = XAIProvider()
    sync = p._client()
    sync2 = p._client()
    asyncc = p._client(async_=True)
    assert sync is sync2  # cached on (provider, sync, key, base)
    assert sync.kind == "sync" and asyncc.kind == "async"
    assert sync.base_url == "https://example/v1"
    assert sync.api_key == "xai-key"


# ===========================================================================
# Async streaming turns — fake async clients (no network, no real SDK calls).
# ===========================================================================

async def _drain(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


class _FakeAnthropicStream:
    """Async context manager yielding content_block_delta events + final message."""
    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for e in self._events:
                yield e
        return _gen()

    async def get_final_message(self):
        return self._final


def _delta_event(dtype, **kw):
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=dtype, **kw))


def test_anthropic_stream_turn_text_thinking_and_tool(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(llm_api_key="sk", llm_model="claude-sonnet-4-6")
    events = [
        SimpleNamespace(type="message_start"),                      # ignored (not a delta)
        _delta_event("text_delta", text="Hello"),
        _delta_event("thinking_delta", thinking="pondering"),
        _delta_event("thinking_delta"),                             # no thinking attr -> ""
        _delta_event("signature_delta"),                            # not thinking/text -> ignored
        _delta_event("input_json_delta", partial_json="x"),         # other delta type -> ignored
    ]
    final = _anthropic_msg(
        text="Hello",
        tool_use=[{"id": "tu_1", "name": "search", "args": {"q": "z"}}],
        stop_reason="tool_use",
        usage=_anthropic_usage(40, 9))
    captured = {}

    def _stream(**kwargs):
        captured.update(kwargs)
        return _FakeAnthropicStream(events, final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=_stream))
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_async_client", lambda: client)

    messages = [{"role": "user", "content": "go"}]
    tools = [ToolDef(name="search", description="d", json_schema={"type": "object"})]
    import asyncio
    out = asyncio.run(_drain(p.stream_turn(
        messages, system="sys", tools=tools, model=None, max_tokens=512, thinking=True)))

    text_deltas = [e.text for e in out if isinstance(e, llm.TextDelta)]
    think_deltas = [e.text for e in out if isinstance(e, llm.ThinkingDelta)]
    tool_events = [e for e in out if isinstance(e, llm.ToolCallEvent)]
    ends = [e for e in out if isinstance(e, llm.TurnEnd)]
    assert text_deltas == ["Hello"]
    assert think_deltas == ["pondering", ""]            # signature_delta -> empty thinking
    assert len(tool_events) == 1
    assert tool_events[0].call == ToolCall(id="tu_1", name="search", args={"q": "z"})
    assert len(ends) == 1
    assert ends[0].stop_reason == "tool_use"
    assert ends[0].usage == {"input_tokens": 40, "output_tokens": 9}
    assert ends[0].tool_calls == [tool_events[0].call]
    # thinking flag wired into kwargs; assistant turn appended verbatim
    assert captured["thinking"] == llm._ANTHROPIC_THINKING
    assert captured["model"] == "claude-sonnet-4-6"
    assert messages[-1] == {"role": "assistant", "content": final.content}
    assert _no_usage_sink[-1]["context"] == "agent"


def test_anthropic_stream_turn_no_thinking_no_usage(patch_settings, monkeypatch):
    patch_settings(llm_api_key="sk")
    final = _anthropic_msg(text="done", stop_reason="end_turn", usage=None)
    captured = {}

    def _stream(**kwargs):
        captured.update(kwargs)
        return _FakeAnthropicStream([_delta_event("text_delta", text="done")], final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=_stream))
    p = AnthropicProvider()
    monkeypatch.setattr(p, "_async_client", lambda: client)
    import asyncio
    out = asyncio.run(_drain(p.stream_turn(
        [{"role": "user", "content": "q"}], system=None, tools=[], model=None,
        max_tokens=64, thinking=False)))
    ends = [e for e in out if isinstance(e, llm.TurnEnd)]
    assert "thinking" not in captured       # flag off -> not wired
    assert ends[0].usage is None            # no usage reported
    assert ends[0].tool_calls == []


class _FakeOAStream:
    """Awaitable-created async-iterable stream with a close()."""
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()

    async def close(self):
        self.closed = True


def _oa_chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _oa_tc_frag(index, *, id=None, name=None, args=None):
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=args))


def test_xai_stream_turn_text_and_tool_calls(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")
    chunks = [
        _oa_chunk(content="Hel"),
        _oa_chunk(content="lo"),
        # tool call streamed in fragments across two chunks
        _oa_chunk(tool_calls=[_oa_tc_frag(0, id="call_1", name="search", args='{"q":')]),
        _oa_chunk(tool_calls=[_oa_tc_frag(0, args='"y"}')]),
        _oa_chunk(finish_reason="tool_calls"),               # stops the loop here
        _oa_chunk(content="never-seen"),                     # after break: not consumed
    ]
    stream = _FakeOAStream(chunks)
    captured = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=_create)))
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)

    messages = [{"role": "user", "content": "find y"}]
    tools = [ToolDef(name="search", description="d", json_schema={"type": "object"})]
    import asyncio
    out = asyncio.run(_drain(p.stream_turn(
        messages, system="sys", tools=tools, model=None, max_tokens=300, thinking=True)))

    text_deltas = [e.text for e in out if isinstance(e, llm.TextDelta)]
    tool_events = [e for e in out if isinstance(e, llm.ToolCallEvent)]
    ends = [e for e in out if isinstance(e, llm.TurnEnd)]
    assert text_deltas == ["Hel", "lo"]                      # "never-seen" not yielded
    assert tool_events[0].call == ToolCall(id="call_1", name="search", args={"q": "y"})
    assert ends[0].stop_reason == "tool_calls"
    assert stream.closed is True                             # stream closed in finally
    # assistant turn appended (OpenAI shape) with assembled tool_calls
    amsg = messages[-1]
    assert amsg["role"] == "assistant" and amsg["content"] == "Hello"
    assert amsg["tool_calls"][0]["function"]["arguments"] == '{"q":"y"}'
    # usage included via stream_options
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}


def test_xai_stream_turn_usage_and_fallback_call_id(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")
    chunks = [
        _oa_chunk(usage=_oa_usage(p=21, c=6)),                       # usage-only chunk
        _oa_chunk(),                                                 # chunk with no choices content
        # tool fragment WITHOUT an id -> fallback id "call_0"
        _oa_chunk(tool_calls=[_oa_tc_frag(0, name="t", args="not json{")]),
        _oa_chunk(finish_reason="stop"),
    ]
    # first chunk has choices? give it an empty-choices chunk for the `not choices` branch
    chunks[0] = SimpleNamespace(choices=None, usage=_oa_usage(p=21, c=6))
    stream = _FakeOAStream(chunks)

    async def _create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=_create)))
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)

    import asyncio
    messages = [{"role": "user", "content": "q"}]
    out = asyncio.run(_drain(p.stream_turn(
        messages, system=None, tools=[], model="grok-x", max_tokens=100, thinking=False)))
    tool_events = [e for e in out if isinstance(e, llm.ToolCallEvent)]
    ends = [e for e in out if isinstance(e, llm.TurnEnd)]
    assert tool_events[0].call.id == "call_0"                # fallback id
    assert tool_events[0].call.args == {}                    # malformed json -> empty
    assert ends[0].usage == {"input_tokens": 21, "output_tokens": 6}
    assert ends[0].stop_reason == "stop"


def test_xai_stream_turn_close_errors_swallowed(patch_settings, monkeypatch, _no_usage_sink):
    patch_settings(xai_api_key="xai", llm_model="grok-4.3")

    class _BadCloseStream(_FakeOAStream):
        async def close(self):
            raise RuntimeError("close failed")

    stream = _BadCloseStream([_oa_chunk(content="hi"), _oa_chunk(finish_reason="stop")])

    async def _create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=_create)))
    p = XAIProvider()
    monkeypatch.setattr(p, "_client", lambda async_=False: client)
    import asyncio
    out = asyncio.run(_drain(p.stream_turn(
        [{"role": "user", "content": "q"}], system=None, tools=[], model=None,
        max_tokens=50, thinking=False)))
    # close() raised but was swallowed; turn still completed
    assert any(isinstance(e, llm.TurnEnd) for e in out)
