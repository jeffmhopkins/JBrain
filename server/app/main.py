"""JBrain API entrypoint: middleware, routers, health, and PWA static serving."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import ensure_access_key
from .config import get_settings
from .db import get_conn, init_db
from .routers import (
    attachments,
    auth_router,
    capture,
    chat,
    graph,
    notes,
    reviews,
    search,
    sql_console,
    staging,
    workflows,
)
from .services import workflows as wf_svc

settings = get_settings()

SCHEDULER_INTERVAL_SECONDS = 60


async def _scheduler_loop():
    """Poll for due scheduled workflows. Errors are swallowed per-iteration."""
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        try:
            wf_svc.run_due_scheduled(get_conn())
        except Exception:  # noqa: BLE001 — never let the loop die
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    generated = ensure_access_key()
    if generated:
        # No key was configured; reveal the generated one once so it can be
        # pasted into the PWA/watch. Also persisted to /data for retrieval.
        try:
            with open("/data/access-key.txt", "w") as fh:
                fh.write(generated + "\n")
        except OSError:
            pass
        print("\n" + "=" * 60, flush=True)
        print("JBrain generated an access key (paste this into the app):", flush=True)
        print(f"    {generated}", flush=True)
        print("Saved to /data/access-key.txt", flush=True)
        print("=" * 60 + "\n", flush=True)

    wf_svc.ingest_repo_workflows(get_conn())  # seed/update repo workflows
    task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="JBrain", lifespan=lifespan)

for r in (auth_router, notes, chat, search, graph, staging, sql_console, capture, attachments, workflows, reviews):
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
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
