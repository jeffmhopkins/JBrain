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


def _compose_starts(date: str, time: str | None) -> tuple[str, int]:
    """(starts_at, all_day) from a date + optional HH:MM."""
    date = (date or "").strip()[:10]
    t = (time or "").strip()
    if t:
        return f"{date}T{t[:5]}:00", 0
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
        "WHERE kind != 'recurring' AND (starts_at IS NULL OR date(starts_at) <= ?) LIMIT 500",
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
    title = (body.title or "").strip()
    date = (body.date or "").strip()[:10]
    if not title or len(date) != 10:
        raise HTTPException(status_code=422, detail="title and a YYYY-MM-DD date are required")
    starts_at, all_day = _compose_starts(date, body.time)
    when = starts_at.replace("T", " ")
    conn = get_conn()
    note_title = notes_svc.next_dated_title(conn, clock.today_local())
    line = f"{title} — {when}" + (f"\n\n{body.detail.strip()}" if body.detail else "")
    try:
        note_id = notes_svc.upsert_note(conn, note_title, line, source="user", fire_events=False)
        cal.upsert_events(conn, note_id, [{
            "title": title, "kind": body.kind, "starts_at": starts_at,
            "all_day": bool(all_day), "detail": body.detail,
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
        "SELECT e.id, e.title, e.kind, e.starts_at, e.all_day, e.note_id, n.title AS note_title "
        "FROM calendar_events e JOIN notes n ON n.id = e.note_id WHERE e.id = ?", (event_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    if not row["starts_at"]:
        raise HTTPException(status_code=422, detail="Event has no date to supersede")
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
    to_date = (body.to_date or "").strip()[:10]
    if len(to_date) != 10:
        raise HTTPException(status_code=422, detail="to_date (YYYY-MM-DD) is required")
    new_starts, new_all_day = _compose_starts(to_date, body.to_time)
    marker = f"supersedes [[{ev['note_title']}]] {old_date}"
    body_md = (f"Rescheduled {ev['title']} from {old_date} to {new_starts.replace('T', ' ')}.\n\n{marker}")
    try:
        note_title = notes_svc.next_dated_title(conn, clock.today_local())
        new_note_id = notes_svc.upsert_note(conn, note_title, body_md, source="user", fire_events=False)
        cal.upsert_events(conn, new_note_id, [{
            "title": ev["title"], "kind": ev["kind"], "starts_at": new_starts,
            "all_day": bool(new_all_day),
        }], source="manual", sweep=False)
        cal.consolidate(conn, [{"id": new_note_id, "content_md": body_md}])
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
    marker = f"cancels [[{ev['note_title']}]] {old_date}"
    body_md = f"Cancelled {ev['title']} on {old_date}.\n\n{marker}"
    try:
        note_title = notes_svc.next_dated_title(conn, clock.today_local())
        new_note_id = notes_svc.upsert_note(conn, note_title, body_md, source="user", fire_events=False)
        cal.consolidate(conn, [{"id": new_note_id, "content_md": body_md}])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"note_id": new_note_id, "cancelled_event_id": event_id}
