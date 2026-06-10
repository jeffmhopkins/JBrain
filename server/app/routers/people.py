"""People registry — owner-only. People label/colour location trails and can be
linked to a KB page; they are NOT auth accounts (JBrain stays single access key).
"""
import secrets
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn, ensure_default_person, submit_write
from ..services import trips as trips_svc
from ..services import people as people_svc

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[CurrentUser])


def _reattribute(conn) -> None:
    """Re-resolve fix and trip attribution after a people/alias/name change.

    A name or alias edit can shift location fixes between people, so this
    rewinds trip detection so segments re-segment correctly. Silently
    swallows errors so it never blocks the people edit. People change rarely.

    Args:
        conn: Active DB connection (already has the change applied).
    """
    try:
        trips_svc.reattribute(conn)
        conn.commit()
    except Exception:  # noqa: BLE001 — never let it block the people edit
        pass


def _row(r) -> dict:
    """Convert a DB row to a dict, normalising ``is_default`` to a Python bool.

    Args:
        r: sqlite3 Row from the ``people`` table.

    Returns:
        Plain dict with ``is_default`` as a bool.
    """
    d = dict(r)
    d["is_default"] = bool(d["is_default"])
    return d


@router.get("")
def list_people():
    """Return all people, default person first, then alphabetically.

    Returns:
        List of person dicts from the ``people`` table.
    """
    ensure_default_person(get_conn())
    return [_row(r) for r in get_conn().execute("SELECT * FROM people ORDER BY is_default DESC, name").fetchall()]


# ── The owner ────────────────────────────────────────────────────────────────
# The brain's OWNER is the default person. Their name is the single source the
# prompts substitute for {owner} — every first-person note ("I", "my") is attributed
# to it, and it titles their kb/People/<name> page. Surfaced as its own setting (and
# a first-run onboarding step) so it's a clear, first-class identity rather than a
# buried People-list edit.
_OWNER_PLACEHOLDER = {"", "me", "the owner"}


def _owner_state(conn) -> dict:
    """Build the owner status dict used by GET and PUT /owner.

    Args:
        conn: Active DB connection.

    Returns:
        Dict with ``id``, ``name``, ``display``, ``aliases``, and ``is_set``.
    """
    o = people_svc.owner(conn)
    raw = (o["name"] if o else "").strip()
    return {
        "id": o["id"] if o else None,
        "name": raw,                              # the stored name ("Me" until set)
        "display": people_svc.owner_name(conn),   # what the prompts use ("the owner" if unset)
        "aliases": (o["aliases"] if o else "") or "",   # CSV of also-known-as names
        "is_set": raw.lower() not in _OWNER_PLACEHOLDER,
    }


class OwnerIn(BaseModel):
    """Input schema for naming/renaming the brain owner."""

    name: str
    aliases: str | None = None     # optional CSV of also-known-as names (e.g. "Jeff, JMH")


@router.get("/owner")
def get_owner():
    """Return the current owner identity: name, display label, aliases, and set status.

    Returns:
        Owner state dict with ``id``, ``name``, ``display``, ``aliases``, and
        ``is_set``.
    """
    conn = get_conn()
    ensure_default_person(conn)
    return _owner_state(conn)


@router.put("/owner")
def set_owner(body: OwnerIn):
    """Name or rename the brain owner (the default person).

    This is what makes the wiki attribute first-person notes to this person
    and stops minting a generic 'Owner' KB page.

    Args:
        body: New ``name`` (required) and optional ``aliases`` CSV string.

    Returns:
        Updated owner state dict.

    Raises:
        HTTPException: 422 if name is empty.
        HTTPException: 409 if a location key is active (revoke first) or
            another person with that name already exists.
    """
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(status_code=422, detail="Owner name required")

    def _write() -> dict:
        """Validate and apply the owner rename on the writer connection, returning the new state."""
        c = get_conn()
        ensure_default_person(c)
        o = people_svc.owner(c)
        if o["location_key"]:
            raise HTTPException(status_code=409, detail="Revoke this person's location key before renaming.")
        if name.lower() != o["name"].lower() and c.execute(
                "SELECT 1 FROM people WHERE name = ? COLLATE NOCASE AND id <> ?", (name, o["id"])).fetchone():
            raise HTTPException(status_code=409, detail=f"A person named “{name}” already exists.")
        c.execute("UPDATE people SET name = ? WHERE id = ?", (name, o["id"]))
        if body.aliases is not None:
            c.execute("UPDATE people SET aliases = ? WHERE id = ?", (body.aliases.strip(), o["id"]))
        c.commit()
        return _owner_state(c)

    state = submit_write(_write).result()
    conn = get_conn()
    # Connect the owner to their kb/People page and register their name/nicknames as durable
    # alias decisions so prose using a nickname links to the canonical page (next rebuild
    # materializes them). Best-effort — never block the rename.
    try:
        from ..services import wiki_build
        wiki_build.reconcile_owner(conn)
    except Exception:  # noqa: BLE001
        pass
    _reattribute(conn)
    return state


