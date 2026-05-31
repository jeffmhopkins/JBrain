"""Notes REST API: list, read, create/update, delete, backlinks, history."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import diffing
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/notes", tags=["notes"], dependencies=[CurrentUser])


class NoteIn(BaseModel):
    title: str
    content_md: str = ""


class EntryIn(BaseModel):
    text: str
    title: str | None = None
    lat: float | None = None
    lon: float | None = None


def _unique_title(conn, base: str) -> str:
    base = base.strip()[:80] or "Entry"
    title, i = base, 2
    while conn.execute("SELECT 1 FROM notes WHERE lower(title)=lower(?)", (title,)).fetchone():
        title = f"{base} ({i})"
        i += 1
    return title


class RestoreIn(BaseModel):
    version_id: int
    note: str | None = None


def _note_by_slug(conn, slug: str, include_deleted: bool = False):
    sql = "SELECT * FROM notes WHERE slug = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    row = conn.execute(sql, (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row


@router.get("")
def list_notes(q: str | None = None, kind: str | None = None, limit: int = 200):
    conn = get_conn()
    clauses = ["deleted_at IS NULL"]
    params: list = []
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    params.append(limit)
    rows = conn.execute(
        "SELECT id, title, slug, kind, updated_at FROM notes "
        f"WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
        params,
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


@router.post("/entry")
def create_entry(body: EntryIn):
    """'Make entry' mode: store text directly as a NEW note (unique title), no LLM.
    Fires the entry_created hooks (auto-tag, etc.)."""
    conn = get_conn()
    text = body.text.strip()
    base = body.title or next((ln.strip() for ln in text.splitlines() if ln.strip()), "") \
        or f"Entry {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    title = _unique_title(conn, base)
    note_id = notes_svc.upsert_note(
        conn, title, text, source="user", lat=body.lat, lon=body.lon,
    )
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
    """Timeline of authored states, newest first. The newest is the current one."""
    conn = get_conn()
    row = _note_by_slug(conn, slug, include_deleted=True)
    rows = conn.execute(
        "SELECT id, title, source, conversation_id, note, created_at, "
        "length(content_md) AS size FROM note_versions "
        "WHERE note_id = ? ORDER BY created_at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    out = [dict(r) for r in rows]
    for i, v in enumerate(out):
        v["version_id"] = v.pop("id")
        v["is_current"] = i == 0
    return out


@router.get("/{slug}/versions/{version_id}")
def get_version(slug: str, version_id: int):
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    v = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
        (version_id, note["id"]),
    ).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")
    return dict(v)


@router.get("/{slug}/diff/{from_id}/{to_id}")
def diff_versions(slug: str, from_id: int, to_id: int):
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)

    def _ver(vid: int):
        v = conn.execute(
            "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
            (vid, note["id"]),
        ).fetchone()
        if not v:
            raise HTTPException(status_code=404, detail=f"Version {vid} not found")
        return v

    a, b = _ver(from_id), _ver(to_id)
    return {
        "from": {"version_id": a["id"], "created_at": a["created_at"], "title": a["title"]},
        "to": {"version_id": b["id"], "created_at": b["created_at"], "title": b["title"]},
        "title_changed": a["title"] != b["title"],
        "hunks": diffing.line_diff(a["content_md"], b["content_md"]),
    }


@router.post("/{slug}/restore")
def restore(slug: str, body: RestoreIn):
    """Restore an old version. Snapshots current first (history is never lost)."""
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    v = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
        (body.version_id, note["id"]),
    ).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")

    notes_svc.upsert_note(
        conn,
        v["title"],
        v["content_md"],
        note_id=note["id"],
        source="restore",
        version_note=body.note or f"restored from version {body.version_id}",
    )
    # Restoring resurrects a soft-deleted note (upsert re-indexed it).
    conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note["id"],))
    conn.commit()
    out = conn.execute(
        "SELECT id, title, slug FROM notes WHERE id = ?", (note["id"],)
    ).fetchone()
    return dict(out)
