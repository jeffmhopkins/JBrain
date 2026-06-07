"""Provider-agnostic LLM layer.

All text generation goes through an LLMProvider. The rest of the app speaks in
neutral terms — Message, ToolDef, ToolCall, ToolResult, and the streaming events
TextDelta / ToolCallEvent / TurnEnd — and each provider adapter translates to and
from its SDK. The agent loop never constructs provider-specific message shapes:
the provider appends its own assistant turn inside stream_turn and exposes
append_tool_results for the loop to hand tool answers back.

Embeddings are local (fastembed) and intentionally NOT part of this layer.

Only AnthropicProvider ships today. To add a provider: implement LLMProvider,
register it in _REGISTRY, and select it via the LLM_PROVIDER setting.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Protocol, runtime_checkable

from ..config import get_settings

# A conversation turn. Seed/history turns are plain {"role","content": str}. Turns
# the model authors (assistant + tool calls) and tool-result turns are opaque,
# provider-owned elements appended via the provider — the app never inspects them.
Message = dict[str, Any]

# Cap how long a single LLM request can block (a hung provider must not freeze a
# scheduled workflow / the request handling it).
_LLM_TIMEOUT = 120.0

# LLM SDK clients each own an httpx connection pool (persistent sockets / file descriptors)
# and are designed to be long-lived and reused. Constructing a fresh one per call leaked FDs,
# sockets, and memory under the single uvicorn worker — they were reclaimed only by nondeterministic
# GC, so "a couple of research turns" (many calls each) climbed toward the open-FD ceiling and the
# server stopped accepting connections. Cache one client per (provider, sync/async, credentials):
# a credential change yields a new cache key and a fresh client; the old one is dropped. httpx
# clients are safe to share across threads (sync) and across awaits on the single event loop (async).
_client_lock = threading.Lock()
_client_cache: dict[tuple, Any] = {}


def _cached_client(key: tuple, factory):
    cli = _client_cache.get(key)
    if cli is None:
        with _client_lock:
            cli = _client_cache.get(key)
            if cli is None:
                cli = factory()
                _client_cache[key] = cli
    return cli


@dataclass
class ToolDef:
    name: str
    description: str
    json_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    tool_call_id: str
    content: str


# --- Neutral streaming events ----------------------------------------------
@dataclass
class TextDelta:
    text: str


# A chunk of the model's extended-thinking (reasoning) text. Only emitted when a
# caller opts in via stream_turn(thinking=True) AND the provider supports it
# (Anthropic only); the signature-bearing thinking blocks themselves are preserved
# verbatim inside the assistant turn the adapter appends to `messages`.
@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class TurnEnd:
    tool_calls: list[ToolCall]  # empty => the model is done (no tools requested)
    usage: dict | None = None   # {"input_tokens", "output_tokens"} if the provider reports it
    stop_reason: str | None = None  # provider's finish reason; "max_tokens"/"length" => truncated


StreamEvent = TextDelta | ThinkingDelta | ToolCallEvent | TurnEnd

# Extended-thinking request shape for Anthropic. `display:"summarized"` is REQUIRED on
# Opus/Sonnet 4.x — the default is "omitted", which streams empty thinking text. Kept as
# a single constant so the rebuild engine's "show the AI's thoughts" can't silently break.
_ANTHROPIC_THINKING = {"type": "adaptive", "display": "summarized"}


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def has_credentials(self) -> bool: ...
    def default_model(self) -> str: ...
    def supports_tools(self) -> bool: ...

    def complete(self, messages: list[Message], *, system: str | None = None,
                 model: str | None = None, max_tokens: int = 1024) -> str: ...

    def complete_with_meta(self, messages: list[Message], *, system: str | None = None,
                           model: str | None = None,
                           max_tokens: int = 1024) -> tuple[str, str | None]: ...

    def complete_with_tools(self, messages: list[Message], *, system: str | None,
                            tools: list[ToolDef], model: str | None,
                            max_tokens: int) -> tuple[str, list[ToolCall], dict | None]: ...

    def stream_turn(self, messages: list[Message], *, system: str | None,
                    tools: list[ToolDef], model: str | None,
                    max_tokens: int, thinking: bool = False) -> AsyncGenerator[StreamEvent, None]: ...

    def append_tool_results(self, messages: list[Message], results: list[ToolResult]) -> None: ...


# --- Anthropic adapter ------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"

    def has_credentials(self) -> bool:
        return bool(get_settings().llm_api_key)

    def default_model(self) -> str:
        return get_settings().llm_model

    def supports_tools(self) -> bool:
        return True

    def _sync_client(self):
        from anthropic import Anthropic
        key = get_settings().llm_api_key
        return _cached_client(("anthropic", "sync", key),
                              lambda: Anthropic(api_key=key, timeout=_LLM_TIMEOUT))

    def _async_client(self):
        from anthropic import AsyncAnthropic
        key = get_settings().llm_api_key
        return _cached_client(("anthropic", "async", key),
                              lambda: AsyncAnthropic(api_key=key, timeout=_LLM_TIMEOUT))

    def complete(self, messages, *, system=None, model=None, max_tokens=1024) -> str:
        text, _ = self.complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)
        return text

    def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024) -> tuple[str, str | None]:
        """Like complete(), but also returns the provider's finish reason. "max_tokens"
        means the body was cut off (the trailing ## References block is the first casualty)
        — batch writers use this to fail rather than save a half-written article."""
        client = self._sync_client()
        kwargs: dict = {"model": model or self.default_model(), "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        _record_usage(kwargs["model"], getattr(msg, "usage", None), "action")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return text, getattr(msg, "stop_reason", None)

    def complete_with_tools(self, messages, *, system=None, tools, model=None, max_tokens=1024):
        """One SYNCHRONOUS tool-capable turn (non-streaming). Appends the model's turn to
        `messages` (opaque SDK blocks, re-sent next iteration) and returns
        (text, tool_calls, usage). The caller dispatches the calls and hands answers back
        via append_tool_results — the same loop shape as stream_turn, minus streaming.
        Used by the recipient labs assistant, which can't run the async stream."""
        client = self._sync_client()
        wire_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.json_schema}
            for t in tools
        ]
        kwargs: dict = {"model": model or self.default_model(), "max_tokens": max_tokens,
                        "messages": messages, "tools": wire_tools}
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        _record_usage(kwargs["model"], getattr(msg, "usage", None), "agent")
        messages.append({"role": "assistant", "content": msg.content})
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        calls = [ToolCall(id=b.id, name=b.name, args=b.input)
                 for b in msg.content if getattr(b, "type", None) == "tool_use"]
        u = getattr(msg, "usage", None)
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0} if u else None
        return text, calls, usage

    async def stream_turn(self, messages, *, system, tools, model, max_tokens, thinking=False):
        client = self._async_client()
        wire_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.json_schema}
            for t in tools
        ]
        kwargs: dict = {
            "model": model or self.default_model(),
            "max_tokens": max_tokens,
            "system": system,
            "tools": wire_tools,
            "messages": messages,
        }
        if thinking:
            kwargs["thinking"] = _ANTHROPIC_THINKING
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                dt = getattr(event.delta, "type", None)
                if dt == "text_delta":
                    yield TextDelta(event.delta.text)
                elif dt == "thinking_delta":
                    # The reasoning summary; signature_delta blocks are ignored here (the
                    # full signed thinking block survives in final.content below).
                    yield ThinkingDelta(getattr(event.delta, "thinking", "") or "")
            final = await stream.get_final_message()

        # Record the model's turn verbatim (SDK content blocks) — opaque state the
        # loop re-sends next iteration without inspecting it.
        messages.append({"role": "assistant", "content": final.content})

        calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in final.content if b.type == "tool_use"
        ]
        for c in calls:
            yield ToolCallEvent(c)
        u = getattr(final, "usage", None)
        _record_usage(model or self.default_model(), u, "agent")
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0} if u else None
        yield TurnEnd(calls, usage=usage, stop_reason=getattr(final, "stop_reason", None))

    def append_tool_results(self, messages, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content}
                for r in results
            ],
        })