class PersonIn(BaseModel):
    """Input schema for creating a new person."""

    name: str
    color: str = "#7f9aa6"
    aliases: str = ""
    note_slug: str | None = None
    is_default: bool = False


@router.post("")
def add_person(body: PersonIn):
    """Create a new person in the registry.

    Args:
        body: Person details: name, color, aliases, optional note_slug, and
            is_default flag.

    Returns:
        JSON ``{"id": int, "name": str}`` for the created person.

    Raises:
        HTTPException: 422 if name is empty.
        HTTPException: 409 if a person with that name already exists.
    """
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(status_code=422, detail="Name required")

    def _write() -> int:
        """Insert the new person atomically (uniqueness check + insert) on the writer connection."""
        c = get_conn()
        if c.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            raise HTTPException(status_code=409, detail=f"A person named “{name}” already exists.")
        try:
            cur = c.execute(
                "INSERT INTO people (name, color, aliases, note_slug, is_default) VALUES (?, ?, ?, ?, 0)",
                (name, body.color.strip()[:9], body.aliases.strip(), body.note_slug),
            )
            if body.is_default:
                _make_default(c, cur.lastrowid)
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            raise HTTPException(status_code=409, detail="That person already exists.")
        return cur.lastrowid

    new_id = submit_write(_write).result()
    _reattribute(get_conn())
    return {"id": new_id, "name": name}


class PersonPatch(BaseModel):
    """Input schema for partial updates to a person record."""

    name: str | None = None
    color: str | None = None
    aliases: str | None = None
    note_slug: str | None = None
    is_default: bool | None = None


@router.patch("/{person_id}")
def update_person(person_id: int, body: PersonPatch):
    """Partially update a person's name, color, aliases, note link, or default flag.

    Args:
        person_id: Primary key of the person to update.
        body: Fields to change; absent fields are left unchanged.

    Returns:
        JSON ``{"ok": true}`` on success.

    Raises:
        HTTPException: 404 if person does not exist.
        HTTPException: 422 if the supplied name is empty.
        HTTPException: 409 if a location key is active and name/aliases would
            change, or another person with the new name already exists.
    """
    def _write() -> None:
        """Validate and apply the partial person update atomically on the writer connection."""
        c = get_conn()
        p = c.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None:
            raise HTTPException(status_code=404, detail="No such person")
        # Name + aliases are baked into the live setup code and drive fix attribution.
        # Locked while a key is active so already-distributed codes can't desync — revoke first.
        if p["location_key"]:
            if body.name is not None and body.name.strip()[:40] != p["name"]:
                raise HTTPException(status_code=409, detail="Revoke the location key before renaming this person.")
            if body.aliases is not None and body.aliases.strip() != (p["aliases"] or ""):
                raise HTTPException(status_code=409, detail="Revoke the location key before changing ingestion sources.")
        name = body.name.strip()[:40] if body.name is not None else None
        if body.name is not None and not name:
            raise HTTPException(status_code=422, detail="Name required")
        if name and name.lower() != p["name"].lower() and \
                c.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE AND id <> ?", (name, person_id)).fetchone():
            raise HTTPException(status_code=409, detail=f"A person named “{name}” already exists.")
        try:
            if name:
                c.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
            if body.color is not None:
                c.execute("UPDATE people SET color = ? WHERE id = ?", (body.color.strip()[:9], person_id))
            if body.aliases is not None:
                c.execute("UPDATE people SET aliases = ? WHERE id = ?", (body.aliases.strip(), person_id))
            if body.note_slug is not None:
                c.execute("UPDATE people SET note_slug = ? WHERE id = ?", (body.note_slug or None, person_id))
            if body.is_default:
                _make_default(c, person_id)
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            raise HTTPException(status_code=409, detail="That name is taken.")

    submit_write(_write).result()
    _reattribute(get_conn())
    return {"ok": True}


