"""Workflows REST API: list/get/create/update/toggle/delete/run + run history.

Any edit via the PWA sets `locked=1` so a repo re-ingest won't overwrite it.
"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import workflows as wf_svc

router = APIRouter(prefix="/api/workflows", tags=["workflows"], dependencies=[CurrentUser])


class WorkflowIn(BaseModel):
    """Input body for creating or updating a workflow."""

    name: str
    trigger_type: str            # 'event' | 'schedule'
    trigger_config: dict = {}
    action_type: str
    action_config: dict = {}
    enabled: bool = True


def _row(conn, wf_id: int):
    """Fetch a workflow row by id, raising 404 if not found.

    Args:
        conn: Active database connection.
        wf_id: Workflow id to look up.

    Returns:
        The raw workflow database row.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return row


def _public(row) -> dict:
    """Convert a raw workflow db row to a JSON-serializable public dict.

    Deserializes trigger_config and action_config from JSON strings and coerces
    boolean fields.

    Args:
        row: Raw database row for a workflow.

    Returns:
        Dict with deserialized config fields and bool-typed enabled/locked.
    """
    d = dict(row)
    d["trigger_config"] = json.loads(d.get("trigger_config") or "{}")
    d["action_config"] = json.loads(d.get("action_config") or "{}")
    d["enabled"] = bool(d["enabled"])
    d["locked"] = bool(d["locked"])
    return d


@router.get("")
def list_workflows():
    """List all workflows, ordered by name.

    Returns:
        List of public workflow dicts.
    """
    rows = get_conn().execute("SELECT * FROM workflows ORDER BY name").fetchall()
    return [_public(r) for r in rows]


@router.get("/action-types")
def action_types():
    """Return the catalog of action types with config-form schemas (data-driven picker).

    Returns:
        Action catalog dict keyed by type name.
    """
    return wf_svc.action_catalog()


@router.get("/{wf_id}")
def get_workflow(wf_id: int):
    """Return a single workflow by id.

    Args:
        wf_id: Workflow id.

    Returns:
        Public workflow dict.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    return _public(_row(get_conn(), wf_id))


@router.post("")
def create_workflow(body: WorkflowIn):
    """Create a new user-owned workflow (locked=1 from the start).

    Args:
        body: Workflow name, trigger, action configuration, and enabled state.

    Returns:
        Public dict of the newly created workflow.
    """
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO workflows (name, trigger_type, trigger_config, action_type, "
        "action_config, enabled, source, locked) VALUES (?, ?, ?, ?, ?, ?, 'user', 1)",
        (body.name, body.trigger_type, json.dumps(body.trigger_config),
         body.action_type, json.dumps(body.action_config), 1 if body.enabled else 0),
    )
    conn.commit()
    return _public(_row(conn, cur.lastrowid))


@router.put("/{wf_id}")
def update_workflow(wf_id: int, body: WorkflowIn):
    """Replace a workflow's configuration (marks it locked so repo re-ingest won't overwrite).

    Args:
        wf_id: Workflow id to update.
        body: New workflow name, trigger, action configuration, and enabled state.

    Returns:
        Updated public workflow dict.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    conn = get_conn()
    _row(conn, wf_id)
    conn.execute(
        "UPDATE workflows SET name=?, trigger_type=?, trigger_config=?, action_type=?, "
        "action_config=?, enabled=?, locked=1, updated_at=datetime('now') WHERE id=?",
        (body.name, body.trigger_type, json.dumps(body.trigger_config), body.action_type,
         json.dumps(body.action_config), 1 if body.enabled else 0, wf_id),
    )
    conn.commit()
    return _public(_row(conn, wf_id))


