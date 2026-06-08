"""Geo math over the location trail — server-side so the LLM never does trig on rows.

Time bounds come in as UTC ISO (the agent is grounded in app_tz and computes them);
`_utc()` normalizes any offset/`Z`/naive form to the stored "YYYY-MM-DD HH:MM:SS".
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import geo

_GAP_CAP_MIN = 90.0      # a gap longer than this means "we lost the trail" — don't count it as dwell
_NOTE_LABEL_M = 150.0    # only label a point with a coord-note this close
_JITTER_FLOOR_M = 30.0   # ignore movement smaller than this (GPS noise) in distance sums
_LEAVE_HYSTERESIS = 1.3  # once inside, only count as "left" beyond radius*this (anti-flap)


def _utc(ts: str | None) -> str | None:
    """Normalise any ISO timestamp (offset, Z, or naive) to 'YYYY-MM-DD HH:MM:SS' UTC.

    Args:
        ts: Timestamp string in any ISO 8601 form, or None.

    Returns:
        Normalised UTC string, or None if the input is empty.
    """
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        d = d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts.strip().replace("T", " ").replace("Z", "")


def _mins(a: str, b: str) -> float:
    """Return the absolute elapsed minutes between two 'YYYY-MM-DD HH:MM:SS' timestamps.

    Args:
        a: First timestamp string.
        b: Second timestamp string.

    Returns:
        Absolute difference in minutes, or 0.0 on parse failure.
    """
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return abs((datetime.strptime(b[:19], fmt) - datetime.strptime(a[:19], fmt)).total_seconds()) / 60.0
    except Exception:
        return 0.0


def _attribute(conn):
    """Return a memoised function mapping a fix's source to a person_id.

    The default person catches any unmatched source. Memoised so a 20k-row scan
    resolves each distinct source only once.

    Args:
        conn: Database connection.

    Returns:
        Callable accepting a source string and returning a person_id or None.
    """
    from . import people as people_svc
    cache: dict[str, int | None] = {}

    def pid(source: str | None) -> int | None:
        key = (source or "").strip().lower()
        if key not in cache:
            p = people_svc.resolve(conn, source or "")
            cache[key] = p["id"] if p else None
        return cache[key]

    return pid


def fixes(conn, since: str | None = None, until: str | None = None, person_id: int | None = None) -> list[dict]:
    """Return location fixes in ascending recorded_at order with optional filters.

    Caps the scan at 20,000 rows, retaining the most recent fixes when a window is
    large (DESC + LIMIT, then reversed). Swaps swapped bounds silently.

    Args:
        conn: Database connection.
        since: Lower time bound (any ISO form); normalised to UTC.
        until: Upper time bound (any ISO form); normalised to UTC.
        person_id: If set, return only this person's fixes.

    Returns:
        List of fix dicts with lat, lon, accuracy_m, recorded_at, and source.
    """
    sql = "SELECT lat, lon, accuracy_m, recorded_at, source FROM locations WHERE 1=1"
    params: list = []
    s, u = _utc(since), _utc(until)
    if s and u and s > u:
        s, u = u, s        # tolerate swapped bounds rather than returning nothing
    if s:
        sql += " AND recorded_at >= ?"; params.append(s)
    if u:
        sql += " AND recorded_at <= ?"; params.append(u)
    # Cap the scan, but keep the MOST RECENT fixes when a window is huge (ASC + LIMIT
    # would silently drop newest); re-sort to ascending for the trail math.
    sql += " ORDER BY recorded_at DESC LIMIT 20000"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    rows.reverse()
    if person_id is not None:                 # keep only this person's fixes
        pid = _attribute(conn)
        rows = [r for r in rows if pid(r["source"]) == person_id]
    return rows


def latest_fix(conn, person_id: int | None = None) -> dict | None:
    """Return the most recent location fix overall or for one specific person.

    The default person (person_id=None) returns the global most-recent fix.

    Args:
        conn: Database connection.
        person_id: If set, return the most recent fix for this person.

    Returns:
        Fix dict with lat, lon, accuracy_m, recorded_at, and source, or None.
    """
    if person_id is None:
        row = conn.execute(
            "SELECT lat, lon, accuracy_m, recorded_at, source FROM locations "
            "ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    pid = _attribute(conn)
    for r in conn.execute(
        "SELECT lat, lon, accuracy_m, recorded_at, source FROM locations "
        "ORDER BY recorded_at DESC LIMIT 5000"
    ).fetchall():
        if pid(r["source"]) == person_id:
            return dict(r)
    return None


def label_point(conn, lat: float, lon: float) -> str | None:
    """Return a human-readable label for a coordinate, or None.

    Returns the name of the nearest saved place whose geofence circle contains the
    point (places win over notes); else the title of the nearest coord-note within
    150 m; else None. Far notes are excluded for privacy.

    Args:
        conn: Database connection.
        lat: Latitude of the point.
        lon: Longitude of the point.

    Returns:
        Place name, note title, or None.
    """
    best = None
    for p in conn.execute("SELECT name, lat, lon, radius_m FROM places").fetchall():
        d = geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0
        if d <= p["radius_m"] and (best is None or d < best[1]):
            best = (p["name"], d)
    if best:
        return best[0]
    note = None
    for r in conn.execute(
        "SELECT title, lat, lon FROM notes WHERE deleted_at IS NULL AND lat IS NOT NULL AND lon IS NOT NULL"
    ).fetchall():
        d = geo.haversine_km(lat, lon, r["lat"], r["lon"]) * 1000.0
        if d <= _NOTE_LABEL_M and (note is None or d < note[1]):
            note = (r["title"], d)
    return note[0] if note else None


def nearest_fix(conn, when: str, person_id: int | None = None) -> tuple[dict | None, float]:
    """Return the closest fix to a given time and the gap in minutes.

    Args:
        conn: Database connection.
        when: Target timestamp in any ISO form.
        person_id: If set, constrain to this person's fixes.

    Returns:
        Tuple of (fix dict or None, gap_minutes float).
    """
    target = _utc(when)
    if not target:
        return None, 0.0
    if person_id is None:
        row = conn.execute(
            "SELECT lat, lon, recorded_at FROM locations "
            "ORDER BY ABS(strftime('%s', recorded_at) - strftime('%s', ?)) ASC LIMIT 1",
            (target,),
        ).fetchone()
        return (dict(row), _mins(row["recorded_at"], target)) if row else (None, 0.0)
    pid = _attribute(conn)
    for r in conn.execute(
        "SELECT lat, lon, recorded_at, source FROM locations "
        "ORDER BY ABS(strftime('%s', recorded_at) - strftime('%s', ?)) ASC LIMIT 2000",
        (target,),
    ).fetchall():
        if pid(r["source"]) == person_id:
            return dict(r), _mins(r["recorded_at"], target)
    return None, 0.0


def dwell_minutes(conn, lat: float, lon: float, radius_m: float, since=None, until=None, pts=None) -> float:
    """Return the minutes spent within radius_m of a coordinate.

    Each inter-fix gap is split half to each endpoint and capped at _GAP_CAP_MIN,
    so a sparse single-fix visit is counted from the surrounding gaps rather than
    zero-or-everything. A lone fix with no neighbour in the window carries no
    duration (0.0), which is correct — one instant says nothing.

    Args:
        conn: Database connection.
        lat: Centre latitude.
        lon: Centre longitude.
        radius_m: Radius of the dwell zone in metres.
        since: Optional lower time bound.
        until: Optional upper time bound.
        pts: Pre-fetched fix list; fetched from the DB when omitted.

    Returns:
        Total dwell time in minutes, rounded to one decimal place.
    """
    pts = pts if pts is not None else fixes(conn, since, until)
    inside = [geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0 <= radius_m for p in pts]
    total = 0.0
    for i in range(len(pts) - 1):
        gap = min(_GAP_CAP_MIN, _mins(pts[i]["recorded_at"], pts[i + 1]["recorded_at"]))
        if inside[i]:
            total += gap / 2.0
        if inside[i + 1]:
            total += gap / 2.0
    return round(total, 1)


def distance_km(conn, since=None, until=None, pts=None) -> float:
    """Return the total distance travelled in km, filtering out GPS jitter.

    Each segment shorter than max(accuracy_m, _JITTER_FLOOR_M) is discarded.

    Args:
        conn: Database connection.
        since: Optional lower time bound.
        until: Optional upper time bound.
        pts: Pre-fetched fix list; fetched from the DB when omitted.

    Returns:
        Distance in kilometres, rounded to two decimal places.
    """
    pts = pts if pts is not None else fixes(conn, since, until)
    total = 0.0
    for i in range(len(pts) - 1):
        seg_m = geo.haversine_km(pts[i]["lat"], pts[i]["lon"], pts[i + 1]["lat"], pts[i + 1]["lon"]) * 1000.0
        floor = max(_JITTER_FLOOR_M, pts[i].get("accuracy_m") or 0.0)
        if seg_m >= floor:
            total += seg_m
    return round(total / 1000.0, 2)


def update_location_state(conn, lat: float, lon: float, fix_time: str) -> None:
    """Refresh per-place inside/since/last_inside_at after a fix is kept.

    Updates the physical truth that the trigger evaluator reads. Hysteresis:
    enter at radius, leave only beyond radius * _LEAVE_HYSTERESIS, so a single
    edge fix cannot flap a geofence. `since` records the time of the last state
    change (entry or departure). Cheap (iterates only a few places); no actions
    fire here — the scheduler decides what to do with the updated state.

    Args:
        conn: Database connection.
        lat: Latitude of the kept fix.
        lon: Longitude of the kept fix.
        fix_time: Timestamp of the fix in any ISO form.
    """
    ft = _utc(fix_time) or fix_time
    for p in conn.execute("SELECT id, lat, lon, radius_m FROM places").fetchall():
        d = geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0
        st = conn.execute(
            "SELECT inside, since, last_inside_at FROM location_state WHERE place_id = ?", (p["id"],)
        ).fetchone()
        was_inside = bool(st["inside"]) if st else False
        threshold = p["radius_m"] * (_LEAVE_HYSTERESIS if was_inside else 1.0)
        inside_now = d <= threshold
        if st is None:
            conn.execute(
                "INSERT INTO location_state (place_id, inside, since, last_inside_at, last_fix_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (p["id"], 1 if inside_now else 0, ft if inside_now else None, ft if inside_now else None, ft),
            )
        else:
            since = ft if inside_now != was_inside else st["since"]
            last_inside = ft if inside_now else st["last_inside_at"]
            conn.execute(
                "UPDATE location_state SET inside = ?, since = ?, last_inside_at = ?, last_fix_at = ? "
                "WHERE place_id = ?",
                (1 if inside_now else 0, since, last_inside, ft, p["id"]),
            )


def stay_points(conn, since=None, until=None, radius_m: float = 150.0, min_min: float = 20.0, pts=None) -> list[dict]:
    """Return greedy clusters of consecutive fixes representing stay points.

    A cluster qualifies when the person remained within radius_m for at least
    min_min minutes. Inter-cluster gaps beyond _GAP_CAP_MIN are treated as trail
    breaks, not extensions of the same visit. Each stay is labelled via
    label_point (place name, coord-note title, or None).

    Args:
        conn: Database connection.
        since: Optional lower time bound.
        until: Optional upper time bound.
        radius_m: Cluster radius in metres.
        min_min: Minimum dwell duration in minutes to qualify as a stay.
        pts: Pre-fetched fix list; fetched from the DB when omitted.

    Returns:
        List of stay dicts with label, lat, lon, arrived, left, and minutes.
    """
    pts = pts if pts is not None else fixes(conn, since, until)
    out: list[dict] = []
    i = 0
    while i < len(pts):
        j = i
        # Extend while the next fix is in-radius AND close in time — a gap beyond the
        # cap means we lost the trail (or it's a later day), so end the stay there
        # rather than fusing two separate visits into one.
        while (j + 1 < len(pts)
               and geo.haversine_km(pts[i]["lat"], pts[i]["lon"], pts[j + 1]["lat"], pts[j + 1]["lon"]) * 1000.0 <= radius_m
               and _mins(pts[j]["recorded_at"], pts[j + 1]["recorded_at"]) <= _GAP_CAP_MIN):
            j += 1
        dur = _mins(pts[i]["recorded_at"], pts[j]["recorded_at"]) if j > i else 0.0
        if dur >= min_min:
            clat = sum(p["lat"] for p in pts[i:j + 1]) / (j - i + 1)
            clon = sum(p["lon"] for p in pts[i:j + 1]) / (j - i + 1)
            out.append({
                "label": label_point(conn, clat, clon),
                "lat": round(clat, 6), "lon": round(clon, 6),
                "arrived": pts[i]["recorded_at"], "left": pts[j]["recorded_at"],
                "minutes": round(dur, 1),
            })
            i = j + 1
        else:
            i += 1
    return out
