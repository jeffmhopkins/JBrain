"""The external-reference capture→promote loop. Load-bearing safety: a "general" kb/Reference page
must inherit NO owner data, candidates capture only the public topic, promotion is STAGED (never
auto-live), repeated/duplicate topics don't spam, and a returned source URL is host-pinned to NLM."""
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from app.services import medref, reference_candidates as rc, reference_promote

pytestmark = pytest.mark.integration

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
    # Even if the model passes a PHI-laden term, once the owner approves the captured candidate holds
    # ONLY the MedlinePlus-resolved topic + source — never the query text or that value.
    from app.services import architect, external_lookups
    monkeypatch.setattr(medref, "health_topic",
                        lambda c, q: {"url": "https://medlineplus.gov/ttp.html", "title": "TTP",
                                      "snippet": "A rare blood disorder."})
    q = "do my platelets of 18 on 2026-05-30 fit TTP?"
    _, ev = architect._tool_medical_reference(conn, None, q)       # proposed (gated; nothing captured yet)
    external_lookups.decide(conn, ev["id"], approve=True)
    out, _ = architect._tool_medical_reference(conn, None, q)      # approved → fetch + capture
    assert "medlineplus.gov/ttp.html" in out and "not medical advice" in out.lower()
    row = conn.execute("SELECT * FROM reference_candidates").fetchone()
    assert row["topic"] == "TTP"                                  # the RESOLVED topic, not the query
    blob = " ".join(str(row[k]) for k in ("topic", "url", "snippet", "category", "source"))
    assert "18" not in blob and "2026-05-30" not in blob and "platelet" not in blob.lower()


# --- the approval gate: medical_reference sends nothing until the owner approves ----

def _boom(*a, **k):
    raise AssertionError("external fetch happened before approval!")


def test_medical_reference_gated_until_approved(conn, monkeypatch):
    from app.services import architect, external_lookups
    monkeypatch.setattr(medref, "health_topic", _boom)             # must NOT be called while pending
    txt, ev = architect._tool_medical_reference(conn, None, "thrombotic thrombocytopenic purpura")
    assert ev and ev["type"] == "external_proposal" and ev["term"].startswith("thrombotic")
    assert "PROPOSED" in txt
    assert conn.execute("SELECT status FROM external_lookups").fetchone()["status"] == "pending"
    assert conn.execute("SELECT COUNT(*) c FROM reference_candidates").fetchone()["c"] == 0   # nothing captured
    # owner approves → the fetch is now allowed, and the topic is captured for the loop
    monkeypatch.setattr(medref, "health_topic", lambda c, q: {"url": "https://medlineplus.gov/ttp.html",
                                                              "title": "Platelet Disorders", "snippet": "summary"})
    external_lookups.decide(conn, ev["id"], approve=True)
    txt2, ev2 = architect._tool_medical_reference(conn, None, "thrombotic thrombocytopenic purpura")
    assert ev2 is None and "medlineplus.gov/ttp.html" in txt2
    assert conn.execute("SELECT COUNT(*) c FROM reference_candidates").fetchone()["c"] == 1


def test_medical_reference_denied_never_fetches(conn, monkeypatch):
    from app.services import architect, external_lookups
    monkeypatch.setattr(medref, "health_topic", _boom)
    _, ev = architect._tool_medical_reference(conn, None, "atrial fibrillation")
    external_lookups.decide(conn, ev["id"], approve=False)
    txt, ev2 = architect._tool_medical_reference(conn, None, "atrial fibrillation")  # _boom still installed
    assert ev2 is None and "declined" in txt.lower()                # denied → no fetch, no re-propose


def test_medical_reference_owner_edits_term_on_approval(conn, monkeypatch):
    from app.services import architect, external_lookups
    fetched = []
    def fake(c, q):
        fetched.append(q)
        return {"url": "https://medlineplus.gov/ttp.html", "title": "Platelet Disorders", "snippet": "s"}
    # the model proposes a PHI-laden term — nothing is fetched
    monkeypatch.setattr(medref, "health_topic", _boom)
    _, ev = architect._tool_medical_reference(conn, None, "do my platelets of 18 fit TTP?")
    # owner EDITS it to a clean topic and approves
    monkeypatch.setattr(medref, "health_topic", fake)
    external_lookups.decide(conn, ev["id"], approve=True, term="thrombotic thrombocytopenic purpura")
    # the model re-calls with its ORIGINAL phrasing → matches, and the EDITED term is what gets sent
    txt, ev2 = architect._tool_medical_reference(conn, None, "do my platelets of 18 fit TTP?")
    assert ev2 is None and "medlineplus.gov/ttp.html" in txt
    assert fetched and fetched[-1] == "thrombotic thrombocytopenic purpura"   # edited term sent, NOT the PHI query
    # and a direct call with the edited topic is also already approved (no fresh proposal)
    _, ev3 = architect._tool_medical_reference(conn, None, "thrombotic thrombocytopenic purpura")
    assert ev3 is None


