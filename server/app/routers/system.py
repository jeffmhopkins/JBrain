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
from fastapi.responses import FileResponse, Response
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
    """Fetch JSON from the configured GitHub repo API path.

    Args:
        path: URL path appended to the GitHub API base, e.g. ``/releases/latest``.

    Returns:
        Parsed JSON response as a dict or list.
    """
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}{path}",
        headers={"User-Agent": "jbrain", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def _parse(v: str | None) -> tuple:
    """Parse a version/tag string into a comparable integer tuple.

    Args:
        v: Version string such as ``"1.2.3"`` or a git tag; ``None`` yields an empty tuple.

    Returns:
        Tuple of integers extracted from the string, e.g. ``(1, 2, 3)``.
    """
    return tuple(int(x) for x in re.findall(r"\d+", v)) if v else ()


def _fetch_latest() -> dict | None:
    """Fetch the newest release or tag from GitHub without caching.

    Prefers a published Release; falls back to the newest git tag if no Release
    exists.

    Returns:
        Dict with keys ``tag``, ``url``, and ``name``, or ``None`` on failure.
    """
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
    """Return the latest release tag (or newest git tag), cached for an hour.

    Returns:
        Dict with keys ``tag``, ``url``, and ``name``, or ``None`` on failure.
    """
    if time.time() - _cache["ts"] < 3600 and _cache["data"] is not None:
        return _cache["data"]
    data = _fetch_latest()
    _cache.update(ts=time.time(), data=data)
    return data


_main_cache: dict = {"ts": 0.0, "data": None}


def _latest_main_commit() -> dict | None:
    """Return the newest commit on main, cached for an hour.

    Returns:
        Dict with keys ``sha`` and ``url``, or ``None`` on failure.
    """
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
    """Return True if main has commits the deployed build does not.

    Args:
        build_ref: The full or abbreviated git SHA of the currently deployed build.

    Returns:
        True when an update exists on main beyond the current build.
    """
    try:
        j = _http_json(f"/compare/{build_ref}...main")
        return int(j.get("ahead_by", 0)) > 0
    except Exception:
        m = _latest_main_commit()
        return bool(m and not m["sha"].startswith(build_ref) and not build_ref.startswith(m["sha"]))


def _current_label(build_ref: str | None) -> str:
    """Build a human-readable version label for the running server.

    Args:
        build_ref: Git SHA of the current build, or ``None`` if unavailable.

    Returns:
        String like ``"1.2.3 (abc1234)"`` when a ref is known, or just
        ``APP_VERSION`` otherwise.
    """
    return f"{APP_VERSION} ({build_ref[:7]})" if build_ref else APP_VERSION


@router.post("/reset-ai")
def reset_ai():
    """Reset the AI layer to recover from a wedged provider: drop cached SDK clients + cancel runs.

    A streaming turn cancelled mid-flight (e.g. closing the "Edit with AI" panel) can leave the
    cached LLM client's connection pool holding a half-open socket that a later chat/research turn
    waits behind, and can orphan a rebuild run. This owner-only recovery drops the cached clients
    (forcing a fresh pool next call) and cancels every active rebuild/suggest run, with no data
    loss — the next AI request reconnects cleanly.

    Returns:
        JSON ``{"ok": true, "clients_dropped": int, "runs_cancelled": int}``.
    """
    from ..services import llm, rebuild_runs
    return {"ok": True,
            "clients_dropped": llm.reset_clients(),
            "runs_cancelled": rebuild_runs.cancel_all()}


@router.get("/version")
def version():
    """Return the running version and whether a newer release or main commit exists.

    Returns:
        JSON with ``current``, ``latest``, ``update_available``, ``release_url``,
        and ``release_name``.
    """
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
    """Return a maintenance snapshot: storage, uptime, and LLM token usage.

    Covers data-volume storage, process uptime, and LLM token usage today
    and month-to-date (token counts exact; cost estimated). Owner-only.

    Returns:
        JSON with ``storage``, ``uptime_seconds``, ``started_at``, ``tokens``,
        and ``daily_warn_usd``.
    """
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
    # The map tile cache (proxied OSM tiles) is the one app-side thing that grows
    # unbounded in the data volume — surface it so it's never an invisible hog.
    tile_dir = db_path.parent / "tilecache"
    tiles_bytes = 0
    if tile_dir.exists():
        try:
            tiles_bytes = sum(f.stat().st_size for f in tile_dir.rglob("*.png"))
        except OSError:
            pass

    try:
        warn_usd = float(get_meta("daily_cost_warn_usd") or 5.0)
    except (TypeError, ValueError):
        warn_usd = 5.0

    return {
        "storage": {**disk, "db_bytes": db_bytes,
                    "attachments_bytes": att["b"], "attachments_count": att["c"],
                    "tiles_bytes": tiles_bytes},
        "uptime_seconds": int(time.time() - _START_TS),
        "started_at": _START_ISO,
        "tokens": usage_svc.summary(conn),
        "daily_warn_usd": warn_usd,
    }


# Shared dir where update.sh / the auto-updater tee their console output (a bind mount
# also read by Caddy, so the log survives the api restart and a failed deploy).
_DEPLOY_DIR = Path(os.environ.get("JBRAIN_DEPLOY_DIR", "/deploy-status"))


@router.get("/update-log")
def update_log(tail: int = 800):
    """Return the console output of the latest or in-progress deploy.

    Provides the log text plus structured status so the PWA can show a live
    update console. Empty when nothing has been recorded yet.

    Args:
        tail: Maximum number of log lines to return (1–5000, default 800).

    Returns:
        JSON with ``log`` (text), ``status`` (dict or null), and ``mtime``
        (Unix timestamp or null).
    """
    tail = max(1, min(int(tail), 5000))
    log_path, status_path = _DEPLOY_DIR / "update.log", _DEPLOY_DIR / "status.json"
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        text = ""
    if text:
        lines = text.splitlines()
        if len(lines) > tail:
            text = "…(truncated)…\n" + "\n".join(lines[-tail:])
    try:
        status = json.loads(status_path.read_text())
    except (OSError, ValueError):
        status = None
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        mtime = None
    return {"log": text, "status": status, "mtime": mtime}


def _seed_deploy_console() -> None:
    """Write an immediate 'queued' status and log line at deploy-request time.

    Ensures the live console reflects the user's click right away instead of
    showing the previous run until the deployer (update.sh / auto-updater)
    wakes up and starts writing. Best-effort: a no-op when the deploy-status
    mount is read-only.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        (_DEPLOY_DIR / "status.json").write_text(json.dumps({"state": "running", "phase": "queued", "at": now}))
    except OSError:
        pass
    try:
        (_DEPLOY_DIR / "update.log").write_text("==> Update queued — waiting for the deployer to start…\n")
    except OSError:
        pass


@router.post("/update")
def update():
    """Trigger a self-update of the server.

    If ``JBRAIN_UPDATE_CMD`` is configured it is run detached; otherwise an
    update-request marker is written for the host helper (see update.sh).
    Either way the database and secrets are preserved.

    Returns:
        JSON with ``started`` (bool) and ``message`` when a command is set, or
        ``scheduled`` (bool), ``auto`` (bool), and ``message`` otherwise.
    """
    cmd = os.environ.get("JBRAIN_UPDATE_CMD")
    if cmd:
        _seed_deploy_console()
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
    if auto:
        _seed_deploy_console()
    return {
        "scheduled": True,
        "auto": auto,
        "message": (
            "Update requested — the auto-updater will apply it within a few seconds "
            "and restart the server."
            if auto else
            "Update requested. Run ./update.sh on the host (or enable the "
            "auto-updater / set JBRAIN_UPDATE_CMD) to finish."
        ),
    }


@router.get("/export/original-notes")
def export_original_notes():
    """Download original user-authored note content before any AI edits.

    Returns the first ``source='user'`` version of every live note — the exact
    text the user wrote, before any AI edit, rename, link-rewrite, or KB
    synthesis. Architect/import/kb/rename/structural versions are excluded.

    Returns:
        JSON file attachment — an array of ``{title, content_md, created_at}``
        objects ordered by creation time.
    """
    conn = db_mod.get_conn()
    rows = conn.execute(
        "SELECT nv.title, nv.content_md, nv.created_at "
        "FROM note_versions nv "
        "JOIN (SELECT note_id, MIN(id) AS fid FROM note_versions "
        "      WHERE source = 'user' GROUP BY note_id) f ON f.fid = nv.id "
        "JOIN notes n ON n.id = nv.note_id AND n.deleted_at IS NULL "
        "ORDER BY nv.created_at, nv.note_id"
    ).fetchall()
    data = [{"title": r["title"], "content_md": r["content_md"], "created_at": r["created_at"]}
            for r in rows]
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fname = f"jbrain-original-notes-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(content=payload, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/backup")
def backup():
    """Download a consistent snapshot of the entire database as a single .db file.

    Returns:
        Binary .db file attachment with a timestamped filename.
    """
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
    """Replace the entire database from an uploaded JBrain backup file.

    Args:
        file: A SQLite .db file previously produced by the /backup endpoint.

    Returns:
        JSON ``{"ok": true, "message": "Database restored."}``.

    Raises:
        HTTPException: 400 if the file is not a valid SQLite database or
            the restore fails validation.
    """
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


# --- Media & transcription settings (DB `meta` overrides; read at runtime, no restart) ------
from pydantic import BaseModel as _BaseModel   # noqa: E402


class MediaSettingsIn(_BaseModel):
    """Input schema for updating media/transcription settings."""

    audio_model: str | None = None
    audio_compute_type: str | None = None
    video_frame_interval: str | None = None
    video_frame_max: int | None = None


def _media_settings() -> dict:
    """Collect current media/transcription settings plus allowed option lists.

    Returns:
        Dict with ``audio_model``, ``audio_compute_type``, ``video_frame_interval``,
        ``video_frame_max``, ``audio_model_options``, and ``compute_type_options``.
    """
    from ..services import audio_transcription as at
    return {
        "audio_model": at.audio_model(),
        "audio_compute_type": at.audio_compute_type(),
        "video_frame_interval": at.video_frame_interval(),
        "video_frame_max": at.video_frame_max(),
        "audio_model_options": ["tiny", "base", "small", "medium", "large-v3"],
        "compute_type_options": ["int8", "int8_float16", "float16", "float32"],
    }


# --- Auto-analyze new notes (single source of truth: the analyze-new-note workflow's
#     enabled flag; see note_analysis.auto_enabled) -----------------------------------


class AutoAnalyzeIn(_BaseModel):
    """Input schema for the auto-analyze toggle."""

    enabled: bool


@router.get("/settings/auto-analyze")
def get_auto_analyze():
    """Return whether auto-analysis of new notes is currently enabled.

    Returns:
        JSON ``{"enabled": bool}``.
    """
    from ..db import get_conn
    from ..services import note_analysis as na
    return {"enabled": na.auto_enabled(get_conn())}


@router.put("/settings/auto-analyze")
def set_auto_analyze(body: AutoAnalyzeIn):
    """Toggle auto-analysis of new notes.

    Flips the analyze-new-note workflow's enabled flag and sets ``locked=1`` so
    a repo re-ingest cannot reset the owner's choice. Seeds the workflow from the
    repo if it is not yet present (e.g. before the first boot ingest).

    Args:
        body: ``{"enabled": bool}`` — the desired state.

    Returns:
        JSON ``{"enabled": bool}`` reflecting the new state.

    Raises:
        HTTPException: 500 if the underlying workflow cannot be found or seeded.
    """
    from ..db import get_conn
    from ..services import note_analysis as na
    from ..services import workflows as wf_svc
    conn = get_conn()
    row = conn.execute("SELECT id FROM workflows WHERE key = ?", (na.AUTO_ANALYZE_WORKFLOW_KEY,)).fetchone()
    if row is None:
        wf_svc.ingest_repo_workflows(conn)
        row = conn.execute("SELECT id FROM workflows WHERE key = ?", (na.AUTO_ANALYZE_WORKFLOW_KEY,)).fetchone()
    if row is None:
        # The repo workflow couldn't be seeded — don't return a success-shaped no-op.
        raise HTTPException(status_code=500, detail="Could not find the analyze-new-note workflow to toggle.")
    conn.execute(
        "UPDATE workflows SET enabled = ?, locked = 1, updated_at = datetime('now') WHERE id = ?",
        (1 if body.enabled else 0, row["id"]),
    )
    conn.commit()
    return {"enabled": na.auto_enabled(conn)}


@router.get("/settings/media")
def get_media_settings():
    """Return current media and transcription settings with available option lists.

    Returns:
        JSON with ``audio_model``, ``audio_compute_type``, ``video_frame_interval``,
        ``video_frame_max``, ``audio_model_options``, and ``compute_type_options``.
    """
    return _media_settings()


@router.put("/settings/media")
def set_media_settings(body: MediaSettingsIn):
    """Persist media and transcription settings overrides to the DB.

    Only fields present in the request body are updated; absent fields are
    left unchanged.

    Args:
        body: Partial or complete set of media settings to apply.

    Returns:
        Updated settings dict (same shape as GET /settings/media).
    """
    from ..db import get_conn, set_meta
    conn = get_conn()
    if body.audio_model is not None:
        set_meta(conn, "audio_model", body.audio_model.strip())
    if body.audio_compute_type is not None:
        set_meta(conn, "audio_compute_type", body.audio_compute_type.strip())
    if body.video_frame_interval is not None:
        set_meta(conn, "video_frame_interval", body.video_frame_interval.strip())
    if body.video_frame_max is not None:
        set_meta(conn, "video_frame_max", str(max(0, int(body.video_frame_max))))
    conn.commit()
    return _media_settings()


# --- Local LLM (Ollama) model management ------------------------------------
from fastapi.responses import StreamingResponse   # noqa: E402


class LocalPullIn(_BaseModel):
    """Input schema for pulling a local model."""

    name: str


@router.get("/local-models")
def get_local_models():
    """List local (Ollama) models with per-model fit verdicts and a hardware profile.

    Returns:
        JSON ``{running, models: [{name, size_bytes, ram_estimate_bytes, fits, warn,
        state}], hardware: {usable_ram_bytes, total_ram_bytes, cpu_only, note}}``.
        ``running`` is False when Ollama is unreachable.
    """
    from ..services import local_models
    return local_models.describe_models()


@router.post("/local-models/pull")
def pull_local_model(body: LocalPullIn):
    """Pull a local model, streaming progress as Server-Sent Events.

    Streams ``data: {json}`` frames the PWA parses: ``{type:"status"|"progress"|
    "done"|"error", ...}``. Multi-GB downloads run for a while; the stream ends with a
    ``done`` (or ``error``) event.

    Args:
        body: ``{"name": "<model:tag>"}`` — the model to pull.

    Returns:
        A text/event-stream StreamingResponse of pull-progress events.
    """
    from ..services import local_models

    def _events():
        """Serialise local_models.pull_events into SSE data frames."""
        for evt in local_models.pull_events(body.name):
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.delete("/local-models/{name:path}")
def delete_local_model(name: str):
    """Delete a pulled local model from Ollama.

    Args:
        name: Model id to remove (``:path`` so an Ollama 'name:tag' passes intact).

    Returns:
        JSON ``{"removed": bool}``.

    Raises:
        HTTPException: 502 if Ollama is unreachable or the delete failed.
    """
    from ..services import local_models
    ok = local_models.delete_model(name)
    if not ok:
        raise HTTPException(status_code=502, detail="Could not delete the model (is Ollama running?).")
    return {"removed": True}
