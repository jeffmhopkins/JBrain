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
MODEL = "grok-e2e"


def _tool_names(body: dict) -> set[str]:
    return {t.get("function", {}).get("name") for t in (body.get("tools") or [])}


def _already_called_tool(body: dict) -> bool:
    # If the transcript already contains a tool result, don't propose again — return text
    # so the architect loop terminates.
    return any(m.get("role") == "tool" for m in body.get("messages", []))


def _wants_propose(body: dict) -> bool:
    return "propose_actions" in _tool_names(body) and not _already_called_tool(body)


def _propose_args() -> str:
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
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def chunk(delta, finish=None):
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
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(_sse(body), media_type="text/event-stream")
    return JSONResponse(_nonstream(body))


@app.get("/healthz")
def healthz():
    return {"ok": True}
