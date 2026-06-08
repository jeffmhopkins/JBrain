"""Calendar API: read the upcoming/history projection, plus the two write actions the
calendar UI exposes — Add to calendar and Remove from calendar.

- Add (quick-add) writes a dated note AND deterministically projects its structured
  event (source='manual'); the LLM extractor skips manual-projected notes, so there's
  no duplicate derivation.
- Remove (dismiss) revokes an event from the calendar WITHOUT writing a note — it hides
  the row and stops re-derivation from re-creating it. Reversible via /undismiss.

Rescheduling and cancelling are deliberately NOT calendar actions: those are changes to
the record, so the owner makes them in the notes themselves (write a superseding/cancel
note + consolidation), and the nightly re-derivation reflects them here.
"""
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn, get_meta, set_meta
from ..services import calendar as cal
from ..services import clock
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[CurrentUser])


import re as _re

_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _sanitize_title(title: str) -> str:
    """Sanitize a user-supplied title for safe embedding in a note body.

    Strips newlines and collapses ``[[...]]`` so a crafted title cannot inject
    a supersession marker.

    Args:
        title: Raw user-supplied title string.

    Returns:
        Single-line, stripped title safe for note body insertion.
    """
    return (title or "").replace("\n", " ").replace("\r", " ").replace("[[", "[").replace("]]", "]").strip()


def _compose_starts(date: str, time: str | None) -> tuple[str, int]:
    """Build a ``(starts_at, all_day)`` pair from a validated date and optional time.

    Args:
        date: Date string in ``YYYY-MM-DD`` format.
        time: Optional time string in ``HH:MM`` (24 h) format.

    Returns:
        Tuple of ``(starts_at, all_day)`` where ``starts_at`` is either
        ``"YYYY-MM-DD"`` (all-day) or ``"YYYY-MM-DDTHH:MM:00"`` (timed), and
        ``all_day`` is 1 or 0 accordingly.

    Raises:
        HTTPException: 422 if ``date`` or ``time`` does not match the expected format.
    """
    date = (date or "").strip()[:10]
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    t = (time or "").strip()
    if t:
        if not _TIME_RE.match(t):
            raise HTTPException(status_code=422, detail="time must be HH:MM (24h)")
        return f"{date}T{t}:00", 0
    return date, 1


@router.get("/upcoming")
def upcoming(within_days: int = 90, limit: int = 100):
    """Return live upcoming events, soonest first.

    Combines one-offs from ``v_upcoming`` with the next occurrence of each
    recurring series. This is the read side for the calendar UI.

    Args:
        within_days: Look-ahead window in days (1–3650, default 90).
        limit: Maximum number of events to return (1–500, default 100).

    Returns:
        List of event dicts ordered by ``starts_at``.
    """
    conn = get_conn()
    within = max(1, min(int(within_days or 90), 3650))
    limit = max(1, min(int(limit or 100), 500))
    horizon = (clock.today_local() + timedelta(days=within)).isoformat()
    today = clock.today_iso()
    out: list[dict] = []
    for r in conn.execute(
        "SELECT id, title, kind, starts_at, ends_at, all_day, status, location_label, "
        "note_id, note_title, note_slug FROM v_upcoming "
        "WHERE kind != 'recurring' AND (starts_at IS NULL OR date(starts_at) <= ?) "
        "ORDER BY starts_at LIMIT 500",
        (horizon,),
    ).fetchall():
        d = dict(r)
        d["recurring"] = False
        out.append(d)
    # Recurring: expand each live series to its next occurrence within the window.
    import json as _json
    for r in conn.execute(
        "SELECT e.id, e.title, e.kind, e.starts_at, e.all_day, e.rrule, e.exdate_json, e.note_id, "
        "n.title AS note_title, n.slug AS note_slug FROM calendar_events e "
        "LEFT JOIN notes n ON n.id = e.note_id "
        "WHERE e.kind='recurring' AND e.rrule IS NOT NULL AND e.starts_at IS NOT NULL "
        "AND e.status NOT IN ('cancelled','done') "
        "AND NOT EXISTS (SELECT 1 FROM calendar_supersedes s WHERE s.old_identity_key = e.identity_key)"
    ).fetchall():
        try:
            ex = _json.loads(r["exdate_json"] or "[]")
        except Exception:  # noqa: BLE001
            ex = []
        occ = cal.expand_rrule(r["rrule"], r["starts_at"], today, horizon, exdates=ex)
        if occ:
            out.append({"id": r["id"], "title": r["title"], "kind": "recurring",
                        "starts_at": occ[0], "all_day": r["all_day"], "rrule": r["rrule"],
                        "note_id": r["note_id"], "note_title": r["note_title"],
                        "note_slug": r["note_slug"], "recurring": True})
    out.sort(key=lambda d: d.get("starts_at") or "9999")
    return out[:limit]


