"""JBrain API entrypoint: middleware, routers, health, and PWA static serving."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import ensure_access_key
from .config import get_settings
from .db import init_db
from .routers import (
    auth_router,
    capture,
    chat,
    graph,
    notes,
    search,
    sql_console,
    staging,
)

settings = get_settings()


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
    yield


app = FastAPI(title="JBrain", lifespan=lifespan)

for r in (auth_router, notes, chat, search, graph, staging, sql_console, capture):
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
