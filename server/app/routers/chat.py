"""Chat: conversations, message history, and the streaming architect endpoint."""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..config import get_settings
from ..db import get_conn
from ..services import architect

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[CurrentUser])

# Emit an SSE keepalive comment after this many seconds of silence. A long tool run
# (or the model "thinking" before the first token) would otherwise leave the stream
# quiet long enough for a proxy — or the client's stall watchdog — to give up.
_SSE_KEEPALIVE_SECONDS = 15.0

# Hard ceiling on how long a turn may make NO progress — no real architect event,
# only keepalives — before we give up and surface an error. The keepalive above
# keeps the socket warm during legitimately long work, but it also masks a wedged
# turn: a ping-stalled model stream (the SDK's per-read timeout is reset by upstream
# pings) or an uncancellable hung tool (asyncio.to_thread can't be interrupted) emits
# no events, so the bridge would keepalive forever — an "unresponsive chat" sitting
# behind a still-green status dot, with the client's byte-level stall watchdog reset
# on every keepalive. This watchdog converts that silent hang into a visible error
# (which also drives the client's llm-fail health signal). The floor sits comfortably
# above one LLM request timeout so a genuinely slow single tool/model call is never
# cut off; it scales with LLM_TIMEOUT_SECONDS so slow local-inference boxes get more
# headroom.
_SSE_SILENCE_FLOOR_SECONDS = 180.0


def _max_silence_seconds() -> float:
    """Return the no-progress watchdog budget for a chat turn, in seconds.

    Derived from the configured LLM request timeout (a turn may legitimately sit
    silent for one slow model/tool call) with a fixed floor, so the watchdog only
    ever fires on a genuinely wedged turn — never on slow-but-progressing work.

    Returns:
        Maximum seconds of no-progress silence tolerated before aborting the turn.
    """
    try:
        return max(2.0 * float(get_settings().llm_timeout_seconds), _SSE_SILENCE_FLOOR_SECONDS)
    except Exception:  # noqa: BLE001 — settings not ready / bad value → safe floor
        return _SSE_SILENCE_FLOOR_SECONDS


def _sse_error(message: str) -> str:
    """Render an SSE error frame the client will actually act on.

    The web client switches on the JSON ``type`` field of each ``data:`` line and
    ignores the SSE ``event:`` name (see web/src/api.ts). A frame whose payload
    omits ``type`` is silently dropped — no error bubble, and the ``llm-fail``
    health signal that downgrades the status dot never fires. Always stamp
    ``type: "error"`` so a failed turn is both shown and reflected in the dot.

    Args:
        message: Human-facing error text for the chat bubble.

    Returns:
        A complete ``event: error`` SSE frame ending in a blank line.
    """
    return f"event: error\ndata: {json.dumps({'type': 'error', 'message': message})}\n\n"


class MessageIn(BaseModel):
    """Input schema for sending a chat message."""

    text: str
    mode: str = "assisted"          # 'assisted' (Full Brain) | 'research' (read-only)
    # Read-only "Deep" opt-in: raise the research budget for a multi-step question. Ignored
    # for write modes (Full Brain already reasons deeply).
    deep: bool = False
    lat: float | None = None
    lon: float | None = None
    location_label: str | None = None
    # Set by the client when the user left the app / navigated away and back / is resuming after a
    # break — clears prior conversation context so the agent re-grounds instead of reusing stale answers.
    fresh_context: bool = False


# Canonical chat modes the architect understands. `mode` is request-scoped only — never
# persisted (there is no `mode` column on conversations/messages), so the wire vocabulary
# can evolve freely as long as both ends agree. Keep RETIRED modes OUT of this tuple — a
# canonical match short-circuits normalize_mode() before the alias table, so listing a
# retired mode here would make it resolve to itself and bypass its read-only fold (the
# "analyze" regression).
_CANONICAL_MODES = ("assisted", "research")
# Legacy / forward-incompatible wire strings mapped to a canonical mode. The invariant:
# read-only strings MUST map to a read-only mode and write strings to a write mode — never
# cross the boundary. (Populated when a mode is retired, e.g. "analyze" -> "research".)
_MODE_ALIASES: dict[str, str] = {
    # The old read-only "analyze" mode folded into "research" (strict prompt + a per-turn
    # "deep" budget opt-in). A stale client sending "analyze" lands read-only, never write.
    "analyze": "research",
}


def normalize_mode(raw: str) -> str:
    """Resolve a wire mode string to a canonical mode.

    Fails closed to the read-only ``'research'`` mode for any unrecognised
    string. A stale PWA or forward-incompatible client must never silently
    gain write tools by sending an unknown mode.

    Args:
        raw: Wire mode string sent by the client (e.g. ``"assisted"``).

    Returns:
        A canonical mode from ``_CANONICAL_MODES``, defaulting to
        ``"research"`` for anything unrecognised.
    """
    if raw in _CANONICAL_MODES:
        return raw
    return _MODE_ALIASES.get(raw, "research")


@router.post("/conversations")
def create_conversation():
    """Create a new empty conversation.

    Returns:
        JSON ``{"id": int}`` with the new conversation's primary key.
    """
    conn = get_conn()
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    conn.commit()
    return {"id": cur.lastrowid}


