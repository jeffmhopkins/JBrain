"""Auto-continue a truncated draft instead of re-drafting / quarantining.

When a draft is cut off at the token cap (TurnEnd.stop_reason / complete_with_meta finish
reason == max_tokens|length), the engine RESUMES it in ONE follow-up turn and STITCHES the
pieces, preserving the already-generated content. The critical hazard the design protects:
the writer's trailing optional fences — a ```talk JSON block and the whole-body ```markdown
wrapper — must never be split across the seam. The safe design (proven here): concatenate
the RAW partial + RAW remainder and run _strip_fence / _extract_talk ONCE on the JOINED
string; cap at ONE continuation; fall back to the existing re-draft (live) / quarantine
(batch) if STILL truncated.

Covers BOTH paths:
  * LIVE  (rebuild_engine._generate, streaming): a fake provider streams a partial with
    stop_reason=max_tokens, then a remainder — exercising the in-stream join.
  * BATCH (wiki_build.write_one / maintain_one, non-streaming): llm.complete_with_meta is
    stubbed to return a truncated text then a remainder.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the model.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")


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


# ---- shared article fragments ----------------------------------------------------------

# A clean, complete writer output: body + trailing ## References + a ```talk block.
_LEAD = "A Ford truck that Jeff Hopkins bought in 2026 and uses daily for work."
_FULL = (
    "# Ford Truck\n\n"
    f"{_LEAD}[^s1]\n\n"
    "## References\n\n"
    "[^s1]: [[notes/2026/02/03]] — 2026-02-03\n\n"
    "```talk\n"
    '[{"kind": "note", "body": "Synthesised from the 2026 entries."}]\n'
    "```\n"
)


def _source_note(conn, title="notes/2026/02/03", body="Jeff bought a Ford truck in 2026."):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind="entry", fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _art(src_id):
    return {"title": "kb/Things/Ford Truck", "domain": "Things",
            "scope": "a truck", "sources": [src_id]}


# =========================================================================================
# LIVE path (rebuild_engine._generate, streaming)
# =========================================================================================

class _FakeProvider:
    """Streams scripted (text, stop_reason) segments, one per stream_turn() call, and — like
    the real adapters — appends the assistant turn to `messages` so run.messages grows exactly
    as it would in production (so run_redraft's unwind logic is exercised on real shapes)."""

    def __init__(self, segments):
        self._segments = list(segments)        # [(text, stop_reason), ...]
        self.calls = []                         # records (max_tokens, thinking) per call

    def has_credentials(self):
        return True

    def default_model(self):
        return "m"

    def supports_tools(self):
        return True

    async def stream_turn(self, messages, *, system, tools, model, max_tokens, thinking=False):
        from app.services.llm import TextDelta, TurnEnd
        self.calls.append({"max_tokens": max_tokens, "thinking": thinking})
        text, stop = self._segments.pop(0)
        yield TextDelta(text)
        messages.append({"role": "assistant", "content": text})   # mirror the real adapter
        yield TurnEnd([], usage=None, stop_reason=stop)


def _drive(run, conn):
    """Run the async _generate generator to completion synchronously, collecting events."""
    import asyncio
    from app.services import rebuild_engine

    async def go():
        return [ev async for ev in rebuild_engine._generate(run, conn)]

    return asyncio.new_event_loop().run_until_complete(go())


def _run(conn, model="m"):
    from app.services import rebuild_runs, wiki_build
    run = rebuild_runs.RebuildRun(run_id="r1", slug="s", title="kb/Things/Ford Truck",
                                  model=model, base_hash="h")
    run.known = wiki_build._known_titles(conn)
    run.messages = [{"role": "user", "content": "Write the article."}]
    return run


def _install(monkeypatch, provider):
    from app.services import llm
    monkeypatch.setattr(llm, "get_provider", lambda model=None: provider)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)


def test_live_truncation_mid_prose_continues_and_completes(conn, monkeypatch):
    _source_note(conn)
    # Cut off mid-body; the continuation supplies the rest, the References, and the talk block.
    partial = "# Ford Truck\n\nA truck owned by Jeff.[^s1]\n\n## Refe"
    remainder = ("rences\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03\n\n"
                 "```talk\n[{\"kind\": \"note\", \"body\": \"From the 2026 entries.\"}]\n```\n")
    prov = _FakeProvider([(partial, "max_tokens"), (remainder, "end_turn")])
    _install(monkeypatch, prov)
    run = _run(conn)

    events = _drive(run, conn)
    done = [e for e in events if e["type"] == "done"][0]
    assert len(prov.calls) == 2                       # one continuation turn
    assert prov.calls[1]["thinking"] is False         # continuation runs thinking-off
    assert done["truncated"] is False                 # recovered → not flagged truncated
    assert "## References" in run.draft
    assert run.draft.strip().endswith("2026-02-03")   # body completed, talk stripped out
    assert run.talk and run.talk[0]["body"] == "From the 2026 entries."
    assert "```talk" not in run.draft                 # no leaked JSON in the article


