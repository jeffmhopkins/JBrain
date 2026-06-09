"""The shared SSE bridge (app.sse): keepalive, the no-progress watchdog, and the typed error frame.

The watchdog is what stops a streaming LLM endpoint from "going dead": a wedged turn (a
ping-stalled model stream or an uncancellable hung tool) emits no real event, so a
keepalive-only bridge would ping forever behind a green status dot. These tests pin that a
wedged stream surfaces a typed ``error`` frame, that healthy events flow through unchanged, and
that the error frame carries ``type: "error"`` so the web clients act on it.
"""
import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from app import sse


def _drain(agen, *, limit=50, timeout=2.0):
    """Collect up to ``limit`` frames from an async SSE generator under a hard timeout."""
    async def go():
        out = []
        async for frame in agen:
            out.append(frame)
            if len(out) >= limit:
                break
        return out
    return asyncio.run(asyncio.wait_for(go(), timeout))


def _data_objs(frames):
    """Parse the JSON payloads of the ``data:`` frames (skipping keepalive comments)."""
    objs = []
    for f in frames:
        for line in f.splitlines():
            if line.startswith("data: "):
                objs.append(json.loads(line[6:]))
    return objs


def test_healthy_events_pass_through_in_order():
    """Real events flow through as event/data frames and the stream ends cleanly (no error)."""
    async def gen():
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}
    frames = _drain(sse.event_stream(gen(), label="test"))
    objs = _data_objs(frames)
    assert [o["type"] for o in objs] == ["token", "done"]
    assert all(o["type"] != "error" for o in objs)


def test_generator_exception_becomes_a_typed_error_frame():
    """A raise inside the source generator surfaces the fail_message as a type:error frame."""
    async def boom():
        raise RuntimeError("kaboom")
        yield  # noqa: unreachable — makes this an async generator
    frames = _drain(sse.event_stream(boom(), fail_message="nope, failed", label="test"))
    objs = _data_objs(frames)
    assert objs and objs[-1] == {"type": "error", "message": "nope, failed"}


def test_wedged_stream_is_aborted_by_the_watchdog(monkeypatch):
    """A stream that makes NO progress is surfaced as an error within the silence budget —
    NOT kept alive forever. This is the guarantee that an Edit-with-AI / chat turn can't hang."""
    # Tiny budgets so the watchdog fires fast in the test (generous keepalive:budget ratio so a
    # loaded CI box still sees keepalive(s) before the abort).
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(sse, "_max_silence_seconds", lambda: 0.2)

    async def wedged():
        await asyncio.sleep(10)       # never makes progress within the budget
        yield {"type": "done"}        # pragma: no cover — never reached

    frames = _drain(sse.event_stream(wedged(), wedged_message="stopped responding", label="test"),
                    timeout=3.0)
    # Keepalive(s) may precede it, but the terminal frame is the typed wedged-error.
    objs = _data_objs(frames)
    assert objs and objs[-1] == {"type": "error", "message": "stopped responding"}
    assert any(f.startswith(": keepalive") for f in frames)   # it kept the socket warm first


def test_keepalive_emitted_during_a_silent_gap_then_event_resumes(monkeypatch):
    """A brief silence yields a keepalive; a later real event still comes through (no false abort)."""
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(sse, "_max_silence_seconds", lambda: 5.0)   # generous: never fires here

    async def slow():
        await asyncio.sleep(0.16)     # >> one keepalive interval, << the silence budget
        yield {"type": "done"}

    frames = _drain(sse.event_stream(slow(), label="test"), timeout=3.0)
    assert any(f.startswith(": keepalive") for f in frames)
    objs = _data_objs(frames)
    assert objs[-1]["type"] == "done"
    assert all(o["type"] != "error" for o in objs)


def test_sse_error_frame_is_typed():
    """sse_error stamps type:error in the data payload (clients switch on it, ignore event name)."""
    frame = sse.sse_error("boom")
    data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    assert json.loads(data) == {"type": "error", "message": "boom"}
