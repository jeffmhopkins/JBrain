"""Named geofences ("places") for the location tools + triggers. Owner-only."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn

router = APIRouter(prefix="/api/places", tags=["places"], dependencies=[CurrentUser])


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


@router.delete("/{place_id}")
def delete_place(place_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    conn.execute("DELETE FROM location_state WHERE place_id = ?", (place_id,))
    conn.commit()
    return {"ok": True}
