"""Read-only builders for the lab trend chart: the analyte picklist and a single analyte's
series + reference-band segments + overlapping encounters.

Design notes (these encode hard-won correctness decisions):
  * Reads `lab_results` DIRECTLY, not `v_lab_trend` — the view drops `value_num IS NULL`
    rows, which would silently hide censored/non-numeric results ('<0.01'). We keep them
    (flagged `censored`) so the chart shows a gap/marker instead of lying by omission.
  * The reference band is SEGMENTED here, in Python: ref_low/ref_high are per-row and
    legitimately change over time, so a single constant band would be clinically wrong.
  * Point status is FLAG-AUTHORITATIVE: the lab's own `flag` wins; only when it's absent do
    we compute from the row's own range; when neither exists the point is `unknown` (never
    guess an abnormality the source didn't assert).
  * One analyte slug can carry cosmetically-different unit strings for the same quantity
    (thou/cumm ≡ Thousand/uL); those co-plot. Genuinely different units are split out.
"""
from __future__ import annotations

import re

# Map cosmetically-different unit strings for the SAME quantity to one token, so they
# co-plot; anything not listed keeps its own normalized form (and so won't be co-plotted).
_UNIT_ALIASES = {
    "thou/cumm": "10e3/ul", "thousand/ul": "10e3/ul", "10^3/ul": "10e3/ul",
    "10*3/ul": "10e3/ul", "k/ul": "10e3/ul", "x10e3/ul": "10e3/ul",
    "mill/cumm": "10e6/ul", "million/ul": "10e6/ul", "10^6/ul": "10e6/ul", "m/ul": "10e6/ul",
}
_FLAG_HIGH = {"H", "HH", "HI", "HIGH"}
_FLAG_LOW = {"L", "LL", "LO", "LOW"}
_FLAG_NORMAL = {"N", "NORMAL", "WNL"}
_FLAG_ABNORMAL = {"A", "AB", "ABN", "ABNORMAL"}


# Rows whose source note was (soft-)deleted are hidden — so deleting a mis-imported note in
# the UI removes its results from the Labs view (and lets you re-upload cleanly).
_LIVE_NOTE = "(note_id IS NULL OR note_id NOT IN (SELECT id FROM notes WHERE deleted_at IS NOT NULL))"


def _unit_norm(unit: str | None) -> str:
    """Normalize a unit string for alias-based co-plotting.

    Args:
        unit: Raw unit string, or None.

    Returns:
        Canonical lower-case unit token with whitespace removed; aliased forms are
        collapsed to a single token so cosmetically different units co-plot.
    """
    s = re.sub(r"\s+", "", (unit or "").lower())
    return _UNIT_ALIASES.get(s, s)


def _status(flag: str | None, vnum, low, high) -> str:
    """Determine a point's status with the lab's own flag taking priority.

    Args:
        flag: Lab-reported flag string (e.g. 'H', 'L', 'N'), or None.
        vnum: Numeric result value, or None for censored values.
        low: Reference range lower bound, or None.
        high: Reference range upper bound, or None.

    Returns:
        One of 'high', 'low', 'normal', 'abnormal', or 'unknown'.
    """
    f = (flag or "").strip().upper()
    if f in _FLAG_HIGH:
        return "high"
    if f in _FLAG_LOW:
        return "low"
    if f in _FLAG_ABNORMAL:
        return "abnormal"
    if f in _FLAG_NORMAL:
        return "normal"
    # No usable flag — compute from this row's OWN range, or stay neutral.
    if vnum is not None:
        if low is not None and vnum < low:
            return "low"
        if high is not None and vnum > high:
            return "high"
        if low is not None or high is not None:
            return "normal"
    return "unknown"


def _modal(conn, analyte: str, col: str) -> str | None:
    """Return the most common non-null value of a column for an analyte.

    Args:
        conn: Database connection.
        analyte: Analyte slug key.
        col: Column name (a fixed, trusted identifier — never user-supplied).

    Returns:
        Modal value as a string, or None if no rows exist.
    """
    r = conn.execute(
        f"SELECT {col} AS v, COUNT(*) AS c FROM lab_results "
        f"WHERE analyte_key = ? AND {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC, {col} LIMIT 1",
        (analyte,)).fetchone()
    return r["v"] if r else None


