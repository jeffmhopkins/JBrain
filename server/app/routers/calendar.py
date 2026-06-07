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
    """Keep a user title on one line and out of marker syntax when it lands in a note
    body (so a crafted title can't inject a supersession marker)."""
    return (title or "").replace("\n", " ").replace("\r", " ").replace("[[", "[").replace("]]", "]").strip()


def _compose_starts(date: str, time: str | None) -> tuple[str, int]:
    """(starts_at, all_day) from a validated date + optional HH:MM. 422 on bad format."""
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
    """Live, future events (one-offs from v_upcoming + the next occurrence of each
    recurring series), soonest first. The read side for the calendar UI."""
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
    """LIVE events whose occurrence falls in [start, end] (inclusive) — the read side
    for the Day/Week/Month grids. One-offs + every recurring occurrence in the window,
    with TRUE times preserved (not forced all-day). Excludes superseded/cancelled rows
    (a reschedule/cancel is a supersession edge); includes done/tentative so a past
    month still shows what happened. Validates ISO + start<=end and clamps the span."""
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
    title: str
    date: str                       # YYYY-MM-DD
    time: str | None = None         # HH:MM (optional)
    kind: str = "event"
    detail: str | None = None
    reminders: list[dict] | None = None     # [{offset_minutes, anchor}] — optional


@router.post("/quick-add")
def quick_add(body: QuickAddIn):
    """Write a dated note for the event AND project its structured row (source='manual').
    The note is the durable record; the row is its (deterministic) projection. Optional
    per-event reminders are attached to the projected event's identity_key."""
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
    """The stable identity_key for an event row id (any kind), or 404."""
    row = conn.execute("SELECT identity_key FROM calendar_events WHERE id=?", (event_id,)).fetchone()
    if not row or not row["identity_key"]:
        raise HTTPException(status_code=404, detail="Event not found")
    return row["identity_key"]


class RemindersIn(BaseModel):
    reminders: list[dict] = []          # [{offset_minutes:int, anchor:'start'|'day_of'}]


@router.get("/events/{event_id}/reminders")
def get_event_reminders(event_id: int):
    conn = get_conn()
    return {"reminders": cal.get_reminders(conn, _event_ik(conn, event_id))}


@router.post("/events/{event_id}/reminders")
def set_event_reminders(event_id: int, body: RemindersIn):
    """Replace an event's reminders (config keyed by the stable identity_key)."""
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
    """Auto-created events added since the last review — feeds the in-calendar review banner."""
    conn = get_conn()
    since = get_meta(_REVIEW_WATERMARK, "", conn=conn) or ""
    return {"since": since, "events": cal.recently_added(conn, since)}


@router.post("/reviewed")
def mark_reviewed():
    """Advance the review watermark to now — clears the 'recently added' banner."""
    conn = get_conn()
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    set_meta(conn, _REVIEW_WATERMARK, now)
    conn.commit()
    return {"ok": True}


@router.post("/events/{event_id}/dismiss")
def dismiss_event_route(event_id: int):
    """Remove an event from the calendar WITHOUT writing a note: hide the row and stop
    re-derivation from re-creating it. Reversible via /undismiss with the returned
    identity_key."""
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
    identity_key: str


@router.post("/undismiss")
def undismiss_event_route(body: UndismissIn):
    """Undo a revoke — restore the snapshotted event and let extraction treat it normally."""
    conn = get_conn()
    try:
        out = cal.undismiss_event(conn, body.identity_key)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out