# --- xAI (Grok) adapter — OpenAI-compatible (api.x.ai/v1) -------------------

class XAIProvider:
    name = "xai"

    def _key(self) -> str:
        s = get_settings()
        return s.xai_api_key or s.llm_api_key

    def _client(self, async_: bool = False):
        from openai import AsyncOpenAI, OpenAI
        s = get_settings()
        cls = AsyncOpenAI if async_ else OpenAI
        key, base = self._key(), s.xai_base_url
        return _cached_client(("xai", "async" if async_ else "sync", key, base),
                              lambda: cls(api_key=key, base_url=base, timeout=_LLM_TIMEOUT))

    def has_credentials(self) -> bool:
        return bool(self._key())

    def default_model(self) -> str:
        m = get_settings().llm_model
        return m if (m or "").lower().startswith("grok") else "grok-4.3"

    def supports_tools(self) -> bool:
        return True

    def _translate(self, m: Message) -> Message:
        """Translate any Anthropic-style content blocks (e.g. vision images) in a
        message to OpenAI/xAI shape. Plain string content passes through."""
        content = m.get("content")
        if not isinstance(content, list):
            return m
        out = []
        for b in content:
            t = b.get("type")
            if t == "text":
                out.append({"type": "text", "text": b.get("text", "")})
            elif t == "image":
                src = b.get("source", {})
                out.append({"type": "image_url", "image_url": {
                    "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
            elif t == "tool_result":
                out.append({"type": "text", "text": str(b.get("content", ""))})
            else:
                out.append(b)
        return {**m, "content": out}

    def _wire(self, messages: list[Message], system: str | None) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend(self._translate(m) for m in messages)
        return out

    def complete(self, messages, *, system=None, model=None, max_tokens=1024) -> str:
        text, _ = self.complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)
        return text

    def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024) -> tuple[str, str | None]:
        """Like complete(), but also returns the OpenAI-compatible finish reason. "length"
        means the body was cut off — batch writers fail rather than save a half-written one."""
        client = self._client()
        resp = client.chat.completions.create(
            model=model or self.default_model(), max_tokens=max_tokens,
            messages=self._wire(messages, system))
        _record_openai_usage(model or self.default_model(), getattr(resp, "usage", None), "action")
        return (resp.choices[0].message.content or "").strip(), getattr(resp.choices[0], "finish_reason", None)

    def complete_with_tools(self, messages, *, system=None, tools, model=None, max_tokens=1024):
        """Synchronous tool-capable turn (OpenAI-compatible). Mirrors the Anthropic adapter:
        appends the assistant turn (incl. any tool_calls) to `messages` and returns
        (text, tool_calls, usage)."""
        client = self._client()
        wire_tools = [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.json_schema}} for t in tools] or None
        resp = client.chat.completions.create(
            model=model or self.default_model(), max_tokens=max_tokens,
            messages=self._wire(messages, system), tools=wire_tools)
        _record_openai_usage(model or self.default_model(), getattr(resp, "usage", None), "agent")
        choice = resp.choices[0].message
        text = choice.content or ""
        calls, oa_calls = [], []
        for tc in (getattr(choice, "tool_calls", None) or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:  # noqa: BLE001 — malformed args blob → empty
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
            oa_calls.append({"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
        amsg: dict = {"role": "assistant", "content": text or None}
        if oa_calls:
            amsg["tool_calls"] = oa_calls
        messages.append(amsg)
        u = getattr(resp, "usage", None)
        usage = {"input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                 "output_tokens": getattr(u, "completion_tokens", 0) or 0} if u else None
        return text, calls, usage

    async def stream_turn(self, messages, *, system, tools, model, max_tokens, thinking=False):
        # xAI/Grok has no extended-thinking concept — the flag is accepted and ignored
        # (the rebuild engine simply won't receive ThinkingDelta events on this provider).
        client = self._client(async_=True)
        wire_tools = [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.json_schema}} for t in tools] or None
        acc: dict[int, dict] = {}     # streamed tool-call fragments, by index
        usage = None
        text_parts: list[str] = []
        finish = None
        stream = await client.chat.completions.create(
            model=model or self.default_model(), max_tokens=max_tokens,
            messages=self._wire(messages, system), tools=wire_tools, stream=True,
            stream_options={"include_usage": True})
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    text_parts.append(delta.content)
                    yield TextDelta(delta.content)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = acc.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                # The turn is COMPLETE once we see a finish_reason — stop here rather than
                # waiting on a trailing usage/[DONE] chunk, which some xAI responses don't
                # close promptly (it left the agent stream hanging on "Responding…").
                if getattr(choice, "finish_reason", None):
                    finish = choice.finish_reason
                    break
        finally:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass
        # Assemble tool calls + record the assistant turn (OpenAI shape) for the loop.
        calls, oa_calls = [], []
        for i in sorted(acc):
            sl = acc[i]
            try:
                args = json.loads(sl["args"] or "{}")
            except Exception:  # noqa: BLE001 — a malformed args blob → empty
                args = {}
            cid = sl["id"] or f"call_{i}"
            calls.append(ToolCall(id=cid, name=sl["name"], args=args))
            oa_calls.append({"id": cid, "type": "function",
                             "function": {"name": sl["name"], "arguments": sl["args"] or "{}"}})
        amsg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
        if oa_calls:
            amsg["tool_calls"] = oa_calls
        messages.append(amsg)
        logging.getLogger("jbrain").info(
            "xai turn: model=%s finish=%s text_chars=%d tool_calls=%d (%s)",
            model or self.default_model(), finish, len("".join(text_parts)),
            len(calls), ",".join(c.name for c in calls))
        for c in calls:
            yield ToolCallEvent(c)
        _record_openai_usage(model or self.default_model(), usage, "agent")
        uu = {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
              "output_tokens": getattr(usage, "completion_tokens", 0) or 0} if usage else None
        yield TurnEnd(calls, usage=uu, stop_reason=finish)

    def append_tool_results(self, messages, results):
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})


