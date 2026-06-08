"""Auto-continue a truncated draft — LIVE path only.

Auto-continue is scoped to the LIVE, streaming rebuild engine, which has a human Accept gate:
when a draft is cut off at the token cap (TurnEnd.stop_reason == max_tokens|length), the engine
RESUMES it in ONE follow-up turn and STITCHES the pieces, then surfaces truncated/lint state so
a human reviews and accepts before anything is saved. The critical hazard the design protects:
the writer's trailing optional fences — a ```talk JSON block and the whole-body ```markdown
wrapper — must never be split across the seam. The safe design (proven here): concatenate the
RAW partial + RAW remainder and run _strip_fence / _clean_wrapper_fence / _extract_talk ONCE on
the JOINED string; cap at ONE continuation; fall back to the user-approved re-draft if STILL
truncated.

The BATCH writers (wiki_build.write_one / maintain_one) AUTO-SAVE with no human gate, so they
DELIBERATELY do NOT auto-continue: a still-truncated draft is retried once at a bigger cap, then
QUARANTINED (write_one ok=False / maintain_one changed=False) — never stitched/auto-saved.

Covers:
  * LIVE  (rebuild_engine._generate, streaming): a fake provider streams a partial with
    stop_reason=max_tokens, then a remainder — exercising the in-stream join + Accept-gate surfacing.
  * BATCH (wiki_build.write_one / maintain_one, non-streaming): asserts the bigger-cap retry +
    quarantine, and that NO continuation turn is ever issued.
  * The seam heuristics the live path still uses — anchored so they can't silently delete
    content (edge-anchored wrapper-fence cleanup; full-line-restatement-only overlap trim).

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

def test_write_one_quarantines_on_persistent_truncation_no_autocontinue(conn, monkeypatch):
    """The BATCH writer has no human Accept gate, so it NEVER auto-continues/stitches: on a
    draft still cut off after the bigger-cap retry it QUARANTINES (ok=False, nothing saved) —
    exactly two completion calls (initial + bigger-cap retry), no continuation turn."""
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    calls = []

    def fake_cwm(messages, **kw):
        calls.append({"n": len(messages), "max_tokens": kw.get("max_tokens")})
        return ("# Ford Truck\n\nA truck owned by Jeff and more and", "max_tokens")  # always cut off
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 2                              # initial + bigger-cap retry, NO continuation
    assert all(c["n"] == 1 for c in calls)             # never appended an assistant/continue turn
    assert calls[0]["max_tokens"] == 3000 and calls[1]["max_tokens"] == 6000
    assert out["ok"] is False
    assert out["content_md"] == ""
    assert "draft truncated at the token limit" in out["errors"]


def test_write_one_saves_when_bigger_cap_retry_succeeds(conn, monkeypatch):
    """The bigger-cap retry path still works: a first call cut off, the retry finishes cleanly,
    and the article is saved (ok=True) — recovery without any auto-continue."""
    from app.services import wiki_build, llm
    src = _source_note(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        if len(calls) == 1:
            return ("# Ford Truck\n\ncut off", "max_tokens")
        return (_FULL, None)                            # bigger-cap retry returns a full draft
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.write_one(conn, _art(src["id"]), known_titles=[])
    assert len(calls) == 2                              # initial + bigger-cap retry, then done
    assert out["ok"] is True
    assert out["content_md"].strip().endswith("2026-02-03")


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


def test_maintain_one_saves_when_bigger_cap_retry_succeeds(conn, monkeypatch):
    """maintain's bigger-cap retry still recovers a first-call truncation: the retry returns a
    complete two-fence reply, which is saved (changed=True) — no auto-continue involved."""
    from app.services import wiki_build, article_talk, llm, notes as notes_svc
    notes_svc.upsert_note(conn, "kb/Things/Ford Truck", _FULL, kind="kb", fire_events=False)
    conn.commit()
    article_talk.add(conn, "kb/Things/Ford Truck", "todo", "Add the year of purchase.")
    conn.commit()
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    full = ("```article\n# Ford Truck\n\nA truck bought in 2026 by Jeff.\n\n"
            "## References\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03\n```\n"
            "```maintain\n{\"resolved\": [], \"new\": []}\n```\n")
    calls = []

    def fake_cwm(messages, **kw):
        calls.append(1)
        if len(calls) == 1:
            return ("```article\n# Ford Truck\n\ncut", "length")
        return (full, None)                             # bigger-cap retry returns a full reply
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.maintain_one(conn, "kb/Things/Ford Truck", known_titles=[])
    assert len(calls) == 2                              # initial + bigger-cap retry, NO continuation
    assert out["ok"] is True
    assert "## References" in out["content_md"]
    assert "```article" not in out["content_md"]       # the article fence was stripped


def test_maintain_one_quarantines_on_persistent_truncation_no_autocontinue(conn, monkeypatch):
    """maintain is a BATCH path with no human Accept gate, so it never auto-continues: a reply
    still truncated after the bigger-cap retry is QUARANTINED (changed=False, nothing saved) in
    exactly two calls — never a continuation turn that could silently lose the maintain JSON."""
    from app.services import wiki_build, article_talk, llm, notes as notes_svc
    notes_svc.upsert_note(conn, "kb/Things/Ford Truck", _FULL, kind="kb", fire_events=False)
    conn.commit()
    article_talk.add(conn, "kb/Things/Ford Truck", "todo", "Add the year of purchase.")
    conn.commit()
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    calls = []

    def fake_cwm(messages, **kw):
        calls.append({"n": len(messages)})
        return ("```article\n# Ford Truck\n\ncut", "length")   # always cut off
    monkeypatch.setattr(llm, "complete_with_meta", fake_cwm)

    out = wiki_build.maintain_one(conn, "kb/Things/Ford Truck", known_titles=[])
    assert len(calls) == 2                              # initial + bigger-cap retry, NO continuation
    assert all(c["n"] == 1 for c in calls)             # never appended an assistant/continue turn
    assert out["ok"] is False
    assert out["changed"] is False
    assert "maintain output truncated" in out["errors"]


# ---- shared helper unit checks ----------------------------------------------------------

def test_join_continuation_is_raw_concatenation():
    from app.services.wiki_build import _join_continuation
    assert _join_continuation("```talk\n[{\"bo", "dy\":1}]\n```") == "```talk\n[{\"body\":1}]\n```"
    assert _join_continuation("", "x") == "x"
    assert _join_continuation("x", "") == "x"


# =========================================================================================
# RED-TEAM HAZARD FIXES — one block per fix.
# =========================================================================================

# ---- FIX 1: wrapper ```markdown fence artifacts must never leak into the SAVED article ----

def test_clean_wrapper_fence_does_not_collapse_legit_adjacent_code_blocks():
    """NEGATIVE (no silent deletion): a doc whose PROSE is about markdown fences can contain two
    LEGITIMATE adjacent code blocks — here a ```text block immediately followed by a ```markdown
    block. The old unanchored close-then-reopen collapse merged them (verified corruption). The
    edge-anchored cleanup must NOT touch a mid-document close/reopen — both blocks survive."""
    from app.services.wiki_build import _strip_fence, _clean_wrapper_fence
    doc = "# Title\n\nIntro about fences.\n\n```text\n```\n```markdown\nhello\n```\n\nOutro.\n"
    out = _clean_wrapper_fence(_strip_fence(doc))
    # The two adjacent blocks are preserved verbatim — nothing was merged/dropped.
    assert "```text\n```\n```markdown\nhello\n```" in out
    assert "hello" in out and "Outro." in out


def test_clean_wrapper_fence_case_b_unterminated_wrapper():
    """Leak case (b): the partial CLOSED the wrapper at the cap, and appended remainder pushed
    text past the trailing ```, so _FENCE_RE leaves the WHOLE ```markdown wrapper in the body.
    _clean_wrapper_fence drops the leading opener AND the now-orphaned ``` closer."""
    from app.services.wiki_build import _strip_fence, _clean_wrapper_fence
    leaked = ("```markdown\n# Ford Truck\n\nThe article body.\n```\n"
              "And a trailing sentence the continuation added after the close.\n")
    out = _clean_wrapper_fence(_strip_fence(leaked))
    assert "```markdown" not in out
    assert "```" not in out
    assert out.startswith("# Ford Truck")
    assert "trailing sentence" in out


