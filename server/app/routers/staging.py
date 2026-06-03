"""The staging area: list, apply, and reject proposed wiki changes.

Nothing the architect proposes touches the wiki until it is applied here.
"""
import hashlib
import json
import sqlite3

from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc
from ..services import places as places_svc
from ..services import quicktasks

router = APIRouter(prefix="/api/staging", tags=["staging"], dependencies=[CurrentUser])


@router.get("")
def list_pending():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, conversation_id, type, payload_json, status, created_at "
        "FROM staging_actions WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    def _current(payload):
        # The note's CURRENT content (for a diff preview), by basis id then title.
        basis = payload.get("_basis") or {}
        if basis.get("note_id"):
            r = conn.execute("SELECT content_md FROM notes WHERE id=? AND deleted_at IS NULL",
                             (basis["note_id"],)).fetchone()
            if r:
                return r["content_md"]
        title = (payload.get("title") or "").strip()
        if title:
            n = notes_svc.get_by_title(conn, title)
            if n:
                return n["content_md"]
        return None
    out = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        item = {**dict(r), "payload": payload}
        if r["type"] in ("CREATE", "UPDATE"):
            item["current_content"] = _current(payload)
        out.append(item)
    return out


def _apply_action(conn, action_type: str, payload: dict, conversation_id: int | None = None,
                  action_id: int | None = None, source: str = "architect") -> None:
    # Architect-applied edits are attributed to 'architect' in the version history,
    # and stamped with where the user was when they had this conversation.
    loc = notes_svc.conversation_location(conn, conversation_id)
    kw = {"source": source, "conversation_id": conversation_id}
    if loc:
        kw.update(lat=loc["lat"], lon=loc["lon"], location_label=loc["location_label"])
    undo = None   # destructive staged actions record an inverse so they're undoable
    if action_type in ("CREATE", "UPDATE"):
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="CREATE/UPDATE action is missing a title")
        basis = payload.get("_basis") or {}
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
            # Update the EXISTING note by id (keeps its title; no notes/ re-root).
            notes_svc.upsert_note(conn, title, payload.get("content") or "", note_id=basis["note_id"],
                                  kind=payload.get("kind"), **kw)
        else:
            # CREATE, or an UPDATE whose title didn't exist at propose time, makes a
            # NEW note (create-only so it can't clobber). A kb proposal lands under
            # kb/; a place note keeps the loc/ root; everything else under notes/.
            if payload.get("kind") == "place" or title.lower().startswith("loc/"):
                root = "loc"
            elif payload.get("kind") == "kb":
                root = "kb"
            else:
                root = "notes"
            title = notes_svc.root_title(title, root)
            notes_svc.upsert_note(conn, title, payload.get("content") or "", create_only=True,
                                  kind=payload.get("kind"), **kw)
    elif action_type == "LINK":
        source_title = (payload.get("source_title") or "").strip()
        target_title = (payload.get("target_title") or "").strip()
        if not source_title or not target_title:
            raise HTTPException(status_code=400, detail="LINK action needs source_title and target_title")
        source = notes_svc.get_by_title(conn, source_title)
        if source and f"[[{target_title}]]" not in source["content_md"]:
            new_content = source["content_md"].rstrip() + f"\n\n[[{target_title}]]\n"
            notes_svc.upsert_note(conn, source["title"], new_content, **kw)
    elif action_type == "RENAME":
        cur_title = (payload.get("title") or "").strip()
        new_title = (payload.get("new_title") or "").strip()
        if not cur_title or not new_title:
            raise HTTPException(status_code=400, detail="RENAME needs the current title and a new_title")
        note = notes_svc.get_by_title(conn, cur_title)
        if note is None:
            raise HTTPException(status_code=404, detail=f"No note titled '{cur_title}' to rename")
        # Keep the note under its proper root (notes/ vs kb/) regardless of how the
        # new title was phrased; rename in place (id-targeted) so inbound [[links]]
        # are rewritten and the kind is preserved.
        root = "kb" if note["kind"] == "kb" else "notes"
        new_title = notes_svc.root_title(new_title, root)
        try:
            notes_svc.upsert_note(conn, new_title, note["content_md"], note_id=note["id"], **kw)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="A note with that title already exists.")
    elif action_type == "LIST_REMOVE_ITEM":
        try:
            r = quicktasks.remove_item(conn, payload.get("list_title") or "", payload.get("item") or "",
                                       ordinal=payload.get("item_index"), source="architect",
                                       conversation_id=conversation_id, location=loc)
        except LookupError as e:
            raise HTTPException(status_code=409, detail=str(e))
        undo = {"op": "insert_line", "title": r["note_title"], "index": r["index"], "line": r["removed_line"]}
    elif action_type == "LIST_EDIT_ITEM":
        new_text = (payload.get("new_item") or "").strip()
        if not new_text:
            raise HTTPException(status_code=400, detail="LIST_EDIT_ITEM needs new_item")
        try:
            r = quicktasks.edit_item(conn, payload.get("list_title") or "", payload.get("item") or "",
                                     new_text, ordinal=payload.get("item_index"), source="architect",
                                     conversation_id=conversation_id, location=loc)
        except LookupError as e:
            raise HTTPException(status_code=409, detail=str(e))
        undo = {"op": "replace_line", "title": r["note_title"], "from": r["new_line"], "to": r["old_line"]}
    elif action_type == "DELETE_LIST":
        # Resolve by the title as given, then under lists/ — don't force-re-root or
        # require kind='list' (the user confirmed deleting this note).
        raw = (payload.get("list_title") or payload.get("title") or "").strip()
        note = notes_svc.get_by_title(conn, raw) or notes_svc.get_by_title(conn, notes_svc.root_title(raw, "lists"))
        if note is None:
            raise HTTPException(status_code=404, detail=f"No list titled '{raw}' to delete")
        notes_svc.soft_delete(conn, note["id"])
        undo = {"op": "restore_note", "note_id": note["id"]}
    elif action_type == "DELETE":
        title = (payload.get("title") or "").strip()
        note = notes_svc.get_by_title(conn, title)
        if note is None:
            raise HTTPException(status_code=404, detail=f"No note titled '{title}' to delete")
        notes_svc.soft_delete(conn, note["id"])
        undo = {"op": "restore_note", "note_id": note["id"]}
    elif action_type == "ADD_PLACE":
        name = (payload.get("name") or "").strip()[:80]
        if not name or payload.get("lat") is None or payload.get("lon") is None:
            raise HTTPException(status_code=400, detail="ADD_PLACE needs name, lat, lon")
        # Idempotent: if the owner already saved this name, applying is a no-op success.
        exists = conn.execute("SELECT 1 FROM places WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
        if not exists:
            cur = conn.execute(
                "INSERT INTO places (name, lat, lon, radius_m, note_slug) VALUES (?, ?, ?, ?, ?)",
                (name, payload["lat"], payload["lon"], max(20, min(int(payload.get("radius_m") or 150), 20000)),
                 payload.get("note_slug")),
            )
            pid = cur.lastrowid
            # Materialise the loc/<name> note so the saved place appears in the Wiki
            # "Places" tab — not only the Map panel. Undo removes geofence + note.
            slug = places_svc.ensure_note(conn, pid)
            undo = {"op": "delete_place", "id": pid, "note_slug": slug}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action type: {action_type}")

    # Make the applied (staged) destructive action undoable by stashing the inverse
    # on its row, so the chat ✓ chip's Undo can replay it.
    if undo and action_id is not None:
        conn.execute("UPDATE staging_actions SET payload_json = ? WHERE id = ?",
                     (json.dumps({**payload, "undo": undo}), action_id))
    # Record the approval in the conversation so it stays in the chat.
    if conversation_id is not None:
        ev = {"summary": _applied_summary(action_type, payload)}
        if undo and action_id is not None:
            ev["undo_id"] = action_id
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'event', ?)",
            (conversation_id, json.dumps(ev)),
        )