# --- drug_reference: the same gate + Medications capture, resolved via RxNorm ---------

def test_drug_reference_gated_and_captures_medication(conn, monkeypatch):
    from app.services import architect, external_lookups
    # The exact-match drug resolution (RxNorm → MedlinePlus Connect), host-pinned.
    monkeypatch.setattr(medref, "resolve", _boom)                  # must NOT resolve while pending
    txt, ev = architect._tool_drug_reference(conn, None, "metformin 500mg twice a day")
    assert ev and ev["type"] == "external_proposal" and ev["tool"] == "drug_reference"
    assert "PROPOSED" in txt
    assert conn.execute("SELECT COUNT(*) c FROM reference_candidates").fetchone()["c"] == 0   # nothing captured
    # owner trims the dose to a clean name and approves
    monkeypatch.setattr(medref, "resolve",
                        lambda c, n: {"match": "exact", "rxcui": "6809", "title": "Metformin: MedlinePlus Drug Information",
                                      "url": "https://medlineplus.gov/druginfo/meds/a696005.html",
                                      "summary": "Metformin treats type 2 diabetes. It works by restoring insulin response."})
    external_lookups.decide(conn, ev["id"], approve=True, term="metformin")
    txt2, ev2 = architect._tool_drug_reference(conn, None, "metformin 500mg twice a day")
    assert ev2 is None and "medlineplus.gov/druginfo" in txt2 and "rxcui 6809" in txt2
    assert "type 2 diabetes" in txt2 and "It works by" in txt2          # the summary, so it can actually explain it
    row = conn.execute("SELECT * FROM reference_candidates").fetchone()
    assert row["topic"] == "Metformin" and row["category"] == "Medications" and row["source"] == "rxnorm"
    assert "type 2 diabetes" in row["snippet"]                          # the public-domain summary is curated too
    # topic-only: no dose / owner phrasing leaked into the candidate
    blob = " ".join(str(row[k]) for k in ("topic", "url", "snippet", "category", "source"))
    assert "500" not in blob and "twice" not in blob


def test_drug_reference_approximate_match_not_captured(conn, monkeypatch):
    from app.services import architect, external_lookups
    _, ev = architect._tool_drug_reference(conn, None, "metformine")
    monkeypatch.setattr(medref, "resolve",
                        lambda c, n: {"match": "approx", "rxcui": "6809", "candidate": "metformin", "score": 90.0,
                                      "title": "Metformin", "url": "https://medlineplus.gov/druginfo/meds/a696005.html"})
    external_lookups.decide(conn, ev["id"], approve=True)
    txt, _ = architect._tool_drug_reference(conn, None, "metformine")
    assert "approximate match" in txt                              # surfaced as a link…
    assert conn.execute("SELECT COUNT(*) c FROM reference_candidates").fetchone()["c"] == 0   # …but not curated


def test_promote_medication_candidate_refetches_via_rxnorm(conn, monkeypatch):
    # A Medications candidate must re-fetch the drug page via resolve()/drug_topic, NOT health_topic
    # (which would find an unrelated health topic and cite the wrong URL).
    from app.services import architect
    monkeypatch.setattr(medref, "health_topic", _boom)            # promote must not use the topics search here
    monkeypatch.setattr(medref, "resolve",
                        lambda c, n: {"match": "exact", "rxcui": "6809", "title": "Metformin",
                                      "url": "https://medlineplus.gov/druginfo/meds/a696005.html"})
    conn.execute("INSERT INTO reference_candidates (topic,norm_key,source,url,snippet,category,hits) "
                 "VALUES ('Metformin', ?, 'rxnorm', 'https://medlineplus.gov/druginfo/meds/a696005.html', '', 'Medications', 3)",
                 (rc._norm("Metformin"),))
    conn.commit()
    out = reference_promote.run(conn, min_hits=2)
    assert out["promoted"] == 1
    act = conn.execute("SELECT payload_json FROM staging_actions WHERE status='pending'").fetchone()
    payload = json.loads(act["payload_json"])
    assert payload["title"] == "kb/Reference/Medicine/Medications/Metformin"
    assert "medlineplus.gov/druginfo" in payload["content"] and "medication you looked up" in payload["content"]


# --- staleness refresh: re-verify a seed's cited source past a TTL, owner prose preserved ----------

def _seed_note(conn, leaf, *, src, url, fetched, category="Conditions", extra=""):
    """Write a kb/Reference seed note (with provenance marker) and return its title."""
    from app.services import reference_promote as rp
    title = f"kb/Reference/Medicine/{category}/{leaf}"
    body = (f"# {leaf}\n\n_Reference seed — general background._\n\nA short public summary.\n\n"
            f"{extra}" + rp._source_block(src, url, fetched, leaf))
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES (?,?,?,'kb')",
                 (leaf.lower().replace(" ", "-"), title, body))
    conn.commit()
    return title, body


