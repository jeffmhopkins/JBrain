"""Scope enforcement for LAB-SHARE links — THE security boundary for recipient-facing labs.

A lab-share link exposes ONLY an owner-approved analyte ALLOW-LIST, clamped to an owner date
WINDOW, with every brain-identity field STRIPPED. This module is the single place a recipient
(or the recipient AI) can read lab data: it borrows lab_series for the math but never lets an
out-of-allowlist analyte, a note id/slug/title, or an encounter label (facility/dates) reach a
recipient.

Read-only != scoped: lab_series.series() reads `lab_results` globally. Scope is enforced HERE,
per call, DEFAULT-DENY — an analyte not in `allowed` returns nothing, an absent/empty allow-list
exposes nothing. The recipient AI module imports ONLY this module (never lab_series, architect,
notes, or query_sql), so the scope holds even if the recipient model is fully adversary-controlled.
Keep this small and well-tested — it's the crown-jewel boundary.
"""
from __future__ import annotations

from . import lab_series

# The ONLY tools a recipient assistant may ever hold — a hardcoded set, NEVER sourced from
# prompts.yaml, so a config edit can't widen it.
RECIPIENT_TOOLS: tuple[str, ...] = ("list_abnormal_labs", "show_lab_chart", "lab_stat", "lab_value_at")

# Tools that must NEVER be reachable by a recipient (defense-in-depth: asserted at startup).
FORBIDDEN_RECIPIENT_TOOLS = frozenset({
    "query_sql", "read_note", "read_notes", "search_notes", "search", "read_attachment",
    "propose_actions", "apply_actions", "create_research_share", "create_labs_share",
    "show_note", "list_notes", "graph",
})

# Identity / brain-structure fields that must never reach a recipient.
_STRIP_POINT = ("note_id", "note_slug", "note_title", "encounter_id")
_STRIP_ROW = ("note_slug", "note_title")


def assert_recipient_tools_safe() -> None:
    """Check at startup that the recipient tool set leaks no forbidden owner tools.

    This is a release-blocker guard: raises RuntimeError if any forbidden tool appears
    in RECIPIENT_TOOLS.

    Raises:
        RuntimeError: If RECIPIENT_TOOLS intersects FORBIDDEN_RECIPIENT_TOOLS.
    """
    bad = set(RECIPIENT_TOOLS) & FORBIDDEN_RECIPIENT_TOOLS
    if bad:
        raise RuntimeError(f"lab-share recipient tool set leaks forbidden tools: {sorted(bad)}")


def _allow(allowed) -> set[str]:
    """Normalize the allow-list to a set of non-empty strings.

    Args:
        allowed: Iterable of analyte slugs, or None.

    Returns:
        Set of non-empty analyte slug strings.
    """
    return {a for a in (allowed or ()) if a}


def _scrub_point(p: dict) -> dict:
    """Remove identity fields from a series point dict.

    Args:
        p: Series point dict.

    Returns:
        Copy of p with note_id, note_slug, note_title, and encounter_id removed.
    """
    return {k: v for k, v in p.items() if k not in _STRIP_POINT}


def _scrub_row(r: dict | None) -> dict | None:
    """Remove identity fields from a stat/point_at row dict.

    Args:
        r: Flat row dict, or None.

    Returns:
        Copy of r with note_slug and note_title removed, or None if r is None.
    """
    return None if r is None else {k: v for k, v in r.items() if k not in _STRIP_ROW}


