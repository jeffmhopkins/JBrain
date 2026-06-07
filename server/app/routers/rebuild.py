"""Live AI page rebuild: stream the AI's thoughts + the article being rewritten, then
Accept (commit a new version) / Reject (discard) / Guide (steer a revision with the same
AI). KB pages only; owner-only. The run is held in-memory (services.rebuild_runs) — closing
the panel / refresh cancels it, and the live note is never touched until Accept.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import llm, rebuild_engine, rebuild_runs, wiki_build
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/kb/rebuild", tags=["rebuild"], dependencies=[CurrentUser])
log = logging.getLogger("jbrain")

# Mirror the chat SSE bridge: keepalive while the model is silent so proxies / the client's
# stall watchdog don't drop a long "thinking" pause.
_SSE_KEEPALIVE_SECONDS = 15.0


class GuideIn(BaseModel):
    text: str


def _sse(agen) -> StreamingResponse:
    """Bridge an async event-dict generator to a keepalive'd SSE response (mirrors chat.py)."""
    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def pump():
            try:
                async for event in agen:
                    await queue.put(("event", event))
            except Exception:  # log detail server-side, never to the user
                log.exception("rebuild stream failed")
                await queue.put(("error", "Something went wrong during the rebuild. Please try again."))
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


def _kb_note(conn, slug: str):
    row = conn.execute(
        "SELECT id, title, slug, kind, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row or row["kind"] != "kb":
        raise HTTPException(status_code=404, detail="Not a KB page")
    return row


@router.post("/start/{slug}")
def start(slug: str):
    conn = get_conn()
    note = _kb_note(conn, slug)
    title = note["title"]
    run = rebuild_runs.create(slug, title, llm.model_for("synthesis"), note["content_md"] or "")

    async def gen():
        yield {"type": "run_started", "run_id": run.run_id, "slug": slug,
               "title": title, "base_rev": run.base_hash}
        async for ev in rebuild_engine.run_rebuild(run):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/guide")
def guide(run_id: str, body: GuideIn):
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")
    if not rebuild_runs.is_live(run):
        raise HTTPException(status_code=409, detail="This rebuild can't be guided right now.")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty guidance.")

    async def gen():
        async for ev in rebuild_engine.run_guide(run, text):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/accept")
def accept(run_id: str):
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")
    if run.status not in ("ready", "guiding"):
        raise HTTPException(status_code=409, detail="This rebuild can't be accepted right now.")
    if not (run.draft or "").strip():
        raise HTTPException(status_code=409, detail="There's no draft to accept.")

    # CAS-ish guard against a double-accept: claim the run before doing any DB work.
    run.status = "accepting"
    conn = get_conn()
    if not wiki_build.kb_lock_acquire(conn):
        run.status = "ready"
        raise HTTPException(status_code=409,
                            detail="The knowledge base is busy (maintenance running) — try Accept again shortly.")
    try:
        current = notes_svc.get_by_title(conn, run.title)
        if current is None:
            run.status = "ready"
            raise HTTPException(status_code=409, detail="The page no longer exists.")
        # Staleness guard (inside the lock): refuse to overwrite a page edited since the
        # rebuild began. The client renders the "stale" state on this 409.
        if rebuild_runs.content_hash(current["content_md"] or "") != run.base_hash:
            run.status = "ready"
            raise HTTPException(status_code=409, detail="stale")
        wiki_build.finalize_rebuild(conn, run.title, run.draft, run.talk, prior_note_id=current["id"])
        conn.commit()
    finally:
        wiki_build.kb_lock_release(conn)

    row = notes_svc.get_by_title(conn, run.title)
    run.status = "accepted"
    rebuild_runs.drop(run_id)
    return {"ok": True, "slug": row["slug"] if row else run.slug}


@router.post("/{run_id}/reject")
def reject(run_id: str):
    rebuild_runs.drop(run_id)
    return {"ok": True}