def test_promote_stub_carries_parseable_provenance(conn):
    from app.services import reference_promote as rp, reference_refresh
    cand = {"topic": "TTP", "category": "Conditions", "source": "medlineplus"}
    body = rp._stub(cand, "https://medlineplus.gov/ttp.html", "summary", "2026-06-06")
    prov = reference_refresh._provenance(body)
    assert prov == {"src": "medlineplus", "url": "https://medlineplus.gov/ttp.html", "fetched": "2026-06-06"}


def test_refresh_stages_citation_update_preserving_owner_prose(conn, monkeypatch):
    from app.services import reference_refresh
    title, _ = _seed_note(conn, "Atrial Fibrillation", src="medlineplus",
                          url="https://medlineplus.gov/afib-OLD.html", fetched="2020-01-01",
                          extra="## My own notes\n\nMy cardiologist said X on 2026-01-01.\n\n")
    monkeypatch.setattr(medref, "health_topic",
                        lambda c, q: {"url": "https://medlineplus.gov/afib-NEW.html", "title": "Atrial Fibrillation",
                                      "snippet": "s"})
    out = reference_refresh.run(conn, ttl_days=180)
    assert out["refreshed"] == 1
    act = conn.execute("SELECT type, payload_json FROM staging_actions WHERE status='pending'").fetchone()
    payload = json.loads(act["payload_json"])
    assert act["type"] == "UPDATE" and payload["title"] == title and payload["kind"] == "kb"
    assert payload["_basis"]["note_id"] and payload["_basis"]["content_hash"]     # optimistic-concurrency basis
    new = payload["content"]
    assert "afib-NEW.html" in new and "afib-OLD.html" not in new                  # citation re-seated
    assert 'fetched="2020-01-01"' not in new and reference_refresh._today() in new  # fetch date bumped
    assert "My cardiologist said X on 2026-01-01." in new                         # owner enrichment preserved
    # STAGED, never live — the note itself is untouched until the owner approves
    assert "afib-NEW" not in conn.execute("SELECT content_md FROM notes WHERE title=?", (title,)).fetchone()["content_md"]
    assert conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"] == 1


def test_refresh_skips_fresh_seed(conn, monkeypatch):
    from app.services import reference_refresh
    monkeypatch.setattr(medref, "health_topic", _boom)         # fresh → must not even re-fetch
    _seed_note(conn, "Hemolytic Anemia", src="medlineplus", url="https://medlineplus.gov/x.html",
               fetched=reference_refresh._today())
    assert reference_refresh.run(conn, ttl_days=180)["refreshed"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM staging_actions").fetchone()["c"] == 0


def test_refresh_skips_when_source_unreachable(conn, monkeypatch):
    from app.services import reference_refresh
    title, body = _seed_note(conn, "Rare Thing", src="medlineplus", url="https://medlineplus.gov/x.html",
                             fetched="2019-01-01")
    monkeypatch.setattr(medref, "health_topic", lambda c, q: None)   # source gone
    assert reference_refresh.run(conn, ttl_days=180)["refreshed"] == 0
    assert conn.execute("SELECT content_md FROM notes WHERE title=?", (title,)).fetchone()["content_md"] == body  # not mangled
    assert conn.execute("SELECT COUNT(*) c FROM staging_actions").fetchone()["c"] == 0


def test_refresh_drug_seed_refetches_via_rxnorm(conn, monkeypatch):
    from app.services import reference_refresh
    monkeypatch.setattr(medref, "health_topic", _boom)         # a drug seed must NOT use the topics search
    monkeypatch.setattr(medref, "resolve",
                        lambda c, n: {"match": "exact", "rxcui": "6809", "title": "Metformin",
                                      "url": "https://medlineplus.gov/druginfo/meds/NEW.html"})
    _seed_note(conn, "Metformin", src="rxnorm", url="https://medlineplus.gov/druginfo/meds/OLD.html",
               fetched="2019-01-01", category="Medications")
    out = reference_refresh.run(conn, ttl_days=180)
    assert out["refreshed"] == 1
    payload = json.loads(conn.execute("SELECT payload_json FROM staging_actions WHERE status='pending'"
                                      ).fetchone()["payload_json"])
    assert "druginfo/meds/NEW.html" in payload["content"] and "druginfo/meds/OLD.html" not in payload["content"]


def test_refresh_does_not_double_stage(conn, monkeypatch):
    from app.services import reference_refresh
    _seed_note(conn, "Atrial Fibrillation", src="medlineplus", url="https://medlineplus.gov/afib.html",
               fetched="2019-01-01")
    monkeypatch.setattr(medref, "health_topic",
                        lambda c, q: {"url": "https://medlineplus.gov/afib2.html", "title": "x", "snippet": "s"})
    assert reference_refresh.run(conn, ttl_days=180)["refreshed"] == 1
    assert reference_refresh.run(conn, ttl_days=180)["refreshed"] == 0   # already pending → not re-staged
    assert conn.execute("SELECT COUNT(*) c FROM staging_actions WHERE status='pending'").fetchone()["c"] == 1