def list_analytes(conn) -> list[dict]:
    """Return one entry per analyte that has dated results.

    Each entry contains the display name (modal test_name), unit, point count, date span,
    and the latest value with its status — for the analyte picker.

    Args:
        conn: Database connection.

    Returns:
        List of analyte dicts sorted by test_name, each with keys: analyte, test_name,
        unit, n, first_at, last_at, last_value, last_status.
    """
    # COUNT DISTINCT (date,value): identical points from an overlapping re-export must not
    # inflate the picker count — match the read-time dedup series()/stat() already apply (F2).
    rows = conn.execute(
        "SELECT analyte_key, COUNT(DISTINCT collected_at || '|' || COALESCE(value_text,'')) AS n, "
        "       MIN(collected_at) AS first_at, MAX(collected_at) AS last_at "
        "FROM lab_results WHERE analyte_key IS NOT NULL AND collected_at IS NOT NULL "
        f"AND {_LIVE_NOTE} GROUP BY analyte_key").fetchall()
    out = []
    for r in rows:
        latest = conn.execute(
            "SELECT value_text, value_num, flag, ref_low, ref_high FROM lab_results "
            f"WHERE analyte_key = ? AND collected_at IS NOT NULL AND {_LIVE_NOTE} "
            "ORDER BY collected_at DESC, collected_time DESC, id DESC LIMIT 1",
            (r["analyte_key"],)).fetchone()
        out.append({
            "analyte": r["analyte_key"],
            "test_name": _modal(conn, r["analyte_key"], "test_name") or r["analyte_key"],
            "unit": _modal(conn, r["analyte_key"], "unit"),
            "n": r["n"], "first_at": r["first_at"], "last_at": r["last_at"],
            "last_value": latest["value_text"] if latest else None,
            "last_status": _status(latest["flag"], latest["value_num"], latest["ref_low"], latest["ref_high"])
            if latest else "unknown",
        })
    out.sort(key=lambda a: (a["test_name"] or "").lower())
    return out


def _segments(points: list[dict]) -> list[dict]:
    """Build a stepped reference band as a list of contiguous segments.

    A segment is created per contiguous run of points sharing the same (ref_low, ref_high).
    A point with no range (both None) breaks the band — never borrow a neighbour's range.

    Args:
        points: Ordered list of series point dicts.

    Returns:
        List of segment dicts with keys: from, to, low, high.
    """
    segs: list[dict] = []
    cur: dict | None = None
    for p in points:
        lo, hi = p["ref_low"], p["ref_high"]
        if lo is None and hi is None:
            cur = None
            continue
        if cur and cur["low"] == lo and cur["high"] == hi:
            cur["to"] = p["t"]
        else:
            cur = {"from": p["t"], "to": p["t"], "low": lo, "high": hi}
            segs.append(cur)
    return segs


def _encounters_in(conn, dfrom: str | None, dto: str | None) -> list[dict]:
    """Return encounters whose date range overlaps [dfrom, dto].

    Args:
        conn: Database connection.
        dfrom: Start of the window (ISO date), or None.
        dto: End of the window (ISO date), or None.

    Returns:
        List of encounter dicts with keys: id, kind, label, from, to.
        Empty if either bound is None.
    """
    if not dfrom or not dto:
        return []
    rows = conn.execute(
        "SELECT id, kind, summary, facility, started_at, ended_at FROM encounters "
        "WHERE started_at IS NOT NULL AND date(started_at) <= date(?) "
        "  AND (ended_at IS NULL OR date(ended_at) >= date(?)) ORDER BY started_at",
        (dto, dfrom)).fetchall()
    return [{"id": r["id"], "kind": r["kind"],
             "label": r["summary"] or r["facility"] or (r["kind"] or "visit"),
             "from": r["started_at"], "to": r["ended_at"]} for r in rows]


