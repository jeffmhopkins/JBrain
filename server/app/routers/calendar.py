"""Calendar API: read the upcoming/history projection, and the WRITE PATHS the
calendar UI uses — which always write NOTES (the source of truth), never edit the
sidecar directly.

- quick-add writes a dated note AND deterministically projects its structured event
  (source='manual'); the LLM extractor skips manual-projected notes, so there's no
  duplicate derivation.
- reschedule / cancel write a SUPERSEDING note carrying the structured
  `supersedes/cancels [[old note]] <date>` marker, then consolidate — exactly the
  re-derivation path the nightly workflow uses.
"""
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
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
        "SELECT e.id, e.title, e.kind, e.starts_at, e.rrule, e.exdate_json, e.note_id, "
        "n.title AS note_title, n.slug AS note_slug FROM calendar_events e "
        "LEFT JOIN notes n ON n.id = e.note_id "
        "WHERE e.kind='recurring' AND e.rrule IS NOT NULL AND e.status NOT IN ('cancelled','done') "
        "AND NOT EXISTS (SELECT 1 FROM calendar_supersedes s WHERE s.old_identity_key = e.identity_key)"
    ).fetchall():
        try:
            ex = _json.loads(r["exdate_json"] or "[]")
        except Exception:  # noqa: BLE001
            ex = []
        occ = cal.expand_rrule(r["rrule"], r["starts_at"] or today, today, horizon, exdates=ex)
        if occ:
            out.append({"id": r["id"], "title": r["title"], "kind": "recurring",
                        "starts_at": occ[0], "all_day": 1, "rrule": r["rrule"],
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
        "FROM v_event_history LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


class QuickAddIn(BaseModel):
    title: str
    date: str                       # YYYY-MM-DD
    time: str | None = None         # HH:MM (optional)
    kind: str = "event"
    detail: str | None = None


@router.post("/quick-add")
def quick_add(body: QuickAddIn):
    """Write a dated note for the event AND project its structured row (source='manual').
    The note is the durable record; the row is its (deterministic) projection."""
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    ev = conn.execute(
        "SELECT id, title, kind, starts_at, all_day, status FROM calendar_events "
        "WHERE note_id=? ORDER BY id DESC LIMIT 1", (note_id,),
    ).fetchone()
    return {"note_id": note_id, "note_title": note_title, "event": dict(ev) if ev else None}


def _load_event(conn, event_id: int) -> dict:
    row = conn.execute(
        "SELECT e.id, e.title, e.kind, e.starts_at, e.all_day, e.identity_key, e.note_id, "
        "n.title AS note_title FROM calendar_events e JOIN notes n ON n.id = e.note_id WHERE e.id = ?",
        (event_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    if not row["starts_at"]:
        raise HTTPException(status_code=422, detail="Event has no date to supersede")
    if row["kind"] == "recurring":
        # A recurring series can't have a single occurrence rescheduled/cancelled yet
        # (would need per-instance EXDATE handling); retiring it would kill the series.
        raise HTTPException(status_code=422,
                            detail="Can't reschedule/cancel one occurrence of a recurring series yet")
    return dict(row)


class RescheduleIn(BaseModel):
    to_date: str
    to_time: str | None = None


@router.post("/events/{event_id}/reschedule")
def reschedule(event_id: int, body: RescheduleIn):
    """Write a SUPERSEDING note (marker + the new occurrence) and consolidate — the old
    event is retired and the new one becomes live, all via notes."""
    conn = get_conn()
    ev = _load_event(conn, event_id)
    old_date = ev["starts_at"][:10]
    new_starts, new_all_day = _compose_starts(body.to_date, body.to_time)
    # Human-readable record in the note (best-effort marker); the edge below is recorded
    # DIRECTLY by identity_key so it's robust even if the old note's title is unusual.
    marker = f"supersedes [[{_sanitize_title(ev['note_title'])}]] {old_date}"
    body_md = f"Rescheduled {_sanitize_title(ev['title'])} from {old_date} to {new_starts.replace('T', ' ')}.\n\n{marker}"
    try:
        note_title = notes_svc.next_dated_title(conn, clock.today_local())
        new_note_id = notes_svc.upsert_note(conn, note_title, body_md, source="user", fire_events=False)
        cal.upsert_events(conn, new_note_id, [{
            "title": ev["title"], "kind": ev["kind"], "starts_at": new_starts,
            "all_day": bool(new_all_day),
        }], source="manual", sweep=False)
        new_ik = cal.identity_key(new_note_id, ev["title"], ev["kind"], 0)
        cal.record_supersession(conn, ev["identity_key"], new_ik, new_note_id, "structured")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"note_id": new_note_id, "superseded_event_id": event_id, "new_starts_at": new_starts}


@router.post("/events/{event_id}/cancel")
def cancel(event_id: int):
    """Write a SUPERSEDING (cancellation) note and consolidate — the event leaves the
    upcoming view, its history preserved."""
    conn = get_conn()
    ev = _load_event(conn, event_id)
    old_date = ev["starts_at"][:10]
    marker = f"cancels [[{_sanitize_title(ev['note_title'])}]] {old_date}"
    body_md = f"Cancelled {_sanitize_title(ev['title'])} on {old_date}.\n\n{marker}"
    try:
        note_title = notes_svc.next_dated_title(conn, clock.today_local())
        new_note_id = notes_svc.upsert_note(conn, note_title, body_md, source="user", fire_events=False)
        # Direct edge (no replacement = cancellation), robust to the old note's title.
        cal.record_supersession(conn, ev["identity_key"], None, new_note_id, "structured")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"note_id": new_note_id, "cancelled_event_id": event_id}
