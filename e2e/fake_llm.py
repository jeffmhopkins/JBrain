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

This same app also fakes a minimal Ollama HOST/admin API (GET /api/tags, POST /api/pull,
DELETE /api/delete) so the local-models settings UI can be exercised end-to-end without a
real Ollama. An in-memory set tracks which models are "installed"; it starts EMPTY so the
curated one-click "Pull" path is the one under test. Pulls stream a couple of NDJSON
progress frames ending in {"status":"success"} — deterministic and fast (no real sleeps).
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

REPLY = os.environ.get("FAKE_LLM_REPLY", "This is a deterministic end-to-end test reply.")
MODEL = "grok-e2e"


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


# --- Fake Ollama admin API ---------------------------------------------------
# In-memory set of "installed" model names. Starts EMPTY so the curated one-click
# "Pull" path is exercised; /api/pull adds, /api/delete removes, /api/tags reflects.
_INSTALLED: set[str] = set()
# Size reported per model (bytes) — small enough that the server's RAM-based "fits"
# verdict is true on any CI runner (qwen2.5:7b is ~4.7 GB on disk).
_MODEL_SIZE = 4_700_000_000


@app.get("/api/tags")
def ollama_tags():
    """List locally 'installed' models, mirroring Ollama's GET /api/tags.

    Returns:
        JSON dict {"models": [{"name": <id>, "size": <bytes>}, ...]} reflecting the
        in-memory installed set (empty until something is pulled).
    """
    return {"models": [{"name": name, "size": _MODEL_SIZE} for name in sorted(_INSTALLED)]}


def _pull_ndjson(name: str):
    """Yield a short deterministic NDJSON pull stream, marking `name` installed.

    Mirrors Ollama's POST /api/pull stream shape: a manifest status, a couple of
    downloading-progress frames, then a terminal {"status": "success"}. No real
    sleeps — fast and deterministic for tests.

    Args:
        name: Model id being pulled, e.g. 'qwen2.5:7b'.

    Yields:
        NDJSON lines (bytes) of Ollama-shaped progress dicts.
    """
    yield json.dumps({"status": "pulling manifest"}).encode() + b"\n"
    yield json.dumps({"status": "downloading", "completed": _MODEL_SIZE // 2, "total": _MODEL_SIZE}).encode() + b"\n"
    yield json.dumps({"status": "downloading", "completed": _MODEL_SIZE, "total": _MODEL_SIZE}).encode() + b"\n"
    _INSTALLED.add(name)
    yield json.dumps({"status": "success"}).encode() + b"\n"


@app.post("/api/pull")
async def ollama_pull(request: Request):
    """Stream a fake model pull as NDJSON, mirroring Ollama's POST /api/pull.

    Adds the requested model to the in-memory installed set on success.

    Args:
        request: Incoming request carrying {"name", "stream"} JSON.

    Returns:
        A StreamingResponse of NDJSON progress lines ending in {"status": "success"}.
    """
    body = await request.json()
    name = body.get("name") or ""
    return StreamingResponse(_pull_ndjson(name), media_type="application/x-ndjson")


@app.delete("/api/delete")
async def ollama_delete(request: Request):
    """Remove a model from the in-memory installed set, mirroring DELETE /api/delete.

    Args:
        request: Incoming request carrying {"name"} JSON.

    Returns:
        JSON dict {"status": "success"} (200) — matching Ollama's empty-body 200.
    """
    body = await request.json()
    _INSTALLED.discard(body.get("name") or "")
    return {"status": "success"}
