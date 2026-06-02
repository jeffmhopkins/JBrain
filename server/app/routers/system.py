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

# Override with JBRAIN_REPO=owner/name if you run a fork (so update checks point
# at your repo, not upstream).
GITHUB_REPO = os.environ.get("JBRAIN_REPO", "jeffmhopkins/JBrain")
_cache: dict = {"ts": 0.0, "data": None}

# Process start ≈ module import (startup). Used for the uptime stat.
_START_TS = time.time()
_START_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _http_json(path: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}{path}",
        headers={"User-Agent": "jbrain", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def _parse(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)) if v else ()


def _fetch_latest() -> dict | None:
    # Prefer a published Release…
    try:
        j = _http_json("/releases/latest")
        if j.get("tag_name"):
            return {"tag": j["tag_name"], "url": j.get("html_url"), "name": j.get("name")}
    except Exception:
        pass
    # …otherwise fall back to the newest git tag (so a pushed tag is enough).
    try:
        tags = _http_json("/tags?per_page=100")
        names = [t["name"] for t in tags if t.get("name")]
        if names:
            top = max(names, key=_parse)
            return {"tag": top, "name": top,
                    "url": f"https://github.com/{GITHUB_REPO}/releases/tag/{top}"}
    except Exception:
        pass
    return None


def _latest_release() -> dict | None:
    """Latest release tag (or newest git tag), cached for an hour. None on failure."""
    if time.time() - _cache["ts"] < 3600 and _cache["data"] is not None:
        return _cache["data"]
    data = _fetch_latest()
    _cache.update(ts=time.time(), data=data)
    return data


_main_cache: dict = {"ts": 0.0, "data": None}


def _latest_main_commit() -> dict | None:
    """Newest commit on main, cached for an hour. None on failure."""
    if time.time() - _main_cache["ts"] < 3600 and _main_cache["data"] is not None:
        return _main_cache["data"]
    data = None
    try:
        j = _http_json("/commits/main")
        data = {"sha": j["sha"], "url": j.get("html_url")}
    except Exception:
        data = None
    _main_cache.update(ts=time.time(), data=data)
    return data


def _main_is_ahead(build_ref: str) -> bool:
    """True if main has commits the deployed build doesn't (i.e. an update exists)."""
    try:
        j = _http_json(f"/compare/{build_ref}...main")
        return int(j.get("ahead_by", 0)) > 0
    except Exception:
        m = _latest_main_commit()
        return bool(m and not m["sha"].startswith(build_ref) and not build_ref.startswith(m["sha"]))


def _current_label(build_ref: str | None) -> str:
    return f"{APP_VERSION} ({build_ref[:7]})" if build_ref else APP_VERSION


@router.get("/version")
def version():
    build_ref = os.environ.get("JBRAIN_BUILD_REF") or None

    # A published release/tag newer than this build wins (if you use tags).
    rel = _latest_release()
    if rel and _parse(rel["tag"]) > _parse(APP_VERSION):
        return {
            "current": _current_label(build_ref), "latest": rel["tag"],
            "update_available": True, "release_url": rel["url"], "release_name": rel.get("name"),
        }

    # Otherwise track main by commit: update available when main is ahead.
    main = _latest_main_commit()
    avail = bool(build_ref and main and _main_is_ahead(build_ref))
    return {
        "current": _current_label(build_ref),
        "latest": ("main@" + main["sha"][:7]) if (avail and main) else None,
        "update_available": avail,
        "release_url": main["url"] if (avail and main) else (rel["url"] if rel else None),
        "release_name": None,
    }


@router.get("/stats")
def stats():
    """Maintenance snapshot: data-volume storage, process uptime, and LLM token
    usage today + month-to-date (token counts exact; $ estimated). Owner-only."""
    import shutil
    from ..db import get_conn, get_meta
    from ..services import usage as usage_svc

    conn = get_conn()
    db_path = Path(get_settings().db_path)
    try:
        du = shutil.disk_usage(db_path.parent)
        disk = {"total": du.total, "used": du.used, "free": du.free,
                "percent": round(du.used / du.total * 100, 1) if du.total else 0.0}
    except OSError:
        disk = {"total": 0, "used": 0, "free": 0, "percent": 0.0}
    # The DB file plus its WAL sidecar is the real on-disk DB footprint.
    db_bytes = sum(
        (db_path.parent / f"{db_path.name}{suffix}").stat().st_size
        for suffix in ("", "-wal", "-shm")
        if (db_path.parent / f"{db_path.name}{suffix}").exists()
    )
    att = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(byte_size), 0) b FROM attachments").fetchone()

    try:
        warn_usd = float(get_meta("daily_cost_warn_usd") or 5.0)
    except (TypeError, ValueError):
        warn_usd = 5.0

    return {
        "storage": {**disk, "db_bytes": db_bytes,
                    "attachments_bytes": att["b"], "attachments_count": att["c"]},
        "uptime_seconds": int(time.time() - _START_TS),
        "started_at": _START_ISO,
        "tokens": usage_svc.summary(conn),
        "daily_warn_usd": warn_usd,
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
    # Re-seed repo workflows + action recipes so the engine doesn't run a stale
    # (or empty) recipe set carried in from an older backup.
    from ..db import get_conn
    from ..services import pipeline, workflows as wf_svc
    wf_svc.ingest_repo_workflows(get_conn())
    pipeline.ingest_repo_action_defs(get_conn())
    return {"ok": True, "message": "Database restored."}
