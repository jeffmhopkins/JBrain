"""The staging area: list, apply, and reject proposed wiki changes.

Nothing the architect proposes touches the wiki until it is applied here.
"""
import json

from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc
from ..services import quicktasks

router = APIRouter(prefix="/api/staging", tags=["staging"], dependencies=[CurrentUser])


@router.get("")
def list_pending():
    rows = get_conn().execute(
        "SELECT id, conversation_id, type, payload_json, status, created_at "
        "FROM staging_actions WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [
        {**dict(r), "payload": json.loads(r["payload_json"])} for r in rows
    ]


def _apply_action(conn, action_type: str, payload: dict, conversation_id: int | None = None) -> None:
    # Architect-applied edits are attributed to 'architect' in the version history.
    kw = {"source": "architect", "conversation_id": conversation_id}
    if action_type in ("CREATE", "UPDATE"):
        notes_svc.upsert_note(conn, payload["title"], payload.get("content", ""), **kw)
    elif action_type == "LINK":
        source = notes_svc.get_by_title(conn, payload["source_title"])
        target_title = payload["target_title"]
        if source and f"[[{target_title}]]" not in source["content_md"]:
            new_content = source["content_md"].rstrip() + f"\n\n[[{target_title}]]\n"
            notes_svc.upsert_note(conn, source["title"], new_content, **kw)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action type: {action_type}")


@router.post("/{action_id}/apply")
def apply_action(action_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM staging_actions WHERE id = ? AND status = 'pending'", (action_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pending action not found")
    _apply_action(conn, row["type"], json.loads(row["payload_json"]), row["conversation_id"])
    conn.execute("UPDATE staging_actions SET status = 'applied' WHERE id = ?", (action_id,))
    conn.commit()
    return {"ok": True}


@router.post("/apply-all")
def apply_all():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM staging_actions WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    for r in rows:
        _apply_action(conn, r["type"], json.loads(r["payload_json"]), r["conversation_id"])
        conn.execute("UPDATE staging_actions SET status = 'applied' WHERE id = ?", (r["id"],))
    conn.commit()
    return {"ok": True, "applied": len(rows)}


@router.post("/{action_id}/reject")
def reject_action(action_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE staging_actions SET status = 'rejected' WHERE id = ? AND status = 'pending'",
        (action_id,),
    )
    conn.commit()
    return {"ok": True}


@router.post("/{action_id}/undo")
def undo_action(action_id: int):
    """Undo an auto-applied additive op by applying its recorded inverse."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM staging_actions WHERE id = ? AND status = 'applied'", (action_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Applied action not found")
    undo = json.loads(row["payload_json"]).get("undo") or {}
    op = undo.get("op")

    if op == "remove_line":
        quicktasks.remove_line_from_note(conn, undo["title"], undo["line"], source="user")
    elif op == "delete_inbox":
        conn.execute("DELETE FROM inbox WHERE id = ?", (undo["id"],))
    elif op == "unmark_inbox":
        conn.executemany(
            "UPDATE inbox SET processed = 0 WHERE id = ?", [(i,) for i in undo.get("ids", [])]
        )
    else:
        raise HTTPException(status_code=400, detail="Action cannot be undone")

    conn.execute("UPDATE staging_actions SET status = 'undone' WHERE id = ?", (action_id,))
    conn.commit()
    return {"ok": True}
