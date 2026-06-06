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
    assert get_meta("calendar:watermark") == "2099-07-01 09:00:00"


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