def _record_openai_usage(model: str, u, context: str | None = None) -> None:
    if u is None:
        return
    try:
        from . import usage as _usage
        _usage.record(model, input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      output_tokens=getattr(u, "completion_tokens", 0) or 0, context=context)
    except Exception:  # noqa: BLE001
        pass


def _record_usage(model: str, u, context: str | None = None) -> None:
    """Log a call's token usage to the meter (best-effort). Reads the standard +
    prompt-cache token fields the Anthropic SDK reports so the cost estimate is
    sane when caching is on."""
    if u is None:
        return
    try:
        from . import usage as _usage
        _usage.record(
            model,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            context=context,
        )
    except Exception:  # noqa: BLE001
        pass


# --- Selection --------------------------------------------------------------

_REGISTRY: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "xai": XAIProvider,
    "grok": XAIProvider,   # alias
}


def _provider_for_model(model: str | None) -> str | None:
    """Infer the provider from a model id so the picker is the only control needed."""
    m = (model or "").lower()
    if m.startswith("grok"):
        return "xai"
    if m.startswith("claude"):
        return "anthropic"
    return None


def get_provider(model: str | None = None) -> LLMProvider:
    """The provider for a given model id — inferred from the id (grok*/claude*), else
    the configured LLM_PROVIDER default. Cheap + stateless, constructed per use."""
    name = _provider_for_model(model) or (get_settings().llm_provider or "anthropic").lower()
    cls = _REGISTRY.get(name, AnthropicProvider)
    return cls()


