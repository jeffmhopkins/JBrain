"""Phase 1 — calendar (temporal projection) tests.

Deterministic where possible (migration, identity_key, upsert/sweep, supersession,
rrule expansion, the views); the one LLM seam (classify_dates / the extract_events
action) is exercised with a stubbed llm.complete. Future-dated rows use far-future
years (2099) so v_upcoming assertions don't depend on the wall clock.
"""
import json
import os
import tempfile
from datetime import datetime

import pytest

TEST_KEY = "k" * 40


@pytest.fixture()
def conn(monkeypatch):
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services import embeddings
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    from app.db import get_conn
    return get_conn()


def _mknote(conn, title, body="", *, dates=None, kind="entry", updated_at=None):
    """Insert a real note (FKs are enforced) + optionally its note_analysis dates."""
    slug = title.lower().replace(" ", "-").replace("/", "-")
    cur = conn.execute(
        "INSERT INTO notes (title, slug, content_md, kind) VALUES (?,?,?,?)",
        (title, slug, body, kind),
    )
    nid = cur.lastrowid
    if updated_at is not None:
        conn.execute("UPDATE notes SET updated_at=? WHERE id=?", (updated_at, nid))
    if dates is not None:
        conn.execute(
            "INSERT INTO note_analysis (note_id, content_hash, dates_json) VALUES (?,?,?)",
            (nid, "h", json.dumps(dates)),
        )
    return nid


def _count(conn, note_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM calendar_events WHERE note_id=?", (note_id,)
    ).fetchone()["c"]


# --- schema / migration -----------------------------------------------------

def test_schema_objects_exist_and_version(conn):
    from app.db import SCHEMA_VERSION, get_meta
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('calendar_events','calendar_supersedes','calendar_fired','v_upcoming','v_event_history')"
    )}
    assert names == {"calendar_events", "calendar_supersedes", "calendar_fired",
                     "v_upcoming", "v_event_history"}
    assert int(get_meta("schema_version")) == SCHEMA_VERSION == 45


def test_migration_recreates_calendar_from_v44(conn):
    """Dropping the calendar objects and re-running migrations from v44 restores them —
    proves the `if current < 45` block + _CALENDAR_SCHEMA_SQL upgrade path."""
    from app.db import _run_migrations, set_meta

    def _cols():  # column set of calendar_events as currently defined
        return {r["name"] for r in conn.execute("PRAGMA table_info(calendar_events)")}
    fresh_cols = _cols()   # from schema.sql

    conn.executescript(
        "DROP VIEW v_upcoming; DROP VIEW v_event_history; "
        "DROP TABLE calendar_supersedes; DROP TABLE calendar_fired; DROP TABLE calendar_events;"
    )
    set_meta(conn, "schema_version", "44")
    _run_migrations(conn)   # rebuilds from _CALENDAR_SCHEMA_SQL
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'calendar%' OR name IN ('v_upcoming','v_event_history')"
    )}
    assert {"calendar_events", "calendar_supersedes", "calendar_fired",
            "v_upcoming", "v_event_history"} <= names
    # schema.sql and the factored migration constant must stay in lockstep.
    assert _cols() == fresh_cols


# --- identity_key / upsert --------------------------------------------------