@router.get("/history")
def history(limit: int = 100):
    """Return past events in reverse chronological order.

    Args:
        limit: Maximum number of events to return (1–500, default 100).

    Returns:
        List of event dicts ordered by ``starts_at`` descending.
    """
    conn = get_conn()
    limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        "SELECT id, title, kind, starts_at, all_day, status, note_id, note_title, note_slug "
        "FROM v_event_history ORDER BY starts_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


_MAX_RANGE_DAYS = 366
_MAX_RANGE_RESULTS = 2000   # bound the payload even with many recurring series


@router.get("/range")
def range_events(start: str, end: str):
    """Return all event occurrences within a date range, inclusive.

    The read side for Day/Week/Month grids. Returns one-offs and every
    recurring occurrence in the window with true times preserved. Excludes
    superseded/cancelled rows; includes done/tentative so past months still
    show what happened.

    Args:
        start: Range start date in ``YYYY-MM-DD`` format.
        end: Range end date in ``YYYY-MM-DD`` format (inclusive).

    Returns:
        List of event dicts ordered by ``starts_at``, capped at 2000 entries.

    Raises:
        HTTPException: 422 if dates are not ISO format, ``start > end``, or
            the range exceeds 366 days.
    """
    start = (start or "").strip()[:10]
    end = (end or "").strip()[:10]
    if not _DATE_RE.match(start) or not _DATE_RE.match(end):
        raise HTTPException(status_code=422, detail="start and end must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    from datetime import date as _date
    if (_date.fromisoformat(end) - _date.fromisoformat(start)).days > _MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"range may not exceed {_MAX_RANGE_DAYS} days")

    conn = get_conn()
    out: list[dict] = []
    # One-offs windowed by start date. Excludes superseded/rescheduled-away rows (the
    # edge) AND directly status='cancelled' rows, so the grid agrees with the live List
    # view (v_upcoming); keeps 'done'/'tentative' so a past month shows what happened.
    # NOTE (v1 limitation, per the hybrid plan): multi-day events are windowed by their
    # START day only — spanning bars across days are deferred.
    for r in conn.execute(
        "SELECT e.id, e.title, e.kind, e.starts_at, e.ends_at, e.all_day, e.status, "
        "e.location_label, e.note_id, n.title AS note_title, n.slug AS note_slug "
        "FROM calendar_events e LEFT JOIN notes n ON n.id = e.note_id "
        "WHERE e.kind != 'recurring' AND e.starts_at IS NOT NULL AND e.status != 'cancelled' "
        "AND date(e.starts_at) BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM calendar_supersedes s WHERE s.old_identity_key = e.identity_key) "
        "ORDER BY e.starts_at",
        (start, end),
    ).fetchall():
        d = dict(r)
        d["recurring"] = False
        out.append(d)

    import json as _json
    for r in conn.execute(
        "SELECT e.id, e.title, e.kind, e.starts_at, e.ends_at, e.all_day, e.status, e.rrule, "
        "e.exdate_json, e.location_label, e.note_id, n.title AS note_title, n.slug AS note_slug "
        "FROM calendar_events e LEFT JOIN notes n ON n.id = e.note_id "
        "WHERE e.kind='recurring' AND e.rrule IS NOT NULL AND e.starts_at IS NOT NULL "
        "AND e.status NOT IN ('cancelled','done') "
        "AND NOT EXISTS (SELECT 1 FROM calendar_supersedes s WHERE s.old_identity_key = e.identity_key)"
    ).fetchall():
        try:
            ex = _json.loads(r["exdate_json"] or "[]")
        except Exception:  # noqa: BLE001
            ex = []
        for occ in cal.expand_rrule(r["rrule"], r["starts_at"], start, end, exdates=ex):
            out.append({"id": r["id"], "title": r["title"], "kind": "recurring",
                        "starts_at": occ, "ends_at": None, "all_day": r["all_day"],
                        "status": r["status"] or "confirmed", "location_label": r["location_label"],
                        "note_id": r["note_id"], "note_title": r["note_title"],
                        "note_slug": r["note_slug"], "recurring": True})
    out.sort(key=lambda d: d.get("starts_at") or "9999")
    return out[:_MAX_RANGE_RESULTS]


class QuickAddIn(BaseModel):
    """Input schema for the quick-add calendar endpoint."""

    title: str
    date: str                       # YYYY-MM-DD
    time: str | None = None         # HH:MM (optional)
    kind: str = "event"
    detail: str | None = None
    reminders: list[dict] | None = None     # [{offset_minutes, anchor}] — optional


