"""Device location trail — opt-in background tracking ingest.

A native client (the Wear OS app) posts fixes; the server is the authoritative
keeper of the "store a point only if >=100 m moved OR >=60 min elapsed since the
last one" rule, so duplicate sends (retries, offline-queue flushes, an over-eager
client) never bloat the trail. Bearer-authed and owner-only — a location history
is sensitive, so it lives behind the same access key as everything else.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import CurrentUser, require_location_writer
from ..db import get_conn
from ..services import geo
from ..services import geotrail
from ..services import people as people_svc
from ..services import trips as trips_svc

# No router-level auth: the WRITE endpoints accept the full key OR a per-person
# location key (require_location_writer); the READ endpoint stays full-key only.
router = APIRouter(prefix="/api/locations", tags=["locations"])

MIN_METERS = 100.0     # store if moved at least this far…
MIN_MINUTES = 60.0     # …OR at least this long since the last stored point


class LocationIn(BaseModel):
    lat: float
    lon: float
    accuracy_m: float | None = None
    recorded_at: str | None = None       # ISO/UTC; defaults to server now
    source: str = "wear"
    # Device-reported motion (GPS), all optional — used for accurate trip analytics.
    speed_mps: float | None = None       # ground speed (m/s)
    bearing_deg: float | None = None     # heading (degrees, 0-360)
    altitude_m: float | None = None      # metres above sea level


class LocationBatch(BaseModel):
    points: list[LocationIn]


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
def add_location(body: LocationIn, writer=Depends(require_location_writer)):
    conn = get_conn()
    now = datetime.now(timezone.utc)
    rec_dt = _parse(body.recorded_at) or now
    rec_str = rec_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # A per-person location key forces the fix's source to that person.
    source = (writer["name"] if writer is not None else (body.source or "wear"))[:32]

    # Dedup against THIS SOURCE's temporal predecessor (latest fix at or before this one
    # from the same device/person) — not the global last fix. Otherwise, with several
    # people tracked, co-located family drop each other's fixes and a stationary person
    # near a moving one logs spurious "moved" points.
    last = conn.execute(
        "SELECT lat, lon, recorded_at FROM locations WHERE source = ? AND recorded_at <= ? "
        "ORDER BY recorded_at DESC LIMIT 1",
        (source, rec_str),
    ).fetchone()
    if last is not None:
        moved_m = geo.haversine_km(last["lat"], last["lon"], body.lat, body.lon) * 1000.0
        last_dt = _parse(last["recorded_at"]) or now
        elapsed_min = abs((rec_dt - last_dt).total_seconds()) / 60.0
        # The rule: keep a point only when it's far enough OR long enough apart.
        if moved_m < MIN_METERS and elapsed_min < MIN_MINUTES:
            return {"stored": False, "reason": "within 100 m and 60 min of the last point"}

    person = people_svc.resolve(conn, source)
    pid = person["id"] if person else None
    cur = conn.execute(
        "INSERT INTO locations (lat, lon, accuracy_m, recorded_at, source, person_id, "
        "speed_mps, bearing_deg, altitude_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (body.lat, body.lon, body.accuracy_m, rec_str, source, pid,
         body.speed_mps, body.bearing_deg, body.altitude_m),
    )
    # Refresh per-place geofence state (cheap, no actions) so the scheduler's
    # trigger evaluator has fresh truth. Never let it break ingest.
    try:
        geotrail.update_location_state(conn, body.lat, body.lon, rec_str)
    except Exception:  # noqa: BLE001
        pass
    try:
        trips_svc.rewind_cursor(conn, pid, rec_str)   # ensure detection revisits this time
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return {"stored": True, "id": cur.lastrowid}


@router.post("/bulk")
def add_locations(body: LocationBatch, writer=Depends(require_location_writer)):
    """Ingest a batch of fixes (a native tracker's offline-queue flush) in one call.

    The keep-if-far-enough-OR-long-enough rule is applied IN ORDER — points are sorted
    chronologically and each compared against the last KEPT point (the DB's newest,
    then whatever we just stored), so an offline burst dedups exactly as a live stream
    would. Capped per request so one flush can't run unbounded."""
    conn = get_conn()
    now = datetime.now(timezone.utc)
    pts = sorted(body.points or [], key=lambda p: _parse(p.recorded_at) or now)[:5000]
    _pid_cache: dict[str, int | None] = {}
    def _pid(src: str):
        if src not in _pid_cache:
            pr = people_svc.resolve(conn, src)
            _pid_cache[src] = pr["id"] if pr else None
        return _pid_cache[src]
    # Dedup PER SOURCE: each device/person's stream is compared against its own last
    # kept fix, seeded from that source's temporal predecessor in the DB. So one
    # device's burst never dedups against another person's fixes.
    last_by_src: dict[str, tuple] = {}    # source -> (lat, lon, datetime)
    def _seed(src: str, rec_str: str):
        if src in last_by_src:
            return
        r = conn.execute(
            "SELECT lat, lon, recorded_at FROM locations WHERE source = ? AND recorded_at <= ? "
            "ORDER BY recorded_at DESC LIMIT 1", (src, rec_str)).fetchone()
        last_by_src[src] = (r["lat"], r["lon"], _parse(r["recorded_at"])) if r else None
    stored = 0
    dirty: dict[int | None, str] = {}    # person_id -> earliest stored recorded_at
    for p in pts:
        rec_dt = _parse(p.recorded_at) or now
        rec_str = rec_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # A per-person location key forces every fix's source to that person.
        source = (writer["name"] if writer is not None else (p.source or "phone"))[:32]
        _seed(source, rec_str)
        prev = last_by_src.get(source)
        if prev is not None:
            moved_m = geo.haversine_km(prev[0], prev[1], p.lat, p.lon) * 1000.0
            elapsed_min = abs((rec_dt - (prev[2] or rec_dt)).total_seconds()) / 60.0
            if moved_m < MIN_METERS and elapsed_min < MIN_MINUTES:
                continue
        pid = _pid(source)
        conn.execute(
            "INSERT INTO locations (lat, lon, accuracy_m, recorded_at, source, person_id, "
            "speed_mps, bearing_deg, altitude_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p.lat, p.lon, p.accuracy_m, rec_str, source, pid, p.speed_mps, p.bearing_deg, p.altitude_m),
        )
        try:
            geotrail.update_location_state(conn, p.lat, p.lon, rec_str)
        except Exception:  # noqa: BLE001
            pass
        if pid not in dirty or rec_str < dirty[pid]:
            dirty[pid] = rec_str
        last_by_src[source] = (p.lat, p.lon, rec_dt)
        stored += 1
    for pid, earliest in dirty.items():
        try:
            trips_svc.rewind_cursor(conn, pid, earliest)
        except Exception:  # noqa: BLE001
            pass
    conn.commit()
    return {"stored": stored, "received": len(body.points or [])}


def _norm(ts: str | None) -> str | None:
    # Normalise an ISO bound ("2026-06-01T00:00:00Z") to the stored format so string
    # comparison against recorded_at ("YYYY-MM-DD HH:MM:SS") works.
    return ts.strip().replace("T", " ").replace("Z", "").strip() if ts else None


@router.get("", dependencies=[CurrentUser])
def list_locations(since: str | None = None, until: str | None = None, limit: int = 12000):
    sql = "SELECT id, lat, lon, accuracy_m, recorded_at, source FROM locations WHERE 1=1"
    params: list = []
    s, u = _norm(since), _norm(until)
    if s:
        sql += " AND recorded_at >= ?"
        params.append(s)
    if u:
        sql += " AND recorded_at <= ?"
        params.append(u)
    # Cap the scan but keep the MOST RECENT fixes when the window is huge (several people
    # share this stream): DESC + LIMIT, then reverse to chronological for the trail viewer.
    sql += " ORDER BY recorded_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 20000)))
    rows = [dict(r) for r in get_conn().execute(sql, params).fetchall()]
    rows.reverse()
    return rows