def test_identity_key_excludes_date_so_edit_moves(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Appt note")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "kind": "appointment", "starts_at": "2099-06-14"}])
    assert _count(conn, nid) == 1
    # Re-extract with a MOVED date — must update in place, not duplicate.
    cal.upsert_events(conn, nid, [{"title": "Dentist", "kind": "appointment", "starts_at": "2099-06-21"}])
    assert _count(conn, nid) == 1
    row = conn.execute("SELECT starts_at FROM calendar_events WHERE note_id=?", (nid,)).fetchone()
    assert row["starts_at"] == "2099-06-21"


def test_upsert_idempotent(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Idem note")
    ev = [{"title": "Mortgage due", "kind": "deadline", "starts_at": "2099-01-01"}]
    cal.upsert_events(conn, nid, ev)
    cal.upsert_events(conn, nid, ev)
    assert _count(conn, nid) == 1


def test_seq_disambiguates_same_title_same_note(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Standups")
    out = cal.upsert_events(conn, nid, [
        {"title": "Standup", "kind": "event", "starts_at": "2099-03-02"},
        {"title": "Standup", "kind": "event", "starts_at": "2099-03-03"},
    ])
    assert out["upserted"] == 2
    assert _count(conn, nid) == 2  # the second did NOT overwrite the first


def test_deletion_sweep_retires_dropped_events(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Sweep note")
    cal.upsert_events(conn, nid, [
        {"title": "A", "starts_at": "2099-01-01"},
        {"title": "B", "starts_at": "2099-02-01"},
    ])
    assert _count(conn, nid) == 2
    out = cal.upsert_events(conn, nid, [{"title": "A", "starts_at": "2099-01-01"}])
    assert out["retired"] == 1 and _count(conn, nid) == 1
    assert conn.execute("SELECT title FROM calendar_events WHERE note_id=?", (nid,)).fetchone()["title"] == "A"


def test_all_day_inferred_and_explicit(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Allday note")
    cal.upsert_events(conn, nid, [
        {"title": "Holiday", "starts_at": "2099-12-25"},                       # date-only -> all_day
        {"title": "Call", "starts_at": "2099-12-26T15:00:00"},                 # timed -> not all_day
    ])
    rows = {r["title"]: r["all_day"] for r in
            conn.execute("SELECT title, all_day FROM calendar_events WHERE note_id=?", (nid,))}
    assert rows["Holiday"] == 1 and rows["Call"] == 0


# --- supersession -----------------------------------------------------------

def test_structured_supersession_and_what_replaced(conn):
    from app.services import calendar as cal
    n1 = _mknote(conn, "Dentist")
    cal.upsert_events(conn, n1, [{"title": "Dentist visit", "kind": "appointment", "starts_at": "2099-06-14"}])
    old_id = conn.execute("SELECT id FROM calendar_events WHERE note_id=?", (n1,)).fetchone()["id"]

    n2 = _mknote(conn, "Reschedule", body="Moved it. supersedes [[Dentist]] 2099-06-14")
    cal.upsert_events(conn, n2, [{"title": "Dentist visit", "kind": "appointment", "starts_at": "2099-06-21"}])

    out = cal.consolidate(conn, [{"id": n2, "content_md": "supersedes [[Dentist]] 2099-06-14"}])
    assert out["edges"] == 1

    # The old event is now excluded from v_upcoming; the new one is present.
    titles = [r["title"] for r in conn.execute(
        "SELECT title FROM v_upcoming WHERE note_id IN (?,?)", (n1, n2))]
    up_notes = [r["note_id"] for r in conn.execute(
        "SELECT note_id FROM v_upcoming WHERE note_id IN (?,?)", (n1, n2))]
    assert n1 not in up_notes and n2 in up_notes

    # v_event_history surfaces the superseded original.
    hist_notes = [r["note_id"] for r in conn.execute(
        "SELECT note_id FROM v_event_history WHERE note_id IN (?,?)", (n1, n2))]
    assert n1 in hist_notes

    # "what replaced X" resolves to the new event.
    repl = cal.what_replaced(conn, old_id)
    assert repl and repl.get("starts_at") == "2099-06-21"


def test_consolidate_idempotent(conn):
    from app.services import calendar as cal
    n1 = _mknote(conn, "Visit")
    cal.upsert_events(conn, n1, [{"title": "Eye exam", "starts_at": "2099-05-05"}])
    n2 = _mknote(conn, "Move", body="supersedes [[Visit]] 2099-05-05")
    cal.upsert_events(conn, n2, [{"title": "Eye exam", "starts_at": "2099-05-12"}])
    cal.consolidate(conn, [{"id": n2, "content_md": "supersedes [[Visit]] 2099-05-05"}])
    cal.consolidate(conn, [{"id": n2, "content_md": "supersedes [[Visit]] 2099-05-05"}])
    n = conn.execute("SELECT COUNT(*) c FROM calendar_supersedes").fetchone()["c"]
    assert n == 1


def test_supersession_marker_parsing():
    from app.services import calendar as cal
    got = cal.parse_supersession_markers("blah cancels [[Mortgage due]] 2099-01-01 end")
    assert got == [{"old_title": "Mortgage due", "old_date": "2099-01-01"}]
    assert cal.parse_supersession_markers("no marker here") == []
    # #6: bounded to one line — a marker must NOT bind a title to a far-away date.
    assert cal.parse_supersession_markers("supersedes [[Dentist]]\n\nUnrelated 2099-09-09") == []


def test_replacement_matches_by_title_not_latest_date(conn):
    """#3: a multi-event reschedule note must link the SAME event, not its latest-dated
    unrelated event."""
    from app.services import calendar as cal
    n1 = _mknote(conn, "Dentist")
    cal.upsert_events(conn, n1, [{"title": "Dentist visit", "starts_at": "2099-06-14"}])
    old_id = conn.execute("SELECT id FROM calendar_events WHERE note_id=?", (n1,)).fetchone()["id"]
    n2 = _mknote(conn, "Daily", body="supersedes [[Dentist]] 2099-06-14")
    cal.upsert_events(conn, n2, [
        {"title": "Dentist visit", "starts_at": "2099-06-21"},
        {"title": "Big party", "starts_at": "2099-06-30"},   # unrelated, later
    ])
    cal.consolidate(conn, [{"id": n2, "content_md": "supersedes [[Dentist]] 2099-06-14"}])
    repl = cal.what_replaced(conn, old_id)
    assert repl and repl["title"] == "Dentist visit" and repl["starts_at"] == "2099-06-21"


def test_stale_edge_does_not_resurrect_after_readd(conn):
    """#4: sweeping the old event purges its edge, so re-adding the same date is clean."""
    from app.services import calendar as cal
    n1 = _mknote(conn, "Appt")
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-14"}])
    n2 = _mknote(conn, "Move", body="supersedes [[Appt]] 2099-06-14")
    cal.upsert_events(conn, n2, [{"title": "Dentist", "starts_at": "2099-06-21"}])
    cal.consolidate(conn, [{"id": n2, "content_md": "supersedes [[Appt]] 2099-06-14"}])
    # Owner removes the date from n1 (sweep), then later re-adds the SAME date.
    cal.upsert_events(conn, n1, [])
    assert conn.execute("SELECT COUNT(*) c FROM calendar_supersedes").fetchone()["c"] == 0
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-14"}])
    up = [r["note_id"] for r in conn.execute("SELECT note_id FROM v_upcoming WHERE note_id=?", (n1,))]
    assert n1 in up   # the re-added event is visible again, not hidden by a stale edge


def test_pure_cancellation_note_is_scanned(conn):
    """#5: a marker-only note (no date of its own, no rows) still gets consolidated."""
    from app.services import calendar as cal
    n1 = _mknote(conn, "Dentist")
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-14"}])
    # A cancellation note: only a marker, no dates, no calendar rows.
    n2 = _mknote(conn, "Oops", body="cancels [[Dentist]] 2099-06-14", dates=[],
                 updated_at="2099-06-15 00:00:00")
    pending = cal.pending_notes(conn, "", 40)
    assert any(p["id"] == n2 for p in pending), "marker-only note must be in the batch"
    cal.consolidate(conn, pending)
    up = [r["note_id"] for r in conn.execute("SELECT note_id FROM v_upcoming WHERE note_id=?", (n1,))]
    assert n1 not in up   # the cancelled event is gone from upcoming


def test_watermark_no_starvation_at_timestamp_tie(conn):
    """#2: notes sharing an updated_at must not be starved when batch_limit < their count.
    The composite (updated_at, id) cursor drains them across runs."""
    from app.services import calendar as cal
    ids = []
    for i in range(3):
        ids.append(_mknote(conn, f"Note{i}", dates=[f"2099-0{i+1}-01: x"],
                           updated_at="2099-05-05 12:00:00"))
    seen = set()
    since = ""
    for _ in range(5):   # iterate batches of 2 until drained
        batch = cal.pending_notes(conn, since, 2)
        if not batch:
            break
        seen.update(p["id"] for p in batch)
        since = cal.cursor_for(batch)
    assert set(ids) <= seen   # every tied-timestamp note was eventually returned


def test_cross_source_sweep_preserves_manual(conn):
    """An extracted-source sweep must not touch a manual-source row for the same note."""
    from app.services import calendar as cal
    nid = _mknote(conn, "Mixed source")
    cal.upsert_events(conn, nid, [{"title": "Manual thing", "starts_at": "2099-01-01"}], source="manual")
    cal.upsert_events(conn, nid, [{"title": "Extracted thing", "starts_at": "2099-02-01"}], source="extracted")
    cal.upsert_events(conn, nid, [], source="extracted")   # sweep extracted only
    rows = {r["title"] for r in conn.execute("SELECT title FROM calendar_events WHERE note_id=?", (nid,))}
    assert rows == {"Manual thing"}


def test_views_now_boundary(conn):
    """now()-boundary realism: a just-past timed event and a today all-day event land
    on the right side of v_upcoming / v_event_history."""
    from app.services import calendar as cal
    nid = _mknote(conn, "Boundary")
    conn.execute(
        "INSERT INTO calendar_events (note_id, title, starts_at, all_day, status, identity_key, source) "
        "VALUES (?,?,datetime('now','-1 minute'),0,'confirmed','bnd-past',?)", (nid, "JustPast", "extracted"))
    conn.execute(
        "INSERT INTO calendar_events (note_id, title, starts_at, all_day, status, identity_key, source) "
        "VALUES (?,?,date('now'),1,'confirmed','bnd-today',?)", (nid, "TodayAllDay", "extracted"))
    up = {r["title"] for r in conn.execute("SELECT title FROM v_upcoming WHERE note_id=?", (nid,))}
    hist = {r["title"] for r in conn.execute("SELECT title FROM v_event_history WHERE note_id=?", (nid,))}
    assert "TodayAllDay" in up and "JustPast" not in up
    assert "JustPast" in hist


def test_classify_cancelled_status_lands_in_history(conn, monkeypatch):
    """A note whose event the LLM marks status=cancelled goes to history, not upcoming."""
    from app.services import calendar as cal
    nid = _mknote(conn, "Cancelled appt", body="Dentist 2099-08-08 — cancelled",
                  dates=["2099-08-08: dentist"])
    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "model_for", lambda *a: "m")
    monkeypatch.setattr(cal.llm, "complete", lambda *a, **k: json.dumps([
        {"title": "Dentist", "starts_at": "2099-08-08", "status": "cancelled"}]))
    events = cal.classify_dates(conn, {"id": nid, "title": "Cancelled appt", "content_md": "x"})
    cal.upsert_events(conn, nid, events)
    up = {r["title"] for r in conn.execute("SELECT title FROM v_upcoming WHERE note_id=?", (nid,))}
    hist = {r["title"] for r in conn.execute("SELECT title FROM v_event_history WHERE note_id=?", (nid,))}
    assert up == set() and "Dentist" in hist


# --- the (b) free-prose supersession path (stubbed matcher) ------------------

def test_propose_supersession_high_applies_low_stages(conn, monkeypatch):
    from app.services import calendar as cal
    n1 = _mknote(conn, "Original")
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-14"}])
    old_id = conn.execute("SELECT id FROM calendar_events WHERE note_id=?", (n1,)).fetchone()["id"]
    n2 = _mknote(conn, "Note", body="had to reschedule the dentist to next week")
    cal.upsert_events(conn, n2, [{"title": "Dentist", "starts_at": "2099-06-21"}])

    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "model_for", lambda *a: "m")
    # HIGH confidence -> auto-applies an 'llm' edge.
    monkeypatch.setattr(cal, "_llm_match_supersession",
                        lambda text, cands: {"index": 0, "confidence": "high", "cancel": False})
    out = cal.propose_supersessions(conn, [{"id": n2, "title": "Note",
                                            "content_md": "reschedule the dentist"}])
    assert out["applied"] == 1
    assert cal.what_replaced(conn, old_id) is not None
    edge = conn.execute("SELECT confidence FROM calendar_supersedes").fetchone()
    assert edge["confidence"] == "llm"


def test_propose_supersession_low_confidence_stages_review(conn, monkeypatch):
    from app.services import calendar as cal
    n1 = _mknote(conn, "Original2")
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-14"}])
    n2 = _mknote(conn, "Vague", body="maybe cancel the dentist, not sure")

    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "model_for", lambda *a: "m")
    monkeypatch.setattr(cal, "_llm_match_supersession",
                        lambda text, cands: {"index": 0, "confidence": "low", "cancel": False})
    out = cal.propose_supersessions(conn, [{"id": n2, "title": "Vague",
                                            "content_md": "maybe cancel the dentist"}])
    assert out["staged"] == 1
    # No edge applied; a review card was posted instead.
    assert conn.execute("SELECT COUNT(*) c FROM calendar_supersedes").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM review_items").fetchone()["c"] == 1


def test_propose_supersession_noop_without_llm(conn):
    from app.services import calendar as cal
    out = cal.propose_supersessions(conn, [{"id": 1, "content_md": "reschedule the thing"}])
    assert out == {"applied": 0, "staged": 0}


# --- rrule expansion --------------------------------------------------------

def test_expand_rrule_daily_count(conn=None):
    from app.services import calendar as cal
    out = cal.expand_rrule("FREQ=DAILY;COUNT=3", "2026-06-01", "2026-06-01", "2026-06-30")
    assert out == ["2026-06-01", "2026-06-02", "2026-06-03"]


def test_expand_rrule_weekly_byday():
    from app.services import calendar as cal
    out = cal.expand_rrule("FREQ=WEEKLY;BYDAY=TH", "2026-06-04", "2026-06-01", "2026-06-30")
    assert "2026-06-04" in out
    assert all(datetime.strptime(d, "%Y-%m-%d").weekday() == 3 for d in out)  # all Thursdays
    assert len(out) == 4


def test_expand_rrule_exdate():
    from app.services import calendar as cal
    out = cal.expand_rrule("FREQ=DAILY;COUNT=3", "2026-06-01", "2026-06-01", "2026-06-30",
                           exdates=["2026-06-02"])
    assert out == ["2026-06-01", "2026-06-03"]


def test_expand_rrule_unparseable_degrades():
    from app.services import calendar as cal
    assert cal.expand_rrule("TOTAL GARBAGE", "2026-06-10", "2026-06-01", "2026-06-30") == ["2026-06-10"]
    # out of window -> empty, never raises
    assert cal.expand_rrule("NOPE", "2030-01-01", "2026-06-01", "2026-06-30") == []


# --- views ------------------------------------------------------------------

def test_views_filter_live_future_vs_history(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Mixed")
    cal.upsert_events(conn, nid, [
        {"title": "FutureGood", "starts_at": "2099-01-01", "status": "confirmed"},
        {"title": "PastDone", "starts_at": "2000-01-01", "status": "confirmed"},
        {"title": "FutureCancelled", "starts_at": "2099-01-01", "status": "cancelled"},
    ])
    up = {r["title"] for r in conn.execute("SELECT title FROM v_upcoming WHERE note_id=?", (nid,))}
    hist = {r["title"] for r in conn.execute("SELECT title FROM v_event_history WHERE note_id=?", (nid,))}
    assert up == {"FutureGood"}
    assert {"PastDone", "FutureCancelled"} <= hist
    assert "FutureGood" not in hist


# --- integration: ingestion + read-only SQL console reachability ------------

def test_workflow_and_action_ingest(conn):
    from app.services import workflows as wf
    from app.services import pipeline
    wf.ingest_repo_workflows(conn)
    keys = {r["key"] for r in conn.execute("SELECT key FROM workflows")}
    assert "extract-events" in keys
    assert "extract_events" in pipeline.action_types()


def test_views_reachable_via_readonly_sql_console(conn):
    from app.services import sqlsafe
    for view in ("v_upcoming", "v_event_history", "calendar_events", "calendar_supersedes"):
        cols, rows = sqlsafe.run_select(conn, f"SELECT * FROM {view}")
        assert isinstance(cols, list)  # passed the keyword filter and executed


# --- the LLM seam: full extract_events action with a stubbed model -----------

def test_extract_events_action_writes_rows_and_advances_watermark(conn, monkeypatch):
    from app.services import calendar as cal
    from app.services import pipeline
    from app.db import get_meta

    nid = _mknote(conn, "Trip planning", body="Flight on 2099-07-01.",
                  dates=["2099-07-01: flight"], updated_at="2099-07-01 09:00:00")

    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "model_for", lambda *a: "m")
    monkeypatch.setattr(cal.llm, "complete", lambda *a, **k: json.dumps([
        {"title": "Flight", "kind": "event", "starts_at": "2099-07-01", "all_day": True}
    ]))

    recipe = pipeline.get_action_def("extract_events")
    assert recipe is not None, "extract_events action recipe must load from actions/*.yaml"
    detail = pipeline.run_pipeline(conn, recipe, {"batch_limit": 80}, None, None)

    rows = conn.execute("SELECT title, starts_at FROM calendar_events WHERE note_id=?", (nid,)).fetchall()
    assert len(rows) == 1 and rows[0]["title"] == "Flight" and rows[0]["starts_at"] == "2099-07-01"
    assert get_meta("calendar:watermark") == f"2099-07-01 09:00:00|{nid}"   # composite cursor


def test_extract_events_action_sweeps_when_dates_removed(conn, monkeypatch):
    """Editing a note to REMOVE its appointment retires the orphaned calendar row,
    with no LLM call (the note is revisited because it still has calendar rows)."""
    from app.services import calendar as cal
    from app.services import pipeline

    nid = _mknote(conn, "Had appt", body="Dentist on 2099-09-09.",
                  dates=["2099-09-09: dentist"], updated_at="2099-09-09 09:00:00")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "starts_at": "2099-09-09"}])
    assert _count(conn, nid) == 1

    # The owner removed the date: analysis now has no dates; note bumped past watermark.
    conn.execute("UPDATE note_analysis SET dates_json='[]' WHERE note_id=?", (nid,))
    conn.execute("UPDATE notes SET updated_at='2099-09-10 09:00:00' WHERE id=?", (nid,))
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return "[]"
    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "complete", _boom)

    recipe = pipeline.get_action_def("extract_events")
    pipeline.run_pipeline(conn, recipe, {}, None, None)
    assert called["n"] == 0          # gated on detected dates — no wasted LLM call
    assert _count(conn, nid) == 0    # orphaned row swept


