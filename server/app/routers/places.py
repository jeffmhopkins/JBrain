"""Named geofences ("places") for the location tools + triggers. Owner-only."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/places", tags=["places"], dependencies=[CurrentUser])


def _loc_title(name: str) -> str:
    return f"loc/{name.strip().strip('/')}"


def _note_by_slug(conn, slug: str):
    if not slug:
        return None
    return conn.execute(
        "SELECT id, slug, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()


class PlaceIn(BaseModel):
    name: str
    lat: float
    lon: float
    radius_m: int = 150
    note_slug: str | None = None


@router.get("")
def list_places():
    return [dict(r) for r in get_conn().execute(
        "SELECT id, name, lat, lon, radius_m, note_slug FROM places ORDER BY name").fetchall()]


@router.post("")
def add_place(body: PlaceIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO places (name, lat, lon, radius_m, note_slug) VALUES (?, ?, ?, ?, ?)",
        (name[:80], body.lat, body.lon, max(20, min(int(body.radius_m), 20000)), body.note_slug),
    )
    conn.commit()
    return {"id": cur.lastrowid, "name": name}


class PlacePatch(BaseModel):
    name: str


@router.patch("/{place_id}")
def rename_place(place_id: int, body: PlacePatch):
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    conn = get_conn()
    place = conn.execute("SELECT name, note_slug FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        raise HTTPException(status_code=404, detail="No such place")
    conn.execute("UPDATE places SET name = ? WHERE id = ?", (name, place_id))
    # Keep the linked loc/ note's title in sync so the place and its page stay paired.
    if place["note_slug"]:
        note = _note_by_slug(conn, place["note_slug"])
        if note is not None:
            notes_svc.upsert_note(conn, _loc_title(name), note["content_md"],
                                  note_id=note["id"], source="user", kind="place")
            new = conn.execute("SELECT slug FROM notes WHERE id = ?", (note["id"],)).fetchone()
            conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (new["slug"], place_id))
    conn.commit()
    return {"ok": True, "name": name}


@router.post("/{place_id}/note")
def ensure_place_note(place_id: int):
    """Lazily create (or find) the loc/<name> note that backs this place and link it.
    The place page is that note + a geofence card; bare geofences carry no note until
    you add content here. Returns the note slug to navigate to."""
    conn = get_conn()
    place = conn.execute("SELECT name, note_slug FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        raise HTTPException(status_code=404, detail="No such place")
    title = _loc_title(place["name"])
    note = (_note_by_slug(conn, place["note_slug"]) if place["note_slug"] else None) \
        or notes_svc.get_by_title(conn, title)
    if note is None:
        nid = notes_svc.upsert_note(conn, title, f"# {place['name']}\n\n", source="user",
                                    kind="place", fire_events=False)
        note = conn.execute("SELECT slug FROM notes WHERE id = ?", (nid,)).fetchone()
    conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (note["slug"], place_id))
    conn.commit()
    return {"slug": note["slug"]}


@router.delete("/{place_id}")
def delete_place(place_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    conn.execute("DELETE FROM location_state WHERE place_id = ?", (place_id,))
    conn.commit()
    return {"ok": True}