def _applied_summary(action_type: str, payload: dict) -> str:
    if action_type == "LINK":
        return f"Linked [[{payload.get('source_title', '')}]] → [[{payload.get('target_title', '')}]]"
    if action_type == "RENAME":
        return f"Renamed “{(payload.get('title') or '').strip()}” → [[{(payload.get('new_title') or '').strip()}]]"
    if action_type == "DELETE":
        return f"Deleted [[{(payload.get('title') or '').strip()}]]"
    if action_type == "ADD_PLACE":
        return f"Saved place “{(payload.get('name') or '').strip()}”"
    lt = notes_svc.root_title(payload.get("list_title") or "", "lists")
    if action_type == "DELETE_LIST":
        return f"Deleted list [[{lt}]]"
    if action_type == "LIST_REMOVE_ITEM":
        return f"Removed “{(payload.get('item') or '').strip()}” from [[{lt}]]"
    if action_type == "LIST_EDIT_ITEM":
        return f"Edited “{(payload.get('item') or '').strip()}” → “{(payload.get('new_item') or '').strip()}” in [[{lt}]]"
    verb = "Created" if action_type == "CREATE" else "Updated"
    return f"{verb} [[{(payload.get('title') or '').strip()}]]"


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
        _apply_action(conn, row["type"], json.loads(row["payload_json"]), row["conversation_id"], action_id=row["id"])
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
            _apply_action(conn, r["type"], json.loads(r["payload_json"]), r["conversation_id"], action_id=r["id"])
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
    elif op == "replace_line":
        if not quicktasks.replace_line_in_note(conn, undo["title"], undo["from"], undo["to"], source="user"):
            conn.rollback()
            raise HTTPException(status_code=409, detail="Nothing to undo — that line was already changed.")
    elif op == "insert_line":
        quicktasks.insert_line_in_note(conn, undo["title"], undo["index"], undo["line"], source="user")
    elif op == "set_tags":
        notes_svc.set_tags(conn, undo["note_id"], undo.get("tags", []))
    elif op == "revoke_share":
        conn.execute("UPDATE share_links SET status='revoked', revoked_at=datetime('now') WHERE token=?",
                     (undo.get("token"),))
    elif op == "reactivate_share":
        if undo.get("token"):
            conn.execute("UPDATE share_links SET status='active', revoked_at=NULL WHERE token=?", (undo["token"],))
        elif undo.get("title"):
            note = notes_svc.get_by_title(conn, undo["title"])
            if note:
                conn.execute("UPDATE share_links SET status='active', revoked_at=NULL WHERE note_id=?", (note["id"],))
    elif op == "restore_note":
        notes_svc.restore(conn, undo["note_id"])   # un-delete + rebuild links/index/embedding
    elif op == "delete_place":
        conn.execute("DELETE FROM places WHERE id = ?", (undo["id"],))
        conn.execute("DELETE FROM location_state WHERE place_id = ?", (undo["id"],))
        # Also retire the loc/<name> note the apply created, so undo fully reverses it.
        if undo.get("note_slug"):
            n = conn.execute("SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL",
                             (undo["note_slug"],)).fetchone()
            if n:
                notes_svc.soft_delete(conn, n["id"])
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
