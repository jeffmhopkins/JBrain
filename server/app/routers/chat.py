"""Chat: conversations, message history, and the streaming architect endpoint."""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import architect

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[CurrentUser])

# Emit an SSE keepalive comment after this many seconds of silence. A long tool run
# (or the model "thinking" before the first token) would otherwise leave the stream
# quiet long enough for a proxy — or the client's stall watchdog — to give up.
_SSE_KEEPALIVE_SECONDS = 15.0


class MessageIn(BaseModel):
    text: str
    mode: str = "assisted"          # 'assisted' | 'research'
    lat: float | None = None
    lon: float | None = None
    location_label: str | None = None
    # Set by the client when the user left the app / navigated away and back / is resuming after a
    # break — clears prior conversation context so the agent re-grounds instead of reusing stale answers.
    fresh_context: bool = False


@router.post("/conversations")
def create_conversation():
    conn = get_conn()
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    conn.commit()
    return {"id": cur.lastrowid}


@router.get("/conversations")
def list_conversations():
    rows = get_conn().execute(
        "SELECT id, title, started_at FROM conversations ORDER BY started_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int):
    rows = get_conn().execute(
        "SELECT role, content, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/conversations/{conversation_id}/message")
def send_message(conversation_id: int, body: MessageIn):
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Title the conversation from its first user message.
    has_title = conn.execute(
        "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()["title"]
    if not has_title:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (body.text[:60], conversation_id),
        )
        conn.commit()

    location = (
        {"lat": body.lat, "lon": body.lon, "location_label": body.location_label}
        if body.lat is not None and body.lon is not None
        else None
    )

    mode = body.mode if body.mode in ("assisted", "research") else "assisted"

    async def event_stream():
        # Bridge the architect's async generator through a queue so we can interleave a
        # periodic keepalive while the real work is silent. A ': keepalive' comment line
        # carries no `data:` field, so the client safely ignores it — but every byte
        # resets the client's idle timer and keeps intermediary proxies from dropping
        # the connection mid-turn. (See the "stall watchdog" in web/src/api.ts.)
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def pump():
            try:
                async for event in architect.run(conversation_id, body.text, location, mode,
                                                 fresh_context=body.fresh_context):
                    await queue.put(("event", event))
            except Exception:  # don't hang the client; log detail server-side, not to the user
                logging.getLogger("jbrain").exception("chat stream failed")
                await queue.put(("error", "Something went wrong while generating the reply. Please try again."))
            finally:
                await queue.put((_DONE, None))

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind is _DONE:
                    break
                if kind == "error":
                    yield f"event: error\ndata: {json.dumps({'message': payload})}\n\n"
                else:
                    yield f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort cleanup
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