def test_live_truncation_mid_references_completes_refs(conn, monkeypatch):
    _source_note(conn)
    partial = "# Ford Truck\n\nA truck owned by Jeff.[^s1]\n\n## References\n\n[^s1]: [[notes/2026/0"
    remainder = "2/03]] — 2026-02-03\n"
    prov = _FakeProvider([(partial, "length"), (remainder, "stop")])
    _install(monkeypatch, prov)
    run = _run(conn)

    _drive(run, conn)
    assert "[[notes/2026/02/03]]" in run.draft         # the split footnote re-formed
    assert run.draft.strip().endswith("2026-02-03")


def test_live_truncation_splits_talk_fence_joined_extraction(conn, monkeypatch):
    """The hazard case: the ```talk fence is split across the seam. Per-segment extraction
    would corrupt it; the joined-once extraction yields the clean article AND the entries."""
    _source_note(conn)
    partial = ("# Ford Truck\n\nA truck owned by Jeff.[^s1]\n\n"
               "## References\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03\n\n"
               "```talk\n[{\"kind\": \"note\", \"bo")        # fence + JSON cut mid-key
    remainder = "dy\": \"Synthesised from the entries.\"}]\n```\n"
    prov = _FakeProvider([(partial, "max_tokens"), (remainder, "end_turn")])
    _install(monkeypatch, prov)
    run = _run(conn)

    _drive(run, conn)
    assert "```talk" not in run.draft                  # no leaked fence/JSON in the article
    assert '"body"' not in run.draft
    assert run.draft.strip().endswith("2026-02-03")
    assert run.talk and run.talk[0]["body"] == "Synthesised from the entries."


def test_live_one_continuation_cap_then_truncated_flag(conn, monkeypatch):
    """A still-truncated continuation is NOT retried again (one-cap); the draft is surfaced
    as truncated so the panel can offer the user-approved re-draft — no infinite loop."""
    _source_note(conn)
    prov = _FakeProvider([("# Ford Truck\n\nA truck owned", "max_tokens"),
                          (" by Jeff and more and more", "max_tokens")])
    _install(monkeypatch, prov)
    run = _run(conn)

    events = _drive(run, conn)
    done = [e for e in events if e["type"] == "done"][0]
    assert len(prov.calls) == 2                        # exactly one continuation, then stop
    assert done["truncated"] is True
    assert done["lint"]["ok"] is False
    # The kept text is the JOIN of both segments (nothing lost).
    assert run.draft.startswith("# Ford Truck")
    assert "and more and more" in run.draft


def test_live_clean_draft_is_unchanged_no_continuation(conn, monkeypatch):
    _source_note(conn)
    prov = _FakeProvider([(_FULL, "end_turn")])
    _install(monkeypatch, prov)
    run = _run(conn)

    events = _drive(run, conn)
    done = [e for e in events if e["type"] == "done"][0]
    assert len(prov.calls) == 1                        # no continuation on a clean finish
    assert done["truncated"] is False
    assert run.draft.strip().endswith("2026-02-03")
    assert run.talk and run.talk[0]["body"] == "Synthesised from the 2026 entries."


def test_live_redraft_after_autocontinue_reasks_original_prompt(conn, monkeypatch):
    """After an auto-continue, an approved re-draft must re-ask the ORIGINAL prompt — the
    CONTINUE_PROMPT scaffolding (+ its partial assistant turn) is unwound, not re-sent."""
    import asyncio
    from app.services import rebuild_engine, wiki_build
    _source_note(conn)
    # First _generate: truncate then continue (still truncated → surfaced for re-draft).
    prov = _FakeProvider([("# Ford Truck\n\npart one", "max_tokens"),
                          (" part two", "max_tokens"),
                          (_FULL, "end_turn")])           # the re-draft turn returns a full draft
    _install(monkeypatch, prov)
    run = _run(conn)
    _drive(run, conn)
    # run.messages now: [user(orig), assistant(part1), user(CONTINUE), assistant(part2)]
    assert run.messages[-1]["role"] == "assistant"
    assert any(m.get("content") == wiki_build.CONTINUE_PROMPT for m in run.messages)

    async def go():
        return [ev async for ev in rebuild_engine.run_redraft(run, max_tokens=12000)]
    asyncio.new_event_loop().run_until_complete(go())

    # The scaffolding is gone; the conversation that was re-sent ended on the ORIGINAL user
    # prompt (run_redraft pops the truncated turn + CONTINUE pair before re-streaming).
    assert not any(m.get("content") == wiki_build.CONTINUE_PROMPT for m in run.messages[:-1])
    assert run.messages[0]["content"] == "Write the article."
    assert run.draft.strip().endswith("2026-02-03")     # the re-draft completed cleanly


