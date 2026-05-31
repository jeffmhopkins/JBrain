"""Quick-capture inbox: a tiny low-friction endpoint for dictation from any
device (phone share-sheet, a Wear OS tile via Tasker, a shortcut, etc.).

Captured text lands in the inbox and is surfaced by the architect later for
Socratic processing + staging — capture is decoupled from organisation.
"""
from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn

router = APIRouter(prefix="/api/capture", tags=["capture"], dependencies=[CurrentUser])


class CaptureIn(BaseModel):
    content: str
    source: str = "capture"
    lat: float | None = None
    lon: float | None = None
    location_label: str | None = None


@router.post("")
def capture(body: CaptureIn):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO inbox (source, content, lat, lon, location_label) VALUES (?, ?, ?, ?, ?)",
        (body.source, body.content.strip(), body.lat, body.lon, body.location_label),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@router.get("")
def list_inbox(include_processed: bool = False):
    where = "" if include_processed else "WHERE processed = 0"
    rows = get_conn().execute(
        f"SELECT id, source, content, lat, lon, location_label, processed, created_at "
        f"FROM inbox {where} ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    return [dict(r) for r in rows]
