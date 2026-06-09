"""Shared Server-Sent Events bridge for the streaming LLM endpoints.

Every live-LLM stream (the architect chat, the KB rebuild / "Edit with AI" engine) runs an
async generator of event dicts and proxies it to the browser as SSE. Two things must hold for
the stream never to "go dead" on the user, and they are easy to get subtly different per
endpoint — so they live here once:

- **Keepalive.** While the model thinks or a tool runs, the stream is legitimately silent; a
  bare ``": keepalive"`` comment every few seconds keeps proxies and the client's byte-level
  stall watchdog from dropping the connection.
- **No-progress watchdog.** Keepalive alone *masks* a wedged turn: a ping-stalled model stream
  (the SDK's per-read timeout is reset by upstream pings) or an uncancellable hung tool
  (``asyncio.to_thread`` can't be interrupted) emits no real event, so a keepalive-only bridge
  would ping forever — an "unresponsive AI" sitting behind a still-green status dot. After a
  budget of total silence the bridge gives up and emits a typed ``error`` frame instead, which
  the client both shows and treats as an ``llm-fail`` health signal.

The error frame always carries ``{"type": "error", ...}`` in its ``data:`` payload: the web
clients switch on that JSON ``type`` field and ignore the SSE ``event:`` name, so a frame
without it is silently dropped (no bubble, no health signal).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, AsyncIterable

from fastapi.responses import StreamingResponse

from .config import get_settings

log = logging.getLogger("jbrain")

# Emit a keepalive comment after this many seconds of silence — long enough not to spam, short
# enough to stay well under proxy/client idle timeouts.
KEEPALIVE_SECONDS = 15.0

# Hard floor on how long a turn may make NO progress (no real event, only keepalives) before the
# watchdog aborts it. Sits comfortably above one LLM request timeout so a genuinely slow single
# model/tool call is never cut off; the live budget scales with LLM_TIMEOUT_SECONDS so a slow
# local-inference box gets more headroom (see _max_silence_seconds).
_SILENCE_FLOOR_SECONDS = 180.0


def _max_silence_seconds() -> float:
    """Return the no-progress watchdog budget in seconds.

    Derived from the configured LLM request timeout (a turn may legitimately sit silent for one
    slow model/tool call) with a fixed floor, so the watchdog fires only on a genuinely wedged
    turn — never on slow-but-progressing work.

    Returns:
        Maximum seconds of no-progress silence tolerated before aborting the stream.
    """
    try:
        return max(2.0 * float(get_settings().llm_timeout_seconds), _SILENCE_FLOOR_SECONDS)
    except Exception:  # noqa: BLE001 — settings not ready / bad value → safe floor
        return _SILENCE_FLOOR_SECONDS


def sse_error(message: str) -> str:
    """Render an SSE error frame the web clients will actually act on.

    Stamps ``type: "error"`` in the ``data:`` payload because the clients switch on that field
    and ignore the SSE ``event:`` name; a frame that omits it is dropped (no error bubble, and
    the ``llm-fail`` health signal that downgrades the status dot never fires).

    Args:
        message: Human-facing error text for the chat/rebuild bubble.

    Returns:
        A complete ``event: error`` SSE frame ending in a blank line.
    """
    return f"event: error\ndata: {json.dumps({'type': 'error', 'message': message})}\n\n"


async def event_stream(
    agen: AsyncIterable[dict],
    *,
    fail_message: str = "Something went wrong. Please try again.",
    wedged_message: str = "The assistant stopped responding. Please try again.",
    label: str = "stream",
) -> AsyncGenerator[str, None]:
    """Bridge an event-dict async generator to SSE text frames, with keepalive + watchdog.

    A background task pumps ``agen``'s events into a queue while this loop drains it with a
    timeout: a silent interval yields a keepalive, an exception in the generator yields the
    ``fail_message`` error frame, and a whole ``_max_silence_seconds`` budget of silence yields
    the ``wedged_message`` error frame (rather than keepalive-ing forever behind a green dot).

    Args:
        agen: Async iterable yielding event dicts that each carry a ``type`` key.
        fail_message: Error text shown when the generator raises.
        wedged_message: Error text shown when the watchdog fires (no progress for the budget).
        label: Short identifier for the server-side log lines (e.g. "chat", "rebuild").

    Yields:
        SSE frame strings (``event:``/``data:`` blocks and ``": keepalive"`` comments).
    """
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def pump() -> None:
        """Drain the source generator into the queue; convert a raise into an error sentinel."""
        try:
            async for event in agen:
                await queue.put(("event", event))
        except Exception:  # noqa: BLE001 — log detail server-side, never leak it to the user
            log.exception("%s stream failed", label)
            await queue.put(("error", fail_message))
        finally:
            await queue.put((_DONE, None))

    task = asyncio.create_task(pump())
    last_progress = time.monotonic()
    max_silence = _max_silence_seconds()
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # One keepalive interval with no event. Hold the socket warm — unless the turn
                # has made no progress for the whole silence budget, which means it's wedged.
                if time.monotonic() - last_progress >= max_silence:
                    log.error("%s stream wedged: no progress for %.0fs; aborting", label, max_silence)
                    yield sse_error(wedged_message)
                    break
                yield ": keepalive\n\n"
                continue
            last_progress = time.monotonic()   # a real event — the turn is making progress
            if kind is _DONE:
                break
            if kind == "error":
                yield sse_error(payload)
            else:
                yield f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort cleanup
            pass


def stream(
    agen: AsyncIterable[dict],
    *,
    fail_message: str = "Something went wrong. Please try again.",
    wedged_message: str = "The assistant stopped responding. Please try again.",
    label: str = "stream",
) -> StreamingResponse:
    """Wrap an event-dict async generator in a keepalive + watchdog SSE StreamingResponse.

    Args:
        agen: Async iterable yielding event dicts that each carry a ``type`` key.
        fail_message: Error text shown when the generator raises.
        wedged_message: Error text shown when the watchdog fires.
        label: Short identifier for the server-side log lines.

    Returns:
        StreamingResponse with ``text/event-stream`` media type.
    """
    return StreamingResponse(
        event_stream(agen, fail_message=fail_message, wedged_message=wedged_message, label=label),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
