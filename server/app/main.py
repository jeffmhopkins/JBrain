"""JBrain API entrypoint: middleware, routers, health, and PWA static serving."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import ensure_access_key
from .config import get_settings
from .db import get_conn, init_db
from .routers import (
    action_defs,
    attachments,
    auth_router,
    push,
    capture,
    lists,
    locations,
    share,
    share_admin,
    chat,
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

    from .services import pipeline
    for w in pipeline.validate_action_defs():
        print(f"[pipeline] action warning: {w}", flush=True)

    # Warm the local embedding model in the background so the first capture/search
    # doesn't block on its (one-time) download/load.
    async def _warm_embeddings():
        try:
            from .services import embeddings
            await asyncio.to_thread(embeddings._get_model)
        except Exception:  # noqa: BLE001
            pass
    asyncio.create_task(_warm_embeddings())

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
)

for r in (auth_router, notes, chat, search, graph, staging, sql_console, capture, attachments, workflows, reviews, system, prompts_router, action_defs, share, share_admin, lists, push, locations):
    app.include_router(r.router)


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
