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

import asyncio
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
# scheduled workflow / the request handling it). This is the fallback default; the
# live value is read from settings via _timeout() so a CPU local box can raise it.
_LLM_TIMEOUT = 120.0


def _timeout() -> float:
    """Return the configured per-request LLM timeout in seconds (LLM_TIMEOUT_SECONDS).

    Falls back to _LLM_TIMEOUT when settings are unavailable or the value is unset.

    Returns:
        Timeout in seconds.
    """
    try:
        return float(get_settings().llm_timeout_seconds) or _LLM_TIMEOUT
    except Exception:  # noqa: BLE001 — settings not ready / bad value → safe default
        return _LLM_TIMEOUT

# Provider SDK retry budget. The Anthropic/OpenAI SDKs default to max_retries=2 and retry on
# 429/5xx/timeout — but the timeout above is PER-ATTEMPT, so the default silently triples the
# effective wait on a slow/overloaded provider (2 retries × per-request timeout + backoff), with
# no log line. That multi-minute wait is one of the most common "the chat just hangs" reports.
# Keep the retry budget low so a wedged provider surfaces an error quickly instead.
_LLM_MAX_RETRIES = 1


def _max_retries() -> int:
    """Return the configured provider-SDK retry budget (LLM_MAX_RETRIES).

    Falls back to _LLM_MAX_RETRIES when settings are unavailable or the value is unset.

    Returns:
        Maximum SDK retry attempts per request (0 disables retries).
    """
    try:
        return int(get_settings().llm_max_retries)
    except Exception:  # noqa: BLE001 — settings not ready / bad value → safe default
        return _LLM_MAX_RETRIES

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
    """Return a cached SDK client, constructing it via factory on first use.

    Double-checked locking so only one client is ever constructed per cache key,
    even under concurrent first-call races.

    Args:
        key: Hashable cache key (provider, sync/async, credentials...).
        factory: Zero-argument callable that constructs the client.

    Returns:
        Cached (or newly constructed) SDK client.
    """
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
    """Definition of a tool the LLM may call."""

    name: str
    description: str
    json_schema: dict


@dataclass
class ToolCall:
    """A single tool-use invocation requested by the model."""

    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    """The caller's answer to one ToolCall, keyed by the call's id."""

    tool_call_id: str
    content: str


# --- Neutral streaming events ----------------------------------------------
@dataclass
class TextDelta:
    """A streamed chunk of the model's text output."""

    text: str


# A chunk of the model's extended-thinking (reasoning) text. Only emitted when a
# caller opts in via stream_turn(thinking=True) AND the provider supports it
# (Anthropic only); the signature-bearing thinking blocks themselves are preserved
# verbatim inside the assistant turn the adapter appends to `messages`.
@dataclass
class ThinkingDelta:
    """A streamed chunk of the model's extended-thinking (reasoning) text."""

    text: str


@dataclass
class ToolCallEvent:
    """Streaming event carrying a single completed ToolCall."""

    call: ToolCall


@dataclass
class TurnEnd:
    """Final streaming event signalling the end of an assistant turn.

    Attributes:
        tool_calls: Tool calls requested this turn; empty means the model is done.
        usage: Token counts {"input_tokens", "output_tokens"} if the provider reports them.
        stop_reason: Provider finish reason; "max_tokens"/"length" means the output was
            truncated at the token cap.
    """

    tool_calls: list[ToolCall]
    usage: dict | None = None
    stop_reason: str | None = None


StreamEvent = TextDelta | ThinkingDelta | ToolCallEvent | TurnEnd

