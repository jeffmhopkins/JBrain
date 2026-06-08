"""Fake OpenAI-compatible LLM server for JBrain end-to-end tests.

The real JBrain server is booted with LLM_PROVIDER=xai and XAI_BASE_URL pointed here,
so its OpenAI SDK client talks to this stub instead of a real model — "LLM mocked at
the boundary", with zero production-code changes. We implement just the slice the xAI
adapter uses: POST /v1/chat/completions (streaming + non-streaming, OpenAI shape).

Determinism: the reply text is fixed (overridable via FAKE_LLM_REPLY). When the request
offers a `propose_actions` tool (Full-Brain architect), we emit a single tool call that
stages one note, so the propose->Apply journey is exercisable; otherwise we return plain
text (Research / quick replies). A trivial in-loop guard avoids re-proposing after the
tool result comes back.
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

REPLY = os.environ.get("FAKE_LLM_REPLY", "This is a deterministic end-to-end test reply.")
# The KB "Edit with AI" / rebuild draft turns ask for a FULL article body (Markdown starting
# with "# "). Returning the default chat reply would not look like an article, so detect those
# prompts and stream a deterministic article instead.
ARTICLE_REPLY = os.environ.get(
    "FAKE_LLM_ARTICLE",
    "# Edited by E2E\n\nThis article was revised by the end-to-end fake model. Pal enjoys sailing.\n")
MODEL = "grok-e2e"


def _last_user_text(body: dict) -> str:
    """Return the text of the most recent user message (string or content-parts shape).

    Args:
        body: Parsed request JSON dict.

    Returns:
        The user message text, or '' if none.
    """
    for m in reversed(body.get("messages", [])):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _wants_article(body: dict) -> bool:
    """Return True when the prompt asks for a full KB article body (rebuild draft / suggest edit).

    Args:
        body: Parsed request JSON dict.

    Returns:
        True if the latest user turn is an article-writing/edit prompt.
    """
    t = _last_user_text(body)
    return ("CURRENT ARTICLE:" in t) or ("revised article" in t) or ("corrected article" in t)


def _tool_names(body: dict) -> set[str]:
    """Extract the set of tool function names from a chat completions request body.

    Args:
        body: Parsed request JSON dict.

    Returns:
        Set of tool function name strings.
    """
    return {t.get("function", {}).get("name") for t in (body.get("tools") or [])}


def _already_called_tool(body: dict) -> bool:
    """Return True if the transcript already contains a tool result message.

    Used to terminate the architect loop: once a tool result is present, we return
    plain text instead of proposing again.

    Args:
        body: Parsed request JSON dict.

    Returns:
        True if any message has role 'tool'.
    """
    return any(m.get("role") == "tool" for m in body.get("messages", []))


def _wants_propose(body: dict) -> bool:
    """Return True if the request should trigger a propose_actions tool call.

    Args:
        body: Parsed request JSON dict.

    Returns:
        True when propose_actions is available and no tool result is in the transcript.
    """
    return "propose_actions" in _tool_names(body) and not _already_called_tool(body)


def _propose_args() -> str:
    """Return the JSON arguments string for a deterministic propose_actions tool call.

    Returns:
        JSON string with a single CREATE action staging a test note.
    """
    # Shape mirrors what the architect's propose_actions tool expects (see
    # server/app/services/architect.py `_TOOL_SCHEMAS["propose_actions"]` and
    # routers/staging.py `_apply_action`): a list of staged actions. A single CREATE of
    # a note — `type` is the UPPERCASE enum, `title` + `content` are what apply writes,
    # and `summary` is required by the tool schema.
    return json.dumps({
        "actions": [{
            "type": "CREATE",
            "title": "E2E Proposed Note",
            "content": "# E2E Proposed Note\n\nStaged by the end-to-end fake model.",
            "summary": "Create a note staged by the end-to-end fake model.",
        }],
    })


def _nonstream(body: dict) -> dict:
    """Build a non-streaming chat.completion response dict.

    Args:
        body: Parsed request JSON dict.

    Returns:
        OpenAI-shaped chat.completion dict with a tool_calls or text choice.
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    base = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": MODEL,
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}}
    if _wants_propose(body):
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": "propose_actions", "arguments": _propose_args()}}]}
        base["choices"] = [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]
    else:
        base["choices"] = [{"index": 0, "message": {"role": "assistant", "content": REPLY},
                            "finish_reason": "stop"}]
    return base


def _sse(body: dict):
    """Yield SSE chunks for a streaming chat.completion response.

    Args:
        body: Parsed request JSON dict.

    Yields:
        SSE data lines (strings) in OpenAI streaming format, ending with [DONE].
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def chunk(delta, finish=None):
        """Serialize one SSE chunk dict to a data line."""
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    if _wants_propose(body):
        tc = {"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
              "function": {"name": "propose_actions", "arguments": _propose_args()}}
        yield chunk({"role": "assistant", "tool_calls": [tc]})
        yield chunk({}, finish="tool_calls")
    elif _wants_article(body):
        yield chunk({"role": "assistant", "content": ""})
        for line in ARTICLE_REPLY.splitlines(keepends=True):
            yield chunk({"content": line})
        yield chunk({}, finish="stop")
    else:
        yield chunk({"role": "assistant", "content": ""})
        for word in REPLY.split(" "):
            yield chunk({"content": word + " "})
        yield chunk({}, finish="stop")
    # usage chunk (include_usage) then DONE
    yield "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
        "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}}) + "\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Handle POST /v1/chat/completions (streaming and non-streaming).

    Args:
        request: Incoming FastAPI request carrying the OpenAI-shaped JSON body.

    Returns:
        StreamingResponse (SSE) when stream=true, else JSONResponse.
    """
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(_sse(body), media_type="text/event-stream")
    return JSONResponse(_nonstream(body))


@app.get("/healthz")
def healthz():
    """Return a simple health-check response.

    Returns:
        JSON dict {"ok": True}.
    """
    return {"ok": True}