def abnormal_analytes(conn, dfrom: str | None = None, dto: str | None = None, limit: int = 8) -> list[dict]:
    """Return analytes with at least one high/low/abnormal result in the date window.

    Ranked most-recently-abnormal first, then by count. Reads lab_results directly and uses
    the same _status the chart colors by — so 'what was abnormal' and the chart always agree.
    A censored row counts only if the lab itself flagged it (else it's unknown, never swept in).

    Args:
        conn: Database connection.
        dfrom: Window start (ISO date), or None for no lower bound.
        dto: Window end (ISO date), or None for no upper bound.
        limit: Maximum number of analytes to return (clamped to 1–12).

    Returns:
        List of analyte summary dicts with keys: analyte, count, last_at, last_status,
        test_name.
    """
    where = f"collected_at IS NOT NULL AND {_LIVE_NOTE}"
    params: list = []
    if dfrom:
        where += " AND date(collected_at) >= date(?)"; params.append(dfrom)
    if dto:
        where += " AND date(collected_at) <= date(?)"; params.append(dto)
    rows = conn.execute(
        f"SELECT analyte_key, value_text, value_num, flag, ref_low, ref_high, collected_at "
        f"FROM lab_results WHERE {where}", params).fetchall()
    agg: dict[str, dict] = {}
    seen: set = set()                          # dedup identical re-exports (F2) — but key on FLAG too
    for r in rows:                             # so an 'H'-flagged row is never suppressed by an
        ident = (r["analyte_key"], r["collected_at"], r["value_text"], r["flag"])   # 'N' duplicate
        if ident in seen:
            continue
        seen.add(ident)
        if _status(r["flag"], r["value_num"], r["ref_low"], r["ref_high"]) not in ("high", "low", "abnormal"):
            continue
        a = agg.setdefault(r["analyte_key"], {"analyte": r["analyte_key"], "count": 0, "last_at": ""})
        a["count"] += 1
        if r["collected_at"] > a["last_at"]:
            a["last_at"] = r["collected_at"]
            a["last_status"] = _status(r["flag"], r["value_num"], r["ref_low"], r["ref_high"])
    out = sorted(agg.values(), key=lambda a: (a["last_at"], a["count"]), reverse=True)
    for a in out:
        a["test_name"] = _modal(conn, a["analyte"], "test_name") or a["analyte"]
    return out[: max(1, min(int(limit), 12))]


def series(conn, analyte: str, unit: str | None = None) -> dict:
    """Return one analyte's full series for chart rendering.

    Includes all points (including censored), stepped reference-band segments, date domain,
    value range, overlapping encounters, and any other units the analyte was recorded in.

    Args:
        conn: Database connection.
        analyte: Analyte slug key.
        unit: Target unit string to filter by; defaults to the modal unit when None.

    Returns:
        Dict with keys: analyte, test_name, unit, points, segments, domain, value_range,
        encounters, other_units.
    """
    name = _modal(conn, analyte, "test_name") or analyte
    target_unit = unit or _modal(conn, analyte, "unit")
    target_norm = _unit_norm(target_unit)
    rows = conn.execute(
        "SELECT lr.collected_at, lr.collected_time, lr.value_text, lr.value_num, lr.unit, lr.flag, lr.ref_low, "
        "       lr.ref_high, lr.ref_text, lr.source, lr.encounter_id, lr.note_id, n.slug AS note_slug, n.title AS note_title "
        "FROM lab_results lr LEFT JOIN notes n ON n.id = lr.note_id "
        "WHERE lr.analyte_key = ? AND lr.collected_at IS NOT NULL AND n.deleted_at IS NULL "
        "ORDER BY lr.collected_at, lr.collected_time, lr.id",   # collected_time orders twice-daily draws (P4)
        (analyte,)).fetchall()

    points: list[dict] = []
    seen: set = set()
    other: set = set()
    for r in rows:
        if _unit_norm(r["unit"]) != target_norm:
            if r["unit"]:
                other.add(r["unit"])
            continue
        key = (r["collected_at"], r["value_text"])
        if key in seen:                          # identical (date,value) from overlapping re-exports
            continue
        seen.add(key)
        points.append({
            "t": r["collected_at"], "time": r["collected_time"], "source": r["source"],
            "v": r["value_num"], "vtext": r["value_text"], "unit": r["unit"],
            "status": _status(r["flag"], r["value_num"], r["ref_low"], r["ref_high"]),
            "censored": r["value_num"] is None,
            "ref_low": r["ref_low"], "ref_high": r["ref_high"], "ref_text": r["ref_text"], "flag": r["flag"],
            "note_id": r["note_id"], "note_slug": r["note_slug"], "note_title": r["note_title"],
            "encounter_id": r["encounter_id"],
        })

    dates = [p["t"] for p in points]
    nums = [p["v"] for p in points if p["v"] is not None]
    return {
        "analyte": analyte, "test_name": name, "unit": target_unit,
        "points": points, "segments": _segments(points),
        "domain": {"from": dates[0], "to": dates[-1]} if dates else None,
        "value_range": {"min": min(nums), "max": max(nums)} if nums else None,
        "encounters": _encounters_in(conn, dates[0] if dates else None, dates[-1] if dates else None),
        "other_units": sorted(other),
    }


