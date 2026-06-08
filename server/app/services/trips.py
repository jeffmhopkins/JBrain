"""Server-side TRIP detection + analytics over the location trail.

A trip is the MOVING segment between two STAYS (dwell clusters), detected per person
from their own fix stream and stored as a derived cache the AI can read cheaply.

Design (hardened via adversarial review):
- Per-person progress is a UTC WATERMARK in `trip_cursor`. Each pass re-segments from
  the watermark forward and DELETES+REINSERTS any trips in that window, so detection is
  idempotent and late/out-of-order fixes just rewind the watermark (`rewind_cursor`).
- Trip aggregates are always recomputed from the full ordered fix slice (never running
  sums), so incremental == batch by construction.
- Stay-end is "movement", not "a gap in fixes": the smart tracker sleeps GPS while
  still, so a long gap between CO-LOCATED fixes is a continued stay, not a boundary —
  otherwise overnight stays vanish and commutes fuse.
- Speed prefers the device's GPS speed; the haversine/dt fallback is filtered (dt>=min,
  displacement>accuracy) and capped, since the raw consecutive-fix max is GPS noise.
- Geofence pass-throughs use segment-vs-circle on the person's own path (catches a
  fly-by with no fix inside), never the global per-place location_state.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import geo
from . import geotrail
from . import people as people_svc

# --- tunables ---------------------------------------------------------------
STAY_RADIUS_M = 120.0      # fixes within this of the cluster are "the same spot"
STAY_MIN_S = 300.0         # stopped >= 5 min ⇒ a stay (shorter stops stay inside a trip)
MIN_TRIP_M = 300.0         # ignore micro-trips shorter than this (displacement)…
MIN_TRIP_S = 120.0         # …or briefer than this
MAX_OPEN_S = 24 * 3600     # force-close a never-ending open trip after this
DT_MIN_S = 5.0             # computed-speed: ignore segments closer in time than this
SPEED_CAP_KMH = 1200.0     # reject teleport speeds (still allows flights)
_GAP_CAP_S = geotrail._GAP_CAP_MIN * 60.0
_FIX_CAP = 5000            # max fixes segmented per person per pass (bounds a tick)
BACKFILL_DAYS = 90         # how far back to consider history on first detection


def _secs(a: str, b: str) -> float:
    """Return the signed elapsed seconds from timestamp a to timestamp b.

    Args:
        a: Start timestamp in 'YYYY-MM-DD HH:MM:SS' UTC format.
        b: End timestamp in 'YYYY-MM-DD HH:MM:SS' UTC format.

    Returns:
        Signed seconds (b - a), or 0.0 on parse failure.
    """
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(b[:19], fmt) - datetime.strptime(a[:19], fmt)).total_seconds()
    except Exception:
        return 0.0


def _now_str() -> str:
    """Return the current UTC time as a 'YYYY-MM-DD HH:MM:SS' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _floor_str(days: int) -> str:
    """Return a UTC timestamp string 'days' days in the past from now.

    Args:
        days: Number of days to look back.

    Returns:
        UTC timestamp string in 'YYYY-MM-DD HH:MM:SS' format.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# --- cursor management ------------------------------------------------------

def ensure_cursor(conn, person_id: int, backfill_days: int = BACKFILL_DAYS) -> dict:
    """Return the trip cursor for a person, creating it if it does not yet exist.

    Args:
        conn: Database connection.
        person_id: Person whose cursor to retrieve or initialise.
        backfill_days: How far back to initialise the watermark on first creation.

    Returns:
        Dict with keys person_id, watermark, and backfilled_to.
    """
    row = conn.execute("SELECT * FROM trip_cursor WHERE person_id = ?", (person_id,)).fetchone()
    if row is None:
        floor = _floor_str(backfill_days)
        conn.execute(
            "INSERT INTO trip_cursor (person_id, watermark, backfilled_to) VALUES (?, ?, ?)",
            (person_id, floor, floor),
        )
        return {"person_id": person_id, "watermark": floor, "backfilled_to": floor}
    return dict(row)


def rewind_cursor(conn, person_id, recorded_at: str) -> None:
    """Move the watermark back so the next detection pass re-segments from recorded_at.

    Called when a fix lands older than the current watermark (late or out-of-order).
    Clamped to the backfill floor so pre-window history is never reprocessed.

    Args:
        conn: Database connection.
        person_id: Person whose watermark to rewind.
        recorded_at: Timestamp of the late-arriving fix.
    """
    if person_id is None:
        return
    row = conn.execute("SELECT watermark, backfilled_to FROM trip_cursor WHERE person_id = ?",
                       (person_id,)).fetchone()
    if row is None:
        ensure_cursor(conn, person_id)
        return
    wm, floor = row["watermark"], row["backfilled_to"]
    target = recorded_at
    if floor and target < floor:
        target = floor
    if wm is None or target < wm:
        conn.execute("UPDATE trip_cursor SET watermark = ?, updated_at = ? WHERE person_id = ?",
                     (target, _now_str(), person_id))


# --- geometry helpers -------------------------------------------------------

def _seg_point_m(plat, plon, alat, alon, blat, blon) -> float:
    """Return the distance in metres from point P to segment AB.

    Uses a local equirectangular projection accurate at trail scales. Used for
    geofence fly-by detection.

    Args:
        plat: Point latitude.
        plon: Point longitude.
        alat: Segment start latitude.
        alon: Segment start longitude.
        blat: Segment end latitude.
        blon: Segment end longitude.

    Returns:
        Distance in metres from P to the nearest point on AB.
    """
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(alat))
    bx, by = (blon - alon) * mlon, (blat - alat) * mlat
    px, py = (plon - alon) * mlon, (plat - alat) * mlat
    L2 = bx * bx + by * by
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / L2))
    return math.hypot(px - t * bx, py - t * by)


def _place_at(conn, lat, lon):
    """Return the (place_id, label) for a coordinate.

    Returns the saved place whose geofence circle contains the point (nearest wins),
    else a nearby coord-note label, else (None, None).

    Args:
        conn: Database connection.
        lat: Latitude of the point.
        lon: Longitude of the point.

    Returns:
        Tuple of (place_id or None, label string or None).
    """
    best = None
    for p in conn.execute("SELECT id, name, lat, lon, radius_m FROM places").fetchall():
        d = geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0
        if d <= p["radius_m"] and (best is None or d < best[2]):
            best = (p["id"], p["name"], d)
    if best:
        return best[0], best[1]
    return None, geotrail.label_point(conn, lat, lon)


# --- segmentation -----------------------------------------------------------

def _stays(fixes: list[dict]) -> list[tuple[int, int]]:
    """Find index ranges [i, j] of dwell clusters in an ordered fix list.

    A cluster qualifies as a stay when the person remained within STAY_RADIUS_M of
    the anchor fix for at least STAY_MIN_S. A long gap between co-located fixes
    counts as a continued stay (GPS slept), not a trip boundary.

    Args:
        fixes: Ordered list of fix dicts with lat, lon, and recorded_at.

    Returns:
        List of (start_index, end_index) tuples for each stay.
    """
    out, i, n = [], 0, len(fixes)
    while i < n:
        j = i
        # extend while the next fix is within radius of the cluster's anchor
        while j + 1 < n and geo.haversine_km(
                fixes[i]["lat"], fixes[i]["lon"], fixes[j + 1]["lat"], fixes[j + 1]["lon"]) * 1000.0 <= STAY_RADIUS_M:
            j += 1
        if _secs(fixes[i]["recorded_at"], fixes[j]["recorded_at"]) >= STAY_MIN_S:
            out.append((i, j))
            i = j + 1
        else:
            i += 1   # not a stay → movement; advance one fix
    return out


def _trip_metrics(conn, slice_: list[dict]) -> dict:
    """Compute all server-side analytics for one trip's ordered fix slice.

    Args:
        conn: Database connection (for place lookups and distance calculation).
        slice_: Ordered list of fix dicts spanning the trip.

    Returns:
        Dict with trip metrics including started_at, ended_at, distance_km,
        displacement_km, duration_s, max_speed_kmh, avg_speed_kmh, fix_count,
        start/end place info, and a private _endpoints set for geofence exclusion.
    """
    a, b = slice_[0], slice_[-1]
    distance_km = geotrail.distance_km(conn, pts=slice_)
    displacement_km = geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    duration_s = max(0.0, _secs(a["recorded_at"], b["recorded_at"]))

    # max speed: device GPS speed when present (accurate), else filtered haversine/dt.
    dev = [f["speed_mps"] for f in slice_ if f["speed_mps"] is not None and 0 <= f["speed_mps"] * 3.6 <= SPEED_CAP_KMH]
    if dev:
        max_kmh = max(dev) * 3.6
    else:
        seg = []
        for k in range(len(slice_) - 1):
            dt = _secs(slice_[k]["recorded_at"], slice_[k + 1]["recorded_at"])
            if dt < DT_MIN_S:
                continue
            d = geo.haversine_km(slice_[k]["lat"], slice_[k]["lon"], slice_[k + 1]["lat"], slice_[k + 1]["lon"]) * 1000.0
            floor = max(2 * (slice_[k].get("accuracy_m") or 0.0), geotrail._JITTER_FLOOR_M)
            if d < floor:
                continue
            kmh = (d / dt) * 3.6
            if kmh <= SPEED_CAP_KMH:
                seg.append(kmh)
        max_kmh = (sorted(seg)[int(0.95 * (len(seg) - 1))] if seg else None)
    avg_kmh = (distance_km / (duration_s / 3600.0)) if duration_s > 0 else None

    sid, slabel = _place_at(conn, a["lat"], a["lon"])
    eid, elabel = _place_at(conn, b["lat"], b["lon"])
    return {
        "started_at": a["recorded_at"], "ended_at": b["recorded_at"],
        "start_lat": a["lat"], "start_lon": a["lon"], "end_lat": b["lat"], "end_lon": b["lon"],
        "start_place_id": sid, "start_place": slabel, "end_place_id": eid, "end_place": elabel,
        "distance_km": round(distance_km, 3), "displacement_km": round(displacement_km, 3),
        "duration_s": int(duration_s), "fix_count": len(slice_),
        "max_speed_kmh": round(max_kmh, 1) if max_kmh is not None else None,
        "avg_speed_kmh": round(avg_kmh, 1) if avg_kmh is not None else None,
        "_endpoints": {sid, eid} - {None},
    }


def _crossings(conn, slice_: list[dict], exclude: set) -> list[dict]:
    """Return geofences the trip passed through, excluding its start and end places.

    Uses segment-vs-circle detection so a fly-by between two sparse fixes still
    counts as a crossing.

    Args:
        conn: Database connection.
        slice_: Ordered fix list for the trip.
        exclude: Set of place_ids to exclude (the trip's endpoints).

    Returns:
        List of crossing dicts sorted by entered_at, each with place_id, place_name,
        lat, lon, entered_at, left_at, and dwell_s.
    """
    out = []
    for p in conn.execute("SELECT id, name, lat, lon, radius_m FROM places").fetchall():
        if p["id"] in exclude:
            continue
        inside_times, near = [], False
        for k in range(len(slice_) - 1):
            f, g = slice_[k], slice_[k + 1]
            if geo.haversine_km(p["lat"], p["lon"], f["lat"], f["lon"]) * 1000.0 <= p["radius_m"]:
                inside_times.append(f["recorded_at"])
            if _seg_point_m(p["lat"], p["lon"], f["lat"], f["lon"], g["lat"], g["lon"]) <= p["radius_m"]:
                near = True
        last = slice_[-1]
        if geo.haversine_km(p["lat"], p["lon"], last["lat"], last["lon"]) * 1000.0 <= p["radius_m"]:
            inside_times.append(last["recorded_at"])
        if not near and not inside_times:
            continue
        if inside_times:
            ent, lft = inside_times[0], inside_times[-1]
            dwell = min(_GAP_CAP_S, max(0.0, _secs(ent, lft)))
        else:                                  # pure fly-by: a moment, no dwell
            ent = lft = slice_[len(slice_) // 2]["recorded_at"]; dwell = 0.0
        out.append({"place_id": p["id"], "place_name": p["name"], "lat": p["lat"], "lon": p["lon"],
                    "entered_at": ent, "left_at": lft, "dwell_s": int(dwell)})
    out.sort(key=lambda c: c["entered_at"])
    return out


# --- detection --------------------------------------------------------------

def _insert_trip(conn, person_id, source, m: dict, status: str) -> int:
    """Insert a trip row and its geofence crossings into the database.

    Args:
        conn: Database connection.
        person_id: Person this trip belongs to.
        source: Device/source identifier for the fix stream.
        m: Metrics dict from _trip_metrics, must also carry a '_slice' key with
            the ordered fix list.
        status: Trip status string ('open' or 'closed').

    Returns:
        ID of the newly inserted trip row.
    """
    cur = conn.execute(
        "INSERT INTO trips (person_id, source, started_at, ended_at, start_lat, start_lon, "
        "end_lat, end_lon, start_place_id, start_place, end_place_id, end_place, distance_km, "
        "displacement_km, duration_s, max_speed_kmh, avg_speed_kmh, fix_count, status, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (person_id, source, m["started_at"], m["ended_at"], m["start_lat"], m["start_lon"],
         m["end_lat"], m["end_lon"], m["start_place_id"], m["start_place"], m["end_place_id"],
         m["end_place"], m["distance_km"], m["displacement_km"], m["duration_s"],
         m["max_speed_kmh"], m["avg_speed_kmh"], m["fix_count"], status, _now_str()),
    )
    tid = cur.lastrowid
    for ordinal, c in enumerate(_crossings(conn, m["_slice"], m["_endpoints"])):
        conn.execute(
            "INSERT INTO trip_places (trip_id, place_id, place_name, lat, lon, entered_at, "
            "left_at, dwell_s, ordinal) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, c["place_id"], c["place_name"], c["lat"], c["lon"], c["entered_at"],
             c["left_at"], c["dwell_s"], ordinal),
        )
    return tid


def _qualifies(m: dict) -> bool:
    """Return True if a trip's metrics meet the minimum displacement or duration thresholds.

    Args:
        m: Metrics dict from _trip_metrics.

    Returns:
        True if the trip is long enough or lasted long enough to be worth recording.
    """
    return m["displacement_km"] * 1000.0 >= MIN_TRIP_M or m["duration_s"] >= MIN_TRIP_S


def detect_for_person(conn, person_id: int, source_hint: str | None = None) -> int:
    """Run one bounded detection pass for a person and return the number of trips written.

    Reads fixes from the watermark forward (capped at _FIX_CAP), segments them into
    stays and trips, replaces stored trips in the window, and advances the watermark.

    Args:
        conn: Database connection.
        person_id: Person to detect trips for.
        source_hint: Device/source label to record on new trips; defaults to the
            last fix's source when omitted.

    Returns:
        Number of trip rows written this pass.
    """
    cur = ensure_cursor(conn, person_id)
    since = cur["watermark"] or cur["backfilled_to"] or _floor_str(BACKFILL_DAYS)
    fixes = [dict(r) for r in conn.execute(
        "SELECT lat, lon, accuracy_m, recorded_at, source, speed_mps FROM locations "
        "WHERE person_id = ? AND recorded_at >= ? ORDER BY recorded_at ASC LIMIT ?",
        (person_id, since, _FIX_CAP),
    ).fetchall()]
    if len(fixes) < 2:
        return 0
    truncated = len(fixes) >= _FIX_CAP
    source = source_hint or fixes[-1]["source"]

    stays = _stays(fixes)
    # Everything we re-derive in this window replaces what's stored there.
    conn.execute("DELETE FROM trips WHERE person_id = ? AND started_at >= ?", (person_id, since))

    written = 0
    # Trips BETWEEN consecutive stays are complete (have a closing stay) → closed.
    last_stay_start = None
    for k in range(len(stays) - 1):
        seg = fixes[stays[k][1]: stays[k + 1][0] + 1]   # depart-fix … arrive-fix
        if len(seg) < 2:
            continue
        m = _trip_metrics(conn, seg); m["_slice"] = seg
        if _qualifies(m):
            _insert_trip(conn, person_id, source, m, "closed"); written += 1
    if stays:
        last_stay_start = fixes[stays[-1][0]]["recorded_at"]

    # A trip before the first stay (first run / backfill: movement with no known origin).
    if stays and stays[0][0] > 0:
        seg = fixes[0: stays[0][0] + 1]
        if len(seg) >= 2:
            m = _trip_metrics(conn, seg); m["_slice"] = seg
            if _qualifies(m):
                _insert_trip(conn, person_id, source, m, "closed"); written += 1

    # Trailing segment after the last stay = movement in progress → an OPEN trip,
    # unless the tail is itself a stay (currently stopped) or too short to matter.
    tail_start = (stays[-1][1] if stays else 0)
    tail = fixes[tail_start:]
    open_written = False
    if len(tail) >= 2 and not (stays and stays[-1][1] == len(fixes) - 1):
        m = _trip_metrics(conn, tail); m["_slice"] = tail
        force = m["duration_s"] >= MAX_OPEN_S
        if _qualifies(m):
            _insert_trip(conn, person_id, source, m, "closed" if force else "open")
            written += 1; open_written = not force

    # Advance the watermark. Keep re-scanning the open trail (don't pass the last stay)
    # so it finalises when the person finally stops; if we never found a stay this pass,
    # don't advance (we'll see more fixes next time) unless the window was truncated.
    new_wm = None
    if last_stay_start is not None and not open_written:
        new_wm = last_stay_start
    elif last_stay_start is not None and open_written:
        new_wm = last_stay_start            # re-derive the open trip from the last stay
    elif truncated:
        new_wm = fixes[-1]["recorded_at"]   # no stays in a full window → force progress
    if new_wm:
        conn.execute("UPDATE trip_cursor SET watermark = ?, updated_at = ? WHERE person_id = ?",
                     (new_wm, _now_str(), person_id))
    return written


def run_detection(conn, max_people: int | None = None) -> int:
    """Detect trips for every person with a tracker stream. Scheduler entrypoint.

    Bounded per person per pass; commits per person so a long backfill never holds
    one large transaction. One person's bad data does not wedge the rest.

    Args:
        conn: Database connection.
        max_people: If set, process at most this many people per call.

    Returns:
        Total number of trip rows written across all people.
    """
    people = conn.execute("SELECT id FROM people ORDER BY id").fetchall()
    total = 0
    for i, row in enumerate(people):
        if max_people is not None and i >= max_people:
            break
        try:
            total += detect_for_person(conn, row["id"])
            conn.commit()
        except Exception:  # noqa: BLE001 — one person's bad data must not wedge the rest
            conn.rollback()
    return total


def query_trips(conn, person_id=None, since=None, until=None, limit=20) -> list[dict]:
    """Query trips with optional filters, ordered by started_at descending.

    Args:
        conn: Database connection.
        person_id: If set, return only this person's trips.
        since: Lower bound on started_at (inclusive).
        until: Upper bound on started_at (inclusive).
        limit: Maximum rows to return (clamped to 1–200).

    Returns:
        List of trip row dicts.
    """
    sql, params = "SELECT * FROM trips WHERE 1=1", []
    if person_id is not None:
        sql += " AND person_id = ?"; params.append(person_id)
    if since:
        sql += " AND started_at >= ?"; params.append(since)
    if until:
        sql += " AND started_at <= ?"; params.append(until)
    sql += " ORDER BY started_at DESC LIMIT ?"; params.append(max(1, min(int(limit or 20), 200)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def trip_detail(conn, trip_id: int):
    """Return a trip row and its ordered place crossings, or None if not found.

    Args:
        conn: Database connection.
        trip_id: Trip row ID.

    Returns:
        Tuple of (trip_dict, list_of_place_dicts), or None if the trip does not exist.
    """
    t = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if t is None:
        return None
    places = [dict(r) for r in conn.execute(
        "SELECT * FROM trip_places WHERE trip_id = ? ORDER BY ordinal", (trip_id,)).fetchall()]
    return dict(t), places


def reattribute(conn) -> None:
    """Re-resolve person_id on all fixes and trips after a people/alias change.

    Rewinds affected cursors so detection re-segments with the updated attribution.
    Cheap: iterates over distinct sources only.

    Args:
        conn: Database connection.
    """
    for r in conn.execute("SELECT DISTINCT source FROM locations").fetchall():
        s = r["source"]
        p = people_svc.resolve(conn, s or "")
        pid = p["id"] if p else None
        conn.execute("UPDATE locations SET person_id = ? WHERE source IS ?", (pid, s))
        conn.execute("UPDATE trips SET person_id = ? WHERE source IS ?", (pid, s))
    # Force a fresh segmentation everywhere (people moves can re-split trails).
    conn.execute("UPDATE trip_cursor SET watermark = backfilled_to, updated_at = ?", (_now_str(),))
