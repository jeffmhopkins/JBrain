"""The staging area: list, apply, and reject proposed wiki changes.

Nothing the architect proposes touches the wiki until it is applied here.
"""
import hashlib
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
    # Architect-applied edits are attributed to 'architect' in the version history,
    # and stamped with where the user was when they had this conversation.
    loc = notes_svc.conversation_location(conn, conversation_id)
    kw = {"source": "architect", "conversation_id": conversation_id}
    if loc:
        kw.update(lat=loc["lat"], lon=loc["lon"], location_label=loc["location_label"])
    if action_type in ("CREATE", "UPDATE"):
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="CREATE/UPDATE action is missing a title")
        basis = payload.get("_basis") or {}
        # CREATE always makes a new note. An UPDATE with no captured basis (the
        # title didn't exist at propose time) is also treated as create-only, so
        # it can't overwrite a note that appeared in the meantime.
        create_only = action_type == "CREATE" or not basis.get("note_id")
        if action_type == "UPDATE" and basis.get("note_id"):
            # Optimistic concurrency: refuse a stale edit whose basis note has
            # changed (or been deleted) since it was proposed — the model's full
            # content would otherwise silently clobber the intervening change.
            live = conn.execute(
                "SELECT content_md FROM notes WHERE id = ? AND deleted_at IS NULL",
                (basis["note_id"],),
            ).fetchone()
            if live is None:
                raise HTTPException(status_code=409, detail="The target note no longer exists — re-propose the change.")
            if basis.get("content_hash"):
                live_hash = hashlib.sha256((live["content_md"] or "").encode("utf-8")).hexdigest()
                if live_hash != basis["content_hash"]:
                    raise HTTPException(status_code=409, detail="The note changed since this edit was proposed — re-open it and re-propose.")
        notes_svc.upsert_note(conn, title, payload.get("content") or "",
                              create_only=create_only, **kw)
    elif action_type == "LINK":
        source_title = (payload.get("source_title") or "").strip()
        target_title = (payload.get("target_title") or "").strip()
        if not source_title or not target_title:
            raise HTTPException(status_code=400, detail="LINK action needs source_title and target_title")
        source = notes_svc.get_by_title(conn, source_title)
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
    # Claim the row atomically first so two concurrent applies can't both pass
    # the pending check and apply the same action twice.
    claim = conn.execute(
        "UPDATE staging_actions SET status = 'applied' WHERE id = ? AND status = 'pending'",
        (action_id,),
    )
    if claim.rowcount != 1:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Action is no longer pending")
    try:
        _apply_action(conn, row["type"], json.loads(row["payload_json"]), row["conversation_id"])
    except Exception:
        conn.rollback()  # undoes the claim + any partial write -> the row stays pending
        raise
    conn.commit()
    return {"ok": True}


@router.post("/apply-all")
def apply_all():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM staging_actions WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    # All-or-nothing: a bad action rolls back the whole batch instead of leaving
    # some rows applied and committed while the rest are abandoned.
    applied = 0
    try:
        for r in rows:
            # Claim each row first; skip any a concurrent single-apply already took.
            claim = conn.execute(
                "UPDATE staging_actions SET status = 'applied' WHERE id = ? AND status = 'pending'",
                (r["id"],),
            )
            if claim.rowcount != 1:
                continue
            _apply_action(conn, r["type"], json.loads(r["payload_json"]), r["conversation_id"])
            applied += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "applied": applied}


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
        if not quicktasks.remove_line_from_note(conn, undo["title"], undo["line"], source="user"):
            # The line was already edited/removed — don't mark the action undone
            # (which would lie to the UI), report that there's nothing to undo.
            conn.rollback()
            raise HTTPException(status_code=409, detail="Nothing to undo — that line was already changed or removed.")
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
