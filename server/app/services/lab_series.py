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


def _unit_norm(unit: str | None) -> str:
    s = re.sub(r"\s+", "", (unit or "").lower())
    return _UNIT_ALIASES.get(s, s)


def _status(flag: str | None, vnum, low, high) -> str:
    """Point status with the lab's flag authoritative: high|low|normal|abnormal|unknown."""
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
    """The most common non-null value of a column for an analyte (col is a fixed identifier)."""
    r = conn.execute(
        f"SELECT {col} AS v, COUNT(*) AS c FROM lab_results "
        f"WHERE analyte_key = ? AND {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC, {col} LIMIT 1",
        (analyte,)).fetchone()
    return r["v"] if r else None


def list_analytes(conn) -> list[dict]:
    """One entry per analyte that has dated results: display name (modal test_name), unit,
    point count, date span, and the latest value + its status — for the picker."""
    rows = conn.execute(
        "SELECT analyte_key, COUNT(*) AS n, MIN(collected_at) AS first_at, MAX(collected_at) AS last_at "
        "FROM lab_results WHERE analyte_key IS NOT NULL AND collected_at IS NOT NULL "
        "GROUP BY analyte_key").fetchall()
    out = []
    for r in rows:
        latest = conn.execute(
            "SELECT value_text, value_num, flag, ref_low, ref_high FROM lab_results "
            "WHERE analyte_key = ? AND collected_at IS NOT NULL ORDER BY collected_at DESC, id DESC LIMIT 1",
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
    """Stepped reference band: a segment per contiguous run of points sharing (low, high).
    A point with no range (both None) breaks the band — never borrow a neighbour's range."""
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
    """Analytes with >=1 non-censored result whose status is high/low/abnormal in the window,
    ranked most-recently-abnormal first (then by count). Reads lab_results directly and uses
    the SAME _status the chart colours by — so 'what was abnormal' and the chart always agree.
    A censored row counts only if the lab itself flagged it (else it's unknown, never swept in)."""
    where = "collected_at IS NOT NULL"
    params: list = []
    if dfrom:
        where += " AND date(collected_at) >= date(?)"; params.append(dfrom)
    if dto:
        where += " AND date(collected_at) <= date(?)"; params.append(dto)
    rows = conn.execute(
        f"SELECT analyte_key, value_num, flag, ref_low, ref_high, collected_at "
        f"FROM lab_results WHERE {where}", params).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
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
    """One analyte's full series: points (incl. censored), stepped band segments, the date
    domain + value range, overlapping encounters, and any other units it was recorded in."""
    name = _modal(conn, analyte, "test_name") or analyte
    target_unit = unit or _modal(conn, analyte, "unit")
    target_norm = _unit_norm(target_unit)
    rows = conn.execute(
        "SELECT lr.collected_at, lr.value_text, lr.value_num, lr.unit, lr.flag, lr.ref_low, "
        "       lr.ref_high, lr.ref_text, lr.encounter_id, lr.note_id, n.slug AS note_slug, n.title AS note_title "
        "FROM lab_results lr LEFT JOIN notes n ON n.id = lr.note_id "
        "WHERE lr.analyte_key = ? AND lr.collected_at IS NOT NULL ORDER BY lr.collected_at, lr.id",
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
            "t": r["collected_at"], "v": r["value_num"], "vtext": r["value_text"], "unit": r["unit"],
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