def test_clean_wrapper_fence_preserves_genuine_inner_code_block():
    """A real fenced CODE block inside prose (```python …) must survive untouched — the
    cleanup only removes a bare/markdown wrapper at the document edge or a close-then-reopen."""
    from app.services.wiki_build import _strip_fence, _clean_wrapper_fence
    doc = "# Title\n\nIntro.\n\n```python\nprint('hi')\n```\n\nMore prose after the block.\n"
    out = _clean_wrapper_fence(_strip_fence(doc))
    assert "```python" in out
    assert "print('hi')" in out
    assert out.count("```") == 2                           # the inner block's own pair, intact


# ---- FIX 3: a RESTATING continuation must not produce duplicated / garbled text ----

def test_join_continuation_drops_duplicate_title_heading():
    from app.services.wiki_build import _join_continuation
    partial = "# Ford Truck\n\nA truck Jeff bought, used for work and weekend trips around town"
    # The model RESTATED the whole thing from the title instead of resuming.
    remainder = ("# Ford Truck\n\nA truck Jeff bought, used for work and weekend trips around "
                 "town, and it is reliable.")
    out = _join_continuation(partial, remainder)
    assert out.count("# Ford Truck") == 1                  # the duplicate H1 was dropped
    assert "and it is reliable." in out                    # the genuinely-new tail is kept


