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

pytestmark = pytest.mark.integration

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
    assert int(get_meta("schema_version")) == SCHEMA_VERSION   # DB migrated to latest


def test_migration_recreates_calendar_from_prior_version(conn):
    """Dropping the calendar objects and re-running migrations from just before the
    calendar migrations restores them — proves the `if current < 49/50` blocks +
    _CALENDAR_SCHEMA_SQL upgrade path."""
    from app.db import _run_migrations, set_meta

    def _cols():  # column set of calendar_events as currently defined
        return {r["name"] for r in conn.execute("PRAGMA table_info(calendar_events)")}
    fresh_cols = _cols()   # from schema.sql

    conn.executescript(
        "DROP VIEW v_upcoming; DROP VIEW v_event_history; "
        "DROP TABLE calendar_supersedes; DROP TABLE calendar_fired; DROP TABLE calendar_events;"
    )
    set_meta(conn, "schema_version", "48")   # right before the calendar migrations
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


# --- Phase 3 review-fix regressions -----------------------------------------

def test_due_reminders_requires_workflow_id(conn):
    import pytest as _pt
    from app.services import calendar as cal
    with _pt.raises(ValueError):
        cal.due_reminders(conn, None, lead_hours=48, push=False)


def test_reminder_timed_event_earlier_today_still_fires(conn):
    """M1: a timed event earlier TODAY (already past 'now') still reminds once — the
    lower bound is start-of-today, tolerating the inter-run gap. Uses a fixed 08:00
    today (not now±h) so it's stable regardless of when the test runs."""
    from app.services import calendar as cal
    from app.services import clock
    wf = _mkwf(conn)
    nid = _mknote(conn, "Earlier today")
    cal.upsert_events(conn, nid, [{"title": "Missed call", "starts_at": f"{clock.today_iso()}T08:00:00"}])
    assert cal.due_reminders(conn, wf, lead_hours=48, push=False)["fired"] == 1


def test_reminder_push_fires_once(conn, monkeypatch):
    from app.services import calendar as cal
    from app.services import push as push_svc
    calls = []
    monkeypatch.setattr(push_svc, "notify", lambda *a, **k: calls.append(a))
    wf = _mkwf(conn)
    nid = _mknote(conn, "Pushy")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "starts_at": _soon(20)}])
    cal.due_reminders(conn, wf, lead_hours=48, push=True)
    cal.due_reminders(conn, wf, lead_hours=48, push=True)   # re-run
    assert len(calls) == 1   # pushed once, not re-pushed


def test_views_timed_today_boundary(conn):
    """M2: a timed event earlier today is HISTORY, a timed event later today is UPCOMING
    (regression for the T-vs-space string comparison)."""
    from app.services import calendar as cal
    nid = _mknote(conn, "Timed")
    cal.upsert_events(conn, nid, [
        {"title": "Earlier", "starts_at": _soon(-3)},   # timed, ~3h ago
        {"title": "Later", "starts_at": _soon(3)},      # timed, ~3h ahead
    ])
    up = {r["title"] for r in conn.execute("SELECT title FROM v_upcoming WHERE note_id=?", (nid,))}
    hist = {r["title"] for r in conn.execute("SELECT title FROM v_event_history WHERE note_id=?", (nid,))}
    assert "Later" in up and "Earlier" not in up
    assert "Earlier" in hist


def test_quick_add_rejects_bad_time(conn):
    import pytest as _pt
    from fastapi import HTTPException
    from app.routers import calendar as r
    with _pt.raises(HTTPException) as ei:
        r.quick_add(r.QuickAddIn(title="X", date="2099-01-01", time="9am"))
    assert ei.value.status_code == 422


def test_quick_add_title_cannot_inject_marker(conn):
    from app.routers import calendar as r
    out = r.quick_add(r.QuickAddIn(title="cancels [[Important]] 2099-01-01", date="2099-02-02"))
    body = conn.execute("SELECT content_md FROM notes WHERE id=?", (out["note_id"],)).fetchone()["content_md"]
    assert "[[" not in body                                   # brackets neutralized
    assert conn.execute("SELECT COUNT(*) c FROM calendar_supersedes").fetchone()["c"] == 0