@router.post("/quick-add")
def quick_add(body: QuickAddIn):
    """Create a dated note and project its structured calendar event.

    Writes a dated note for the event and deterministically projects its
    structured row with ``source='manual'``. The note is the durable record;
    the event row is its projection. The LLM extractor skips manual-projected
    notes so there is no duplicate derivation. Optional per-event reminders
    are attached via the projected event's identity_key.

    Args:
        body: Event details including title, date, optional time/kind/detail,
            and optional reminder list.

    Returns:
        JSON with ``note_id``, ``note_title``, and the created ``event`` dict.

    Raises:
        HTTPException: 422 if title is empty or date/time format is invalid.
    """
    title = _sanitize_title(body.title)
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    starts_at, all_day = _compose_starts(body.date, body.time)
    when = starts_at.replace("T", " ")
    conn = get_conn()
    note_title = notes_svc.next_dated_title(conn, clock.today_local())
    detail = _sanitize_title(body.detail) if body.detail else None
    line = f"{title} — {when}" + (f"\n\n{detail}" if detail else "")
    try:
        note_id = notes_svc.upsert_note(conn, note_title, line, source="user", fire_events=False)
        cal.upsert_events(conn, note_id, [{
            "title": title, "kind": body.kind, "starts_at": starts_at,
            "all_day": bool(all_day), "detail": detail,
        }], source="manual")
        if body.reminders:
            ik = cal.identity_key(note_id, title, body.kind, 0)
            cal.set_reminders(conn, ik, body.reminders)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    ev = conn.execute(
        "SELECT id, title, kind, starts_at, all_day, status FROM calendar_events "
        "WHERE note_id=? ORDER BY id DESC LIMIT 1", (note_id,),
    ).fetchone()
    return {"note_id": note_id, "note_title": note_title, "event": dict(ev) if ev else None}


_REVIEW_WATERMARK = "calendar.review:seen"


def _event_ik(conn, event_id: int) -> str:
    """Return the stable identity_key for an event row.

    Args:
        conn: Active DB connection.
        event_id: Primary key of the calendar_events row.

    Returns:
        The non-null ``identity_key`` string for the event.

    Raises:
        HTTPException: 404 if the event does not exist or has no identity_key.
    """
    row = conn.execute("SELECT identity_key FROM calendar_events WHERE id=?", (event_id,)).fetchone()
    if not row or not row["identity_key"]:
        raise HTTPException(status_code=404, detail="Event not found")
    return row["identity_key"]


class RemindersIn(BaseModel):
    """Input schema for setting event reminders."""

    reminders: list[dict] = []          # [{offset_minutes:int, anchor:'start'|'day_of'}]


@router.get("/events/{event_id}/reminders")
def get_event_reminders(event_id: int):
    """Return the reminders configured for a calendar event.

    Args:
        event_id: Primary key of the calendar event.

    Returns:
        JSON ``{"reminders": [...]}`` with each reminder's offset and anchor.

    Raises:
        HTTPException: 404 if the event does not exist.
    """
    conn = get_conn()
    return {"reminders": cal.get_reminders(conn, _event_ik(conn, event_id))}


@router.post("/events/{event_id}/reminders")
def set_event_reminders(event_id: int, body: RemindersIn):
    """Replace all reminders for a calendar event.

    Configuration is keyed by the event's stable identity_key so reminders
    survive rescheduling.

    Args:
        event_id: Primary key of the calendar event.
        body: New reminder list; an empty list clears all reminders.

    Returns:
        Updated reminder configuration from the service layer.

    Raises:
        HTTPException: 404 if the event does not exist.
    """
    conn = get_conn()
    ik = _event_ik(conn, event_id)
    try:
        out = cal.set_reminders(conn, ik, body.reminders)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


@router.get("/recently-added")
def recently_added():
    """Return auto-created events added since the last review.

    Feeds the in-calendar review banner so the owner knows what the AI added.

    Returns:
        JSON with ``since`` (watermark ISO string) and ``events`` (list).
    """
    conn = get_conn()
    since = get_meta(_REVIEW_WATERMARK, "", conn=conn) or ""
    return {"since": since, "events": cal.recently_added(conn, since)}


@router.post("/reviewed")
def mark_reviewed():
    """Advance the review watermark to now, clearing the recently-added banner.

    Returns:
        JSON ``{"ok": true}``.
    """
    conn = get_conn()
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    set_meta(conn, _REVIEW_WATERMARK, now)
    conn.commit()
    return {"ok": True}


@router.post("/events/{event_id}/dismiss")
def dismiss_event_route(event_id: int):
    """Remove an event from the calendar without writing a note.

    Hides the row and prevents re-derivation from re-creating it. Reversible
    via ``POST /undismiss`` using the returned identity_key.

    Args:
        event_id: Primary key of the calendar event to dismiss.

    Returns:
        Dict including the event's ``identity_key`` needed to undo the action.

    Raises:
        HTTPException: 404 if the event does not exist.
    """
    conn = get_conn()
    ik = _event_ik(conn, event_id)
    try:
        out = cal.dismiss_event(conn, ik)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


class UndismissIn(BaseModel):
    """Input schema for undismissing a previously dismissed calendar event."""

    identity_key: str


@router.post("/undismiss")
def undismiss_event_route(body: UndismissIn):
    """Undo a dismiss — restore the snapshotted event and allow normal re-extraction.

    Args:
        body: ``{"identity_key": str}`` identifying the event to restore.

    Returns:
        Restored event dict from the service layer.
    """
    conn = get_conn()
    try:
        out = cal.undismiss_event(conn, body.identity_key)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out
