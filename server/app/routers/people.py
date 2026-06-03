"""People registry — owner-only. People label/colour location trails and can be
linked to a KB page; they are NOT auth accounts (JBrain stays single access key)."""
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn, ensure_default_person

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[CurrentUser])


def _row(r) -> dict:
    d = dict(r)
    d["is_default"] = bool(d["is_default"])
    return d


@router.get("")
def list_people():
    ensure_default_person(get_conn())
    return [_row(r) for r in get_conn().execute("SELECT * FROM people ORDER BY is_default DESC, name").fetchall()]


class PersonIn(BaseModel):
    name: str
    color: str = "#7f9aa6"
    aliases: str = ""
    note_slug: str | None = None
    is_default: bool = False


@router.post("")
def add_person(body: PersonIn):
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise HTTPException(status_code=409, detail=f"A person named “{name}” already exists.")
    try:
        cur = conn.execute(
            "INSERT INTO people (name, color, aliases, note_slug, is_default) VALUES (?, ?, ?, ?, 0)",
            (name, body.color.strip()[:9], body.aliases.strip(), body.note_slug),
        )
        if body.is_default:
            _make_default(conn, cur.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="That person already exists.")
    return {"id": cur.lastrowid, "name": name}


class PersonPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    aliases: str | None = None
    note_slug: str | None = None
    is_default: bool | None = None


@router.patch("/{person_id}")
def update_person(person_id: int, body: PersonPatch):
    conn = get_conn()
    p = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if p is None:
        raise HTTPException(status_code=404, detail="No such person")
    name = body.name.strip()[:40] if body.name is not None else None
    if body.name is not None and not name:
        raise HTTPException(status_code=422, detail="Name required")
    if name and name.lower() != p["name"].lower() and \
            conn.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE AND id <> ?", (name, person_id)).fetchone():
        raise HTTPException(status_code=409, detail=f"A person named “{name}” already exists.")
    try:
        if name:
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
        if body.color is not None:
            conn.execute("UPDATE people SET color = ? WHERE id = ?", (body.color.strip()[:9], person_id))
        if body.aliases is not None:
            conn.execute("UPDATE people SET aliases = ? WHERE id = ?", (body.aliases.strip(), person_id))
        if body.note_slug is not None:
            conn.execute("UPDATE people SET note_slug = ? WHERE id = ?", (body.note_slug or None, person_id))
        if body.is_default:
            _make_default(conn, person_id)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="That name is taken.")
    return {"ok": True}


@router.delete("/{person_id}")
def delete_person(person_id: int):
    conn = get_conn()
    p = conn.execute("SELECT is_default FROM people WHERE id = ?", (person_id,)).fetchone()
    if p is None:
        raise HTTPException(status_code=404, detail="No such person")
    if p["is_default"]:
        raise HTTPException(status_code=409, detail="Can't delete the default person — set another as default first.")
    conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    conn.commit()
    return {"ok": True}


class TagNoteIn(BaseModel):
    slug: str


@router.post("/from-note")
def person_from_note(body: TagNoteIn):
    """Tag a KB note AS a person: create (or adopt) a person named after the note and
    link the note as their page. Idempotent — re-tagging just re-links."""
    conn = get_conn()
    note = conn.execute(
        "SELECT id, title, slug FROM notes WHERE slug = ? AND deleted_at IS NULL", (body.slug,)
    ).fetchone()
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")
    # Person name = the note's leaf title (kb/People/Family/Mom → "Mom").
    name = note["title"].split("/")[-1].strip()[:40] or note["title"]
    existing = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    try:
        if existing:
            conn.execute("UPDATE people SET note_slug = ? WHERE id = ?", (note["slug"], existing["id"]))
            pid = existing["id"]
        else:
            pid = conn.execute(
                "INSERT INTO people (name, note_slug) VALUES (?, ?)", (name, note["slug"])
            ).lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Couldn't tag this note as a person.")
    return {"id": pid, "name": name}


def _make_default(conn, person_id: int) -> None:
    conn.execute("UPDATE people SET is_default = 0 WHERE is_default = 1")
    conn.execute("UPDATE people SET is_default = 1 WHERE id = ?", (person_id,))