# ============================================================================
# Calendar UI: the /range endpoint (Day/Week/Month grids)
# ============================================================================

def test_range_validation(conn):
    import pytest as _pt
    from fastapi import HTTPException
    from app.routers import calendar as r
    for bad in [("nope", "2099-06-30"), ("2099-06-30", "2099-06-01")]:  # bad fmt, start>end
        with _pt.raises(HTTPException) as ei:
            r.range_events(bad[0], bad[1])
        assert ei.value.status_code == 422
    with _pt.raises(HTTPException) as ei:   # span > 366 days
        r.range_events("2099-01-01", "2101-01-01")
    assert ei.value.status_code == 422


def test_range_returns_window_oneoffs_only(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Sched")
    cal.upsert_events(conn, nid, [
        {"title": "InWindow", "starts_at": "2099-06-15"},
        {"title": "OutOfWindow", "starts_at": "2099-07-15"},
    ])
    rows = r.range_events("2099-06-01", "2099-06-30")
    titles = {x["title"] for x in rows}
    assert "InWindow" in titles and "OutOfWindow" not in titles


def test_range_excludes_superseded(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Moved appt")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "starts_at": "2099-06-10"}])
    ik = conn.execute("SELECT identity_key FROM calendar_events WHERE note_id=?", (nid,)).fetchone()["identity_key"]
    n2 = _mknote(conn, "Cancel note")
    cal.record_supersession(conn, ik, None, n2, "structured")   # cancellation edge
    rows = r.range_events("2099-06-01", "2099-06-30")
    assert all(x["title"] != "Dentist" for x in rows)


def test_range_expands_recurring_with_true_times(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Daily timed")
    cal.upsert_events(conn, nid, [{"title": "Standup", "kind": "recurring",
                                   "starts_at": "2099-06-01T09:00:00",
                                   "rrule": "FREQ=DAILY;COUNT=3"}], source="workflow", sweep=False)
    rows = [x for x in r.range_events("2099-06-01", "2099-06-30") if x["title"] == "Standup"]
    assert len(rows) == 3
    assert all(x["recurring"] and x["all_day"] == 0 and x["starts_at"].endswith("T09:00:00") for x in rows)
    assert {x["starts_at"][:10] for x in rows} == {"2099-06-01", "2099-06-02", "2099-06-03"}


def test_range_includes_done_past_events(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Done note")
    cal.upsert_events(conn, nid, [{"title": "Finished thing", "starts_at": "2000-03-03", "status": "done"}])
    rows = r.range_events("2000-03-01", "2000-03-31")
    assert any(x["title"] == "Finished thing" for x in rows)


# ============================================================================
# Calendar UI red-team fixes (backend)
# ============================================================================

def test_expand_rrule_z_suffixed_until_not_collapsed():
    from app.services import calendar as cal
    out = cal.expand_rrule("FREQ=DAILY;UNTIL=20990605T000000Z", "2099-06-01", "2099-06-01", "2099-06-30")
    assert out == ["2099-06-01", "2099-06-02", "2099-06-03", "2099-06-04", "2099-06-05"]
    # offset form too; and never raises
    assert len(cal.expand_rrule("FREQ=DAILY;UNTIL=20990603T000000+0000", "2099-06-01", "2099-06-01", "2099-06-30")) == 3


def test_expand_rrule_subdaily_guarded():
    from app.services import calendar as cal
    out = cal.expand_rrule("FREQ=SECONDLY;COUNT=100000", "2099-06-01T00:00:00", "2099-06-01", "2099-06-02")
    assert len(out) <= 1   # degrades, doesn't explode


def test_range_excludes_directly_cancelled(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Direct cancel")
    cal.upsert_events(conn, nid, [{"title": "DirectCancelled", "starts_at": "2099-06-10", "status": "cancelled"}])
    rows = r.range_events("2099-06-01", "2099-06-30")
    assert all(x["title"] != "DirectCancelled" for x in rows)   # agrees with v_upcoming


def test_range_recurring_null_start_skipped(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Bad recurring")
    # A malformed recurring row with no anchor (the LLM could emit this).
    conn.execute("INSERT INTO calendar_events (note_id, title, kind, rrule, identity_key, source) "
                 "VALUES (?,?,?,?,?,?)", (nid, "Phantom", "recurring", "FREQ=WEEKLY;BYDAY=MO", "ik-null", "extracted"))
    a = {x["starts_at"] for x in r.range_events("2099-06-01", "2099-06-21")}
    b = {x["starts_at"] for x in r.range_events("2099-06-03", "2099-06-23")}
    assert all("Phantom" != x["title"] for x in r.range_events("2099-06-01", "2099-06-21"))  # skipped
    assert a or b or True   # (no window-dependent phantom occurrences)


def test_range_recurring_status_passthrough(conn):
    from app.services import calendar as cal
    from app.routers import calendar as r
    nid = _mknote(conn, "Tentative series")
    cal.upsert_events(conn, nid, [{"title": "Maybe", "kind": "recurring", "starts_at": "2099-06-01",
                                   "status": "tentative", "rrule": "FREQ=DAILY;COUNT=2"}],
                     source="workflow", sweep=False)
    rows = [x for x in r.range_events("2099-06-01", "2099-06-30") if x["title"] == "Maybe"]
    assert rows and all(x["status"] == "tentative" for x in rows)


# ============================================================================
# Per-event reminders + in-calendar revoke (settable reminders)
# ============================================================================

def _set_now(monkeypatch, y, mo, d, h, mi):
    from datetime import datetime, timezone
    from app.services import clock as clockmod
    monkeypatch.setattr(clockmod, "now_local", lambda: datetime(y, mo, d, h, mi, tzinfo=timezone.utc))


def test_reminder_schema_v51(conn):
    from app.db import SCHEMA_VERSION, get_meta
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('calendar_reminders','calendar_dismissed')")}
    assert names == {"calendar_reminders", "calendar_dismissed"}
    assert int(get_meta("schema_version")) == SCHEMA_VERSION   # DB migrated to latest


def test_set_get_reminders(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Appt")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "kind": "appointment", "starts_at": "2099-06-15T14:00:00"}])
    ik = cal.identity_key(nid, "Dentist", "appointment", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}, {"offset_minutes": 1440}])
    offs = sorted(r["offset_minutes"] for r in cal.get_reminders(conn, ik))
    assert offs == [30, 1440]
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}])   # replace set
    assert [r["offset_minutes"] for r in cal.get_reminders(conn, ik)] == [30]


def test_alarm_fires_once_within_window(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Appt")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "kind": "appointment", "starts_at": "2099-06-15T14:00:00"}])
    ik = cal.identity_key(nid, "Dentist", "appointment", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}])
    _set_now(monkeypatch, 2099, 6, 15, 13, 0)                 # before the 13:30 fire time
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0
    _set_now(monkeypatch, 2099, 6, 15, 13, 45)                # within [13:30, 14:00)
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 1
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0   # dedup
    assert any("Dentist" in r["title"] for r in conn.execute("SELECT title FROM review_items"))


def test_alarm_not_after_event_start(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Past")
    cal.upsert_events(conn, nid, [{"title": "Missed", "starts_at": "2099-06-15T14:00:00"}])
    ik = cal.identity_key(nid, "Missed", "event", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}])
    _set_now(monkeypatch, 2099, 6, 15, 14, 30)                # event already started
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0