@router.delete("/{person_id}")
def delete_person(person_id: int):
    """Delete a non-default person from the registry.

    Args:
        person_id: Primary key of the person to delete.

    Returns:
        JSON ``{"ok": true}`` on success.

    Raises:
        HTTPException: 404 if person does not exist.
        HTTPException: 409 if attempting to delete the default person.
    """
    def _write() -> None:
        """Validate and delete the (non-default) person atomically on the writer connection."""
        c = get_conn()
        p = c.execute("SELECT is_default FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None:
            raise HTTPException(status_code=404, detail="No such person")
        if p["is_default"]:
            raise HTTPException(status_code=409, detail="Can't delete the default person — set another as default first.")
        c.execute("DELETE FROM people WHERE id = ?", (person_id,))
        c.commit()

    submit_write(_write).result()
    _reattribute(get_conn())
    return {"ok": True}


class TagNoteIn(BaseModel):
    """Input schema for tagging a KB note as a person."""

    slug: str


@router.post("/from-note")
def person_from_note(body: TagNoteIn):
    """Tag a KB note as a person, creating or adopting a person named after the note.

    Idempotent — re-tagging an already-linked note just refreshes the link.

    Args:
        body: ``{"slug": str}`` — slug of the KB note to tag.

    Returns:
        JSON ``{"id": int, "name": str}`` of the created or updated person.

    Raises:
        HTTPException: 404 if the note does not exist.
        HTTPException: 409 if the person record cannot be created or linked.
    """
    def _write() -> tuple[int, str]:
        """Resolve the note, then create or relink the person atomically on the writer connection."""
        c = get_conn()
        note = c.execute(
            "SELECT id, title, slug FROM notes WHERE slug = ? AND deleted_at IS NULL", (body.slug,)
        ).fetchone()
        if note is None:
            raise HTTPException(status_code=404, detail="No such note")
        # Person name = the note's leaf title (kb/People/Family/Mom → "Mom").
        name = note["title"].split("/")[-1].strip()[:40] or note["title"]
        existing = c.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        try:
            if existing:
                c.execute("UPDATE people SET note_slug = ? WHERE id = ?", (note["slug"], existing["id"]))
                pid = existing["id"]
            else:
                pid = c.execute(
                    "INSERT INTO people (name, note_slug) VALUES (?, ?)", (name, note["slug"])
                ).lastrowid
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            raise HTTPException(status_code=409, detail="Couldn't tag this note as a person.")
        return pid, name

    pid, name = submit_write(_write).result()
    _reattribute(get_conn())
    return {"id": pid, "name": name}


@router.post("/{person_id}/location-key")
def generate_location_key(person_id: int):
    """Mint or rotate a scoped location key for a person.

    The key can only POST this person's location fixes; it cannot read the
    trail or reach any other endpoint. Every fix it sends is attributed to
    the person. Paste it into the tracker app's Access key field.

    Args:
        person_id: Primary key of the person to generate a key for.

    Returns:
        JSON ``{"location_key": str}`` containing the new key.

    Raises:
        HTTPException: 404 if the person does not exist.
    """
    key = "jbloc_" + secrets.token_urlsafe(24)

    def _write() -> None:
        """Verify the person exists, then store the new location key on the writer connection."""
        c = get_conn()
        if c.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such person")
        c.execute("UPDATE people SET location_key = ? WHERE id = ?", (key, person_id))
        c.commit()

    submit_write(_write).result()
    return {"location_key": key}


@router.delete("/{person_id}/location-key")
def revoke_location_key(person_id: int):
    """Revoke a person's location key, disabling further location ingestion for them.

    Args:
        person_id: Primary key of the person whose key should be revoked.

    Returns:
        JSON ``{"ok": true}``.
    """
    def _write() -> None:
        """Clear the person's location key on the writer connection."""
        c = get_conn()
        c.execute("UPDATE people SET location_key = NULL WHERE id = ?", (person_id,))
        c.commit()

    submit_write(_write).result()
    return {"ok": True}


def _make_default(conn, person_id: int) -> None:
    """Promote a person to the default (owner) slot, demoting the current default.

    Args:
        conn: Active DB connection.
        person_id: Primary key of the person to make default.
    """
    conn.execute("UPDATE people SET is_default = 0 WHERE is_default = 1")
    conn.execute("UPDATE people SET is_default = 1 WHERE id = ?", (person_id,))
