"""Place (geofence) ↔ loc/<name> note pairing.

A place is a named geofence row in `places`; its page is a `loc/<name>` note
(kind='place'). `ensure_note` materialises that note — creating it, restoring a
soft-deleted tombstone, or adopting an existing one — and links it back to the
place, so every saved place shows up in the Wiki "Places" tab, not just the Map
panel. Callers own the transaction: this never commits and raises on error so the
caller's rollback unwinds cleanly. Returns the note slug (or None if no such place).
"""
from . import notes as notes_svc


def _loc_title(name: str) -> str:
    return f"loc/{name.strip().strip('/')}"


def _note_by_slug(conn, slug: str):
    if not slug:
        return None
    return conn.execute(
        "SELECT id, slug, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()


def ensure_note(conn, place_id: int) -> str | None:
    """Create/find/restore the loc/<name> note backing this place and link it back."""
    place = conn.execute("SELECT name, note_slug FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        return None
    title = _loc_title(place["name"])
    note = (_note_by_slug(conn, place["note_slug"]) if place["note_slug"] else None) \
        or notes_svc.get_by_title(conn, title)
    if note is None:
        # A soft-deleted note still owns the (UNIQUE) title; restore it instead of
        # colliding — re-saving a place brings its prior content back.
        tomb = conn.execute(
            "SELECT id, slug FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NOT NULL",
            (title,),
        ).fetchone()
        if tomb is not None:
            notes_svc.restore(conn, tomb["id"])
            note = tomb
        else:
            nid = notes_svc.upsert_note(conn, title, f"# {place['name']}\n\n", source="user",
                                        kind="place", fire_events=False)
            note = conn.execute("SELECT slug FROM notes WHERE id = ?", (nid,)).fetchone()
    conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (note["slug"], place_id))
    return note["slug"]