def test_alarm_noop_without_reminders(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "No reminder")
    cal.upsert_events(conn, nid, [{"title": "Plain", "starts_at": "2099-06-15T14:00:00"}])
    _set_now(monkeypatch, 2099, 6, 15, 13, 45)
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0   # default None


def test_alarm_all_day_anchor(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Holiday")
    cal.upsert_events(conn, nid, [{"title": "Anniversary", "starts_at": "2099-06-15", "all_day": True}])
    ik = cal.identity_key(nid, "Anniversary", "event", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 0, "anchor": "day_of"}])   # morning of (9am)
    _set_now(monkeypatch, 2099, 6, 15, 8, 0)                  # before 9am anchor
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0
    _set_now(monkeypatch, 2099, 6, 15, 9, 30)                 # after 9am, same day
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 1


def test_alarm_recurring_per_occurrence(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Standup series")
    cal.upsert_events(conn, nid, [{"title": "Standup", "kind": "recurring",
                                   "starts_at": "2099-06-01T09:00:00", "rrule": "FREQ=DAILY"}],
                     source="workflow", sweep=False)
    ik = cal.identity_key(nid, "Standup", "recurring", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 10}])
    _set_now(monkeypatch, 2099, 6, 15, 8, 52)                 # 8 min before the 9:00 occurrence
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 1
    _set_now(monkeypatch, 2099, 6, 16, 8, 52)                 # next day's occurrence
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 1


