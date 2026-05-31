"""Notes REST API: list, read, create/update, delete, backlinks, versions."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/notes", tags=["notes"], dependencies=[CurrentUser])


class NoteIn(BaseModel):
    title: str
    content_md: str = ""


@router.get("")
def list_notes(q: str | None = None, limit: int = 200):
    conn = get_conn()
    if q:
        rows = conn.execute(
            "SELECT id, title, slug, updated_at FROM notes "
            "WHERE deleted_at IS NULL AND title LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, slug, updated_at FROM notes "
            "WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{slug}")
def get_note(slug: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    note = dict(row)
    note["backlinks"] = notes_svc.backlinks(conn, row["id"])
    note["tags"] = [
        t["name"]
        for t in conn.execute(
            "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = ? ORDER BY t.name",
            (row["id"],),
        ).fetchall()
    ]
    return note


@router.post("")
def create_or_update(body: NoteIn):
    conn = get_conn()
    note_id = notes_svc.upsert_note(conn, body.title, body.content_md)
    conn.commit()
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@router.delete("/{slug}")
def delete_note(slug: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    notes_svc.soft_delete(conn, row["id"])
    conn.commit()
    return {"ok": True}


@router.get("/{slug}/versions")
def versions(slug: str):
    conn = get_conn()
    row = conn.execute("SELECT id FROM notes WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    rows = conn.execute(
        "SELECT id, title, content_md, created_at FROM note_versions "
        "WHERE note_id = ? ORDER BY created_at DESC",
        (row["id"],),
    ).fetchall()
    return [dict(r) for r in rows]
