"""JBrain API entrypoint: middleware, routers, health, and PWA static serving."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# App-level logs ("jbrain") were filtered out by the default root level, so INFO
# diagnostics never reached `docker compose logs`. Emit them alongside uvicorn's.
_jlog = logging.getLogger("jbrain")
if not _jlog.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     [jbrain] %(message)s"))
    _jlog.addHandler(_h)
    _jlog.setLevel(logging.INFO)
    _jlog.propagate = False

from .auth import ensure_access_key
from .config import get_settings
from .db import get_conn, init_db
from .routers import (
    action_defs,
    attachments,
    auth_router,
    events,
    push,
    lists,
    locations,
    medical,
    places,
    people,
    tiles,
    share,
    share_admin,
    chat,
    external,
    entities,
    graph,
    notes,
    prompts_router,
    reviews,
    search,
    sql_console,
    staging,
    system,
    workflows,
)
from .services import trips as trips_svc
from .services import workflows as wf_svc

settings = get_settings()

SCHEDULER_INTERVAL_SECONDS = 60


async def _scheduler_loop():
    """Poll for due scheduled workflows. Runs in a worker THREAD so a slow/blocking
    LLM call inside an action can't freeze the event loop (and all HTTP traffic).
    Errors are swallowed per-iteration."""
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(lambda: wf_svc.run_due_scheduled(get_conn()))
        except Exception:  # noqa: BLE001 — never let the loop die
            pass
        try:
            # Location triggers are evaluated here (NOT at ingest): ingest only
            # refreshes geofence state; the firing decision + (LLM-capable) action
            # run on this worker thread where latency is fine.
            await asyncio.to_thread(lambda: wf_svc.evaluate_location_triggers(get_conn()))
        except Exception:  # noqa: BLE001
            pass
        try:
            # Detect/segment trips from new fixes (bounded per pass; commits per person).
            await asyncio.to_thread(lambda: trips_svc.run_detection(get_conn()))
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    generated = ensure_access_key()
    _key_file = "/data/access-key.txt"
    if generated:
        # No key was configured; reveal the generated one once so it can be
        # pasted into the PWA/watch. Written 0600 (owner-only).
        try:
            import os as _os
            fd = _os.open(_key_file, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            with _os.fdopen(fd, "w") as fh:
                fh.write(generated + "\n")
        except OSError:
            pass
        print("\n" + "=" * 60, flush=True)
        print("JBrain generated an access key (paste this into the app):", flush=True)
        print(f"    {generated}", flush=True)
        print(f"Saved to {_key_file} (delete it once you've copied the key).", flush=True)
        print("=" * 60 + "\n", flush=True)
    else:
        # A key is configured in the env: remove any stale cleartext key file
        # from a previous generated-key run so it can't mislead/leak.
        try:
            import os as _os
            if _os.path.exists(_key_file):
                _os.unlink(_key_file)
        except OSError:
            pass

    wf_svc.ingest_repo_workflows(get_conn())  # seed/update repo workflows
    wf_svc.reset_stale_runs(get_conn())        # fail any run left 'running' by a prior process

    from .services import image_analysis
    image_analysis.reset_stale(get_conn())     # fail any attachment analysis left 'pending'

    from .services import push as _push
    _push.ensure_vapid()                       # generate/seed the Web Push VAPID keypair

    from .services import pipeline as _pipeline
    _pipeline.ingest_repo_action_defs(get_conn())  # seed/update action recipes

    from .services import architect
    for w in architect.validate_agent_config(get_conn()):
        print(f"[agent] config warning: {w}", flush=True)

    # Release-blocker: the lab-share recipient tool set must never include an owner tool
    # (query_sql / notes / search). Fail fast at boot if that invariant is ever broken.
    from .services import lab_share_scope
    lab_share_scope.assert_recipient_tools_safe()
    # The recipient labs tool LOOP (assisted shares): its dispatch table must be EXACTLY the
    # declared scoped tools and nothing forbidden — makes the RECIPIENT_TOOLS contract non-vacuous.
    from .services import research_labs_ai
    research_labs_ai.assert_dispatch_safe()
    # Release-blocker: a private-domain (kb/Health/* or kb/Finance/*) note share must always be
    # browser-bound and finite-TTL — never a permanent bearer link — no matter which caller mints it.
    from .services import share as share_svc
    share_svc.assert_private_share_policy()

    from .services import pipeline
    for w in pipeline.validate_action_defs():
        print(f"[pipeline] action warning: {w}", flush=True)

    # Warm the local embedding model in the background so the first capture/search
    # doesn't block on its (one-time) download/load — then one-shot backfill any
    # notes that predate per-note chunk vectors (so long notes become searchable on
    # their bodies, not just their truncated head). Runs off the event loop; search
    # keeps working off vec_notes meanwhile.
    async def _warm_embeddings():
        try:
            from .services import embeddings, wiki_guides
            await asyncio.to_thread(embeddings._get_model)
            # One-time full re-chunk after a chunking-strategy migration (flag set by the
            # migration runner). Runs first so the corpus is uniformly re-indexed; a no-op
            # otherwise. Off the event loop so re-embedding never blocks the server.
            rc = await asyncio.to_thread(lambda: embeddings.run_pending_rechunk(get_conn()))
            if rc is not None:
                print(f"[embeddings] re-chunked {rc} item(s) under the new chunking strategy", flush=True)
            n = await asyncio.to_thread(lambda: embeddings.reindex_missing_note_chunks(get_conn()))
            if n:
                print(f"[embeddings] backfilled chunk vectors for {n} note(s)", flush=True)
            # Same for AI image-analysis sidecars analyzed before they were embedded, so
            # search_notes' semantic half reaches them too (keyword already did via FTS).
            a = await asyncio.to_thread(lambda: embeddings.reindex_missing_attachment_analysis(get_conn()))
            if a:
                print(f"[embeddings] backfilled chunk vectors for {a} attachment sidecar(s)", flush=True)
            # Seed/update the read-only KB guide pages (off the event loop, after the
            # model is warm so the seed writes don't block startup on the first embed).
            g = await asyncio.to_thread(lambda: wiki_guides.seed_guides(get_conn()))
            if g:
                print(f"[wiki] seeded/updated {g} guide page(s)", flush=True)
        except Exception:  # noqa: BLE001
            pass
    asyncio.create_task(_warm_embeddings())

    # Warm the local speech-to-text model too, so the FIRST audio attachment doesn't
    # block on its one-time (~hundreds of MB) download/load. Best-effort and fully
    # decoupled: if faster-whisper isn't installed it raises (caught), and audio simply
    # transcribes on demand instead. Runs off the event loop; nothing waits on it.
    async def _warm_audio():
        try:
            from .services import audio_transcription
            await asyncio.to_thread(audio_transcription._get_model)
            print("[audio] speech-to-text model warmed", flush=True)
        except Exception as exc:  # noqa: BLE001 — missing dep / download failure is non-fatal
            print(f"[audio] transcription model not warmed: {str(exc)[:120]}", flush=True)
    asyncio.create_task(_warm_audio())

    task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="JBrain", lifespan=lifespan)

# Allow a separately-hosted PWA (e.g. GitHub Pages) to call the API with a bearer
# token. allow_credentials is left OFF, so with "*" browsers will NOT attach cookies
# to cross-origin XHR/fetch — the authed API stays bearer-only. The public /share/*
# routes DO use httponly cookies, but those are protected separately: same-origin
# only (samesite=strict/lax) plus explicit sec-fetch-site cross-site rejection on the
# state-changing/billing endpoints. Tighten via JBRAIN_CORS_ORIGINS.
_origins = [o.strip() for o in settings.jbrain_cors_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    # Custom response headers aren't readable by a cross-origin browser unless explicitly
    # exposed (the PWA can be hosted separately from the API). Surface the trail
    # truncation signal so a remotely-hosted client can read it.
    expose_headers=["X-Locations-Truncated", "X-Locations-Count"],
)

for r in (auth_router, notes, chat, external, search, graph, staging, sql_console, attachments, workflows, reviews, system, prompts_router, action_defs, share, share_admin, lists, push, locations, places, people, entities, tiles, medical, events):
    app.include_router(r.router)
# The dictation-capture route (POST /api/notes/entry) accepts a per-person location key
# in addition to the full key, so it lives on a separate, less-restricted router.
app.include_router(notes.entry_router)
# Token-gated media streaming (signed URL, not bearer) so <img>/<audio>/<video> can load
# attachments directly same-origin — no blob:, no media-src CSP dependency.
app.include_router(attachments.public_router)


@app.get("/api/health")
def health():
    return {"ok": True, "brain": settings.brain_name}


# --- Serve the built PWA (single-page app) ----------------------------------
STATIC_DIR = Path(__file__).parent.parent / "static"

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Real files (manifest, icons, sw) are served directly; everything else
        # falls back to index.html for client-side routing.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            # Service-worker scripts must always revalidate so a new push handler
            # ships promptly (they're not content-hashed like /assets).
            if full_path in ("sw.js", "push-sw.js"):
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