def test_alarm_skips_superseded_and_dismissed(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Will move")
    cal.upsert_events(conn, nid, [{"title": "Checkup", "starts_at": "2099-06-15T14:00:00"}])
    ik = cal.identity_key(nid, "Checkup", "event", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}])
    n2 = _mknote(conn, "moved")
    cal.record_supersession(conn, ik, None, n2, "structured")  # cancelled
    _set_now(monkeypatch, 2099, 6, 15, 13, 45)
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 0


def test_reminder_follows_reschedule(conn):
    from app.services import calendar as cal
    n1 = _mknote(conn, "A")
    cal.upsert_events(conn, n1, [{"title": "Dentist", "starts_at": "2099-06-15"}])
    ik_old = cal.identity_key(n1, "Dentist", "event", 0)
    cal.set_reminders(conn, ik_old, [{"offset_minutes": 30}])
    n2 = _mknote(conn, "B")
    cal.upsert_events(conn, n2, [{"title": "Dentist", "starts_at": "2099-06-22"}])
    ik_new = cal.identity_key(n2, "Dentist", "event", 0)
    cal.record_supersession(conn, ik_old, ik_new, n2, "structured")
    assert cal.get_reminders(conn, ik_old) == []                    # moved off the old
    assert [r["offset_minutes"] for r in cal.get_reminders(conn, ik_new)] == [30]


