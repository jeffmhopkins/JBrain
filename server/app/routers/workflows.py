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
    name: str
    trigger_type: str            # 'event' | 'schedule'
    trigger_config: dict = {}
    action_type: str
    action_config: dict = {}
    enabled: bool = True


def _row(conn, wf_id: int):
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return row


def _public(row) -> dict:
    d = dict(row)
    d["trigger_config"] = json.loads(d.get("trigger_config") or "{}")
    d["action_config"] = json.loads(d.get("action_config") or "{}")
    d["enabled"] = bool(d["enabled"])
    d["locked"] = bool(d["locked"])
    return d


@router.get("")
def list_workflows():
    rows = get_conn().execute("SELECT * FROM workflows ORDER BY name").fetchall()
    return [_public(r) for r in rows]


@router.get("/action-types")
def action_types():
    """The catalog of action types + config-form schemas (data-driven picker)."""
    return wf_svc.action_catalog()


@router.get("/{wf_id}")
def get_workflow(wf_id: int):
    return _public(_row(get_conn(), wf_id))


@router.post("")
def create_workflow(body: WorkflowIn):
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
    conn = get_conn()
    _row(conn, wf_id)
    conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
    conn.commit()
    return {"ok": True}


@router.post("/{wf_id}/run")
def run_now(wf_id: int):
    conn = get_conn()
    status, detail = wf_svc.run_workflow(conn, _row(conn, wf_id))
    return {"status": status, "detail": detail}


@router.post("/sync")
def sync_from_repo():
    """Re-ingest repo workflow YAML: add new ones, update unlocked changed ones.
    Lets the PWA pull newly-deployed/updated workflows without a restart."""
    conn = get_conn()
    n = wf_svc.ingest_repo_workflows(conn)
    return {"synced": n, "workflows": [_public(r) for r in
            conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()]}


@router.post("/{wf_id}/reset")
def reset_to_repo(wf_id: int):
    """Unlock a user-edited workflow so the repo definition can refresh it."""
    conn = get_conn()
    _row(conn, wf_id)
    conn.execute("UPDATE workflows SET locked = 0 WHERE id = ?", (wf_id,))
    conn.commit()
    wf_svc.ingest_repo_workflows(conn)  # re-apply repo definition if present
    return _public(_row(conn, wf_id))


@router.get("/{wf_id}/runs")
def runs(wf_id: int):
    rows = get_conn().execute(
        "SELECT id, started_at, status, detail FROM workflow_runs "
        "WHERE workflow_id = ? ORDER BY id DESC LIMIT 50",
        (wf_id,),
    ).fetchall()
    return [dict(r) for r in rows]
