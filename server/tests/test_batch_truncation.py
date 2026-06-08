"""Batch-writer truncation guard.

PR #97 stopped the LIVE rebuild engine from saving an article whose trailing
"## References" got cut off at the token limit (it reads stop_reason off the streaming
TurnEnd). The BATCH path (wiki_build.write_one / maintain_one) used llm.complete(), which
returns only text and never sees the finish reason — so a truncated draft was saved
silently. These tests pin the mirrored protection: complete_with_meta surfaces the finish
reason, the batch writers retry once at a bigger cap, then FAIL rather than save half an
article.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the model.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn(monkeypatch):
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY="test-access-key-1234567890",
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services import embeddings, entity_index
    for name in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
                 "delete_attachment_embeddings", "delete_entity_embedding"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)
    return c


# ---- helpers ----------------------------------------------------------------------------

_FULL_ARTICLE = (
    "# Ford Truck\n\n"
    "A truck owned by Jeff Hopkins.[^s1]\n\n"
    "## References\n\n"
    "[^s1]: [[notes/2026/02/03]] — 2026-02-03\n"
)


def _source_note(conn, title="notes/2026/02/03", body="Jeff Hopkins bought a Ford truck."):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind="entry", fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _art(src_id):
    return {"title": "kb/Things/Ford Truck", "domain": "Things",
            "scope": "a truck", "sources": [src_id]}


# ---- write_one truncation guard ---------------------------------------------------------

def test_write_one_retries_after_truncation_then_succeeds(conn, monkeypatch):
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    calls = []

    def fake_cwm(messages, **kw):
        calls.append(kw.get("max_tokens"))
        if len(calls) == 1:
            return ("# X\n\nlead...\n", "max_tokens")     # first call truncated
        return (_FULL_ARTICLE, None)                       # retry returns a full article
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 2                                  # it retried exactly once
    assert calls[1] > calls[0]                              # at a higher cap
    assert out["ok"] is True
    assert out["content_md"].strip().endswith("2026-02-03")  # the full draft was kept


def test_write_one_fails_when_always_truncated(conn, monkeypatch):
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete_with_meta",
                        lambda messages, **kw: ("# X\n\nlead...\n", "max_tokens"))

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert out["ok"] is False
    assert out["content_md"] == ""
    assert "draft truncated at the token limit" in out["errors"]


def test_write_one_no_truncation_is_normal(conn, monkeypatch):
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        return (_FULL_ARTICLE, None)                        # stop_reason None → not truncated
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 1                                  # no retry on a clean finish
    assert out["ok"] is True
    assert out["content_md"].strip().endswith("2026-02-03")


# ---- maintain_one truncation guard ------------------------------------------------------

def test_maintain_one_fails_when_truncated(conn, monkeypatch):
    from app.services import wiki_build, article_talk, llm, notes as notes_svc
    notes_svc.upsert_note(conn, "kb/Things/Ford Truck", _FULL_ARTICLE, kind="kb", fire_events=False)
    conn.commit()
    article_talk.add(conn, "kb/Things/Ford Truck", "todo", "Add the year of purchase.")
    conn.commit()
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete_with_meta",
                        lambda messages, **kw: ("```article\n# Ford Truck\n\ncut", "length"))

    out = wiki_build.maintain_one(conn, "kb/Things/Ford Truck", known_titles=[])
    assert out["ok"] is False
    assert out["changed"] is False
    assert "maintain output truncated" in out["errors"]


# ---- llm.complete_with_meta wrapper shape -----------------------------------------------

def test_complete_with_meta_wrapper_returns_text_and_stop_reason(monkeypatch):
    from app.services import llm

    class FakeProvider:
        def complete_with_meta(self, messages, *, system=None, model=None, max_tokens=1024):
            return ("hello", "max_tokens")

    monkeypatch.setattr(llm, "get_provider", lambda model=None: FakeProvider())
    text, stop = llm.complete_with_meta([{"role": "user", "content": "hi"}], max_tokens=50)
    assert text == "hello"
    assert stop == "max_tokens"


def test_complete_delegates_and_drops_stop_reason(monkeypatch):
    from app.services import llm

    # Use the real AnthropicProvider so we exercise its complete()->complete_with_meta
    # delegation; stub the network call (client.messages.create) at the SDK boundary.
    class _Msg:
        content = [type("B", (), {"type": "text", "text": "body text"})()]
        usage = None
        stop_reason = "end_turn"

    class _Client:
        def __init__(self, *a, **k):
            self.messages = type("M", (), {"create": lambda _s, **kw: _Msg()})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr(llm, "get_provider", lambda model=None: llm.AnthropicProvider())
    # The legacy complete() wrapper still returns a bare string (no other caller breaks).
    assert llm.complete([{"role": "user", "content": "hi"}]) == "body text"
