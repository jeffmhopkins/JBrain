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
from ..services import llm, rebuild_engine, rebuild_runs, search, wiki_build
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/kb/rebuild", tags=["rebuild"], dependencies=[CurrentUser])
log = logging.getLogger("jbrain")

# Mirror the chat SSE bridge: keepalive while the model is silent so proxies / the client's
# stall watchdog don't drop a long "thinking" pause.
_SSE_KEEPALIVE_SECONDS = 15.0


class GuideIn(BaseModel):
    """Input schema for the rebuild guide endpoint."""

    text: str


class RegatherIn(BaseModel):
    """Input schema for the regather endpoint."""

    hint: str | None = None


class DraftIn(BaseModel):
    """Input schema for the draft endpoint."""

    source_ids: list[int]


class SuggestIn(BaseModel):
    """Input schema for the first conversational-edit turn."""

    source_ids: list[int] = []
    text: str = ""


class FindFactsIn(BaseModel):
    """Input schema for the fact-finder endpoint."""

    query: str = ""


class RedraftIn(BaseModel):
    """Input schema for the redraft endpoint."""

    max_tokens: int | None = None     # approved larger budget after a truncation


class AcceptIn(BaseModel):
    """Input schema for accepting a rebuild draft."""

    rename_to: str | None = None     # opt-in retitle (must stay under kb/); None = keep title


