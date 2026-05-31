"""System: version check against GitHub releases + trigger a self-update.

The update is non-destructive: the SQLite DB and Caddy certs live on named Docker
volumes and are untouched; .env (access key, API keys) is a persistent file and
is not regenerated; schema changes are applied by the migration runner on the
next boot. See update.sh.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import db as db_mod
from ..auth import CurrentUser, ensure_access_key
from ..config import get_settings
from ..version import APP_VERSION

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[CurrentUser])

GITHUB_REPO = "jeffmhopkins/jbrain"
_cache: dict = {"ts": 0.0, "data": None}


def _latest_release() -> dict | None:
    """Fetch the latest GitHub release, cached for an hour. None on any failure."""
    if time.time() - _cache["ts"] < 3600 and _cache["data"] is not None:
        return _cache["data"]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": "jbrain", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.load(r)
        data = {"tag": j.get("tag_name"), "url": j.get("html_url"), "name": j.get("name")}
    except Exception:
        data = None
    _cache.update(ts=time.time(), data=data)
    return data


def _parse(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)) if v else ()


@router.get("/version")
def version():
    rel = _latest_release()
    latest = rel["tag"] if rel else None
    available = bool(latest and _parse(latest) > _parse(APP_VERSION))
    return {
        "current": APP_VERSION,
        "latest": latest,
        "update_available": available,
        "release_url": rel["url"] if rel else None,
        "release_name": rel["name"] if rel else None,
    }


@router.post("/update")
def update():
    """Trigger a self-update. If JBRAIN_UPDATE_CMD is configured it is run
    (detached); otherwise an update-request marker is written for a host helper
    (see update.sh). Either way the database and secrets are preserved."""
    cmd = os.environ.get("JBRAIN_UPDATE_CMD")
    if cmd:
        subprocess.Popen(cmd, shell=True, start_new_session=True)
        return {"started": True, "message": "Update started; the server will restart shortly."}

    rel = _latest_release()
    flag = Path(get_settings().db_path).parent / "update-requested.json"
    try:
        flag.write_text(json.dumps({
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "target": rel["tag"] if rel else "latest",
        }))
    except OSError:
        pass

    auto = "autoupdate" in os.environ.get("COMPOSE_PROFILES", "")
    return {
        "scheduled": True,
        "auto": auto,
        "message": (
            "Update requested — the auto-updater will apply it within ~30s and "
            "restart the server."
            if auto else
            "Update requested. Run ./update.sh on the host (or enable the "
            "auto-updater / set JBRAIN_UPDATE_CMD) to finish."
        ),
    }


@router.get("/backup")
def backup():
    """Download a consistent snapshot of the entire database (one .db file)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_mod.backup_to_file(tmp.name)
    fname = f"jbrain-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(
        tmp.name, media_type="application/octet-stream", filename=fname,
        background=BackgroundTask(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name)),
    )


@router.post("/restore")
async def restore(file: UploadFile = File(...)):
    """Replace the entire database from an uploaded JBrain backup (.db)."""
    raw = await file.read()
    if raw[:16] != b"SQLite format 3\x00":
        raise HTTPException(status_code=400, detail="Not a SQLite database file.")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(raw)
    tmp.close()
    try:
        db_mod.restore_from_file(tmp.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        os.path.exists(tmp.name) and os.unlink(tmp.name)
    # Keep the configured access key valid even if the backup carried a different one.
    ensure_access_key()
    return {"ok": True, "message": "Database restored."}
