"""The lab-share security boundary. These are the load-bearing tests for the feature: an
out-of-allow-list analyte must be unreachable (default-deny), and NO brain-identity field
(note id/slug/title, encounter labels) may ever reach a recipient payload. Uses a raw SQLite
DB from schema.sql — no app/LLM needed."""
import sqlite3
from pathlib import Path

import pytest

from app.services import lab_share_scope as scope

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('s','notes/medical/Real-Name/labs','b')")
    nid = c.execute("SELECT id FROM notes WHERE slug='s'").fetchone()["id"]
    eid = c.execute("INSERT INTO encounters (kind, summary, facility, started_at, ended_at) "
                    "VALUES ('admission','Psych admission','Some Hospital','2025-01-01','2025-01-10') "
                    "RETURNING id").fetchone()["id"]

    def add(analyte, name, date, vtext, vnum, unit, lo, hi, enc=None):
        c.execute("INSERT INTO lab_results (note_id, encounter_id, test_name, analyte_key, value_text, "
                  "value_num, unit, ref_low, ref_high, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (nid, enc, name, analyte, vtext, vnum, unit, lo, hi, date))
    add("wbc", "WBC", "2024-06-01", "6.0", 6.0, "thou/cumm", 3.9, 11.0, eid)
    add("wbc", "WBC", "2025-06-01", "12.0", 12.0, "thou/cumm", 3.9, 11.0)
    add("hiv_ab", "HIV Ab", "2025-06-01", "0.1", 0.1, "index", 0.0, 1.0)   # sensitive, NOT shared
    c.commit()
    return c


def test_out_of_allowlist_analyte_is_unreachable(conn):
    allowed = {"wbc"}
    assert scope.series_scoped(conn, "wbc", allowed=allowed) is not None
    assert scope.series_scoped(conn, "hiv_ab", allowed=allowed) is None     # default-deny
    assert scope.stat_scoped(conn, "hiv_ab", allowed=allowed) is None
    assert scope.point_at_scoped(conn, "hiv_ab", "latest", allowed=allowed) is None
    # An empty / missing allow-list exposes NOTHING.
    assert scope.series_scoped(conn, "wbc", allowed=set()) is None
    assert scope.list_analytes_scoped(conn, set()) == []


def test_picklist_and_abnormal_limited_to_allowlist(conn):
    names = {a["analyte"] for a in scope.list_analytes_scoped(conn, {"wbc"})}
    assert names == {"wbc"} and "hiv_ab" not in names                       # never enumerate hiv
    abn = {a["analyte"] for a in scope.abnormal_scoped(conn, {"wbc"})}
    assert abn <= {"wbc"}


def test_no_identity_fields_reach_the_recipient(conn):
    s = scope.series_scoped(conn, "wbc", allowed={"wbc"})
    for p in s["points"]:
        for k in ("note_id", "note_slug", "note_title", "encounter_id"):
            assert k not in p                                               # no name/folder leak
    assert s["encounters"] == []                                           # no facility/admission leak
    st = scope.stat_scoped(conn, "wbc", allowed={"wbc"})
    for key in ("min", "max", "latest", "first"):
        if st[key]:
            assert "note_title" not in st[key] and "note_slug" not in st[key]


def test_window_clamps_domain(conn):
    s = scope.series_scoped(conn, "wbc", allowed={"wbc"}, dfrom="2025-01-01", dto="2025-12-31")
    assert [p["t"] for p in s["points"]] == ["2025-06-01"]                  # the 2024 point is gone
    assert s["domain"] == {"from": "2025-06-01", "to": "2025-06-01"}


def test_recipient_tool_set_excludes_dangerous_tools():
    scope.assert_recipient_tools_safe()                                    # must not raise
    assert "query_sql" not in scope.RECIPIENT_TOOLS
    assert set(scope.RECIPIENT_TOOLS).isdisjoint(scope.FORBIDDEN_RECIPIENT_TOOLS)
    # The assertion actually fires if the set is ever widened to a forbidden tool.
    saved = scope.RECIPIENT_TOOLS
    scope.RECIPIENT_TOOLS = saved + ("query_sql",)
    try:
        with pytest.raises(RuntimeError):
            scope.assert_recipient_tools_safe()
    finally:
        scope.RECIPIENT_TOOLS = saved
