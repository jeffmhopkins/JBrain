"""Device location trail — opt-in background tracking ingest.

A native client (the Wear OS app) posts fixes; the server is the authoritative
keeper of the "store a point only if >=100 m moved OR >=60 min elapsed since the
last one" rule, so duplicate sends (retries, offline-queue flushes, an over-eager
client) never bloat the trail. Bearer-authed and owner-only — a location history
is sensitive, so it lives behind the same access key as everything else.
"""
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import geo
from ..services import geotrail

router = APIRouter(prefix="/api/locations", tags=["locations"], dependencies=[CurrentUser])

MIN_METERS = 100.0     # store if moved at least this far…
MIN_MINUTES = 60.0     # …OR at least this long since the last stored point


class LocationIn(BaseModel):
    lat: float
    lon: float
    accuracy_m: float | None = None
    recorded_at: str | None = None       # ISO/UTC; defaults to server now
    source: str = "wear"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.strip().replace(" ", "T").replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@router.post("")
def add_location(body: LocationIn):
    conn = get_conn()
    now = datetime.now(timezone.utc)
    rec_dt = _parse(body.recorded_at) or now
    rec_str = rec_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    last = conn.execute(
        "SELECT lat, lon, recorded_at FROM locations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last is not None:
        moved_m = geo.haversine_km(last["lat"], last["lon"], body.lat, body.lon) * 1000.0
        last_dt = _parse(last["recorded_at"]) or now
        elapsed_min = abs((rec_dt - last_dt).total_seconds()) / 60.0
        # The rule: keep a point only when it's far enough OR long enough apart.
        if moved_m < MIN_METERS and elapsed_min < MIN_MINUTES:
            return {"stored": False, "reason": "within 100 m and 60 min of the last point"}

    cur = conn.execute(
        "INSERT INTO locations (lat, lon, accuracy_m, recorded_at, source) VALUES (?, ?, ?, ?, ?)",
        (body.lat, body.lon, body.accuracy_m, rec_str, (body.source or "wear")[:32]),
    )
    # Refresh per-place geofence state (cheap, no actions) so the scheduler's
    # trigger evaluator has fresh truth. Never let it break ingest.
    try:
        geotrail.update_location_state(conn, body.lat, body.lon, rec_str)
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return {"stored": True, "id": cur.lastrowid}


def _norm(ts: str | None) -> str | None:
    # Normalise an ISO bound ("2026-06-01T00:00:00Z") to the stored format so string
    # comparison against recorded_at ("YYYY-MM-DD HH:MM:SS") works.
    return ts.strip().replace("T", " ").replace("Z", "").strip() if ts else None


@router.get("")
def list_locations(since: str | None = None, until: str | None = None, limit: int = 5000):
    sql = "SELECT id, lat, lon, accuracy_m, recorded_at FROM locations WHERE 1=1"
    params: list = []
    s, u = _norm(since), _norm(until)
    if s:
        sql += " AND recorded_at >= ?"
        params.append(s)
    if u:
        sql += " AND recorded_at <= ?"
        params.append(u)
    # ASC: chronological order suits trail/playback; the trail viewer is the main reader.
    sql += " ORDER BY recorded_at ASC LIMIT ?"
    params.append(max(1, min(int(limit), 20000)))
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]
