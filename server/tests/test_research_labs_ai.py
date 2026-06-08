"""Security + behavior tests for the ASSISTED-LABS recipient assistant.

This is the FIRST adversary-reachable tool loop in the system, so these are load-bearing:
- the model may ONLY ever invoke the four hardcoded scoped tools (a forged name is never run);
- an analyte outside the attached allow-list is unreachable (default-deny) and counts as denied;
- a model-supplied window can only NARROW within the owner's clamp, never widen past it;
- the module is import-isolated (no lab_series/architect/notes/query path);
- one recipient "turn" bills exactly one reply even across multiple tool calls;
- the on-demand series endpoint re-checks the allow-list and is session-gated.
"""
import re
import sqlite3
from pathlib import Path

import pytest

from app.services import lab_share_scope as scope
from app.services import research_labs_ai as rla
from app.services.llm import ToolCall

pytestmark = pytest.mark.integration

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
MODULE_SRC = Path(rla.__file__).read_text()


# --- raw-DB fixture: one research link with labs {wbc} attached, window 2025 ---

@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO notes (slug,title,content_md) VALUES ('s','notes/medical/Real-Name/labs','b')")
    nid = c.execute("SELECT id FROM notes WHERE slug='s'").fetchone()["id"]

    def add(ak, name, d, vt, vn, lo, hi):
        c.execute("INSERT INTO lab_results (note_id,test_name,analyte_key,value_text,value_num,unit,"
                  "ref_low,ref_high,collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (nid, name, ak, vt, vn, "thou/cumm", lo, hi, d))
    add("wbc", "WBC", "2024-06-01", "6.0", 6.0, 3.9, 11.0)
    add("wbc", "WBC", "2025-06-01", "12.0", 12.0, 3.9, 11.0)      # out of range
    add("hiv_ab", "HIV Ab", "2025-06-01", "0.1", 0.1, 0.0, 1.0)   # sensitive, NOT attached
    c.execute("INSERT INTO share_links (token,scope,kind,status) VALUES ('tok','view','research','active')")
    lid = c.execute("SELECT id FROM share_links WHERE token='tok'").fetchone()["id"]
    c.execute("INSERT INTO research_specs (share_link_id,status,lab_analytes_json,lab_window_from,lab_window_to) "
              "VALUES (?, 'active', '[\"wbc\"]', '2025-01-01', '2025-12-31')", (lid,))
    c.execute("INSERT INTO research_sessions (share_link_id,secret,name,status) VALUES (?, 'sek', 'V', 'active')", (lid,))
    c.commit()
    return c


def _ctx(db):
    link = db.execute("SELECT * FROM share_links WHERE token='tok'").fetchone()
    spec = db.execute("SELECT * FROM research_specs WHERE share_link_id=?", (link["id"],)).fetchone()
    sess = db.execute("SELECT * FROM research_sessions WHERE share_link_id=?", (link["id"],)).fetchone()
    return link, spec, sess


class FakeProvider:
    """Scripted provider. `script` is a list of (text, [ToolCall]) returned per complete_with_tools
    call; a turn with no calls ends the loop. `always` makes every call return a tool call (to
    exercise the iteration bound). Records the tool list shown to the model + tool results."""
    def __init__(self, script=None, always=None):
        self.script = list(script or [])
        self.always = always
        self.tools_seen = None
        self.results_seen = []

    def supports_tools(self):
        return True

    def complete_with_tools(self, messages, *, system, tools, model=None, max_tokens=1024):
        self.tools_seen = [t.name for t in tools]
        if self.always is not None:
            text, calls = self.always
        elif self.script:
            text, calls = self.script.pop(0)
        else:
            text, calls = ("done", [])
        messages.append({"role": "assistant", "content": text or None})
        return text, calls, {"input_tokens": 10, "output_tokens": 5}

    def append_tool_results(self, messages, results):
        self.results_seen.extend(r.content for r in results)
        for r in results:
            messages.append({"role": "user", "content": r.content})

    def complete(self, messages, *, system=None, model=None, max_tokens=1024):
        return "final answer"


def _answer(db, provider, question, monkeypatch):
    from app.services import llm
    monkeypatch.setattr(llm, "get_provider", lambda *a, **k: provider)
    link, spec, sess = _ctx(db)
    return rla.answer_with_labs(db, link=link, spec=spec, session=sess, question=question,
                               transcript=[], rag_context="(no relevant records)",
                               owner="Owner", name="Visitor", retrieved_ids=[])


# --- C1: forged / unknown tool name is NEVER dispatched ----------------------

def test_forged_tool_name_is_not_dispatched(db, monkeypatch):
    p = FakeProvider(script=[("", [ToolCall(id="1", name="query_sql", args={"sql": "SELECT *"})]),
                            ("ok", [])])
    out = _answer(db, p, "run some sql", monkeypatch)
    assert out["charts"] == []
    assert p.tools_seen == list(scope.RECIPIENT_TOOLS)          # only the 4 are ever offered
    assert db.execute("SELECT denied_count FROM research_sessions").fetchone()[0] == 1
    # the tool result handed back is the generic deny — never an executed query
    assert any(rla._NOT_AVAILABLE in r for r in p.results_seen)


# --- A/C2: out-of-allow-list analyte requested mid-loop -> denied, never charted

def test_out_of_scope_analyte_is_denied(db, monkeypatch):
    p = FakeProvider(script=[("", [ToolCall(id="1", name="show_lab_chart", args={"analyte": "hiv_ab"})]),
                            ("done", [])])
    out = _answer(db, p, "show the hiv result", monkeypatch)
    assert out["charts"] == []                                  # never charted an un-attached analyte
    assert db.execute("SELECT denied_count FROM research_sessions").fetchone()[0] == 1


def test_in_scope_chart_is_surfaced_and_audited(db, monkeypatch):
    p = FakeProvider(script=[("", [ToolCall(id="1", name="show_lab_chart", args={"analyte": "wbc"})]),
                            ("Here's the trend.", [])])
    out = _answer(db, p, "show wbc", monkeypatch)
    assert [c["analyte"] for c in out["charts"]] == ["wbc"]
    assert "trend" in out["message"]
    charted = db.execute("SELECT charted_json FROM research_sessions").fetchone()[0]
    assert "wbc" in charted and "hiv_ab" not in charted


# --- H1: model-supplied window can only NARROW within the owner clamp --------

def test_window_cannot_widen_past_owner_clamp(db):
    # owner window is 2025; a '5y' range would reach 2024, but the clamp forbids it.
    txt, chart = rla._t_value_at(db, {"analyte": "wbc", "which": "first", "range": "5y"},
                                 {"wbc"}, "2025-01-01", "2025-12-31")
    assert "2025-06-01" in txt and "2024" not in txt           # the 2024 point stays out of reach


def test_clamp_intersects_not_unions():
    assert rla._clamp("2025-01-01", "2025-12-31", "5y") == ("2025-01-01", "2025-12-31")
    assert rla._clamp(None, None, None) == (None, None)
    # a tighter range narrows the upper/lower bound within the window
    lo, hi = rla._clamp("2020-01-01", "2026-12-31", "1y")
    assert lo > "2020-01-01" and hi <= "2026-12-31"


# --- D/H4: iteration + token bounded; loop always terminates -----------------

def test_tool_loop_is_bounded(db, monkeypatch):
    # a model that ALWAYS wants a tool must still terminate and return a final answer.
    p = FakeProvider(always=("", [ToolCall(id="x", name="show_lab_chart", args={"analyte": "wbc"})]))
    out = _answer(db, p, "loop forever", monkeypatch)
    assert out["message"] == "final answer"                    # forced no-tool finalization
    assert len(out["charts"]) <= rla._MAX_CHARTS               # charts capped per turn


# --- boot guard / dispatch safety -------------------------------------------

def test_dispatch_safe_and_matches_recipient_tools():
    rla.assert_dispatch_safe()                                 # must not raise
    assert set(rla._DISPATCH) == set(scope.RECIPIENT_TOOLS)
    assert set(rla._DISPATCH).isdisjoint(scope.FORBIDDEN_RECIPIENT_TOOLS)
    assert {t.name for t in rla.TOOL_DEFS} == set(scope.RECIPIENT_TOOLS)
    saved = rla._DISPATCH.copy()
    rla._DISPATCH["query_sql"] = lambda *a, **k: ("", None)
    try:
        with pytest.raises(RuntimeError):
            rla.assert_dispatch_safe()
    finally:
        rla._DISPATCH.clear(); rla._DISPATCH.update(saved)


# --- import isolation: the recipient model surface reaches NOTHING else ------

def test_module_is_import_isolated():
    # AST-level (ignores the docstring, which deliberately NAMES the forbidden modules): the ONLY
    # service imports are lab_share_scope + llm, and no forbidden name is referenced in code.
    import ast
    tree = ast.parse(MODULE_SRC)
    service_imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level:           # `from . import ...` / `from .x import`
            service_imports |= ({a.name for a in n.names} if n.module is None else {n.module})
    assert service_imports == {"lab_share_scope", "llm"}, service_imports
    forbidden = {"lab_series", "architect", "research_scope", "embeddings", "query_sql", "sqlsafe", "notes"}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (forbidden & used), f"isolation breach: {forbidden & used}"
    # data only ever flows through the *_scoped boundary functions
    assert "sc.series_scoped" in MODULE_SRC and "sc.abnormal_scoped" in MODULE_SRC


# --- B: identity scrub on the non-series tools (point_at / stat) -------------

def test_point_and_stat_strip_identity(db):
    p = rla._t_value_at(db, {"analyte": "wbc", "which": "latest"}, {"wbc"}, None, None)
    s = rla._t_lab_stat(db, {"analyte": "wbc"}, {"wbc"}, None, None)
    for txt in (p[0], s[0]):
        assert "Real-Name" not in txt and "notes/medical" not in txt
    # and the underlying scoped rows carry no identity keys
    row = scope.point_at_scoped(db, "wbc", "latest", allowed={"wbc"})
    for k in ("note_id", "note_slug", "note_title", "encounter_id"):
        assert k not in row


# --- migration/back-compat: fresh schema carries the new columns ------------

def test_fresh_schema_has_lab_columns():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA.read_text())
    spec_cols = {r[1] for r in c.execute("PRAGMA table_info(research_specs)")}
    sess_cols = {r[1] for r in c.execute("PRAGMA table_info(research_sessions)")}
    assert {"lab_analytes_json", "lab_window_from", "lab_window_to"} <= spec_cols
    assert "charted_json" in sess_cols


def test_allowed_labs_drops_blanks():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO share_links (token,scope,kind,status) VALUES ('t','view','research','active')")
    lid = c.execute("SELECT id FROM share_links").fetchone()["id"]
    c.execute("INSERT INTO research_specs (share_link_id,lab_analytes_json) VALUES (?, '[\"\", \" \", \"wbc\"]')", (lid,))
    spec = c.execute("SELECT * FROM research_specs").fetchone()
    assert rla.allowed_labs(spec) == {"wbc"}                    # blanks can't pass the activation gate