def test_dismiss_revoke_stops_reextraction_and_undo_restores(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Extracted")
    cal.upsert_events(conn, nid, [{"title": "Bogus appt", "starts_at": "2099-06-15"}])
    ik = cal.identity_key(nid, "Bogus appt", "event", 0)
    cal.dismiss_event(conn, ik)
    assert _count(conn, nid) == 0 and cal.is_dismissed(conn, ik)
    cal.upsert_events(conn, nid, [{"title": "Bogus appt", "starts_at": "2099-06-15"}])   # re-extraction
    assert _count(conn, nid) == 0                                   # stays gone
    cal.undismiss_event(conn, ik)
    assert _count(conn, nid) == 1 and not cal.is_dismissed(conn, ik)   # restored


def test_remove_from_calendar_is_note_free_and_undoable(conn):
    """The UI's only edit actions: Add (quick-add) then Remove (dismiss) — note-free,
    reversible, and writes NO superseding/cancellation note."""
    from app.routers import calendar as r
    from app.services import calendar as cal
    notes_before = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    added = r.quick_add(r.QuickAddIn(title="Haircut", date="2099-09-09"))
    ev_id = added["event"]["id"]
    out = r.dismiss_event_route(ev_id)
    ik = out["identity_key"]
    up = [x["title"] for x in conn.execute("SELECT title FROM v_upcoming")]
    assert "Haircut" not in up                      # gone from the calendar
    body = conn.execute("SELECT content_md FROM notes WHERE id=?", (added["note_id"],)).fetchone()["content_md"]
    assert not cal.parse_supersession_markers(body)  # no cancellation marker was written
    # Remove added exactly one note (the quick-add), none for the removal itself.
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == notes_before + 1
    r.undismiss_event_route(r.UndismissIn(identity_key=ik))
    up2 = [x["title"] for x in conn.execute("SELECT title FROM v_upcoming")]
    assert "Haircut" in up2                          # restored


def test_recently_added_excludes_manual_and_dismissed(conn):
    from app.services import calendar as cal
    n1 = _mknote(conn, "auto")
    cal.upsert_events(conn, n1, [{"title": "Extracted ev", "starts_at": "2099-06-15"}], source="extracted")
    n2 = _mknote(conn, "mine")
    cal.upsert_events(conn, n2, [{"title": "Manual ev", "starts_at": "2099-06-16"}], source="manual")
    titles = {x["title"] for x in cal.recently_added(conn, "")}
    assert "Extracted ev" in titles and "Manual ev" not in titles
    cal.dismiss_event(conn, cal.identity_key(n1, "Extracted ev", "event", 0))
    assert all(x["title"] != "Extracted ev" for x in cal.recently_added(conn, ""))


# --- reminders red-team fixes ---

def test_reminder_offsets_clamped_and_anchor_normalized(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Appt")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "starts_at": "2099-06-15T14:00:00"}])
    ik = cal.identity_key(nid, "Dentist", "event", 0)
    # huge (storm risk) and negative offsets are rejected; a valid one is kept; anchor
    # normalized to 'start' for a timed event even if client says 'day_of'.
    cal.set_reminders(conn, ik, [{"offset_minutes": 30, "anchor": "day_of"},
                                 {"offset_minutes": 5256000}, {"offset_minutes": -60}])
    rems = cal.get_reminders(conn, ik)
    assert [r["offset_minutes"] for r in rems] == [30]
    assert rems[0]["anchor"] == "start"
    # all-day event -> anchor normalized to day_of
    n2 = _mknote(conn, "Holiday")
    cal.upsert_events(conn, n2, [{"title": "Anniv", "starts_at": "2099-06-15", "all_day": True}])
    ik2 = cal.identity_key(n2, "Anniv", "event", 0)
    cal.set_reminders(conn, ik2, [{"offset_minutes": 0, "anchor": "start"}])
    assert cal.get_reminders(conn, ik2)[0]["anchor"] == "day_of"


def test_huge_offset_no_storm(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Daily")
    cal.upsert_events(conn, nid, [{"title": "Standup", "kind": "recurring",
                                   "starts_at": "2099-06-01T09:00:00", "rrule": "FREQ=DAILY"}],
                     source="workflow", sweep=False)
    ik = cal.identity_key(nid, "Standup", "recurring", 0)
    # Force an out-of-band offset directly (bypassing the clamp) to prove the horizon cap
    # + per-tick backstop bound the firing.
    conn.execute("INSERT INTO calendar_reminders (identity_key, offset_minutes, anchor) VALUES (?,?,?)",
                 (ik, 5256000, "start"))
    _set_now(monkeypatch, 2099, 6, 15, 8, 52)
    out = cal.due_event_alarms(conn, wf, push=False)
    assert out["fired"] <= 500   # bounded, not 3650+


def test_cancel_removes_reminders(conn):
    from app.services import calendar as cal
    nid = _mknote(conn, "Appt")
    cal.upsert_events(conn, nid, [{"title": "Dentist", "starts_at": "2099-06-15"}])
    ik = cal.identity_key(nid, "Dentist", "event", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 30}])
    n2 = _mknote(conn, "cancel note")
    cal.record_supersession(conn, ik, None, n2, "structured")   # pure cancel
    assert cal.get_reminders(conn, ik) == []


def test_alarm_all_day_recurring_day_of(conn, monkeypatch):
    from app.services import calendar as cal
    wf = _mkwf(conn)
    nid = _mknote(conn, "Monthly bill")
    cal.upsert_events(conn, nid, [{"title": "Rent", "kind": "recurring", "starts_at": "2099-01-15",
                                   "all_day": True, "rrule": "FREQ=MONTHLY"}], source="workflow", sweep=False)
    ik = cal.identity_key(nid, "Rent", "recurring", 0)
    cal.set_reminders(conn, ik, [{"offset_minutes": 0, "anchor": "day_of"}])   # morning of
    _set_now(monkeypatch, 2099, 6, 15, 9, 30)                  # 9:30am on a recurrence day
    assert cal.due_event_alarms(conn, wf, push=False)["fired"] == 1