def _sse(agen) -> StreamingResponse:
    """Bridge an async event-dict generator to a keepalive SSE response.

    Mirrors the pattern used in chat.py: a background task pumps events into a
    queue while the stream loop drains it with a timeout, emitting SSE keepalive
    comments during silent stretches so proxies do not drop the connection.

    Args:
        agen: Async generator yielding event dicts with at least a ``type`` key.

    Returns:
        StreamingResponse with ``text/event-stream`` media type.
    """
    async def event_stream():
        """Yield the rebuild run's SSE events, interleaving keepalive comments."""
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def pump():
            """Drain the rebuild async generator into the queue."""
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
    """Fetch a live KB note row by slug, or raise 404 if not found or not a KB page.

    Args:
        conn: Active DB connection.
        slug: URL slug of the note.

    Returns:
        sqlite3 Row with ``id``, ``title``, ``slug``, ``kind``, and ``content_md``.

    Raises:
        HTTPException: 404 if no live note with that slug exists, or it is not
            of kind ``'kb'``.
    """
    row = conn.execute(
        "SELECT id, title, slug, kind, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row or row["kind"] != "kb":
        raise HTTPException(status_code=404, detail="Not a KB page")
    return row


@router.post("/start/{slug}")
def start(slug: str, mode: str = "rebuild"):
    """Start a rebuild/suggest run for a KB page and stream the gather agent's source proposals.

    Stage 1: creates an in-memory run and streams the gather agent's tool use
    through to the client as SSE events ending with a proposed source list. In
    ``suggest`` mode the run seeds the current article body so the panel can show it
    immediately and the conversational edit turns revise FROM it.

    Args:
        slug: URL slug of the KB page to rebuild.
        mode: ``rebuild`` (write from sources) or ``suggest`` (edit the current article).

    Yields:
        SSE events: ``run_started``, gather-agent tool events, and proposed
        source events.

    Raises:
        HTTPException: 404 if the note does not exist or is not a KB page.
    """
    conn = get_conn()
    note = _kb_note(conn, slug)
    title = note["title"]
    kind = "suggest" if mode == "suggest" else "rebuild"
    run = rebuild_runs.create(slug, title, llm.model_for("synthesis"), note["content_md"] or "", kind=kind)
    if kind == "suggest":
        run.draft = note["content_md"] or ""     # show the current article while sources gather

    async def gen():
        """Stream the initial gather (+ seeded draft for suggest) as Server-Sent Events."""
        yield {"type": "run_started", "run_id": run.run_id, "slug": slug,
               "title": title, "base_rev": run.base_hash, "kind": kind,
               "draft": run.draft if kind == "suggest" else ""}
        async for ev in rebuild_engine.run_gather(run):
            yield ev

    return _sse(gen())


def _live_run(run_id: str):
    """Retrieve a live rebuild run or raise 410 if it has expired.

    Args:
        run_id: UUID string identifying the in-memory run.

    Returns:
        The live run object.

    Raises:
        HTTPException: 410 if the run no longer exists.
    """
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")
    return run


@router.post("/{run_id}/regather")
def regather(run_id: str, body: RegatherIn):
    """Re-run source gathering for an existing rebuild run.

    Appends newly-found sources while keeping the user's current picks.

    Args:
        run_id: UUID of the active rebuild run.
        body: Optional ``hint`` string to steer the re-gather.

    Yields:
        SSE gather-agent events with additional proposed sources.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = _live_run(run_id)

    async def gen():
        """Stream the re-gather pass as Server-Sent Events."""
        async for ev in rebuild_engine.run_gather(run, hint=(body.hint or None), append=True):
            yield ev

    return _sse(gen())


@router.get("/{run_id}/search")
def search_add(run_id: str, q: str):
    """Search primary notes to add as additional sources on the curate screen.

    Args:
        run_id: UUID of the active rebuild run (must exist).
        q: Free-text search query.

    Returns:
        List of dicts with ``note_id``, ``title``, and ``date`` for up to 8 hits.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = _live_run(run_id)
    conn = get_conn()
    hits = [h for h in search.hybrid_notes(conn, q, 8, require_kb_ingest=True)
            if not h["title"].lower().startswith("kb/")]
    meta = rebuild_engine._notes_meta(conn, [h["id"] for h in hits])
    return [{"note_id": m["id"], "title": m["title"], "date": m["date"]} for m in meta]


@router.post("/{run_id}/draft")
def draft(run_id: str, body: DraftIn):
    """Draft the rebuilt article from a curated set of source note IDs.

    Stage 2: streams the writer agent's output for the curated sources.

    Args:
        run_id: UUID of the active rebuild run.
        body: ``{"source_ids": [int, ...]}`` — note IDs chosen on the curate screen.

    Yields:
        SSE drafting events including incremental article text and completion.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = _live_run(run_id)

    async def gen():
        """Stream the draft pass as Server-Sent Events."""
        async for ev in rebuild_engine.run_draft(run, body.source_ids):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/suggest")
def suggest(run_id: str, body: SuggestIn):
    """Open a conversational targeted edit: revise the current article at the owner's direction.

    The first turn of "Suggest revisions" — seeds the live article body, the curated source
    notes, and read-only backlinks, then streams the revised article. Follow-up edits use the
    shared ``/guide`` endpoint, which continues this same loaded transcript.

    Args:
        run_id: UUID of the active run.
        body: ``{"source_ids": [int, ...], "text": str}`` — curated sources and the owner's
            first guidance.

    Yields:
        SSE editing events, same shape as ``/draft``.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = _live_run(run_id)

    async def gen():
        """Stream the first conversational-edit turn as Server-Sent Events."""
        async for ev in rebuild_engine.run_suggest(run, body.source_ids, body.text):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/find_facts")
def find_facts(run_id: str, body: FindFactsIn):
    """Find owner-approvable candidate facts from the owner's notes during a suggest session.

    The privacy-filtered, human-gated truth-seeker: returns salient one-sentence facts (each tied
    to an exact source note) for the owner to approve before any is folded into the edit. A
    sensitivity floor drops private-adjacent notes when the target article is non-private; nothing
    is applied to the article here.

    Args:
        run_id: UUID of the active run.
        body: ``{"query": str}`` — what to look for.

    Returns:
        List of ``{claim, source_id, source_title, date}`` candidate-fact dicts.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = _live_run(run_id)
    conn = get_conn()
    return rebuild_engine.find_facts(conn, run.title, body.query)


@router.post("/{run_id}/redraft")
def redraft(run_id: str, body: RedraftIn):
    """Re-run the last drafting turn at a user-approved larger token budget.

    Called after a truncation warning when the user approves a higher budget.

    Args:
        run_id: UUID of the active rebuild run.
        body: ``{"max_tokens": int | null}`` — approved budget override.

    Yields:
        SSE drafting events, same shape as ``/draft``.

    Raises:
        HTTPException: 410 if the run has expired.
    """
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")

    async def gen():
        """Stream the redraft pass as Server-Sent Events."""
        async for ev in rebuild_engine.run_redraft(run, body.max_tokens):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/guide")
def guide(run_id: str, body: GuideIn):
    """Steer the current rebuild draft with natural-language guidance.

    Args:
        run_id: UUID of the active rebuild run.
        body: ``{"text": str}`` — guidance instruction for the writer agent.

    Yields:
        SSE revision events, same shape as ``/draft``.

    Raises:
        HTTPException: 410 if the run has expired.
        HTTPException: 409 if the run is not in a guidable state.
        HTTPException: 400 if guidance text is empty.
    """
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")
    if not rebuild_runs.is_live(run):
        raise HTTPException(status_code=409, detail="This rebuild can't be guided right now.")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty guidance.")

    async def gen():
        """Stream the guided-revision pass as Server-Sent Events."""
        async for ev in rebuild_engine.run_guide(run, text):
            yield ev

    return _sse(gen())


@router.post("/{run_id}/accept")
def accept(run_id: str, body: AcceptIn):
    """Commit the rebuild draft as a new note version, replacing the live KB page.

    Optionally renames the page (must remain under ``kb/``). A staleness guard
    rejects the commit if the page was edited since the rebuild began.

    Args:
        run_id: UUID of the active rebuild run.
        body: Optional ``rename_to`` title (must start with ``"kb/"``).

    Returns:
        JSON ``{"ok": true, "slug": str}`` with the (possibly renamed) page slug.

    Raises:
        HTTPException: 410 if the run has expired.
        HTTPException: 409 if the run cannot be accepted (wrong state, no draft,
            KB lock busy, page deleted, or content is stale).
        HTTPException: 400 if ``rename_to`` does not start with ``"kb/"``.
    """
    run = rebuild_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=410, detail="This rebuild session expired — please rebuild again.")
    if run.status not in ("ready", "guiding"):
        raise HTTPException(status_code=409, detail="This rebuild can't be accepted right now.")
    if not (run.draft or "").strip():
        raise HTTPException(status_code=409, detail="There's no draft to accept.")

    # Opt-in rename (user-approved only). Keep it under kb/ so a rebuild can't move the page out.
    rename_to = (body.rename_to or "").strip()
    if rename_to and not rename_to.lower().startswith("kb/"):
        raise HTTPException(status_code=400, detail="A rebuilt page must stay under kb/.")
    if not rename_to or rename_to.lower() == run.title.lower():
        rename_to = None

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
        wiki_build.finalize_rebuild(conn, run.title, run.draft, run.talk,
                                    prior_note_id=current["id"], rename_to=rename_to)
        conn.commit()
    finally:
        wiki_build.kb_lock_release(conn)

    row = notes_svc.get_by_title(conn, rename_to or run.title)
    run.status = "accepted"
    rebuild_runs.drop(run_id)
    return {"ok": True, "slug": row["slug"] if row else run.slug}


@router.post("/{run_id}/reject")
def reject(run_id: str):
    """Discard a rebuild run without writing anything.

    Args:
        run_id: UUID of the rebuild run to discard.

    Returns:
        JSON ``{"ok": true}``.
    """
    rebuild_runs.drop(run_id)
    return {"ok": True}