def model_for(tier: str) -> str | None:
    """Resolve a task tier (prompts.yaml `models.<tier>`) to a model id, or None to
    use the provider default. Lets routine jobs (tags, summaries, filing) run on a
    cheaper model than the interactive agent. Fallback: models.<tier> ->
    models.default -> None (provider default = LLM_MODEL)."""
    from . import prompts
    m = (prompts.get(f"models.{tier}") or "").strip()
    if m:
        return m
    return (prompts.get("models.default") or "").strip() or None


def has_credentials() -> bool:
    return get_provider().has_credentials()


def complete(messages: list[Message], *, system: str | None = None,
             model: str | None = None, max_tokens: int = 1024) -> str:
    return get_provider(model).complete(messages, system=system, model=model, max_tokens=max_tokens)


def complete_with_meta(messages: list[Message], *, system: str | None = None,
                       model: str | None = None, max_tokens: int = 1024) -> tuple[str, str | None]:
    """Non-streaming completion that ALSO surfaces the provider's finish reason. A
    stop_reason of "max_tokens" (Anthropic) / "length" (xAI) means the output was cut off
    at the token cap — batch writers (wiki_build) use this to retry-then-fail instead of
    silently saving a truncated article. Mirrors the live engine's TurnEnd.stop_reason."""
    return get_provider(model).complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)


def complete_with_tools(messages: list[Message], *, system: str | None = None, tools: list[ToolDef],
                        model: str | None = None, max_tokens: int = 1024) -> tuple[str, list[ToolCall], dict | None]:
    return get_provider(model).complete_with_tools(messages, system=system, tools=tools,
                                                   model=model, max_tokens=max_tokens)


def append_tool_results(messages: list[Message], results: list[ToolResult], *, model: str | None = None) -> None:
    get_provider(model).append_tool_results(messages, results)