def series_from_results(results: list[dict], analyte: str, unit: str | None = None) -> dict:
    """Build the same series payload as series() from staged parser results (no DB).

    Used for previewing a lab import before it's approved. No flag, encounter, or note
    context is available in this path.

    Args:
        results: List of raw parser result dicts (from parse_lab_pdf or parse_lab_image).
        analyte: Analyte slug key to filter.
        unit: Target unit string; defaults to the modal unit among matching rows when None.

    Returns:
        Dict with the same shape as series(): analyte, test_name, unit, points, segments,
        domain, value_range, encounters (always []), other_units.
    """
    rows = [r for r in results if r.get("analyte_key") == analyte and r.get("collected_at")]
    rows.sort(key=lambda r: (r["collected_at"], r.get("collected_time") or ""))
    import collections
    name = (collections.Counter(r["test_name"] for r in rows if r.get("test_name")).most_common(1) or [(analyte,)])[0][0]
    units = collections.Counter(r["unit"] for r in rows if r.get("unit"))
    target_norm = _unit_norm(unit or (units.most_common(1)[0][0] if units else None))
    points: list[dict] = []
    seen: set = set()
    other: set = set()
    for r in rows:
        if _unit_norm(r.get("unit")) != target_norm:
            if r.get("unit"):
                other.add(r["unit"])
            continue
        key = (r["collected_at"], r["value_text"])
        if key in seen:
            continue
        seen.add(key)
        points.append({
            "t": r["collected_at"], "time": r.get("collected_time"), "source": r.get("source"),
            "v": r["value_num"], "vtext": r["value_text"], "unit": r["unit"],
            "status": _status(None, r["value_num"], r["ref_low"], r["ref_high"]),
            "censored": r["value_num"] is None,
            "ref_low": r["ref_low"], "ref_high": r["ref_high"], "ref_text": r.get("ref_text"), "flag": None,
            "note_id": None, "note_slug": None, "note_title": None, "encounter_id": None,
        })
    dates = [p["t"] for p in points]
    nums = [p["v"] for p in points if p["v"] is not None]
    return {
        "analyte": analyte, "test_name": name, "unit": unit or (units.most_common(1)[0][0] if units else None),
        "points": points, "segments": _segments(points),
        "domain": {"from": dates[0], "to": dates[-1]} if dates else None,
        "value_range": {"min": min(nums), "max": max(nums)} if nums else None,
        "encounters": [], "other_units": sorted(other),
    }


# --- Analytic reductions over a series (for the lab_stat / lab_value_at tools) -----------
# All of these run over series().points, so they inherit the chart's exact handling of units
# (equivalent-unit grouping), censored '<0.01' rows, flag-authoritative status, and the
# soft-deleted-note filter — and every result is a WHOLE ROW, so a value always carries its
# own date/status/ref-range (the fix for "value with no date").
_OUT = ("high", "low", "abnormal")


def _slim(p: dict | None) -> dict | None:
    """Project a full series point to the flat stat/point_at return schema.

    Args:
        p: Series point dict, or None.

    Returns:
        Flat dict with value, value_text, unit, collected_at, collected_time, source,
        status, ref_low, ref_high, ref_text, flag, note_slug, note_title; or None.
    """
    if p is None:
        return None
    return {"value": p["v"], "value_text": p["vtext"], "unit": p["unit"], "collected_at": p["t"],
            "collected_time": p.get("time"), "source": p.get("source"),
            "status": p["status"], "ref_low": p["ref_low"], "ref_high": p["ref_high"],
            "ref_text": p["ref_text"], "flag": p["flag"], "note_slug": p["note_slug"],
            "note_title": p["note_title"]}


