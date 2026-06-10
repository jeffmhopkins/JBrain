"""Unit-ish coverage for trip detection / segmentation (app/services/trips.py).

trips.py reads the location trail (and `places`/`notes`/`people`/`trip_cursor`)
out of SQLite, so this is a DB test built the way test_lab_share_scope.py /
test_staged_verify.py do: a raw in-memory sqlite3 connection loaded from the real
app/schema.sql. No app bootstrap, network, or LLM involved.

All coordinates sit on the equator (lat=0) where 1 deg of longitude == 111320 m,
so a lon delta d (deg) is exactly d*111320 m east — letting us craft fixes whose
inter-point distances/speeds are known and assert concrete outputs. We seed the
`people` table with one default person (so source->person resolution + the
trip_cursor FK are satisfied) and write fixes straight into `locations`.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import trips

pytestmark = pytest.mark.integration

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"

# 1 degree of longitude at the equator, in metres (matches geo.haversine_km).
M_PER_DEG = 111320.0
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO people (name, is_default, aliases) VALUES ('Owner', 1, 'wear')")
    yield c
    c.close()


@pytest.fixture()
def writer_on(conn, monkeypatch):
    """Route the trips single-writer seam at this isolated in-memory ``conn``.

    ``run_detection``/``reattribute`` now persist through ``submit_write`` on the
    dedicated writer connection (``db.get_conn()`` on the writer thread). These
    tests inject their own ``:memory:`` connection with no app bootstrap, so the
    real writer would open an unrelated file DB. Patch the module seams to run the
    write unit inline against the test ``conn`` instead, preserving the test's
    isolation while exercising the refactored code path.
    """
    from concurrent.futures import Future

    def _inline(fn):
        f: Future = Future()
        try:
            f.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 — mirror submit_write's contract
            f.set_exception(exc)
        return f

    monkeypatch.setattr(trips, "get_conn", lambda: conn)
    monkeypatch.setattr(trips, "submit_write", _inline)
    return conn


def _person_id(conn) -> int:
    return conn.execute("SELECT id FROM people WHERE is_default=1").fetchone()["id"]


def _add_fix(conn, pid, lon_deg, t_s, *, lat_deg=0.0, accuracy=5.0, speed=None, source="wear"):
    """Insert a location fix at (lat_deg, lon_deg) recorded t_s seconds after T0."""
    conn.execute(
        "INSERT INTO locations (lat, lon, accuracy_m, recorded_at, source, person_id, speed_mps) "
        "VALUES (?,?,?,?,?,?,?)",
        (lat_deg, lon_deg, accuracy, _ts(t_s), source, pid, speed),
    )


# --------------------------------------------------------------------------- #
# Pure helpers (no DB rows needed beyond the connection / none at all)
# --------------------------------------------------------------------------- #

def test_secs_signed_and_malformed():
    assert trips._secs("2026-01-01 12:00:00", "2026-01-01 12:01:30") == 90.0
    assert trips._secs("2026-01-01 12:01:30", "2026-01-01 12:00:00") == -90.0
    # unparseable input is swallowed -> 0.0 (never raises mid-detection)
    assert trips._secs("not-a-date", "2026-01-01 12:00:00") == 0.0


def test_now_and_floor_str_format_and_ordering():
    now = trips._now_str()
    floor = trips._floor_str(90)
    assert datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    assert datetime.strptime(floor, "%Y-%m-%d %H:%M:%S")
    assert floor < now  # 90 days ago precedes now


def test_seg_point_m_endpoints_and_perpendicular():
    # Distance from P to segment A->B on the equator.
    # A=(0,0) B=(0,0.001)≈111.32m east. P on the segment -> ~0.
    assert trips._seg_point_m(0.0, 0.0005, 0.0, 0.0, 0.0, 0.001) < 1.0
    # P north of the midpoint by ~111.32m -> that perpendicular distance.
    d = trips._seg_point_m(0.001, 0.0005, 0.0, 0.0, 0.0, 0.001)
    assert abs(d - 111.32) < 2.0
    # Degenerate segment (A==B): falls back to point distance.
    d0 = trips._seg_point_m(0.0, 0.001, 0.0, 0.0, 0.0, 0.0)
    assert abs(d0 - 111.32) < 2.0


def test_qualifies_distance_or_duration_gate():
    assert trips._qualifies({"displacement_km": 0.4, "duration_s": 10}) is True   # >=300m
    assert trips._qualifies({"displacement_km": 0.05, "duration_s": 200}) is True  # >=120s
    assert trips._qualifies({"displacement_km": 0.05, "duration_s": 10}) is False  # neither


def test_place_at_inside_circle_vs_unlabeled(conn):
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Home', 0, 0, 150)")
    pid_in, label_in = trips._place_at(conn, 0.0, 0.0)
    assert label_in == "Home" and pid_in is not None
    # A coordinate with no place and no nearby note -> (None, None)
    far_id, far_label = trips._place_at(conn, 0.0, 1.0)
    assert far_id is None and far_label is None


# --------------------------------------------------------------------------- #
# _stays: stop vs move classification
# --------------------------------------------------------------------------- #

def test_stays_detects_dwell_cluster():
    # 4 co-located fixes (within STAY_RADIUS_M) spanning 600s (>= STAY_MIN_S=300) -> one stay.
    fixes = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.0005, "recorded_at": _ts(200)},   # ~55m, in radius
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(400)},
        {"lat": 0.0, "lon": 0.0005, "recorded_at": _ts(600)},
    ]
    assert trips._stays(fixes) == [(0, 3)]


def test_stays_brief_stop_is_not_a_stay():
    # Co-located but only 100s (< STAY_MIN_S) -> movement, no stay emitted.
    fixes = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.0003, "recorded_at": _ts(100)},
    ]
    assert trips._stays(fixes) == []


def test_stays_two_clusters_split_by_movement():
    # Cluster at 0, then a far cluster ~2.2km east; each held 600s.
    fixes = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(600)},      # stay A [0,1]
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(1200)},    # ~2226m east
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(1800)},    # stay B [2,3]
    ]
    assert trips._stays(fixes) == [(0, 1), (2, 3)]


# --------------------------------------------------------------------------- #
# _trip_metrics: distance / duration / speed / endpoints
# --------------------------------------------------------------------------- #

def test_trip_metrics_uses_device_speed_when_present(conn):
    # Two fixes 0.02 deg (~2226m) apart over 200s; device speeds 10 & 30 m/s.
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": 10.0},
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(200), "accuracy_m": 5.0, "speed_mps": 30.0},
    ]
    m = trips._trip_metrics(conn, seg)
    assert m["fix_count"] == 2
    assert m["duration_s"] == 200
    assert abs(m["displacement_km"] - 2.226) < 0.01
    # max speed prefers device GPS speed: max(10,30) m/s * 3.6 = 108.0 km/h
    assert m["max_speed_kmh"] == 108.0
    # avg = distance_km / hours; ~2.226 km over 200s (0.05556 h) ~= 40 km/h
    assert abs(m["avg_speed_kmh"] - 40.1) < 1.0


def test_trip_metrics_haversine_speed_fallback(conn):
    # No device speed -> filtered haversine/dt. ~2226m over 200s -> ~40.1 km/h.
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": None},
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(200), "accuracy_m": 5.0, "speed_mps": None},
    ]
    m = trips._trip_metrics(conn, seg)
    assert m["max_speed_kmh"] is not None and abs(m["max_speed_kmh"] - 40.1) < 1.0


def test_trip_metrics_zero_duration_and_jitter_no_speed(conn):
    # Same instant + tiny sub-jitter displacement: duration 0 -> avg None; the only
    # segment is filtered (dt<DT_MIN_S and below jitter floor) -> max None.
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": None},
        {"lat": 0.0, "lon": 0.00001, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": None},
    ]
    m = trips._trip_metrics(conn, seg)
    assert m["duration_s"] == 0
    assert m["avg_speed_kmh"] is None
    assert m["max_speed_kmh"] is None


def test_trip_metrics_skips_subjitter_segment_in_speed(conn):
    # 3 fixes, no device speed: first segment is sub-jitter (filtered at the floor),
    # the second is a real ~2226m/200s leg -> ~40 km/h is the only speed sample.
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": None},
        {"lat": 0.0, "lon": 0.0001, "recorded_at": _ts(60), "accuracy_m": 5.0, "speed_mps": None},  # ~11m < floor
        {"lat": 0.0, "lon": 0.02001, "recorded_at": _ts(260), "accuracy_m": 5.0, "speed_mps": None},
    ]
    m = trips._trip_metrics(conn, seg)
    assert m["max_speed_kmh"] is not None and abs(m["max_speed_kmh"] - 40.1) < 1.5


def test_crossings_ignores_far_place_and_counts_endpoint_inside(conn):
    # 'Far' is nowhere near the path -> skipped (no inside fix, segment never near).
    # 'End' contains only the LAST fix -> counted via the last-fix inside check.
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Far', 1.0, 1.0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('End', 0, 0.02, 150)")
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.01, "recorded_at": _ts(300)},
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(600)},   # inside 'End'
    ]
    cr = trips._crossings(conn, seg, exclude=set())
    assert [c["place_name"] for c in cr] == ["End"]


def test_trip_metrics_endpoints_set_from_places(conn):
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Home', 0, 0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Work', 0, 0.02, 150)")
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0), "accuracy_m": 5.0, "speed_mps": None},
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(600), "accuracy_m": 5.0, "speed_mps": None},
    ]
    m = trips._trip_metrics(conn, seg)
    assert m["start_place"] == "Home" and m["end_place"] == "Work"
    assert m["_endpoints"] == {m["start_place_id"], m["end_place_id"]}


# --------------------------------------------------------------------------- #
# _crossings: geofence pass-through detection
# --------------------------------------------------------------------------- #

def test_crossings_flyby_between_sparse_fixes(conn):
    # A place at the midpoint of a long east leg with no fix landing inside it:
    # segment-vs-circle still catches the fly-by (dwell 0).
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Gate', 0, 0.01, 150)")
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(600)},
    ]
    cr = trips._crossings(conn, seg, exclude=set())
    assert len(cr) == 1 and cr[0]["place_name"] == "Gate"
    assert cr[0]["dwell_s"] == 0


def test_crossings_excludes_endpoints_and_computes_dwell(conn):
    # Endpoint place excluded; an intermediate place with two in-radius fixes -> dwell.
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Start', 0, 0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Mid', 0, 0.01, 200)")
    start_id = conn.execute("SELECT id FROM places WHERE name='Start'").fetchone()["id"]
    seg = [
        {"lat": 0.0, "lon": 0.0, "recorded_at": _ts(0)},
        {"lat": 0.0, "lon": 0.01, "recorded_at": _ts(100)},      # inside Mid
        {"lat": 0.0, "lon": 0.0101, "recorded_at": _ts(220)},    # inside Mid, +120s
        {"lat": 0.0, "lon": 0.02, "recorded_at": _ts(400)},
    ]
    cr = trips._crossings(conn, seg, exclude={start_id})
    names = [c["place_name"] for c in cr]
    assert names == ["Mid"]            # Start excluded
    assert cr[0]["dwell_s"] == 120     # 100s -> 220s inside Mid


# --------------------------------------------------------------------------- #
# Cursor management
# --------------------------------------------------------------------------- #

def test_ensure_cursor_creates_then_returns(conn):
    pid = _person_id(conn)
    first = trips.ensure_cursor(conn, pid, backfill_days=90)
    assert first["person_id"] == pid and first["watermark"] == first["backfilled_to"]
    # Idempotent: a second call returns the existing row, no duplicate insert.
    again = trips.ensure_cursor(conn, pid)
    assert again["watermark"] == first["watermark"]
    assert conn.execute("SELECT COUNT(*) c FROM trip_cursor").fetchone()["c"] == 1


def test_rewind_cursor_moves_back_and_clamps_to_floor(conn):
    pid = _person_id(conn)
    trips.ensure_cursor(conn, pid)
    # Push the watermark forward to a known recent value first.
    conn.execute("UPDATE trip_cursor SET watermark='2026-06-01 00:00:00' WHERE person_id=?", (pid,))
    # An older fix rewinds the watermark back to it.
    trips.rewind_cursor(conn, pid, "2026-05-01 00:00:00")
    assert conn.execute("SELECT watermark FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()[0] == "2026-05-01 00:00:00"
    # A pre-floor target is clamped to backfilled_to, never reprocessing older history.
    floor = conn.execute("SELECT backfilled_to FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()[0]
    trips.rewind_cursor(conn, pid, "1990-01-01 00:00:00")
    assert conn.execute("SELECT watermark FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()[0] == floor
    # A newer target does NOT advance the watermark (rewind only).
    trips.rewind_cursor(conn, pid, "2030-01-01 00:00:00")
    assert conn.execute("SELECT watermark FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()[0] == floor


def test_rewind_cursor_noop_for_none_person(conn):
    trips.rewind_cursor(conn, None, "2026-01-01 00:00:00")  # must not raise / write
    assert conn.execute("SELECT COUNT(*) c FROM trip_cursor").fetchone()["c"] == 0


def test_rewind_cursor_creates_when_missing(conn):
    pid = _person_id(conn)
    trips.rewind_cursor(conn, pid, "2026-01-01 00:00:00")  # no cursor yet -> ensure
    assert conn.execute("SELECT COUNT(*) c FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()["c"] == 1


# --------------------------------------------------------------------------- #
# detect_for_person: end-to-end segmentation
# --------------------------------------------------------------------------- #

def _seed_two_stays_one_closed_trip(conn, pid):
    """Stay at Home (0,0) for 10 min, drive ~2.2km east, stay at Work (0,0.02) for 10 min."""
    # Set a backfill floor BEFORE our fixes so the window includes them.
    conn.execute(
        "INSERT INTO trip_cursor (person_id, watermark, backfilled_to) VALUES (?,?,?)",
        (pid, _ts(-3600), _ts(-3600)),
    )
    # Stay A (indices 0..3): co-located, spanning 900s.
    for k, t in enumerate((0, 300, 600, 900)):
        _add_fix(conn, pid, 0.0 if k % 2 == 0 else 0.0003, t)
    # Move east (indices 4..6).
    _add_fix(conn, pid, 0.006, 1100, speed=15.0)
    _add_fix(conn, pid, 0.013, 1300, speed=20.0)
    _add_fix(conn, pid, 0.02, 1500, speed=12.0)
    # Stay B (indices 7..10): co-located at ~0.02, spanning 900s.
    for k, t in enumerate((1500, 1800, 2100, 2400)):
        _add_fix(conn, pid, 0.02 if k % 2 == 0 else 0.0203, t)
    conn.commit()


def test_detect_for_person_closed_trip_between_two_stays(conn):
    pid = _person_id(conn)
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Home', 0, 0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Work', 0, 0.02, 150)")
    _seed_two_stays_one_closed_trip(conn, pid)

    written = trips.detect_for_person(conn, pid)
    assert written == 1
    rows = trips.query_trips(conn, person_id=pid)
    assert len(rows) == 1
    t = rows[0]
    assert t["status"] == "closed"
    assert t["start_place"] == "Home" and t["end_place"] == "Work"
    assert abs(t["displacement_km"] - 2.226) < 0.05
    assert t["duration_s"] > 0
    # The watermark advanced to the last stay's start so the closed window won't re-run.
    wm = conn.execute("SELECT watermark FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()[0]
    assert wm == _ts(1500)


def test_detect_for_person_idempotent_rerun(conn):
    pid = _person_id(conn)
    _seed_two_stays_one_closed_trip(conn, pid)
    first = trips.detect_for_person(conn, pid)
    # Rewind so the second pass re-segments the same window, then confirm no duplication.
    trips.rewind_cursor(conn, pid, _ts(0))
    trips.detect_for_person(conn, pid)
    assert first == 1
    assert conn.execute("SELECT COUNT(*) c FROM trips WHERE person_id=?", (pid,)).fetchone()["c"] == 1


def test_detect_for_person_open_trailing_trip(conn):
    pid = _person_id(conn)
    conn.execute(
        "INSERT INTO trip_cursor (person_id, watermark, backfilled_to) VALUES (?,?,?)",
        (pid, _ts(-3600), _ts(-3600)),
    )
    # Stay (0..3) then a trailing move that never closes with a stay -> OPEN trip.
    for k, t in enumerate((0, 300, 600, 900)):
        _add_fix(conn, pid, 0.0 if k % 2 == 0 else 0.0003, t)
    _add_fix(conn, pid, 0.01, 1100, speed=15.0)
    _add_fix(conn, pid, 0.02, 1300, speed=18.0)
    conn.commit()

    written = trips.detect_for_person(conn, pid)
    assert written == 1
    t = trips.query_trips(conn, person_id=pid)[0]
    assert t["status"] == "open"


def test_detect_for_person_too_few_fixes_returns_zero(conn):
    pid = _person_id(conn)
    trips.ensure_cursor(conn, pid, backfill_days=0)
    _add_fix(conn, pid, 0.0, 0)  # a single fix
    conn.commit()
    assert trips.detect_for_person(conn, pid) == 0
    assert trips.query_trips(conn, person_id=pid) == []


def test_detect_for_person_trip_before_first_stay(conn):
    # First-run / backfill: movement with no known origin that arrives at a stay.
    # No leading stay -> the "before the first stay" branch emits a closed trip.
    pid = _person_id(conn)
    conn.execute(
        "INSERT INTO trip_cursor (person_id, watermark, backfilled_to) VALUES (?,?,?)",
        (pid, _ts(-3600), _ts(-3600)),
    )
    # Move east (no opening stay) ...
    _add_fix(conn, pid, 0.0, 0, speed=15.0)
    _add_fix(conn, pid, 0.01, 200, speed=18.0)
    _add_fix(conn, pid, 0.02, 400, speed=12.0)
    # ... then a closing stay (indices 3..6) co-located, spanning 900s.
    for k, t in enumerate((400, 700, 1000, 1300)):
        _add_fix(conn, pid, 0.02 if k % 2 == 0 else 0.0203, t)
    conn.commit()

    written = trips.detect_for_person(conn, pid)
    assert written == 1
    t = trips.query_trips(conn, person_id=pid)[0]
    assert t["status"] == "closed"
    # No leading place -> unlabeled origin.
    assert t["start_place"] is None


def test_detect_for_person_skips_zero_length_inter_stay_gap(conn):
    # Two stays that share their boundary fix (no fixes between) -> the inter-stay
    # segment has < 2 fixes and is skipped; only later branches may emit.
    pid = _person_id(conn)
    conn.execute(
        "INSERT INTO trip_cursor (person_id, watermark, backfilled_to) VALUES (?,?,?)",
        (pid, _ts(-3600), _ts(-3600)),
    )
    # Long single stay (everything co-located, 1800s) -> exactly one stay, no trips.
    for k, t in enumerate((0, 300, 600, 900, 1200, 1500, 1800)):
        _add_fix(conn, pid, 0.0 if k % 2 == 0 else 0.0003, t)
    conn.commit()
    assert trips.detect_for_person(conn, pid) == 0
    assert trips.query_trips(conn, person_id=pid) == []


def test_run_detection_all_people(conn, writer_on):
    pid = _person_id(conn)
    _seed_two_stays_one_closed_trip(conn, pid)
    total = trips.run_detection(conn)
    assert total == 1
    assert conn.execute("SELECT COUNT(*) c FROM trips").fetchone()["c"] == 1


def test_run_detection_respects_max_people(conn, writer_on):
    # A second person exists; max_people=0 means process nobody.
    conn.execute("INSERT INTO people (name, aliases) VALUES ('Other', 'phone')")
    pid = _person_id(conn)
    _seed_two_stays_one_closed_trip(conn, pid)
    assert trips.run_detection(conn, max_people=0) == 0
    assert conn.execute("SELECT COUNT(*) c FROM trips").fetchone()["c"] == 0


# --------------------------------------------------------------------------- #
# Read APIs: query_trips / trip_detail
# --------------------------------------------------------------------------- #

def test_query_trips_filters_and_limit(conn):
    pid = _person_id(conn)
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Home', 0, 0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Work', 0, 0.02, 150)")
    _seed_two_stays_one_closed_trip(conn, pid)
    trips.detect_for_person(conn, pid)

    # since after the trip excludes it; since before includes it.
    assert trips.query_trips(conn, person_id=pid, since=_ts(10000)) == []
    inc = trips.query_trips(conn, person_id=pid, since=_ts(-100), until=_ts(10000), limit=5)
    assert len(inc) == 1
    # limit is clamped into [1,200].
    assert len(trips.query_trips(conn, person_id=pid, limit=0)) == 1


def test_query_trips_without_person_filter(conn):
    pid = _person_id(conn)
    _seed_two_stays_one_closed_trip(conn, pid)
    trips.detect_for_person(conn, pid)
    # No person_id -> the WHERE-person branch is skipped; all trips returned.
    assert len(trips.query_trips(conn)) == 1


def test_run_detection_swallows_per_person_error(conn, writer_on, monkeypatch):
    # One person's bad data must not wedge the rest: detect raises -> rollback, total 0.
    _person_id(conn)

    def _boom(*a, **k):
        raise RuntimeError("bad fix stream")

    monkeypatch.setattr(trips, "detect_for_person", _boom)
    assert trips.run_detection(conn) == 0


def test_trip_detail_with_crossing_and_missing(conn):
    pid = _person_id(conn)
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Home', 0, 0, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Work', 0, 0.02, 150)")
    conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES ('Gate', 0, 0.01, 200)")
    _seed_two_stays_one_closed_trip(conn, pid)
    trips.detect_for_person(conn, pid)

    tid = trips.query_trips(conn, person_id=pid)[0]["id"]
    detail, places = trips.trip_detail(conn, tid)
    assert detail["id"] == tid
    # The mid 'Gate' geofence is recorded as a pass-through (start/end excluded).
    assert [p["place_name"] for p in places] == ["Gate"]
    # A non-existent trip id -> None.
    assert trips.trip_detail(conn, 999999) is None


# --------------------------------------------------------------------------- #
# reattribute
# --------------------------------------------------------------------------- #

def test_reattribute_resolves_and_rewinds(conn, writer_on):
    pid = _person_id(conn)
    trips.ensure_cursor(conn, pid)
    conn.execute("UPDATE trip_cursor SET watermark='2026-06-01 00:00:00' WHERE person_id=?", (pid,))
    # A fix whose source has no person_id yet (alias 'wear' resolves to default Owner).
    conn.execute("INSERT INTO locations (lat, lon, recorded_at, source) VALUES (0,0,?, 'wear')", (_ts(0),))
    conn.commit()

    trips.reattribute(conn)
    # Source 'wear' is an alias of the default person -> fix attributed.
    assert conn.execute("SELECT person_id FROM locations WHERE source='wear'").fetchone()[0] == pid
    # Cursors were rewound to their backfill floor for a fresh segmentation.
    row = conn.execute("SELECT watermark, backfilled_to FROM trip_cursor WHERE person_id=?", (pid,)).fetchone()
    assert row["watermark"] == row["backfilled_to"]
