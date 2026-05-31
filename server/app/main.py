"""JBrain API entrypoint: middleware, routers, health, and PWA static serving."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

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
    yield


app = FastAPI(title="JBrain", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.jbrain_domain not in ("localhost", "127.0.0.1", ""),
    max_age=60 * 60 * 24 * 30,  # 30 days
)

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