def _window(points: list[dict], dfrom: str | None, dto: str | None) -> list[dict]:
    """Filter a series point list to an inclusive date window.

    Args:
        points: Ordered list of series point dicts.
        dfrom: Start of the window (ISO date), or None for no lower bound.
        dto: End of the window (ISO date), or None for no upper bound.

    Returns:
        Filtered list of points within [dfrom, dto].
    """
    return [p for p in points if (not dfrom or p["t"] >= dfrom) and (not dto or p["t"] <= dto)]


def stat(conn, analyte: str, unit: str | None = None, dfrom: str | None = None, dto: str | None = None) -> dict:
    """Return a scalar summary for one analyte over an optional date window.

    Covers min/max/latest/first/mean/count and the out-of-range count. Each extreme is the
    whole row (value + its date + status); numeric extremes exclude censored values, which
    are counted separately.

    Args:
        conn: Database connection.
        analyte: Analyte slug key.
        unit: Target unit string; defaults to modal unit when None.
        dfrom: Window start (ISO date), or None.
        dto: Window end (ISO date), or None.

    Returns:
        Dict with keys: analyte, test_name, unit, window, count, numeric_count,
        censored_count, out_of_range_count, min, min_dates, max, max_dates, latest,
        first, mean, other_units, domain.
    """
    s = series(conn, analyte, unit)
    pts = _window(s["points"], dfrom, dto)
    numeric = [p for p in pts if p["v"] is not None]
    mn = min(numeric, key=lambda p: p["v"]) if numeric else None   # ties -> earliest (pts are date-asc)
    mx = max(numeric, key=lambda p: p["v"]) if numeric else None
    ties_min = [p["t"] for p in numeric if mn and p["v"] == mn["v"]]
    ties_max = [p["t"] for p in numeric if mx and p["v"] == mx["v"]]
    return {
        "analyte": analyte, "test_name": s["test_name"], "unit": s["unit"],
        "window": {"from": dfrom, "to": dto} if (dfrom or dto) else None,
        "count": len(pts), "numeric_count": len(numeric),
        "censored_count": sum(1 for p in pts if p["censored"]),
        "out_of_range_count": sum(1 for p in pts if p["status"] in _OUT),
        "min": _slim(mn), "min_dates": ties_min, "max": _slim(mx), "max_dates": ties_max,
        "latest": _slim(pts[-1]) if pts else None, "first": _slim(pts[0]) if pts else None,
        "mean": round(sum(p["v"] for p in numeric) / len(numeric), 2) if numeric else None,
        "other_units": s["other_units"], "domain": s["domain"],
    }


def point_at(conn, analyte: str, which: str, *, unit: str | None = None, threshold: float | None = None,
             direction: str = "above", dfrom: str | None = None, dto: str | None = None) -> dict | None:
    """Return a single point selected from an analyte's series.

    Selector options: 'latest', 'first', 'first_out_of_range', 'last_out_of_range',
    'first_cross', 'last_cross' (cross requires threshold and direction).

    Args:
        conn: Database connection.
        analyte: Analyte slug key.
        which: Point selector; one of the options listed above.
        unit: Target unit string; defaults to modal unit when None.
        threshold: Threshold value for cross selectors.
        direction: 'above' or 'below' for cross selectors.
        dfrom: Window start (ISO date), or None.
        dto: Window end (ISO date), or None.

    Returns:
        Slim row dict (value + date + status), or None if no matching point exists.
    """
    pts = _window(series(conn, analyte, unit)["points"], dfrom, dto)
    if not pts:
        return None
    if which == "latest":
        return _slim(pts[-1])
    if which == "first":
        return _slim(pts[0])
    if which in ("first_out_of_range", "last_out_of_range"):
        oor = [p for p in pts if p["status"] in _OUT]
        return _slim((oor[0] if which.startswith("first") else oor[-1]) if oor else None)
    if which in ("first_cross", "last_cross") and threshold is not None:
        hit = [p for p in pts if p["v"] is not None and
               (p["v"] >= threshold if direction == "above" else p["v"] <= threshold)]
        return _slim((hit[0] if which.startswith("first") else hit[-1]) if hit else None)
    return None