@router.get("/conversations")
def list_conversations():
    """Return the 100 most recent conversations, newest first.

    Returns:
        List of dicts with ``id``, ``title``, and ``started_at``.
    """
    rows = get_conn().execute(
        "SELECT id, title, started_at FROM conversations ORDER BY started_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int):
    """Return all messages in a conversation with per-reply tool-call counts.

    The ``step_count`` field lets the client render the "how I answered this"
    pill without fetching the full tool log up front. The ``id`` field lets
    the client lazily fetch that log per message when the panel opens.

    Args:
        conversation_id: Primary key of the conversation.

    Returns:
        List of message dicts with ``id``, ``role``, ``content``,
        ``created_at``, and ``step_count``.
    """
    # `step_count` (a cheap LEFT JOIN aggregate) lets the client render the "how I answered
    # this" pill without fetching the full tool log for every reply up front; `id` lets it
    # lazily fetch that log per message when the pill/swipe opens it.
    rows = get_conn().execute(
        "SELECT m.id, m.role, m.content, m.created_at, COUNT(s.id) AS step_count "
        "FROM messages m LEFT JOIN message_steps s ON s.message_id = m.id "
        "WHERE m.conversation_id = ? GROUP BY m.id ORDER BY m.id",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/messages/{message_id}/steps")
def get_message_steps(message_id: int):
    """Return the full tool-call history for one assistant reply.

    Lazily fetched when the reply's history panel is opened by the user.

    Args:
        message_id: Primary key of the assistant message.

    Returns:
        List of step dicts with ``step_index``, ``tool_name``, ``args_json``,
        ``result_text``, ``is_error``, ``event_json``, and ``created_at``.
    """
    rows = get_conn().execute(
        "SELECT step_index, tool_name, args_json, result_text, is_error, event_json, created_at "
        "FROM message_steps WHERE message_id = ? ORDER BY step_index",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.delete("/conversations/{conversation_id}/steps")
def clear_conversation_steps(conversation_id: int):
    """Wipe the stored tool-call history for a conversation.

    Invoked when the user runs /clear so full tool logs do not accumulate
    across throwaway chats.

    Args:
        conversation_id: Primary key of the conversation to clear.

    Returns:
        JSON ``{"ok": true}``.
    """
    conn = get_conn()
    conn.execute("DELETE FROM message_steps WHERE conversation_id = ?", (conversation_id,))
    conn.commit()
    return {"ok": True}


@router.post("/conversations/{conversation_id}/message")
def send_message(conversation_id: int, body: MessageIn):
    """Send a user message and stream the architect's reply as SSE events.

    Titles the conversation from the first user message. Runs the architect
    agent in a background task and proxies its events through a keepalive
    queue so proxies and the client's stall watchdog do not drop the stream
    during long tool runs or model thinking pauses.

    Args:
        conversation_id: Primary key of the conversation.
        body: Message text, mode, optional location, optional deep flag, and
            optional fresh_context flag.

    Yields:
        SSE events from the architect, plus ``': keepalive'`` comments during
        silent periods. On error, an ``event: error`` frame is emitted.

    Raises:
        HTTPException: 404 if the conversation does not exist.
    """
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

    mode = normalize_mode(body.mode)

    async def event_stream():
        """Yield architect SSE events, interleaving periodic keepalive comments."""
        # Bridge the architect's async generator through a queue so we can interleave a
        # periodic keepalive while the real work is silent. A ': keepalive' comment line
        # carries no `data:` field, so the client safely ignores it — but every byte
        # resets the client's idle timer and keeps intermediary proxies from dropping
        # the connection mid-turn. (See the "stall watchdog" in web/src/api.ts.)
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def pump():
            """Drain the architect's async generator into the queue."""
            try:
                async for event in architect.run(conversation_id, body.text, location, mode,
                                                 fresh_context=body.fresh_context, deep=body.deep):
                    await queue.put(("event", event))
            except Exception:  # don't hang the client; log detail server-side, not to the user
                logging.getLogger("jbrain").exception("chat stream failed")
                await queue.put(("error", "Something went wrong while generating the reply. Please try again."))
            finally:
                await queue.put((_DONE, None))

        task = asyncio.create_task(pump())
        last_progress = time.monotonic()
        max_silence = _max_silence_seconds()
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # One keepalive interval with no event. Hold the socket warm — unless
                    # the turn has made no progress for the whole silence budget, which means
                    # it's wedged (ping-stalled model stream or an uncancellable hung tool).
                    # Surface an error instead of keepalive-ing forever behind a green dot.
                    if time.monotonic() - last_progress >= max_silence:
                        logging.getLogger("jbrain").error(
                            "chat stream wedged: no progress for %.0fs; aborting turn", max_silence)
                        yield _sse_error("The assistant stopped responding. Please try again.")
                        break
                    yield ": keepalive\n\n"
                    continue
                last_progress = time.monotonic()   # a real event — the turn is making progress
                if kind is _DONE:
                    break
                if kind == "error":
                    yield _sse_error(payload)
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
