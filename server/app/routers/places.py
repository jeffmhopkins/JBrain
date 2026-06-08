"""Named geofences ("places") for the location tools + triggers. Owner-only."""
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc
from ..services import places as places_svc

router = APIRouter(prefix="/api/places", tags=["places"], dependencies=[CurrentUser])


def _loc_title(name: str) -> str:
    """Build the canonical loc/<name> note title for a place.

    Args:
        name: Place name (stripped of surrounding slashes).

    Returns:
        Title string of the form 'loc/<name>'.
    """
    return f"loc/{name.strip().strip('/')}"


def _note_by_slug(conn, slug: str):
    """Fetch a live note row by slug, returning None if not found or slug is empty.

    Args:
        conn: Active database connection.
        slug: Note slug to look up.

    Returns:
        Row with id, slug, and content_md, or None.
    """
    if not slug:
        return None
    return conn.execute(
        "SELECT id, slug, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()


class PlaceIn(BaseModel):
    """Input body for creating a new named geofence."""

    name: str
    lat: float
    lon: float
    radius_m: int = 150
    note_slug: str | None = None


@router.get("")
def list_places():
    """List all named geofences, ordered by name.

    Returns:
        List of place dicts (id, name, lat, lon, radius_m, note_slug).
    """
    return [dict(r) for r in get_conn().execute(
        "SELECT id, name, lat, lon, radius_m, note_slug FROM places ORDER BY name").fetchall()]


@router.post("")
def add_place(body: PlaceIn):
    """Create a new named geofence and its backing loc/<name> note.

    Names are unique (case-insensitive) so a place and its note stay paired.

    Args:
        body: Place name, coordinates, radius, and optional note slug.

    Returns:
        Dict with 'id' and 'name' of the new place.

    Raises:
        HTTPException: 409 if a place with the same name (case-insensitive) already exists.
        HTTPException: 422 if the name is blank.
    """
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    conn = get_conn()
    # Names are the place's identity (they map 1:1 to a loc/<name> note), so keep them
    # unique (case-insensitive) — otherwise two places fight over one note.
    if conn.execute("SELECT 1 FROM places WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise HTTPException(status_code=409, detail=f"A place named “{name}” already exists.")
    cur = conn.execute(
        "INSERT INTO places (name, lat, lon, radius_m, note_slug) VALUES (?, ?, ?, ?, ?)",
        (name, body.lat, body.lon, max(20, min(int(body.radius_m), 20000)), body.note_slug),
    )
    # Back every place with its loc/<name> note so it shows in the Wiki "Places" tab.
    try:
        places_svc.ensure_note(conn, cur.lastrowid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": cur.lastrowid, "name": name}


class PlacePatch(BaseModel):
    """Input body for a partial update to a place (rename and/or resize)."""

    name: str | None = None
    radius_m: int | None = None


@router.patch("/{place_id}")
def update_place(place_id: int, body: PlacePatch):
    """Edit a place: rename it and/or resize its geofence radius. Either field is optional.

    A rename also updates the linked loc/<name> note's title to keep them in sync.

    Args:
        place_id: ID of the place to update.
        body: Optional new name and/or radius_m.

    Returns:
        Dict with 'ok' and the effective 'name' after the update.

    Raises:
        HTTPException: 404 if the place is not found.
        HTTPException: 409 if renaming would conflict with another existing place or note.
        HTTPException: 422 if an explicit blank name is provided.
    """
    conn = get_conn()
    place = conn.execute("SELECT name, note_slug, radius_m FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        raise HTTPException(status_code=404, detail="No such place")
    name = body.name.strip()[:80] if body.name is not None else None
    if body.name is not None and not name:
        raise HTTPException(status_code=422, detail="Name required")
    renaming = name is not None and name.lower() != place["name"].lower()
    if renaming and conn.execute("SELECT 1 FROM places WHERE name = ? COLLATE NOCASE AND id <> ?",
                                 (name, place_id)).fetchone():
        raise HTTPException(status_code=409, detail=f"A place named “{name}” already exists.")
    try:
        if renaming:
            conn.execute("UPDATE places SET name = ? WHERE id = ?", (name, place_id))
            # Keep the linked loc/ note's title in sync so the place and its page stay paired.
            if place["note_slug"]:
                note = _note_by_slug(conn, place["note_slug"])
                if note is not None:
                    notes_svc.upsert_note(conn, _loc_title(name), note["content_md"],
                                          note_id=note["id"], source="user", kind="place")
                    new = conn.execute("SELECT slug FROM notes WHERE id = ?", (note["id"],)).fetchone()
                    conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (new["slug"], place_id))
        if body.radius_m is not None:
            radius = max(20, min(int(body.radius_m), 20000))
            conn.execute("UPDATE places SET radius_m = ? WHERE id = ?", (radius, place_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="A place note with that name already exists.")
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "name": name or place["name"]}


@router.post("/{place_id}/note")
def ensure_place_note(place_id: int):
    """Lazily create (or find) the loc/<name> note that backs this place and link it.

    The place page is that note plus a geofence card; bare geofences carry no note
    until content is added here.

    Args:
        place_id: ID of the place to back with a note.

    Returns:
        Dict with 'slug' of the backing note to navigate to.

    Raises:
        HTTPException: 404 if the place is not found.
        HTTPException: 409 if the place note could not be created or adopted.
    """
    conn = get_conn()
    place = conn.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        raise HTTPException(status_code=404, detail="No such place")
    try:
        slug = places_svc.ensure_note(conn, place_id)
        conn.commit()
    except sqlite3.IntegrityError:
        # Lost a create race — the note exists now; adopt it (idempotent "ensure").
        conn.rollback()
        existing = notes_svc.get_by_title(conn, _loc_title(place["name"]))
        if existing is None:
            raise HTTPException(status_code=409, detail="Couldn't create the place note.")
        conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (existing["slug"], place_id))
        conn.commit()
        return {"slug": existing["slug"]}
    except Exception:
        conn.rollback()
        raise
    return {"slug": slug}


@router.delete("/{place_id}")
def delete_place(place_id: int):
    """Delete a place and remove any associated location state.

    Args:
        place_id: ID of the place to delete.

    Returns:
        Dict with key 'ok' set to True.
    """
    conn = get_conn()
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    conn.execute("DELETE FROM location_state WHERE place_id = ?", (place_id,))
    conn.commit()
    return {"ok": True}