@router.post("/{wf_id}/toggle")
def toggle_workflow(wf_id: int):
    """Toggle a workflow's enabled state (also marks it locked).

    Args:
        wf_id: Workflow id to toggle.

    Returns:
        Updated public workflow dict.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    conn = get_conn()
    row = _row(conn, wf_id)
    conn.execute(
        "UPDATE workflows SET enabled=?, locked=1, updated_at=datetime('now') WHERE id=?",
        (0 if row["enabled"] else 1, wf_id),
    )
    conn.commit()
    return _public(_row(conn, wf_id))


@router.delete("/{wf_id}")
def delete_workflow(wf_id: int):
    """Delete a workflow permanently.

    Args:
        wf_id: Workflow id to delete.

    Returns:
        Dict with key 'ok' set to True.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    conn = get_conn()
    _row(conn, wf_id)
    conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
    conn.commit()
    return {"ok": True}


@router.post("/{wf_id}/run")
def run_now(wf_id: int):
    """Start the workflow trigger in the background and return immediately.

    Poll /{wf_id}/run-status for progress; runs can take a while (e.g. LLM calls).

    Args:
        wf_id: Workflow id to run.

    Returns:
        Dict with run initiation status from wf_svc.start_manual_run.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    conn = get_conn()
    _row(conn, wf_id)  # 404 if missing
    return wf_svc.start_manual_run(conn, wf_id)


@router.get("/{wf_id}/run-status")
def run_status(wf_id: int):
    """Return the latest run's state for the workflow watch modal.

    Status is 'running' until the job finishes, then 'ok' | 'error' | 'skipped'.
    step_since/now let the watch modal show how long the current step has been running
    (a long LLM step looks frozen otherwise) and survive a modal reopen or page reload
    mid-run. Returns {status: 'none'} when no run has started yet.

    Args:
        wf_id: Workflow id whose latest run to query.

    Returns:
        Dict with status, detail, events list, step_since, and server-clock now.
    """
    from datetime import datetime, timezone
    row = get_conn().execute(
        "SELECT id, started_at, status, detail FROM workflow_runs "
        "WHERE workflow_id = ? ORDER BY id DESC LIMIT 1",
        (wf_id,),
    ).fetchone()
    if not row:
        return {"status": "none", "detail": "", "events": [], "step_since": None, "now": None}
    d = dict(row)
    prog = wf_svc.run_progress(row["id"])          # live step trace for the watch modal
    evs = prog["events"] if prog else []
    d["events"] = [e["name"] for e in evs]
    d["step_since"] = evs[-1]["at"] if evs else None   # when the latest step began
    d["now"] = datetime.now(timezone.utc).isoformat()  # server clock (avoids skew)
    return d


@router.post("/sync")
def sync_from_repo():
    """Re-ingest repo workflow YAML, adding new ones and updating unlocked changed ones.

    Allows the PWA to pull newly-deployed or updated workflows without a server restart.

    Returns:
        Dict with 'synced' count and the full updated 'workflows' list.
    """
    conn = get_conn()
    n = wf_svc.ingest_repo_workflows(conn)
    return {"synced": n, "workflows": [_public(r) for r in
            conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()]}


@router.post("/{wf_id}/reset")
def reset_to_repo(wf_id: int):
    """Unlock a user-edited workflow so the repo definition can refresh it on next sync.

    Args:
        wf_id: Workflow id to unlock and refresh.

    Returns:
        Updated public workflow dict after re-applying the repo definition.

    Raises:
        HTTPException: 404 if the workflow does not exist.
    """
    conn = get_conn()
    _row(conn, wf_id)
    conn.execute("UPDATE workflows SET locked = 0 WHERE id = ?", (wf_id,))
    conn.commit()
    wf_svc.ingest_repo_workflows(conn)  # re-apply repo definition if present
    return _public(_row(conn, wf_id))


@router.get("/{wf_id}/runs")
def runs(wf_id: int):
    """Return the 50 most recent run records for a workflow.

    Args:
        wf_id: Workflow id whose run history to retrieve.

    Returns:
        List of run dicts (id, started_at, status, detail), newest first.
    """
    rows = get_conn().execute(
        "SELECT id, started_at, status, detail FROM workflow_runs "
        "WHERE workflow_id = ? ORDER BY id DESC LIMIT 50",
        (wf_id,),
    ).fetchall()
    return [dict(r) for r in rows]
