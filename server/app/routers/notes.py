"""Notes REST API: list, read, create/update, delete, backlinks, history."""
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..db import get_conn
from ..services import clock, diffing
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/notes", tags=["notes"], dependencies=[CurrentUser])


class NoteIn(BaseModel):
    title: str
    content_md: str = ""


class EntryIn(BaseModel):
    text: str
    title: str | None = None
    # Provenance for the version badge. The watch relays dictations through the phone,
    # which tags them `watch` so the note's history shows where it came from. Anything
    # unrecognised is clamped to `user` in the handler (capture must never 422 away a
    # note over a bad label).
    source: str | None = None
    # Bounded so a stray reading (incl. NaN/inf, which fail the bounds) can't be
    # stored and break downstream distance math / JSON serialisation.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


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


@router.get("/located")
def located_notes(since: str | None = None, until: str | None = None, limit: int = 2000):
    """Notes that carry a capture coordinate — drives the Map's note pins. Declared
    BEFORE /{slug} so 'located' isn't swallowed as a slug. Owner-only (bearer)."""
    conn = get_conn()
    sql = ("SELECT slug, title, lat, lon, location_label, kind, created_at FROM notes "
           "WHERE deleted_at IS NULL AND lat IS NOT NULL AND lon IS NOT NULL")
    params: list = []
    s = since.strip().replace("T", " ").replace("Z", "") if since else None
    u = until.strip().replace("T", " ").replace("Z", "") if until else None
    if s:
        sql += " AND created_at >= ?"; params.append(s)
    if u:
        sql += " AND created_at <= ?"; params.append(u)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


@router.get("/{slug}/analysis")
def note_analysis(slug: str):
    """The read-only AI analysis sidecar for a note (gist, salient facts, entities,
    domain). {} when none has been computed yet. Never mutates the note."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    from ..services import note_analysis as na
    return na.get(conn, row["id"]) or {}


class TalkIn(BaseModel):
    kind: str = "note"
    body: str


def _note_title(conn, slug: str) -> str:
    row = conn.execute("SELECT title FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row["title"]


@router.get("/{slug}/talk")
def get_talk(slug: str):
    """The article's 'talk' entries (decisions/conflicts/questions/directives) — the
    maintenance memory beside the article."""
    conn = get_conn()
    from ..services import article_talk
    return article_talk.list_for(conn, _note_title(conn, slug))


@router.post("/{slug}/talk")
def add_talk(slug: str, body: TalkIn):
    """Add an owner note/directive/question to an article's talk. (There is intentionally
    no user 'resolve' — open items are addressed through the Review flow / maintenance pass
    when the underlying issue is actually handled, not by ticking a box.)"""
    conn = get_conn()
    from ..services import article_talk
    tid = article_talk.add(conn, _note_title(conn, slug), body.kind, body.body, author="user")
    conn.commit()
    return {"id": tid}


@router.get("/kb/dead-links")
def kb_dead_links():
    """KB health: dangling cross-links from articles (target doesn't exist)."""
    from ..services import wiki_build
    items = wiki_build.dead_links(get_conn())
    return {"count": len(items), "items": items}


@router.post("")
def create_or_update(body: NoteIn):
    conn = get_conn()
    try:
        note_id = notes_svc.upsert_note(conn, body.title, body.content_md, fire_events=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    notes_svc.flush_entry_events(conn)  # fire entry_created AFTER commit
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@router.put("/{slug}")
def update_note(slug: str, body: NoteIn):
    """Edit an existing note in place, including RENAMING it. Targets the note by
    id so a new title renames THIS note (and its slug) rather than creating a
    duplicate. Use it to move notes under the notes/ or kb/ roots."""
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    try:
        note_id = notes_svc.upsert_note(
            conn, new_title, body.content_md, note_id=note["id"], source="user",
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="A note with that title already exists.")
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


class TagsIn(BaseModel):
    tags: list[str] = []


@router.put("/{slug}/tags")
def set_note_tags(slug: str, body: TagsIn):
    """Replace a note's tags directly (the owner editing their own note). The AI path
    stages a tag change for approval; the owner editing in the UI is a direct edit."""
    conn = get_conn()
    note = _note_by_slug(conn, slug)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")
    tags = notes_svc.set_tags(conn, note["id"], body.tags)
    conn.commit()
    return {"tags": tags}


@router.post("/entry")
def create_entry(body: EntryIn):
    """'Make entry' mode: store text directly as a NEW note (unique title), no LLM.
    Fires the entry_created hooks (auto-tag, etc.)."""
    conn = get_conn()
    text = body.text.strip()
    explicit = (body.title or "").strip()
    if not text and not explicit:
        raise HTTPException(status_code=422, detail="Entry text cannot be empty")
    # Provenance: `watch` for relayed wrist dictations, else a plain human `user`
    # entry. Clamp anything unknown so the version badge stays a known value and the
    # entry_created enrichment still fires (see upsert_note's human-source gate).
    source = (body.source or "user").strip().lower()
    if source not in ("user", "watch"):
        source = "user"
    if explicit:
        # An explicit title (the assisted-attachment path) keeps its own name —
        # "assisted notes can go somewhere else". The first line is NOT a title.
        title = notes_svc._unique_title(conn, notes_svc.root_title(explicit, "notes"))
    else:
        # Pure Entry capture: no title. File chronologically under the standard flat
        # dated tree as notes/YYYY/MM/DD/NN; the whole text is the body. Day boundary
        # is the app timezone (same TZ the scheduler uses for midnight).
        title = notes_svc.next_dated_title(conn, clock.today_local())
    try:
        note_id = notes_svc.upsert_note(
            conn, title, text, source=source, lat=body.lat, lon=body.lon, fire_events=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()  # don't leave a half-written note on the pooled connection
        raise
    # Fire entry_created AFTER commit so an (optional, LLM-backed) auto-tag
    # workflow doesn't hold the note's write lock or freeze the "no-LLM" Send.
    notes_svc.flush_entry_events(conn)
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

    try:
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
    except Exception:
        conn.rollback()
        raise
    out = conn.execute(
        "SELECT id, title, slug FROM notes WHERE id = ?", (note["id"],)
    ).fetchone()
    return dict(out)
