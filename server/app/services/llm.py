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

from dataclasses import dataclass
from typing import Any, AsyncGenerator, Protocol, runtime_checkable

from ..config import get_settings

# A conversation turn. Seed/history turns are plain {"role","content": str}. Turns
# the model authors (assistant + tool calls) and tool-result turns are opaque,
# provider-owned elements appended via the provider — the app never inspects them.
Message = dict[str, Any]


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


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class TurnEnd:
    tool_calls: list[ToolCall]  # empty => the model is done (no tools requested)


StreamEvent = TextDelta | ToolCallEvent | TurnEnd


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def has_credentials(self) -> bool: ...
    def default_model(self) -> str: ...
    def supports_tools(self) -> bool: ...

    def complete(self, messages: list[Message], *, system: str | None = None,
                 model: str | None = None, max_tokens: int = 1024) -> str: ...

    def stream_turn(self, messages: list[Message], *, system: str | None,
                    tools: list[ToolDef], model: str | None,
                    max_tokens: int) -> AsyncGenerator[StreamEvent, None]: ...

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

    def complete(self, messages, *, system=None, model=None, max_tokens=1024) -> str:
        from anthropic import Anthropic

        client = Anthropic(api_key=get_settings().llm_api_key)
        kwargs: dict = {"model": model or self.default_model(), "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    async def stream_turn(self, messages, *, system, tools, model, max_tokens):
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=get_settings().llm_api_key)
        wire_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.json_schema}
            for t in tools
        ]
        async with client.messages.stream(
            model=model or self.default_model(),
            max_tokens=max_tokens,
            system=system,
            tools=wire_tools,
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and getattr(event.delta, "type", None) == "text_delta":
                    yield TextDelta(event.delta.text)
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
        yield TurnEnd(calls)

    def append_tool_results(self, messages, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content}
                for r in results
            ],
        })


# --- Selection --------------------------------------------------------------

_REGISTRY: dict[str, type] = {
    "anthropic": AnthropicProvider,
    # Add providers here: "openai": OpenAIProvider, "gemini": GeminiProvider, ...
}


def get_provider() -> LLMProvider:
    """The configured provider (LLM_PROVIDER), defaulting to Anthropic. Cheap and
    stateless — reads settings on each call — so it's constructed per use."""
    name = (get_settings().llm_provider or "anthropic").lower()
    cls = _REGISTRY.get(name, AnthropicProvider)
    return cls()


def has_credentials() -> bool:
    return get_provider().has_credentials()


def complete(messages: list[Message], *, system: str | None = None,
             model: str | None = None, max_tokens: int = 1024) -> str:
    return get_provider().complete(messages, system=system, model=model, max_tokens=max_tokens)