# Extended-thinking request shape for Anthropic. `display:"summarized"` is REQUIRED on
# Opus/Sonnet 4.x — the default is "omitted", which streams empty thinking text. Kept as
# a single constant so the rebuild engine's "show the AI's thoughts" can't silently break.
_ANTHROPIC_THINKING = {"type": "adaptive", "display": "summarized"}


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol all LLM provider adapters must satisfy.

    Attributes:
        name: Short provider identifier (e.g. 'anthropic', 'xai').
    """

    name: str

    def has_credentials(self) -> bool:
        """Return True if an API key for this provider is configured."""
        ...

    def default_model(self) -> str:
        """Return the default model id for this provider."""
        ...

    def supports_tools(self) -> bool:
        """Return True if the provider supports tool-use (function calling)."""
        ...

    def complete(self, messages: list[Message], *, system: str | None = None,
                 model: str | None = None, max_tokens: int = 1024) -> str:
        """Return the model's text reply for the given messages (non-streaming).

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses provider default when None.
            max_tokens: Maximum output tokens.

        Returns:
            Model reply as a plain string.
        """
        ...

    def complete_with_meta(self, messages: list[Message], *, system: str | None = None,
                           model: str | None = None,
                           max_tokens: int = 1024) -> tuple[str, str | None]:
        """Like complete(), but also returns the provider's finish reason.

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses provider default when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, stop_reason). stop_reason is "max_tokens"/"length" when
            the output was truncated.
        """
        ...

    def complete_with_tools(self, messages: list[Message], *, system: str | None,
                            tools: list[ToolDef], model: str | None,
                            max_tokens: int) -> tuple[str, list[ToolCall], dict | None]:
        """One synchronous tool-capable turn; appends the assistant turn to messages.

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses provider default when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, tool_calls, usage_dict).
        """
        ...

    def stream_turn(self, messages: list[Message], *, system: str | None,
                    tools: list[ToolDef], model: str | None,
                    max_tokens: int, thinking: bool = False) -> AsyncGenerator[StreamEvent, None]:
        """Stream one assistant turn, yielding StreamEvent instances.

        Appends the completed assistant turn to messages. Yields TextDelta / ThinkingDelta
        / ToolCallEvent / TurnEnd events. The caller loops, dispatching tool calls and
        calling append_tool_results, until TurnEnd.tool_calls is empty.

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses provider default when None.
            max_tokens: Maximum output tokens.
            thinking: Whether to request extended thinking (Anthropic only).

        Yields:
            StreamEvent instances in order.
        """
        ...

    def append_tool_results(self, messages: list[Message], results: list[ToolResult]) -> None:
        """Append tool-result messages to the conversation history.

        Args:
            messages: Conversation history to mutate.
            results: Tool call answers to append.
        """
        ...


# --- Anthropic adapter ------------------------------------------------------

class AnthropicProvider:
    """LLMProvider adapter for the Anthropic API (Claude models)."""

    name = "anthropic"

    def has_credentials(self) -> bool:
        """Return True if an Anthropic API key is configured."""
        return bool(get_settings().llm_api_key)

    def default_model(self) -> str:
        """Return the configured default Anthropic model id."""
        return get_settings().llm_model

    def supports_tools(self) -> bool:
        """Return True; Anthropic supports tool use."""
        return True

    def _sync_client(self):
        """Return a cached synchronous Anthropic client for the current API key."""
        from anthropic import Anthropic
        key = get_settings().llm_api_key
        return _cached_client(("anthropic", "sync", key),
                              lambda: Anthropic(api_key=key, timeout=_timeout(), max_retries=_max_retries()))

    def _async_client(self):
        """Return a cached async Anthropic client for the current API key."""
        from anthropic import AsyncAnthropic
        key = get_settings().llm_api_key
        return _cached_client(("anthropic", "async", key),
                              lambda: AsyncAnthropic(api_key=key, timeout=_timeout(), max_retries=_max_retries()))

    def complete(self, messages, *, system=None, model=None, max_tokens=1024) -> str:
        """Return the model's text reply (non-streaming, Anthropic).

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Model reply as a plain string.
        """
        text, _ = self.complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)
        return text

    def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024) -> tuple[str, str | None]:
        """Like complete(), but also returns the Anthropic finish reason.

        "max_tokens" means the body was cut off (the trailing ## References block is the
        first casualty) — batch writers use this to fail rather than save a half-written
        article.

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, stop_reason).
        """
        client = self._sync_client()
        kwargs: dict = {"model": model or self.default_model(), "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        _record_usage(kwargs["model"], getattr(msg, "usage", None), "action")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return text, getattr(msg, "stop_reason", None)

    def complete_with_tools(self, messages, *, system=None, tools, model=None, max_tokens=1024):
        """One synchronous tool-capable turn (non-streaming) via Anthropic.

        Appends the model's turn to messages (opaque SDK blocks, re-sent next iteration)
        and returns (text, tool_calls, usage). The caller dispatches the calls and hands
        answers back via append_tool_results — the same loop shape as stream_turn, minus
        streaming. Used by the recipient labs assistant, which can't run the async stream.

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, tool_calls, usage_dict).
        """
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
        """Stream one assistant turn via the Anthropic API, yielding StreamEvent instances.

        Appends the completed assistant turn to messages. Optionally enables extended
        thinking (adaptive summarized mode), which yields ThinkingDelta events.

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.
            thinking: If True, request extended thinking (Anthropic adaptive mode).

        Yields:
            TextDelta, ThinkingDelta, ToolCallEvent, and TurnEnd events.
        """
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
        # Offload the usage write: it opens a fresh connection and commits a row (a WAL writer),
        # which under write-lock contention blocks up to busy_timeout. Doing that inline here would
        # block the EVENT LOOP (stream_turn is iterated on it), freezing every other chat/keepalive/
        # poll — and the SSE no-progress watchdog can't save a frozen loop. Awaited so the write
        # still completes (and stays ordered) before the turn ends.
        await asyncio.to_thread(_record_usage, model or self.default_model(), u, "agent")
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0} if u else None
        yield TurnEnd(calls, usage=usage, stop_reason=getattr(final, "stop_reason", None))

    def append_tool_results(self, messages, results):
        """Append tool results as an Anthropic-style user turn.

        Args:
            messages: Conversation history to mutate.
            results: Tool call answers to append.
        """
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content}
                for r in results
            ],
        })


# --- xAI (Grok) adapter — OpenAI-compatible (api.x.ai/v1) -------------------

class XAIProvider:
    """LLMProvider adapter for the xAI Grok API (OpenAI-compatible, api.x.ai/v1)."""

    name = "xai"

    def _key(self) -> str:
        """Return the xAI API key, falling back to the generic LLM key."""
        s = get_settings()
        return s.xai_api_key or s.llm_api_key

    def _client(self, async_: bool = False):
        """Return a cached OpenAI-compatible client pointed at the xAI base URL.

        Args:
            async_: If True, return an AsyncOpenAI client; otherwise OpenAI.

        Returns:
            Cached sync or async OpenAI-compatible client.
        """
        from openai import AsyncOpenAI, OpenAI
        s = get_settings()
        cls = AsyncOpenAI if async_ else OpenAI
        key, base = self._key(), s.xai_base_url
        return _cached_client(("xai", "async" if async_ else "sync", key, base),
                              lambda: cls(api_key=key, base_url=base, timeout=_timeout(), max_retries=_max_retries()))

    def has_credentials(self) -> bool:
        """Return True if an xAI (or fallback LLM) API key is configured."""
        return bool(self._key())

    def default_model(self) -> str:
        """Return the configured model if it starts with 'grok', else 'grok-4.3'."""
        m = get_settings().llm_model
        return m if (m or "").lower().startswith("grok") else "grok-4.3"

    def supports_tools(self) -> bool:
        """Return True; xAI Grok supports tool use."""
        return True

    def _translate(self, m: Message) -> Message:
        """Translate Anthropic-style content blocks to OpenAI/xAI shape.

        Handles text, image (base64), and tool_result blocks. Plain string content
        passes through unchanged.

        Args:
            m: Message dict, possibly with a list 'content' of typed blocks.

        Returns:
            Message dict with content translated to OpenAI-compatible blocks.
        """
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
        """Convert messages + optional system prompt to the OpenAI-compatible wire format.

        Args:
            messages: Conversation history in neutral format.
            system: Optional system prompt; prepended as a 'system' role message.

        Returns:
            List of OpenAI-compatible message dicts.
        """
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend(self._translate(m) for m in messages)
        return out

    def complete(self, messages, *, system=None, model=None, max_tokens=1024) -> str:
        """Return the model's text reply (non-streaming, xAI).

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Model reply as a plain string.
        """
        text, _ = self.complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)
        return text

    def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024) -> tuple[str, str | None]:
        """Like complete(), but also returns the OpenAI-compatible finish reason.

        "length" means the body was cut off — batch writers fail rather than save a
        half-written one.

        Args:
            messages: Conversation history.
            system: Optional system prompt.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, finish_reason).
        """
        client = self._client()
        resp = client.chat.completions.create(
            model=model or self.default_model(), max_tokens=max_tokens,
            messages=self._wire(messages, system))
        _record_openai_usage(model or self.default_model(), getattr(resp, "usage", None), "action")
        return (resp.choices[0].message.content or "").strip(), getattr(resp.choices[0], "finish_reason", None)

    def complete_with_tools(self, messages, *, system=None, tools, model=None, max_tokens=1024):
        """Synchronous tool-capable turn (OpenAI-compatible, xAI).

        Mirrors the Anthropic adapter: appends the assistant turn (including any
        tool_calls) to messages and returns (text, tool_calls, usage).

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (text, tool_calls, usage_dict).
        """
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
        """Stream one assistant turn via the xAI (Grok) API, yielding StreamEvent instances.

        xAI/Grok has no extended-thinking concept — the thinking flag is accepted and
        ignored (no ThinkingDelta events will be yielded on this provider). Stops as soon
        as a finish_reason is seen rather than waiting for trailing usage/[DONE] chunks,
        which some xAI responses don't close promptly.

        Args:
            messages: Conversation history; mutated to append the assistant turn.
            system: System prompt.
            tools: Tool definitions available this turn.
            model: Model id override; uses default_model() when None.
            max_tokens: Maximum output tokens.
            thinking: Accepted but ignored on xAI.

        Yields:
            TextDelta, ToolCallEvent, and TurnEnd events.
        """
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
        # Offload the usage write off the event loop — see the note in AnthropicProvider.stream_turn.
        await asyncio.to_thread(_record_openai_usage, model or self.default_model(), usage, "agent")
        uu = {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
              "output_tokens": getattr(usage, "completion_tokens", 0) or 0} if usage else None
        yield TurnEnd(calls, usage=uu, stop_reason=finish)

    def append_tool_results(self, messages, results):
        """Append tool results as individual OpenAI-compatible tool role messages.

        Args:
            messages: Conversation history to mutate.
            results: Tool call answers to append.
        """
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})


# --- Local (Ollama / OpenAI-compatible) adapter ----------------------------

class LocalProvider(XAIProvider):
    """LLMProvider for a local OpenAI-compatible server (Ollama's /v1 endpoint).

    Reuses the xAI adapter's OpenAI-compatible wire/translate/stream logic, pointed at
    the local server (LLM_LOCAL_BASE_URL) with a placeholder key (Ollama ignores auth).
    Intended for the `cheap` tier (tagging/titling/summaries) on CPU-only hardware via
    plain complete()/complete_with_meta(); the interactive agent stays on the cloud
    provider. Tool-use and streaming are inherited but best-effort on Ollama — do NOT
    route the agent here.
    """

    name = "local"

    def _key(self) -> str:
        """Return a placeholder key (the local server ignores authentication)."""
        return "ollama"

    def _client(self, async_: bool = False):
        """Return a cached OpenAI-compatible client pointed at the local base URL.

        Args:
            async_: If True, return an AsyncOpenAI client; otherwise OpenAI.

        Returns:
            Cached sync or async OpenAI-compatible client.
        """
        from openai import AsyncOpenAI, OpenAI
        s = get_settings()
        cls = AsyncOpenAI if async_ else OpenAI
        key, base = self._key(), s.llm_local_base_url
        return _cached_client(("local", "async" if async_ else "sync", key, base),
                              lambda: cls(api_key=key, base_url=base, timeout=_timeout(), max_retries=_max_retries()))

    def has_credentials(self) -> bool:
        """Return True when local-LLM support is enabled (no API key is required)."""
        return bool(get_settings().has_local)

    def default_model(self) -> str:
        """Return the configured local model id (LLM_LOCAL_MODEL)."""
        return get_settings().llm_local_model


def _record_openai_usage(model: str, u, context: str | None = None) -> None:
    """Log OpenAI-compatible token usage to the meter (best-effort).

    Args:
        model: Model id string for the meter record.
        u: OpenAI usage object with prompt_tokens/completion_tokens, or None.
        context: Optional label (e.g. 'agent' or 'action') for the usage record.
    """
    if u is None:
        return
    try:
        from . import usage as _usage
        _usage.record(model, input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      output_tokens=getattr(u, "completion_tokens", 0) or 0, context=context)
    except Exception:  # noqa: BLE001
        pass


def _record_usage(model: str, u, context: str | None = None) -> None:
    """Log Anthropic token usage to the meter (best-effort).

    Reads the standard and prompt-cache token fields the Anthropic SDK reports so
    the cost estimate is accurate when caching is on.

    Args:
        model: Model id string for the meter record.
        u: Anthropic usage object, or None.
        context: Optional label (e.g. 'agent' or 'action') for the usage record.
    """
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
    "local": LocalProvider,
    "ollama": LocalProvider,   # alias
}


def _is_local_model_id(model: str) -> bool:
    """Return True if a model id should route to the local provider.

    Only when local support is enabled (LLM_LOCAL_ENABLE + a base URL). An Ollama id
    carries a 'name:tag' colon that cloud ids never have; an explicit prefix list
    (LLM_LOCAL_PREFIXES) covers tagless local names like 'mistral'. The enable gate
    means a stray ':' in a cloud id can't silently route into a dead provider on a box
    with no local server.

    Args:
        model: Lowercased model id.

    Returns:
        True if the id maps to the local provider.
    """
    s = get_settings()
    if not (getattr(s, "llm_local_enable", False) and getattr(s, "llm_local_base_url", "")):
        return False
    if ":" in model:
        return True
    prefixes = [p.strip().lower() for p in (getattr(s, "llm_local_prefixes", "") or "").split(",") if p.strip()]
    return any(model.startswith(p) for p in prefixes)


def _provider_for_model(model: str | None) -> str | None:
    """Infer the provider name from a model id, or None if unrecognised.

    Recognises grok*/claude* by prefix and, when local support is enabled, Ollama-style
    ids (a 'name:tag' containing ':', or any configured local prefix) as 'local'. Returns
    None for an unrecognised id, so get_provider falls back to the configured default.

    Args:
        model: Model id string, e.g. 'claude-sonnet-4', 'grok-4.3', 'qwen2.5:7b'.

    Returns:
        Provider name ('anthropic', 'xai', 'local'), or None.
    """
    m = (model or "").lower()
    if m.startswith("grok"):
        return "xai"
    if m.startswith("claude"):
        return "anthropic"
    if m and _is_local_model_id(m):
        return "local"
    return None


def get_provider(model: str | None = None) -> LLMProvider:
    """Return the LLMProvider for a given model id.

    Provider is inferred from the model id prefix (grok*/claude*); falls back to the
    configured LLM_PROVIDER setting. Cheap and stateless — constructed per use.

    Args:
        model: Optional model id; None uses the configured default provider.

    Returns:
        An instantiated LLMProvider.
    """
    name = _provider_for_model(model) or (get_settings().llm_provider or "anthropic").lower()
    cls = _REGISTRY.get(name, AnthropicProvider)
    return cls()


def model_for(tier: str) -> str | None:
    """Resolve a task tier to a model id from prompts.yaml, or None for the provider default.

    Lets routine jobs (tags, summaries, filing) run on a cheaper model than the
    interactive agent. Fallback chain: models.<tier> → models.default → None
    (None means use the provider's LLM_MODEL setting).

    Args:
        tier: Tier key, e.g. 'cheap', 'synthesis', or 'default'.

    Returns:
        Model id string, or None to use the provider default.
    """
    from . import prompts
    m = (prompts.get(f"models.{tier}") or "").strip()
    if m:
        return m
    return (prompts.get("models.default") or "").strip() or None


def has_credentials() -> bool:
    """Return True if the default provider has an API key configured."""
    return get_provider().has_credentials()


def _local_known_down() -> bool:
    """Return True when the local LLM is in a known-unavailable readiness state.

    Lets the cloud fallback fire IMMEDIATELY instead of first waiting out a local
    request's full timeout (which on a CPU box is raised toward ~600s) when the
    readiness probe already shows the local box is unreachable, its last warm/pull
    failed, or the model isn't present. A wedged-but-listening local still reports
    'ready' and stays bounded only by the request timeout — this catches the cheap,
    detectable cases without second-guessing a transient warming/pulling state.

    Returns:
        True if local readiness is 'unavailable', 'failed', or 'absent'.
    """
    try:
        from . import local_models
        return local_models.readiness().get("state") in ("unavailable", "failed", "absent")
    except Exception:  # noqa: BLE001 — readiness must never break a generation
        return False


def _cloud_fallback_provider() -> LLMProvider | None:
    """Return a non-local cloud provider with credentials, for local-failure fallback.

    Used only when LLM_LOCAL_FALLBACK is on: a local-tier call that errors retries on
    the configured cloud default so a local outage degrades (costs a cloud call) rather
    than breaking a background job. Returns None when fallback is off or the cloud
    default has no credentials (e.g. an all-local install), in which case the caller
    surfaces the original error.

    Returns:
        A cloud LLMProvider to retry on, or None.
    """
    s = get_settings()
    if not getattr(s, "llm_local_fallback", False):
        return None
    name = (s.llm_provider or "anthropic").lower()
    if name in ("local", "ollama"):       # all-local: nothing cloud to fall back to
        return None
    prov = _REGISTRY.get(name, AnthropicProvider)()
    return prov if prov.has_credentials() else None


def complete(messages: list[Message], *, system: str | None = None,
             model: str | None = None, max_tokens: int = 1024) -> str:
    """Non-streaming text completion via the appropriate provider.

    When the resolved provider is local and the call fails, retries on the cloud default
    (if LLM_LOCAL_FALLBACK is on and a cloud key exists) so a local outage degrades
    rather than breaks. The retry uses the cloud provider's own default model.

    Args:
        messages: Conversation history.
        system: Optional system prompt.
        model: Model id; provider is inferred from it when given.
        max_tokens: Maximum output tokens.

    Returns:
        Model reply as a plain string.
    """
    prov = get_provider(model)
    fb = _cloud_fallback_provider() if getattr(prov, "name", None) == "local" else None
    # Fast-fail: when local is already known-down, go straight to cloud rather than waiting out
    # the local request's full timeout before the except below would fall back anyway.
    if fb is not None and _local_known_down():
        logging.getLogger("jbrain").warning("local LLM unavailable; using %s without attempting local", fb.name)
        return fb.complete(messages, system=system, model=None, max_tokens=max_tokens)
    try:
        return prov.complete(messages, system=system, model=model, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 — local outage → degrade to cloud when configured
        if fb is None:
            raise
        logging.getLogger("jbrain").warning("local LLM failed; falling back to %s", fb.name)
        return fb.complete(messages, system=system, model=None, max_tokens=max_tokens)


def complete_with_meta(messages: list[Message], *, system: str | None = None,
                       model: str | None = None, max_tokens: int = 1024) -> tuple[str, str | None]:
    """Non-streaming completion that also surfaces the provider's finish reason.

    A stop_reason of "max_tokens" (Anthropic) / "length" (xAI) means the output was cut
    off at the token cap — batch writers (wiki_build) use this to retry-then-fail instead
    of silently saving a truncated article. Mirrors the live engine's TurnEnd.stop_reason.

    Args:
        messages: Conversation history.
        system: Optional system prompt.
        model: Model id; provider is inferred from it when given.
        max_tokens: Maximum output tokens.

    Returns:
        Tuple of (text, stop_reason).
    """
    prov = get_provider(model)
    fb = _cloud_fallback_provider() if getattr(prov, "name", None) == "local" else None
    # Fast-fail: skip a known-down local outright (see complete() for the rationale).
    if fb is not None and _local_known_down():
        logging.getLogger("jbrain").warning("local LLM unavailable; using %s without attempting local", fb.name)
        return fb.complete_with_meta(messages, system=system, model=None, max_tokens=max_tokens)
    try:
        return prov.complete_with_meta(messages, system=system, model=model, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 — local outage → degrade to cloud when configured
        if fb is None:
            raise
        logging.getLogger("jbrain").warning("local LLM failed; falling back to %s", fb.name)
        return fb.complete_with_meta(messages, system=system, model=None, max_tokens=max_tokens)


def complete_with_tools(messages: list[Message], *, system: str | None = None, tools: list[ToolDef],
                        model: str | None = None, max_tokens: int = 1024) -> tuple[str, list[ToolCall], dict | None]:
    """Synchronous tool-capable completion via the appropriate provider.

    Args:
        messages: Conversation history; mutated to append the assistant turn.
        system: Optional system prompt.
        tools: Tool definitions available this turn.
        model: Model id; provider is inferred from it when given.
        max_tokens: Maximum output tokens.

    Returns:
        Tuple of (text, tool_calls, usage_dict).
    """
    return get_provider(model).complete_with_tools(messages, system=system, tools=tools,
                                                   model=model, max_tokens=max_tokens)


def append_tool_results(messages: list[Message], results: list[ToolResult], *, model: str | None = None) -> None:
    """Append tool results to the conversation using the appropriate provider's format.

    Args:
        messages: Conversation history to mutate.
        results: Tool call answers to append.
        model: Model id used to infer the provider format.
    """
    get_provider(model).append_tool_results(messages, results)