def test_trim_restated_overlap_keeps_coincidental_recurring_phrase():
    """NEGATIVE (no silent deletion): a phrase that COINCIDENTALLY recurs across the seam as a
    mid-line substring must NOT be trimmed. Verified corruption: the partial ends '…the city of
    Washington' and the remainder genuinely continues 'the city of Washington remains…' — the
    old any-suffix-overlap trim deleted the phrase. Because the overlap is a mid-line substring
    (not a restatement of the partial's whole last line) the restricted trim leaves it intact."""
    from app.services.wiki_build import _trim_restated_overlap, _join_continuation
    partial = "The capital sits within the District of Columbia, the city of Washington"
    remainder = "the city of Washington remains the seat of the federal government."
    # The remainder is returned unchanged — the coincidental phrase is preserved (the human sees
    # the harmless visible dup rather than us silently dropping real continuation text).
    assert _trim_restated_overlap(partial, remainder) == remainder
    joined = _join_continuation(partial, remainder)
    assert "the city of Washington remains the seat" in joined


def test_trim_restated_overlap_trims_edge_anchored_full_line_restatement():
    """POSITIVE: a TRUE restatement — the remainder repeats the partial's last COMPLETE LINE
    verbatim, then continues — IS trimmed (the model clearly re-emitted the whole line it had
    just produced before resuming)."""
    from app.services.wiki_build import _trim_restated_overlap
    partial = "# Ford Truck\n\nIntro paragraph one.\nThe truck was purchased in early 2026."
    # Remainder restarts the last whole line verbatim, then adds the genuinely-new tail.
    remainder = "The truck was purchased in early 2026. It remains reliable today."
    out = _trim_restated_overlap(partial, remainder)
    assert out == " It remains reliable today."           # the restated whole line was dropped


def test_join_continuation_trims_full_line_restatement_no_garble():
    from app.services.wiki_build import _join_continuation
    partial = "# Ford Truck\n\nIntro paragraph one.\nThe truck was purchased in early 2026."
    remainder = "The truck was purchased in early 2026. It remains reliable today."
    out = _join_continuation(partial, remainder)
    assert out.count("The truck was purchased in early 2026.") == 1   # not doubled
    assert out.endswith("It remains reliable today.")


def test_live_restating_continuation_not_duplicated(conn, monkeypatch):
    """End-to-end LIVE: the continuation RESTATES the article (incl. the # heading) before
    adding the rest. The stitched draft (the human reviews/accepts) must carry no doubled
    heading — _drop_duplicate_title removes the repeated H1 at the seam."""
    _source_note(conn)
    lead = "A Ford truck Jeff Hopkins bought in 2026 and drives daily for his work commute"
    partial = f"# Ford Truck\n\n{lead}"
    remainder = (f"# Ford Truck\n\n{lead}[^s1]\n\n## References\n\n"
                 "[^s1]: [[notes/2026/02/03]] — 2026-02-03\n")
    prov = _FakeProvider([(partial, "max_tokens"), (remainder, "end_turn")])
    _install(monkeypatch, prov)
    run = _run(conn)

    _drive(run, conn)
    assert run.draft.count("# Ford Truck") == 1                # heading not doubled
    assert "## References" in run.draft


# ---- FIX 4: stripped seam must not merge words / break a heading off start-of-line ----

def test_seam_inserts_newline_before_markdown_block():
    """A provider that .strip()s each segment (xAI) drops the newline before a "## References"
    heading; the seam repair restores it so the heading stays a heading."""
    from app.services.wiki_build import _join_continuation
    partial = "…and that is the end of the prose."             # stripped: no trailing newline
    remainder = "## References\n\n[^s1]: [[notes/2026/02/03]] — 2026-02-03"   # stripped: starts at heading
    out = _join_continuation(partial, remainder)
    assert "\n## References" in out                          # heading re-anchored to its own line
    assert "prose.## References" not in out


def test_seam_preserves_genuine_mid_word_resume():
    """The core design relies on a mid-word resume gluing with NO separator. A word/word seam
    is indistinguishable from a stripped space, so we conservatively glue raw rather than risk
    splitting a word — 'Refe' + 'rences' must stay 'References'."""
    from app.services.wiki_build import _join_continuation
    assert _join_continuation("## Refe", "rences") == "## References"
    assert _join_continuation("the comm", "itment") == "the commitment"
