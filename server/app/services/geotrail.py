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
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        d = d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts.strip().replace("T", " ").replace("Z", "")


def _mins(a: str, b: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return abs((datetime.strptime(b[:19], fmt) - datetime.strptime(a[:19], fmt)).total_seconds()) / 60.0
    except Exception:
        return 0.0


def fixes(conn, since: str | None = None, until: str | None = None) -> list[dict]:
    sql = "SELECT lat, lon, accuracy_m, recorded_at FROM locations WHERE 1=1"
    params: list = []
    s, u = _utc(since), _utc(until)
    if s:
        sql += " AND recorded_at >= ?"; params.append(s)
    if u:
        sql += " AND recorded_at <= ?"; params.append(u)
    sql += " ORDER BY recorded_at ASC LIMIT 20000"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def label_point(conn, lat: float, lon: float) -> str | None:
    """Nearest place whose circle contains the point → its name; else nearest coord-note
    within 150 m → its title; else None. Places win; far notes never label (privacy)."""
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


def nearest_fix(conn, when: str) -> tuple[dict | None, float]:
    """Closest fix to `when` (UTC ISO). Returns (fix, gap_minutes)."""
    target = _utc(when)
    if not target:
        return None, 0.0
    row = conn.execute(
        "SELECT lat, lon, recorded_at FROM locations "
        "ORDER BY ABS(strftime('%s', recorded_at) - strftime('%s', ?)) ASC LIMIT 1",
        (target,),
    ).fetchone()
    if not row:
        return None, 0.0
    return dict(row), _mins(row["recorded_at"], target)


def dwell_minutes(conn, lat: float, lon: float, radius_m: float, since=None, until=None) -> float:
    """Minutes spent within radius_m of (lat,lon). Each inter-fix gap is split half to
    each endpoint and capped, so a sparse single-fix visit is counted from the
    surrounding gaps rather than 0-or-everything."""
    pts = fixes(conn, since, until)
    inside = [geo.haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0 <= radius_m for p in pts]
    total = 0.0
    for i in range(len(pts) - 1):
        gap = min(_GAP_CAP_MIN, _mins(pts[i]["recorded_at"], pts[i + 1]["recorded_at"]))
        if inside[i]:
            total += gap / 2.0
        if inside[i + 1]:
            total += gap / 2.0
    return round(total, 1)


def distance_km(conn, since=None, until=None) -> float:
    pts = fixes(conn, since, until)
    total = 0.0
    for i in range(len(pts) - 1):
        seg_m = geo.haversine_km(pts[i]["lat"], pts[i]["lon"], pts[i + 1]["lat"], pts[i + 1]["lon"]) * 1000.0
        floor = max(_JITTER_FLOOR_M, pts[i].get("accuracy_m") or 0.0)
        if seg_m >= floor:
            total += seg_m
    return round(total / 1000.0, 2)


def update_location_state(conn, lat: float, lon: float, fix_time: str) -> None:
    """After a fix is KEPT, refresh per-place inside/since/last_inside_at — the
    physical truth the trigger evaluator reads. Hysteresis: enter at radius, leave
    only beyond radius*1.3, so a single edge fix can't flap a geofence. `since` is
    the time of the LAST state change (entry, or departure); cheap (a few places),
    no actions fire here — the scheduler decides what to do with the state."""
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


def stay_points(conn, since=None, until=None, radius_m: float = 150.0, min_min: float = 20.0) -> list[dict]:
    """Greedy clusters of consecutive fixes within radius held for >= min_min. Each is
    labeled via label_point (place/note/None)."""
    pts = fixes(conn, since, until)
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