# =========================================================================================
# BATCH path (wiki_build.write_one / maintain_one, non-streaming)
# =========================================================================================

def test_write_one_autocontinues_after_persistent_truncation(conn, monkeypatch):
    """complete_with_meta truncates on BOTH the first call and the bigger-cap retry, then the
    auto-continue turn returns the remainder; the joined draft is saved (not quarantined)."""
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    partial = f"# Ford Truck\n\n{_LEAD}[^s1]\n\n## Refe"
    remainder = "rences\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03\n"
    calls = []

    def fake_cwm(messages, **kw):
        calls.append({"n": len(messages), "max_tokens": kw.get("max_tokens")})
        if len(calls) <= 2:
            return (partial, "max_tokens")             # initial + bigger-cap retry both cut off
        return (remainder, None)                       # the continuation finishes it
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 3                              # retry, then ONE continuation
    # The continuation call carried the partial assistant turn + the CONTINUE prompt.
    assert calls[2]["n"] == 3
    assert out["ok"] is True
    assert "## References" in out["content_md"]
    assert out["content_md"].strip().endswith("2026-02-03")


def test_write_one_talk_fence_split_recovers_clean_article(conn, monkeypatch):
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    partial = (f"# Ford Truck\n\n{_LEAD}[^s1]\n\n"
               "## References\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03\n\n"
               "```talk\n[{\"kind\": \"note\", \"bo")
    remainder = "dy\": \"From the entries.\"}]\n```\n"
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        if len(calls) <= 2:
            return (partial, "max_tokens")
        return (remainder, None)
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert out["ok"] is True
    assert "```talk" not in out["content_md"]          # no leaked fence/JSON
    assert out["content_md"].strip().endswith("2026-02-03")
    assert out["talk"] and out["talk"][0]["body"] == "From the entries."


def test_write_one_quarantines_when_continuation_still_truncated(conn, monkeypatch):
    """One-continuation cap: if the continuation is STILL truncated, fall back to the
    existing quarantine — no infinite loop, nothing saved."""
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        return ("# X\n\nstill going", "max_tokens")     # always cut off
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 3                              # initial + retry + ONE continuation
    assert out["ok"] is False
    assert out["content_md"] == ""
    assert "draft truncated at the token limit" in out["errors"]


def test_write_one_no_truncation_makes_one_call(conn, monkeypatch):
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        return (_FULL, None)
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 1                              # clean finish → no retry / continuation
    assert out["ok"] is True
    assert out["content_md"].strip().endswith("2026-02-03")


def test_maintain_one_autocontinues_then_saves(conn, monkeypatch):
    from app.services import wiki_build, article_talk, llm, notes as notes_svc
    notes_svc.upsert_note(conn, "kb/Things/Ford Truck", _FULL, kind="kb", fire_events=False)
    conn.commit()
    article_talk.add(conn, "kb/Things/Ford Truck", "todo", "Add the year of purchase.")
    conn.commit()
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    partial = "```article\n# Ford Truck\n\nA truck bought in 2026 by Jeff.\n\n## Re"
    remainder = "ferences\n\n(none)\n```\n```maintain\n{\"resolved\": [], \"new\": []}\n```\n"
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        if len(calls) <= 2:
            return (partial, "length")
        return (remainder, None)
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.maintain_one(conn, "kb/Things/Ford Truck", known_titles=[])
    assert len(calls) == 3
    assert out["ok"] is True
    assert "## References" in out["content_md"]
    assert "```article" not in out["content_md"]       # the article fence was stripped


def test_maintain_one_quarantines_when_continuation_still_truncated(conn, monkeypatch):
    from app.services import wiki_build, article_talk, llm, notes as notes_svc
    notes_svc.upsert_note(conn, "kb/Things/Ford Truck", _FULL, kind="kb", fire_events=False)
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


# ---- shared helper unit checks ----------------------------------------------------------

def test_join_continuation_is_raw_concatenation():
    from app.services.wiki_build import _join_continuation
    assert _join_continuation("```talk\n[{\"bo", "dy\":1}]\n```") == "```talk\n[{\"body\":1}]\n```"
    assert _join_continuation("", "x") == "x"
    assert _join_continuation("x", "") == "x"
