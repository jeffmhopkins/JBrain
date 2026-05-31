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
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from ..auth import CurrentUser
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