def series_scoped(conn, analyte, *, allowed, dfrom=None, dto=None, unit=None) -> dict | None:
    """Return one analyte's series, only if it is in the allow-list.

    Clamped to [dfrom, dto], identity-stripped, and with encounter labels removed (they
    leak facility names and admission dates). domain, value_range, and segments are
    recomputed from the windowed points so the recipient can't pan past the owner's window.
    Default-deny: returns None for any out-of-scope analyte.

    Args:
        conn: Database connection.
        analyte: Analyte slug to retrieve.
        allowed: Set of allowed analyte slugs.
        dfrom: Window start (ISO date), or None.
        dto: Window end (ISO date), or None.
        unit: Target unit string; defaults to modal unit when None.

    Returns:
        Scoped series dict, or None if the analyte is not in the allow-list.
    """
    if analyte not in _allow(allowed):
        return None
    s = lab_series.series(conn, analyte, unit)
    pts = [_scrub_point(p) for p in lab_series._window(s["points"], dfrom, dto)]
    dates = [p["t"] for p in pts]
    nums = [p["v"] for p in pts if p["v"] is not None]
    return {
        "analyte": s["analyte"], "test_name": s["test_name"], "unit": s["unit"],
        "points": pts, "segments": lab_series._segments(pts),
        "domain": {"from": dates[0], "to": dates[-1]} if dates else None,
        "value_range": {"min": min(nums), "max": max(nums)} if nums else None,
        "encounters": [],                            # facility/summary/dates leak — never share
        "other_units": s["other_units"],
    }


def list_analytes_scoped(conn, allowed) -> list[dict]:
    """Return the analyte picklist limited to the allow-list.

    lab_series.list_analytes carries no identity fields, so no additional scrubbing is
    needed beyond the allow-list filter.

    Args:
        conn: Database connection.
        allowed: Set of allowed analyte slugs.

    Returns:
        Filtered list of analyte dicts from lab_series.list_analytes.
    """
    al = _allow(allowed)
    return [a for a in lab_series.list_analytes(conn) if a["analyte"] in al]


def abnormal_scoped(conn, allowed, dfrom=None, dto=None, limit=8) -> list[dict]:
    """Return out-of-range analytes filtered to the allow-list.

    Ensures 'what's abnormal' can never enumerate an analyte the owner didn't approve.

    Args:
        conn: Database connection.
        allowed: Set of allowed analyte slugs.
        dfrom: Window start (ISO date), or None.
        dto: Window end (ISO date), or None.
        limit: Maximum number of analytes to return.

    Returns:
        Filtered list of abnormal analyte dicts from lab_series.abnormal_analytes.
    """
    al = _allow(allowed)
    return [a for a in lab_series.abnormal_analytes(conn, dfrom, dto, limit=limit) if a["analyte"] in al]


def stat_scoped(conn, analyte, *, allowed, unit=None, dfrom=None, dto=None) -> dict | None:
    """Return a scalar summary for one analyte, only if it is in the allow-list.

    The min/max/latest/first extremes have their note id and title stripped.

    Args:
        conn: Database connection.
        analyte: Analyte slug.
        allowed: Set of allowed analyte slugs.
        unit: Target unit string; defaults to modal unit when None.
        dfrom: Window start (ISO date), or None.
        dto: Window end (ISO date), or None.

    Returns:
        Scoped stat dict, or None if the analyte is not in the allow-list.
    """
    if analyte not in _allow(allowed):
        return None
    st = lab_series.stat(conn, analyte, unit, dfrom, dto)
    for key in ("min", "max", "latest", "first"):
        st[key] = _scrub_row(st.get(key))
    return st


def point_at_scoped(conn, analyte, which, *, allowed, **kw) -> dict | None:
    """Return a single selected point, only if the analyte is in the allow-list.

    Note id and title are stripped from the result.

    Args:
        conn: Database connection.
        analyte: Analyte slug.
        which: Point selector (see lab_series.point_at for valid values).
        allowed: Set of allowed analyte slugs.
        **kw: Additional keyword arguments forwarded to lab_series.point_at.

    Returns:
        Scoped slim row dict, or None if the analyte is not in the allow-list or no
        matching point exists.
    """
    if analyte not in _allow(allowed):
        return None
    return _scrub_row(lab_series.point_at(conn, analyte, which, **kw))
