"""The external-reference capture→promote loop. Load-bearing safety: a "general" kb/Reference page
must inherit NO owner data, candidates capture only the public topic, promotion is STAGED (never
auto-live), repeated/duplicate topics don't spam, and a returned source URL is host-pinned to NLM."""
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from app.services import medref, reference_candidates as rc, reference_promote

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    return c


# --- host pinning (a response can't redirect us off NLM) --------------------

def test_host_pin_only_allows_nlm():
    assert medref._host_ok("https://medlineplus.gov/ency/article/000552.htm")
    assert medref._host_ok("https://www.nlm.nih.gov/x")
    assert not medref._host_ok("https://evil.example/medlineplus")
    assert not medref._host_ok("https://medlineplus.gov.evil.com/x")
    assert not medref._host_ok("")


def test_health_topic_rejects_off_nlm_href(conn, monkeypatch):
    xml = '<nlmSearchResult><list><document url="https://evil.example/ttp" rank="0">' \
          '<content name="title">TTP</content></document></list></nlmSearchResult>'
    monkeypatch.setattr(medref, "_http_get_text", lambda url: xml)
    assert medref.health_topic(conn, "ttp") is None          # off-NLM href → rejected


def test_health_topic_parses_and_pins(conn, monkeypatch):
    xml = '<nlmSearchResult><list><document url="https://medlineplus.gov/ttp.html" rank="0">' \
          '<content name="title">Thrombotic Thrombocytopenic Purpura</content>' \
          '<content name="FullSummary">A rare <span class="qt0">blood</span> disorder.</content>' \
          '</document></list></nlmSearchResult>'
    monkeypatch.setattr(medref, "_http_get_text", lambda url: xml)
    res = medref.health_topic(conn, "TTP")
    assert res["url"] == "https://medlineplus.gov/ttp.html"
    assert res["title"].startswith("Thrombotic") and "blood disorder" in res["snippet"]   # highlight tags stripped


# --- capture: topic-only, dedup, upsert -------------------------------------

def test_candidate_captures_topic_only_no_owner_data(conn):
    rc.record(conn, topic="Thrombotic Thrombocytopenic Purpura", url="https://medlineplus.gov/ttp.html",
              snippet="A rare blood disorder.")
    row = conn.execute("SELECT * FROM reference_candidates").fetchone()
    assert row["topic"].startswith("Thrombotic") and row["hits"] == 1 and row["status"] == "new"
    # the content columns carry no owner value/query — only the public topic + source (the only
    # dates in the row are its own first_seen/last_seen timestamps, which aren't owner data)
    blob = " ".join(str(row[k]) for k in ("topic", "norm_key", "source", "url", "snippet", "category"))
    assert "18" not in blob and "platelet" not in blob.lower()


def test_candidate_upserts_hits_not_rows(conn):
    for _ in range(3):
        rc.record(conn, topic="Atrial Fibrillation", url="https://medlineplus.gov/afib.html")
    rows = conn.execute("SELECT hits FROM reference_candidates").fetchall()
    assert len(rows) == 1 and rows[0]["hits"] == 3              # repeated interest, one row


def test_candidate_dedups_against_existing_reference_article(conn):
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES "
                 "('a','kb/Reference/Medicine/Conditions/Atrial Fibrillation','x','kb')")
    conn.commit()
    rc.record(conn, topic="Atrial Fibrillation", url="https://medlineplus.gov/afib.html")
    assert conn.execute("SELECT COUNT(*) c FROM reference_candidates").fetchone()["c"] == 0  # already curated


# --- promote: threshold, staged-never-live, no owner data, dedup ------------

def _cand(conn, topic, hits=2, cat="Conditions"):
    conn.execute("INSERT INTO reference_candidates (topic,norm_key,source,url,snippet,category,hits) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (topic, rc._norm(topic), "medlineplus", "https://medlineplus.gov/x.html",
                  "A short public summary.", cat, hits))
    conn.commit()


def test_promote_below_threshold_does_nothing(conn, monkeypatch):
    monkeypatch.setattr(medref, "health_topic", lambda c, q: None)
    _cand(conn, "Hemolytic Anemia", hits=1)
    out = reference_promote.run(conn, min_hits=2)
    assert out["promoted"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM staging_actions").fetchone()["c"] == 0


def test_promote_stages_stub_never_live(conn, monkeypatch):
    monkeypatch.setattr(medref, "health_topic",
                        lambda c, q: {"url": "https://medlineplus.gov/ttp.html",
                                      "title": q, "snippet": "A rare blood disorder."})
    _cand(conn, "Thrombotic Thrombocytopenic Purpura", hits=2)
    out = reference_promote.run(conn, min_hits=2)
    assert out["promoted"] == 1
    # staged as a pending CREATE — and NOT live in notes
    act = conn.execute("SELECT type, payload_json FROM staging_actions WHERE status='pending'").fetchone()
    payload = json.loads(act["payload_json"])
    assert act["type"] == "CREATE" and payload["kind"] == "kb"
    assert payload["title"] == "kb/Reference/Medicine/Conditions/Thrombotic Thrombocytopenic Purpura"
    assert "medlineplus.gov/ttp.html" in payload["content"] and "not medical advice" in payload["content"].lower()
    assert conn.execute("SELECT COUNT(*) c FROM notes WHERE title LIKE 'kb/Reference/%'").fetchone()["c"] == 0
    assert conn.execute("SELECT status FROM reference_candidates").fetchone()["status"] == "staged"
    # and a single review card was posted
    assert conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"] == 1


def test_promoted_stub_contains_no_owner_value():
    # the stub is built from source fields only — assert the formatter can't emit an owner figure
    cand = {"topic": "TTP", "category": "Conditions"}
    body = reference_promote._stub(cand, "https://medlineplus.gov/ttp.html", "A rare blood disorder.", "2026-06-06")
    assert "blood disorder" in body and "medlineplus.gov" in body
    assert "18" not in body and "platelet count of" not in body


def test_promote_skips_topic_already_articled(conn, monkeypatch):
    monkeypatch.setattr(medref, "health_topic", lambda c, q: {"url": "https://medlineplus.gov/x.html",
                                                              "title": q, "snippet": "s"})
    _cand(conn, "Hemolytic Anemia", hits=5)
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES "
                 "('h','kb/Reference/Medicine/Conditions/Hemolytic Anemia','x','kb')")
    conn.commit()
    out = reference_promote.run(conn, min_hits=2)
    assert out["promoted"] == 0
    assert conn.execute("SELECT status FROM reference_candidates").fetchone()["status"] == "published"


def test_medical_reference_tool_captures_resolved_topic_not_owner_query(conn, monkeypatch):
    # The owner's query carries PHI ("platelets of 18"); the captured candidate must hold ONLY the
    # MedlinePlus-resolved topic + source, never the query text or that value.
    from app.services import architect
    monkeypatch.setattr(medref, "health_topic",
                        lambda c, q: {"url": "https://medlineplus.gov/ttp.html", "title": "TTP",
                                      "snippet": "A rare blood disorder."})
    out = architect._tool_medical_reference(conn, "do my platelets of 18 on 2026-05-30 fit TTP?")
    assert "medlineplus.gov/ttp.html" in out and "not medical advice" in out.lower()
    row = conn.execute("SELECT * FROM reference_candidates").fetchone()
    assert row["topic"] == "TTP"                                  # the RESOLVED topic, not the query
    blob = " ".join(str(row[k]) for k in ("topic", "url", "snippet", "category", "source"))
    assert "18" not in blob and "2026-05-30" not in blob and "platelet" not in blob.lower()