def test_extract_events_action_noop_without_dates(conn, monkeypatch):
    from app.services import calendar as cal
    from app.services import pipeline
    # A changed note with NO detected dates is never sent to the LLM.
    _mknote(conn, "Just musings", body="thinking out loud", dates=[], updated_at="2099-01-01 00:00:00")
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return "[]"
    monkeypatch.setattr(cal.llm, "has_credentials", lambda: True)
    monkeypatch.setattr(cal.llm, "complete", _boom)

    recipe = pipeline.get_action_def("extract_events")
    pipeline.run_pipeline(conn, recipe, {}, None, None)
    assert called["n"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 0


# ============================================================================
# Phase 2 — Research tools + recurrence promotion
# ============================================================================

def test_infer_rrule_cadences():
    from app.services import calendar as cal
    assert cal.infer_rrule(["2099-01-01", "2099-01-08", "2099-01-15", "2099-01-22"]) == "FREQ=WEEKLY;BYDAY=TH"
    assert cal.infer_rrule(["2099-01-01", "2099-01-02", "2099-01-03"]) == "FREQ=DAILY"
    assert cal.infer_rrule(["2099-01-15", "2099-02-15", "2099-03-15"]) == "FREQ=MONTHLY"
    assert cal.infer_rrule(["2099-01-01", "2099-01-05", "2099-01-20"]) is None   # irregular
    assert cal.infer_rrule(["2099-01-01", "2099-01-08"]) is None                 # < 3 days
    assert cal.infer_rrule([]) is None
    # Fortnightly (gap 14) and mixed gaps are NOT promoted.
    assert cal.infer_rrule(["2099-01-01", "2099-01-15", "2099-01-29"]) is None
    assert cal.infer_rrule(["2099-01-01", "2099-01-08", "2099-02-07"]) is None   # gaps [7,30]
    # A malformed token refuses (won't compute a cadence from a subset).
    assert cal.infer_rrule(["notadate", "2099-01-01", "2099-01-08", "2099-01-15"]) is None


def test_emit_recurrence_stable_anchor_across_earlier_member(conn):
    """#1 BLOCKER regression: a cluster that later gains an EARLIER member must update
    the existing recurring row in place, not spawn a duplicate on the new anchor."""
    from app.services import calendar as cal
    days = {"A": "2099-01-01", "B": "2099-01-08", "C": "2099-01-15", "D": "2099-01-22"}
    ids = {}
    for k, d in days.items():
        nid = _mknote(conn, f"Gym note {k}")
        conn.execute("UPDATE notes SET created_at=? WHERE id=?", (f"{d} 07:00:00", nid))
        ids[k] = nid

    def ent(k):
        return {"id": ids[k], "title": "Morning gym", "created_at": f"{days[k]} 07:00:00"}

    cal.emit_recurrence(conn, {"entries": [ent("B"), ent("C"), ent("D")]})
    # Re-run after an EARLIER member (A) joined the cluster.
    cal.emit_recurrence(conn, {"entries": [ent("A"), ent("B"), ent("C"), ent("D")]})
    rows = conn.execute("SELECT note_id, starts_at FROM calendar_events WHERE kind='recurring'").fetchall()
    assert len(rows) == 1                       # no duplicate
    assert rows[0]["note_id"] == ids["B"]       # updated the original anchor in place
    assert rows[0]["starts_at"] == "2099-01-01"  # first occurrence moved earlier


def test_emit_recurrence_tolerates_one_dateless_member(conn):
    """#4: a member with no created_at must not become a broken anchor or suppress a
    genuine pattern."""
    from app.services import calendar as cal
    ids = []
    for d in ["2099-01-01", "2099-01-08", "2099-01-15"]:
        nid = _mknote(conn, f"g{d}")
        ids.append({"id": nid, "title": "Yoga", "created_at": f"{d} 06:00:00"})
    bad = _mknote(conn, "gnone")
    ids.append({"id": bad, "title": "Yoga", "created_at": None})
    out = cal.emit_recurrence(conn, {"entries": ids})
    assert out["emitted"] == 1 and out["rrule"].startswith("FREQ=WEEKLY")
    row = conn.execute("SELECT starts_at FROM calendar_events WHERE kind='recurring'").fetchone()
    assert row["starts_at"] == "2099-01-01"


def test_emit_recurrence_creates_row_idempotent(conn):
    from app.services import calendar as cal
    ids = []
    for i, day in enumerate(["2099-01-01", "2099-01-08", "2099-01-15"]):
        nid = _mknote(conn, f"Gym day {i}")
        conn.execute("UPDATE notes SET created_at=? WHERE id=?", (f"{day} 07:00:00", nid))
        ids.append({"id": nid, "title": "Morning gym", "created_at": f"{day} 07:00:00"})
    cluster = {"entries": ids, "distinct_days": 3}
    out = cal.emit_recurrence(conn, cluster)
    assert out["emitted"] == 1 and out["rrule"].startswith("FREQ=WEEKLY")
    anchor = ids[0]["id"]
    row = conn.execute("SELECT kind, rrule, source FROM calendar_events WHERE note_id=?", (anchor,)).fetchone()
    assert row["kind"] == "recurring" and row["source"] == "workflow"
    # Idempotent: re-emit keeps a single row on the anchor.
    cal.emit_recurrence(conn, cluster)
    assert conn.execute("SELECT COUNT(*) c FROM calendar_events WHERE note_id=?", (anchor,)).fetchone()["c"] == 1


def test_emit_recurrence_skips_irregular(conn):
    from app.services import calendar as cal
    ids = []
    for i, day in enumerate(["2099-01-01", "2099-01-05", "2099-01-20"]):
        nid = _mknote(conn, f"Headache {i}")
        ids.append({"id": nid, "title": "headache again", "created_at": f"{day} 09:00:00"})
    out = cal.emit_recurrence(conn, {"entries": ids})
    assert out["emitted"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 0


def test_emit_recurrence_two_clusters_same_anchor_dont_clobber(conn):
    """sweep=False: two recurring rows whose earliest member is the SAME note coexist."""
    from app.services import calendar as cal
    base = _mknote(conn, "Busy Monday")
    conn.execute("UPDATE notes SET created_at='2099-01-04 07:00:00' WHERE id=?", (base,))
    others = []
    for day in ["2099-01-11", "2099-01-18"]:
        o = _mknote(conn, f"f{day}")
        others.append(o)
    def cluster(title):
        ents = [{"id": base, "title": title, "created_at": "2099-01-04 07:00:00"}]
        for k, o in enumerate(others):
            ents.append({"id": o, "title": title, "created_at": f"{['2099-01-11','2099-01-18'][k]} 07:00:00"})
        return {"entries": ents}
    cal.emit_recurrence(conn, cluster("Gym"))
    cal.emit_recurrence(conn, cluster("Standup"))
    rows = {r["title"] for r in conn.execute("SELECT title FROM calendar_events WHERE note_id=?", (base,))}
    assert rows == {"Gym", "Standup"}


# --- Research-mode tools ----------------------------------------------------

def test_calendar_tools_registered_in_research_mode():
    from app.services import architect
    names = architect._mode_tool_names("research")
    assert "list_upcoming" in names and "event_history" in names
    assert "list_upcoming" in architect._RETRIEVAL_TOOLS


def test_tool_list_upcoming_and_event_history(conn):
    from app.services import calendar as cal
    from app.services import architect
    nid = _mknote(conn, "Schedule")
    cal.upsert_events(conn, nid, [
        {"title": "Dentist", "kind": "appointment", "starts_at": "2099-06-14"},
        {"title": "Old thing", "starts_at": "2000-01-01"},
    ])
    up = architect._tool_list_upcoming(conn, within_days=400000)
    assert "Dentist" in up and "Old thing" not in up and "Schedule" in up   # cites source note
    hist = architect._tool_event_history(conn)
    assert "Old thing" in hist and "Dentist" not in hist


def test_list_upcoming_expands_recurring_to_next_occurrence(conn):
    """A recurring series (stored starts_at is its first, past occurrence) surfaces in
    list_upcoming via rrule expansion to the NEXT occurrence."""
    from app.services import calendar as cal
    from app.services import architect
    nid = _mknote(conn, "Gym")
    # A weekly series anchored in the past; the next occurrence is in the future.
    cal.upsert_events(conn, nid, [{"title": "Morning gym", "kind": "recurring",
                                   "starts_at": "2020-01-06", "all_day": True,
                                   "rrule": "FREQ=WEEKLY;BYDAY=MO"}], source="workflow", sweep=False)
    out = architect._tool_list_upcoming(conn, within_days=30)
    assert "Morning gym" in out and "[recurring]" in out


def test_tool_list_upcoming_kind_filter_and_empty(conn):
    from app.services import calendar as cal
    from app.services import architect
    nid = _mknote(conn, "Sched2")
    cal.upsert_events(conn, nid, [
        {"title": "Pay rent", "kind": "deadline", "starts_at": "2099-02-01"},
        {"title": "Lunch", "kind": "appointment", "starts_at": "2099-02-02"},
    ])
    only = architect._tool_list_upcoming(conn, within_days=400000, kind="deadline")
    assert "Pay rent" in only and "Lunch" not in only
    assert "No upcoming" in architect._tool_list_upcoming(conn, within_days=1)   # nothing within a day


def test_run_tool_mode_boundary_blocks_calendar_in_unknown_mode(conn):
    from app.services import architect
    # Research mode advertises it -> dispatches (empty DB -> the no-events message).
    out, _ = architect._run_tool(conn, None, "list_upcoming", {}, mode="research")
    assert "No upcoming calendar events" in out
    # A mode that doesn't advertise it is refused (fail-closed boundary).
    blocked, _ = architect._run_tool(conn, None, "list_upcoming", {}, mode="entry")
    assert "not available" in blocked.lower()


def test_list_upcoming_within_days_boundary(conn):
    """#missing: an event exactly at the horizon is included; one day past is excluded."""
    from datetime import timedelta
    from app.services import calendar as cal
    from app.services import architect, clock
    nid = _mknote(conn, "Horizon")
    at = (clock.today_local() + timedelta(days=10)).isoformat()
    past_h = (clock.today_local() + timedelta(days=11)).isoformat()
    cal.upsert_events(conn, nid, [
        {"title": "AtHorizon", "starts_at": at},
        {"title": "PastHorizon", "starts_at": past_h},
    ])
    out = architect._tool_list_upcoming(conn, within_days=10)
    assert "AtHorizon" in out and "PastHorizon" not in out


def test_list_upcoming_excludes_superseded_recurring(conn):
    from app.services import calendar as cal
    from app.services import architect
    nid = _mknote(conn, "Standup series")
    cal.upsert_events(conn, nid, [{"title": "Standup", "kind": "recurring",
                                   "starts_at": "2020-01-06", "all_day": True,
                                   "rrule": "FREQ=WEEKLY;BYDAY=MO"}], source="workflow", sweep=False)
    ik = conn.execute("SELECT identity_key FROM calendar_events WHERE note_id=?", (nid,)).fetchone()["identity_key"]
    n2 = _mknote(conn, "End it")
    cal.record_supersession(conn, ik, None, n2, "structured")
    out = architect._tool_list_upcoming(conn, within_days=30)
    assert "Standup" not in out


def test_event_history_bound_excludes_undated(conn):
    """#6: an explicit since/until window drops undated terminal rows."""
    from app.services import calendar as cal
    from app.services import architect
    nid = _mknote(conn, "Histnote")
    cal.upsert_events(conn, nid, [
        {"title": "DatedOld", "starts_at": "2010-05-05", "status": "done"},
        {"title": "Undated", "starts_at": None, "status": "done"},
    ])
    bounded = architect._tool_event_history(conn, since="2009-01-01", until="2011-01-01")
    assert "DatedOld" in bounded and "Undated" not in bounded
    # With no bounds, the undated terminal event is still visible.
    allh = architect._tool_event_history(conn)
    assert "Undated" in allh


# ============================================================================
# Phase 3 — reminders (Review + Web Push) + note-write paths
# ============================================================================

def _soon(hours):
    from datetime import timedelta
    from app.services import clock
    return (clock.now_local().replace(tzinfo=None) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _mkwf(conn):
    """A minimal workflow row (its id is what the scheduler passes as workflow_id —
    calendar_fired.workflow_id is NOT NULL and review_items.workflow_id has an FK)."""
    cur = conn.execute(
        "INSERT INTO workflows (name, trigger_type, trigger_config, action_type, action_config) "
        "VALUES ('t','schedule','{}','calendar_reminders','{}')"
    )
    return cur.lastrowid


def test_due_reminders_fires_once_per_instance(conn):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Appt")
    cal.upsert_events(conn, nid, [
        {"title": "Dentist", "kind": "appointment", "starts_at": _soon(24)},   # within 48h -> due
        {"title": "FarOff", "starts_at": "2099-01-01"},                        # not due
    ])
    out = cal.due_reminders(conn, wf, lead_hours=48, push=False)
    assert out["fired"] == 1
    revs = conn.execute("SELECT title FROM review_items").fetchall()
    assert any("Dentist" in r["title"] for r in revs) and not any("FarOff" in r["title"] for r in revs)
    # Idempotent: a second pass fires nothing more.
    assert cal.due_reminders(conn, wf, lead_hours=48, push=False)["fired"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM review_items").fetchone()["c"] == 1


def test_due_reminders_recurring_next_instance(conn):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Daily standup")
    # A daily series anchored in the past — its next instance is today/tomorrow (within 48h).
    cal.upsert_events(conn, nid, [{"title": "Standup", "kind": "recurring",
                                   "starts_at": "2020-01-01", "all_day": True,
                                   "rrule": "FREQ=DAILY"}], source="workflow", sweep=False)
    out = cal.due_reminders(conn, wf, lead_hours=48, push=False)
    assert out["fired"] >= 1
    assert any("Standup" in r["title"] for r in conn.execute("SELECT title FROM review_items"))


def test_due_reminders_skips_superseded(conn):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Will move")
    cal.upsert_events(conn, nid, [{"title": "Checkup", "starts_at": _soon(12)}])
    ik = conn.execute("SELECT identity_key FROM calendar_events WHERE note_id=?", (nid,)).fetchone()["identity_key"]
    n2 = _mknote(conn, "Moved")
    cal.record_supersession(conn, ik, None, n2, "structured")
    assert cal.due_reminders(conn, wf, lead_hours=48, push=False)["fired"] == 0


# --- write-path endpoints (called directly; they use get_conn + Pydantic bodies) ---

def test_quick_add_writes_note_and_manual_row_and_skips_llm(conn):
    from app.routers import calendar as r
    out = r.quick_add(r.QuickAddIn(title="Dentist", date="2099-06-14", time="15:00", kind="appointment"))
    assert out["event"]["title"] == "Dentist" and out["event"]["all_day"] == 0
    row = conn.execute("SELECT source, starts_at FROM calendar_events WHERE id=?", (out["event"]["id"],)).fetchone()
    assert row["source"] == "manual" and row["starts_at"] == "2099-06-14T15:00:00"
    # The note exists (durable record) and is EXCLUDED from LLM extraction (manual row).
    from app.services import calendar as cal
    conn.execute("UPDATE notes SET updated_at='2099-06-14 00:00:00' WHERE id=?", (out["note_id"],))
    assert all(p["id"] != out["note_id"] for p in cal.pending_notes(conn, "", 100))


def test_reschedule_supersedes_old_and_adds_new(conn):
    from app.routers import calendar as r
    added = r.quick_add(r.QuickAddIn(title="Dentist", date="2099-06-14"))
    old_id = added["event"]["id"]
    r.reschedule(old_id, r.RescheduleIn(to_date="2099-06-21"))
    up = {(row["title"], row["starts_at"]) for row in conn.execute("SELECT title, starts_at FROM v_upcoming")}
    assert ("Dentist", "2099-06-21") in up
    assert ("Dentist", "2099-06-14") not in up   # old occurrence superseded


def test_cancel_removes_from_upcoming(conn):
    from app.routers import calendar as r
    added = r.quick_add(r.QuickAddIn(title="Lunch", date="2099-07-01"))
    r.cancel(added["event"]["id"])
    up = [row["title"] for row in conn.execute("SELECT title FROM v_upcoming")]
    assert "Lunch" not in up


def test_upcoming_endpoint_merges_oneoff_and_recurring(conn):
    from app.routers import calendar as r
    from app.services import calendar as cal
    from datetime import timedelta
    from app.services import clock
    n1 = _mknote(conn, "One off")
    flight = (clock.today_local() + timedelta(days=30)).isoformat()   # within the API's day cap
    cal.upsert_events(conn, n1, [{"title": "Flight", "starts_at": flight}])
    n2 = _mknote(conn, "Series")
    cal.upsert_events(conn, n2, [{"title": "Gym", "kind": "recurring", "starts_at": "2020-01-06",
                                  "all_day": True, "rrule": "FREQ=WEEKLY;BYDAY=MO"}],
                     source="workflow", sweep=False)
    rows = r.upcoming(within_days=365)
    titles = {x["title"] for x in rows}
    assert "Flight" in titles and "Gym" in titles
    assert any(x["recurring"] for x in rows if x["title"] == "Gym")
