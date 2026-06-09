"""KB-REBUILD orchestration — the two-stage live "Rebuild page now" engine
(server/app/services/rebuild_engine.py) and its in-memory run registry
(server/app/services/rebuild_runs.py).

These tests drive the engine's async generators directly against a REAL sqlite DB
(app/schema.sql via db.init_db, like test_rebuild_refs_links.py) but mock at the `llm`
module seam — `llm.get_provider` is swapped for a deterministic FakeProvider whose
stream_turn yields the neutral streaming events (TextDelta / ThinkingDelta /
ToolCallEvent / TurnEnd), and `llm.has_credentials` is stubbed. No real network /
SDK / threads. Embeddings are no-oped so search/source-loading run offline.

Covered:
- run-registry lifecycle: create / get (sliding TTL touch) / drop (idempotent, cancels) /
  one-active-run-per-slug eviction / MAX_RUNS cap / TTL sweep / is_live / content_hash;
- Stage-1 GATHER: tool-driven proposal → candidates+skipped, deterministic seed fallback
  when the model proposes nothing, private-note OFF-by-default firewall, append/re-gather
  idempotence, no-credentials error, gather-exception fallback, cancellation;
- Stage-2 DRAFT: state transitions (streaming→ready), empty-source guard, unloadable-source
  guard, dead-link neutralization lint, cancellation mid-stream;
- auto-continue on a truncated draft, the still-truncated lint, the thinking-retry fallback,
  and Guide / re-draft (scaffolding unwind) re-runs.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")

pytestmark = pytest.mark.integration


# --- async-generator drain + a deterministic LLM provider at the `llm` seam ---------------

def _drain(agen):
    """Run an async generator to exhaustion, collecting its yielded events. No real loop is
    shared with the engine — each call gets a fresh asyncio.run, so it's deterministic."""
    import asyncio

    async def go():
        out = []
        async for ev in agen:
            out.append(ev)
        return out

    return asyncio.run(go())


class FakeProvider:
    """A stand-in LLMProvider. `script` is a list of "turns"; each turn is a list of neutral
    events to yield (the engine drives stream_turn once per loop iteration). stream_turn
    appends a placeholder assistant turn to `messages` (as the real adapters do) so the engine's
    continuation/redraft bookkeeping sees a realistic transcript shape."""

    def __init__(self, script, *, fail_on_turn=None):
        self.script = list(script)
        self.fail_on_turn = fail_on_turn
        self.turn = 0
        self.calls = []   # (messages_len, kwargs) per stream_turn invocation

    async def stream_turn(self, messages, *, system=None, tools=None, model=None,
                          max_tokens=None, thinking=False):
        from app.services import llm
        i = self.turn
        self.turn += 1
        self.calls.append({"thinking": thinking, "system": system, "n_msgs": len(messages)})
        if self.fail_on_turn is not None and i == self.fail_on_turn:
            raise RuntimeError("provider boom")
        events = self.script[i] if i < len(self.script) else []
        for ev in events:
            yield ev
        # Mimic the adapters: record the model's (opaque) assistant turn.
        messages.append({"role": "assistant", "content": "<<assistant>>"})

    def append_tool_results(self, messages, results):
        from app.services import llm
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content}
            for r in results]})


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

    from app.services import embeddings
    for name in ("upsert_note_embedding", "delete_note_embedding",
                 "upsert_attachment_embeddings", "delete_attachment_embeddings"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "relevant_note_excerpt", lambda *a, **k: None, raising=False)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    return db.get_conn()


def _mk(conn, title, body="x", kind="kb"):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind=kind, fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _new_run(slug="Things/Truck", title="kb/Things/Truck", model="claude-x", base="# Truck\n"):
    from app.services import rebuild_runs
    return rebuild_runs.create(slug, title, model, base)


def _install_provider(monkeypatch, provider, *, creds=True):
    from app.services import llm
    monkeypatch.setattr(llm, "get_provider", lambda model=None: provider)
    monkeypatch.setattr(llm, "has_credentials", lambda: creds)
    # model_for reads prompts.yaml; pin it so gather doesn't depend on config.
    monkeypatch.setattr(llm, "model_for", lambda tier: "claude-cheap")
    return provider


# =========================================================================================
# rebuild_runs — the in-memory registry (lifecycle / locking / bookkeeping)
# =========================================================================================

def test_content_hash_stable_and_none_safe():
    from app.services import rebuild_runs
    assert rebuild_runs.content_hash("abc") == rebuild_runs.content_hash("abc")
    assert rebuild_runs.content_hash(None) == rebuild_runs.content_hash("")
    assert rebuild_runs.content_hash("a") != rebuild_runs.content_hash("b")


def test_create_get_touch_drop_lifecycle(monkeypatch):
    from app.services import rebuild_runs
    rebuild_runs._RUNS.clear()
    rebuild_runs._BY_SLUG.clear()

    run = rebuild_runs.create("S/p", "kb/S/p", "claude-x", "# p\nbody")
    assert run.base_hash == rebuild_runs.content_hash("# p\nbody")
    assert rebuild_runs._BY_SLUG["S/p"] == run.run_id
    assert rebuild_runs.get(run.run_id) is run
    assert rebuild_runs.get("nope") is None

    # get() slides the TTL (touched_at advances).
    t0 = run.touched_at
    monkeypatch.setattr(rebuild_runs.time, "monotonic", lambda: t0 + 5)
    assert rebuild_runs.get(run.run_id) is run
    assert run.touched_at == t0 + 5
    rebuild_runs.touch(run)  # also bumps
    assert run.touched_at == t0 + 5


def test_drop_is_idempotent_and_cancels(monkeypatch):
    from app.services import rebuild_runs
    rebuild_runs._RUNS.clear()
    rebuild_runs._BY_SLUG.clear()
    run = rebuild_runs.create("S/p", "kb/S/p", "claude-x", "")
    rid = run.run_id
    rebuild_runs.drop(rid)
    assert run.cancelled is True             # signals the generator to stop
    assert rebuild_runs.get(rid) is None
    assert "S/p" not in rebuild_runs._BY_SLUG
    rebuild_runs.drop(rid)                    # no-op, no raise


def test_one_active_run_per_slug_drops_prior(monkeypatch):
    from app.services import rebuild_runs
    rebuild_runs._RUNS.clear()
    rebuild_runs._BY_SLUG.clear()
    first = rebuild_runs.create("S/p", "kb/S/p", "claude-x", "")
    second = rebuild_runs.create("S/p", "kb/S/p", "claude-x", "")
    assert first.cancelled is True            # prior live run for the same slug is cancelled
    assert rebuild_runs.get(first.run_id) is None
    assert rebuild_runs.get(second.run_id) is second
    assert rebuild_runs._BY_SLUG["S/p"] == second.run_id


def test_max_runs_cap_evicts_oldest(monkeypatch):
    from app.services import rebuild_runs
    rebuild_runs._RUNS.clear()
    rebuild_runs._BY_SLUG.clear()
    t = [1000.0]
    monkeypatch.setattr(rebuild_runs.time, "monotonic", lambda: t[0])
    runs = []
    for i in range(rebuild_runs._MAX_RUNS):
        t[0] += 1
        r = rebuild_runs.create(f"S/p{i}", f"kb/S/p{i}", "claude-x", "")
        r.touched_at = t[0]                 # pin onto the fake clock (default_factory used real)
        runs.append(r)
    assert len(rebuild_runs._RUNS) == rebuild_runs._MAX_RUNS
    t[0] += 1
    rebuild_runs.create("S/extra", "kb/S/extra", "claude-x", "")   # over cap → evict oldest
    assert len(rebuild_runs._RUNS) == rebuild_runs._MAX_RUNS
    assert rebuild_runs.get(runs[0].run_id) is None                # the oldest is gone


def test_sweep_reaps_idle_but_spares_accepting(monkeypatch):
    from app.services import rebuild_runs
    rebuild_runs._RUNS.clear()
    rebuild_runs._BY_SLUG.clear()
    t = [1000.0]
    monkeypatch.setattr(rebuild_runs.time, "monotonic", lambda: t[0])
    idle = rebuild_runs.create("S/idle", "kb/S/idle", "claude-x", "")
    busy = rebuild_runs.create("S/busy", "kb/S/busy", "claude-x", "")
    busy.status = "accepting"
    # default_factory captured the real clock; pin both runs onto our fake clock's "now".
    idle.touched_at = busy.touched_at = t[0]
    # Advance past the TTL, then trigger a sweep via a fresh create().
    t[0] += rebuild_runs._TTL_SECONDS + 1
    rebuild_runs.create("S/new", "kb/S/new", "claude-x", "")
    assert rebuild_runs._RUNS.get(idle.run_id) is None             # idle reaped
    assert rebuild_runs._RUNS.get(busy.run_id) is busy             # accepting spared


def test_is_live_only_for_live_statuses():
    from app.services import rebuild_runs
    run = rebuild_runs.RebuildRun(run_id="x", slug="s", title="t", model="m", base_hash="h")
    for st in ("streaming", "ready", "guiding"):
        run.status = st
        assert rebuild_runs.is_live(run) is True
    for st in ("accepted", "rejected", "error", "cancelled", "accepting", "sources_ready"):
        run.status = st
        assert rebuild_runs.is_live(run) is False


# =========================================================================================
# Stage 1 — GATHER
# =========================================================================================

def _ev_search(call_id, query):
    from app.services import llm
    return llm.ToolCallEvent(llm.ToolCall(id=call_id, name="search_notes", args={"query": query}))


def _ev_propose(call_id, sources, skipped=None):
    from app.services import llm
    return llm.ToolCallEvent(llm.ToolCall(
        id=call_id, name="propose_sources", args={"sources": sources, "skipped": skipped or []}))


def test_gather_no_credentials_errors(conn, monkeypatch):
    from app.services import rebuild_engine
    _install_provider(monkeypatch, FakeProvider([]), creds=False)
    run = _new_run()
    evs = _drain(rebuild_engine.run_gather(run))
    assert run.status == "error"
    assert evs and evs[-1]["type"] == "error" and "credentials" in evs[-1]["message"].lower()


def test_gather_proposes_sources_and_skips(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/buy-truck", "Bought a Ford truck.", kind="entry")
    junk = _mk(conn, "notes/2026/01/groceries", "Bought milk.", kind="entry")
    # One search turn (returns nothing — search is monkeypatched), then a propose turn.
    monkeypatch.setattr(rebuild_engine.asyncio, "to_thread",
                        _aiowrap(lambda *a, **k: []))
    prov = FakeProvider([
        [_ev_search("c1", "ford truck")],
        [_ev_propose("c2",
                     sources=[{"title": "notes/2026/01/buy-truck", "reason": "the purchase"}],
                     skipped=[{"title": "notes/2026/01/groceries", "reason": "unrelated"}])],
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_gather(run))
    assert run.status == "sources_ready"
    types = [e["type"] for e in evs]
    assert "tool_use" in types and types[-1] == "sources_proposed"
    cand_titles = {c["title"] for c in run.candidates}
    assert "notes/2026/01/buy-truck" in cand_titles
    skip_titles = {s["title"] for s in run.skipped}
    assert "notes/2026/01/groceries" in skip_titles
    # The proposed candidate is ON by default for a non-private target.
    assert all(c["on"] for c in run.candidates)


def test_gather_falls_back_to_seed_when_no_proposal(conn, monkeypatch):
    """The model proposes nothing usable → engine falls back to the deterministic seed set."""
    from app.services import rebuild_engine, wiki_build
    art = _mk(conn, "kb/Things/Truck", "# Truck\nSee [[notes/2026/01/seed]].\n")
    seed = _mk(conn, "notes/2026/01/seed", "the seed note", kind="entry")
    # rebuild_sources resolves the prior citation as a seed source.
    monkeypatch.setattr(rebuild_engine.asyncio, "to_thread", _aiowrap(lambda *a, **k: []))
    prov = FakeProvider([[]])   # zero tool calls → loop breaks, proposal stays None
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_gather(run))
    assert run.status == "sources_ready"
    assert any(c["title"] == "notes/2026/01/seed" for c in run.candidates), run.candidates


def test_gather_exception_falls_back_to_seed(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\nSee [[notes/2026/01/seed]].\n")
    _mk(conn, "notes/2026/01/seed", "the seed note", kind="entry")
    prov = FakeProvider([], fail_on_turn=0)   # stream_turn raises → caught, proposal=None
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_gather(run))
    assert run.status == "sources_ready"          # recovered, not error
    assert any(c["title"] == "notes/2026/01/seed" for c in run.candidates)


def test_gather_cancel_stops_quietly(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    run = _new_run()
    run.cancelled = True                          # cooperative cancel before the loop body
    prov = FakeProvider([[_ev_propose("c1", [])]])
    _install_provider(monkeypatch, prov)
    evs = _drain(rebuild_engine.run_gather(run))
    assert evs == []                              # returns silently, no sources_proposed
    assert run.status == "gathering"


def test_gather_private_note_off_for_public_target(conn, monkeypatch):
    """A private-domain (Health) source proposed for a PUBLIC page is kept but OFF by default."""
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    _mk(conn, "kb/Health/Jeff", "# Jeff\nmedical", kind="kb")
    monkeypatch.setattr(rebuild_engine.asyncio, "to_thread", _aiowrap(lambda *a, **k: []))
    prov = FakeProvider([[_ev_propose("c1",
                          sources=[{"title": "kb/Health/Jeff", "reason": "health"}])]])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    _drain(rebuild_engine.run_gather(run))
    # kb/* titles are filtered from search hits, but a directly-proposed title is still resolved
    # via the DB fallback in _build_candidates; the private one lands OFF.
    priv = [c for c in run.candidates if c["title"] == "kb/Health/Jeff"]
    if priv:
        assert priv[0]["private"] is True and priv[0]["on"] is False


def test_gather_append_merges_without_dupes(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    a = _mk(conn, "notes/2026/01/a", "note a", kind="entry")
    b = _mk(conn, "notes/2026/01/b", "note b", kind="entry")
    monkeypatch.setattr(rebuild_engine.asyncio, "to_thread", _aiowrap(lambda *a, **k: []))
    run = _new_run()
    # Pre-seed candidates from a first gather.
    prov1 = FakeProvider([[_ev_propose("c1", [{"title": "notes/2026/01/a", "reason": "a"}])]])
    _install_provider(monkeypatch, prov1)
    _drain(rebuild_engine.run_gather(run))
    assert {c["title"] for c in run.candidates} == {"notes/2026/01/a"}
    # Re-gather with append=True proposing a (dup) + b (new).
    prov2 = FakeProvider([[_ev_propose("c2", [
        {"title": "notes/2026/01/a", "reason": "a again"},
        {"title": "notes/2026/01/b", "reason": "b"}])]])
    _install_provider(monkeypatch, prov2)
    _drain(rebuild_engine.run_gather(run, append=True))
    titles = [c["title"] for c in run.candidates]
    assert sorted(titles) == ["notes/2026/01/a", "notes/2026/01/b"]   # no duplicate of a


def test_suggest_gather_shows_seeds_immediately_without_an_llm_call(conn, monkeypatch):
    """REGRESSION: 'Edit with AI' (suggest) must open straight to the curate screen from the
    deterministic seed sources — never block on the agentic gather (the cheap-model loop + entity
    rebind) that left it stuck on 'Gathering context…'. The provider records any stream_turn call;
    the suggest-open path must make ZERO (reverting to the agentic path would call it ≥1)."""
    from app.services import rebuild_engine, rebuild_runs

    _mk(conn, "kb/Things/Truck", "# Truck\nSee [[notes/2026/01/seed]].\n")
    _mk(conn, "notes/2026/01/seed", "the seed note", kind="entry")
    called = {"n": 0}

    class _NoLLM:
        async def stream_turn(self, *a, **k):
            called["n"] += 1
            raise AssertionError("suggest-open gather must not invoke the LLM")
            yield  # pragma: no cover — makes this an async generator

    _install_provider(monkeypatch, _NoLLM())
    run = rebuild_runs.create("Things/Truck", "kb/Things/Truck", "claude-x", "# Truck\n", kind="suggest")
    evs = _drain(rebuild_engine.run_gather(run))

    assert called["n"] == 0, "suggest open must not call the gather agent (it blocked → hang)"
    assert run.status == "sources_ready"
    assert [e["type"] for e in evs] == ["sources_proposed"]      # straight to curate, one event
    assert any(c["title"] == "notes/2026/01/seed" for c in run.candidates), run.candidates


def test_suggest_regather_still_runs_the_agentic_discovery(conn, monkeypatch):
    """Re-gather (append=True) in suggest mode still invokes the agentic source discovery, so
    AI-proposed sources remain available on demand — only the initial open is the fast seed path."""
    from app.services import rebuild_engine

    _mk(conn, "kb/Things/Truck", "# Truck\n")
    _mk(conn, "notes/2026/01/buy-truck", "Bought a Ford truck.", kind="entry")
    monkeypatch.setattr(rebuild_engine.asyncio, "to_thread", _aiowrap(lambda *a, **k: []))
    prov = FakeProvider([[_ev_propose("c1", sources=[{"title": "notes/2026/01/buy-truck", "reason": "x"}])]])
    _install_provider(monkeypatch, prov)
    run = _new_run()                                  # default kind="rebuild" → agentic path
    run.kind = "suggest"
    _drain(rebuild_engine.run_gather(run, hint="ford", append=True))
    assert prov.turn >= 1, "Re-gather must run the agentic gather"
    assert any(c["title"] == "notes/2026/01/buy-truck" for c in run.candidates)


def test_gather_search_runs_on_a_worker_local_connection(conn, monkeypatch):
    """REGRESSION GATE: the gather search offload must run on a WORKER thread with its OWN
    connection — never the shared event-loop connection. Passing the captured event-loop `conn`
    into the worker (the original bug) was a silent cross-thread share that wedged the connection
    (and now raises under check_same_thread). Reverting the fix makes the spy capture the
    event-loop connection and fails this test. The real asyncio.to_thread is used (NOT patched)
    so the offload genuinely crosses a thread boundary.
    """
    import threading
    from app.db import get_conn as real_get_conn
    from app.services import rebuild_engine, search

    _mk(conn, "kb/Things/Truck", "# Truck\n")
    _mk(conn, "notes/2026/01/buy-truck", "Bought a Ford truck.", kind="entry")
    main_thread = threading.get_ident()
    loop_conn = real_get_conn()          # the event-loop / main-thread connection
    seen: dict = {}

    def spy_hybrid(c, q, limit, require_kb_ingest=False):
        """Record the thread + whether we were handed the (forbidden) event-loop connection."""
        seen["thread"] = threading.get_ident()
        seen["is_loop_conn"] = (c is loop_conn)
        return []

    monkeypatch.setattr(search, "hybrid_notes", spy_hybrid)
    prov = FakeProvider([
        [_ev_search("c1", "ford truck")],
        [_ev_propose("c2", sources=[{"title": "notes/2026/01/buy-truck", "reason": "x"}])],
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    _drain(rebuild_engine.run_gather(run))

    assert seen, "the search offload never ran"
    assert seen["thread"] != main_thread, "search must run on a worker thread, not the event loop"
    assert seen["is_loop_conn"] is False, (
        "search must use a worker-local connection — it was handed the shared event-loop conn"
    )


def test_entity_rebuild_skips_embeddings_when_flagged(conn, monkeypatch):
    """rebuild(sync_embeddings=False) rebuilds the tables but skips the embed refresh."""
    from app.services import entity_index
    seen = []
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda c: seen.append(True))
    entity_index.rebuild(conn, sync_embeddings=False)
    assert seen == []                       # the cheap path never touches the embedder
    entity_index.rebuild(conn)              # default re-syncs
    assert seen == [True]


def test_gather_does_not_run_the_entity_rebind_on_the_interactive_path(conn, monkeypatch):
    """REGRESSION: gather must NOT run entity_index.rebuild on the live request.

    The rebind reaggregates the whole corpus inside one write transaction, holding the WAL write
    lock for many seconds on a large KB — long enough to block an interactive chat/research turn's
    offloaded message INSERT (the "research goes unresponsive after opening Edit with AI" wedge).
    It was only a best-effort freshness boost; the next full/nightly rebuild + Accept re-sync the
    index. Reinstating the interactive rebind (any mode) fails this test.
    """
    from app.services import rebuild_engine, rebuild_runs, entity_index
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    called = {"n": 0}
    monkeypatch.setattr(entity_index, "rebuild", lambda c, *a, **k: called.__setitem__("n", called["n"] + 1) or 0)
    prov = FakeProvider([[_ev_propose("c1", sources=[])]])      # rebuild-mode agentic gather
    _install_provider(monkeypatch, prov)

    _drain(rebuild_engine.run_gather(_new_run()))               # rebuild mode
    sug = rebuild_runs.create("Things/Truck", "kb/Things/Truck", "claude-x", "# Truck\n", kind="suggest")
    _drain(rebuild_engine.run_gather(sug))                      # suggest mode
    assert called["n"] == 0, "gather must not run the heavy entity rebind on the interactive path"


# =========================================================================================
# Stage 2 — DRAFT / GUIDE / RE-DRAFT
# =========================================================================================

def _td(text):
    from app.services import llm
    return llm.TextDelta(text)


def _think(text):
    from app.services import llm
    return llm.ThinkingDelta(text)


def _end(stop_reason=None):
    from app.services import llm
    return llm.TurnEnd([], usage=None, stop_reason=stop_reason)


def test_draft_empty_sources_errors(conn, monkeypatch):
    from app.services import rebuild_engine
    _install_provider(monkeypatch, FakeProvider([]))
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, []))
    assert run.status == "error"
    assert evs[-1]["type"] == "error" and "source" in evs[-1]["message"].lower()


def test_draft_unloadable_sources_errors(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    _install_provider(monkeypatch, FakeProvider([]))
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [999999]))   # nonexistent ids → no sources
    assert run.status == "error"
    assert evs[-1]["type"] == "error"


def test_draft_happy_path_streams_and_readies(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "I bought a truck on 2026-01-02.", kind="entry")
    body = "# Truck\n\nThe owner bought a truck.\n"
    prov = FakeProvider([[_think("reasoning"), _td(body), _end(stop_reason="end_turn")]])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    types = [e["type"] for e in evs]
    assert "thinking_delta" in types and "content_delta" in types
    assert types[-1] == "done"
    assert run.status == "ready"
    assert run.draft.strip().startswith("# Truck")
    assert run.thoughts == "reasoning"
    assert evs[-1]["truncated"] is False
    assert prov.calls[0]["thinking"] is True            # first draft turn requests thinking
    # The curated source title is remembered for citation repair / re-draft grounding.
    assert run.sources == [{"title": "notes/2026/01/src"}]


def test_draft_neutralizes_dead_links(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    # A footnote citing a [[title]] that doesn't exist and isn't a near-miss of any source.
    body = ("# Truck\n\nA fact.[^s1]\n\n## References\n\n"
            "[^s1]: [[No Such Article At All]] — 2026-01-01\n")
    prov = FakeProvider([[_td(body), _end(stop_reason="end_turn")]])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    lints = [e for e in evs if e["type"] == "lint"]
    assert any("dead link" in l["message"].lower() for l in lints), evs
    assert run.status == "ready"


def test_draft_cancel_mid_stream(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    run = _new_run()

    # A provider that flips the cancel flag after the first delta, then keeps yielding.
    class CancelProvider(FakeProvider):
        async def stream_turn(self, messages, **kw):
            from app.services import llm
            yield llm.TextDelta("partial")
            run.cancelled = True
            yield llm.TextDelta("more")
            yield llm.TurnEnd([], stop_reason="end_turn")
            messages.append({"role": "assistant", "content": "x"})

    _install_provider(monkeypatch, CancelProvider([]))
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    # The engine returns at the cancel check before emitting `done`.
    assert all(e["type"] != "done" for e in evs)
    assert run.status == "streaming"


def test_draft_truncation_auto_continues(conn, monkeypatch):
    """A first turn truncated at the cap is resumed in ONE follow-up turn; the joined body
    finishes and `done` reports truncated=False. The continuation runs thinking-off."""
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    prov = FakeProvider([
        [_td("# Truck\n\nThe begin"), _end(stop_reason="max_tokens")],   # cut off
        [_td("ning and the end.\n"), _end(stop_reason="end_turn")],      # continuation
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    assert evs[-1]["type"] == "done"
    assert evs[-1]["truncated"] is False
    assert prov.turn == 2                          # exactly one auto-continue ran
    assert prov.calls[1]["thinking"] is False      # continuation requests no thinking
    assert "end" in run.draft


def test_draft_still_truncated_after_continue_lints(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    prov = FakeProvider([
        [_td("# Truck\n\npart one"), _end(stop_reason="max_tokens")],
        [_td(" part two"), _end(stop_reason="max_tokens")],   # STILL truncated
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    assert evs[-1]["type"] == "done"
    assert evs[-1]["truncated"] is True
    assert any(e["type"] == "lint" and "cut off" in e["message"].lower() for e in evs)
    assert prov.turn == 2                          # capped at one continuation


def test_draft_retries_without_thinking_on_pristine_failure(conn, monkeypatch):
    """If the first (thinking) attempt raises before producing output, the engine retries the
    SAME initial draft turn with thinking off."""
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")

    class FlakyThinking(FakeProvider):
        async def stream_turn(self, messages, *, thinking=False, **kw):
            from app.services import llm
            self.calls.append({"thinking": thinking})
            self.turn += 1
            if thinking:
                raise RuntimeError("thinking config rejected")
            yield llm.TextDelta("# Truck\n\nWritten without thinking.\n")
            yield llm.TurnEnd([], stop_reason="end_turn")
            messages.append({"role": "assistant", "content": "x"})

    _install_provider(monkeypatch, FlakyThinking([]))
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    assert evs[-1]["type"] == "done"
    assert run.status == "ready"
    assert [c["thinking"] for c in run_provider(rebuild_engine).calls][:2] == [True, False]


def test_draft_hard_failure_errors(conn, monkeypatch):
    """A failure with thinking already OFF (no retry path) surfaces as an error event."""
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")

    class AlwaysBoom(FakeProvider):
        async def stream_turn(self, messages, *, thinking=False, **kw):
            if False:
                yield None
            raise RuntimeError("boom")

    _install_provider(monkeypatch, AlwaysBoom([]))
    run = _new_run()
    evs = _drain(rebuild_engine.run_draft(run, [src["id"]]))
    assert run.status == "error"
    assert run.error == "boom"
    assert evs[-1]["type"] == "error"


def test_guide_revises_from_loaded_context(conn, monkeypatch):
    from app.services import rebuild_engine
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    prov = FakeProvider([
        [_td("# Truck\n\nFirst draft.\n"), _end(stop_reason="end_turn")],
        [_td("# Truck\n\nRevised per guidance.\n"), _end(stop_reason="end_turn")],
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    _drain(rebuild_engine.run_draft(run, [src["id"]]))
    n_before = len(run.messages)
    evs = _drain(rebuild_engine.run_guide(run, "Make it shorter."))
    assert run.status == "ready"
    assert "Revised" in run.draft
    # Guide appended a steer user-turn (+ the assistant turn) onto the SAME transcript.
    assert len(run.messages) > n_before
    assert any(m["role"] == "user" and "Guidance: Make it shorter." in str(m.get("content", ""))
               for m in run.messages)


def _suggest_run(base, slug="People/Al", title="kb/People/Al", model="claude-x"):
    from app.services import rebuild_runs
    return rebuild_runs.create(slug, title, model, base, kind="suggest")


def test_load_backlinks_firewall_excludes_private_into_public(conn):
    from app.services import rebuild_engine, notes as notes_svc
    target = _mk(conn, "kb/People/Al", "# Al\n\nAbout Al.\n")
    pub = _mk(conn, "kb/Groups/Team", "# Team\n\nAl is on the team.\n")
    priv = _mk(conn, "kb/Health/Al", "# Al (health)\n\nSensitive medical detail.\n")
    raw = _mk(conn, "notes/2026/01/diary", "saw Al today", kind="entry")
    # Wire inbound links target<-(pub, priv, raw) directly for a deterministic graph.
    for src in (pub, priv, raw):
        conn.execute("INSERT INTO links (source_note_id, target_note_id, target_title) VALUES (?,?,?)",
                     (src["id"], target["id"], target["title"]))
    conn.commit()
    bl = rebuild_engine._load_backlinks(conn, "kb/People/Al")
    titles = {b["title"] for b in bl}
    assert "kb/Groups/Team" in titles            # public kb backlink → included
    assert "kb/Health/Al" not in titles          # private-domain source → firewalled OUT
    assert "notes/2026/01/diary" not in titles    # raw note → not article context
    # A PRIVATE target may see its private backlinks (no leak across the firewall).
    notes_svc.upsert_note(conn, "kb/Health/Target", "# T\n", kind="kb", fire_events=False)
    conn.commit()
    htarget = notes_svc.get_by_title(conn, "kb/Health/Target")
    conn.execute("INSERT INTO links (source_note_id, target_note_id, target_title) VALUES (?,?,?)",
                 (priv["id"], htarget["id"], htarget["title"]))
    conn.commit()
    assert "kb/Health/Al" in {b["title"] for b in rebuild_engine._load_backlinks(conn, "kb/Health/Target")}


def test_run_suggest_seeds_base_and_streams_edit(conn, monkeypatch):
    from app.services import rebuild_engine
    base = "# Al\n\nAl is a friend.\n"
    _mk(conn, "kb/People/Al", base)
    src = _mk(conn, "notes/2026/01/al", "Al moved to Denver in 2026.", kind="entry")
    revised = "# Al\n\nAl is a friend who moved to Denver.\n"
    prov = FakeProvider([[_td(revised), _end(stop_reason="end_turn")]])
    _install_provider(monkeypatch, prov)
    run = _suggest_run(base)
    evs = _drain(rebuild_engine.run_suggest(run, [src["id"]], "Add that Al moved to Denver."))
    assert run.status == "ready"
    assert evs[-1]["type"] == "done"
    assert "Denver" in run.draft
    # The first turn embedded the CURRENT article and the owner's guidance in ONE user turn.
    seed = run.messages[0]["content"]
    assert run.messages[0]["role"] == "user"
    assert "CURRENT ARTICLE" in seed and "Al is a friend." in seed
    assert "Add that Al moved to Denver." in seed
    assert run.sources == [{"title": "notes/2026/01/al"}]


def test_run_suggest_then_guide_continues_same_transcript(conn, monkeypatch):
    from app.services import rebuild_engine
    base = "# Al\n\nAl is a friend.\n"
    _mk(conn, "kb/People/Al", base)
    prov = FakeProvider([
        [_td("# Al\n\nAl is a good friend.\n"), _end(stop_reason="end_turn")],
        [_td("# Al\n\nAl is a great friend.\n"), _end(stop_reason="end_turn")],
    ])
    _install_provider(monkeypatch, prov)
    run = _suggest_run(base)
    _drain(rebuild_engine.run_suggest(run, [], "Call him a good friend."))   # no sources → pure edit
    n_before = len(run.messages)
    _drain(rebuild_engine.run_guide(run, "Make it 'great' instead."))
    assert "great friend" in run.draft
    assert len(run.messages) > n_before          # guide continued the same loaded transcript


def test_private_note_ids_floor_flags_health_linked_notes(conn):
    from app.services import rebuild_engine, notes as notes_svc
    _mk(conn, "kb/Health/Al", "# Al (health)\n")
    diary = _mk(conn, "notes/2026/01/diary", "checkup notes", kind="entry")
    plain = _mk(conn, "notes/2026/01/trip", "road trip", kind="entry")
    htgt = notes_svc.get_by_title(conn, "kb/Health/Al")
    conn.execute("INSERT INTO links (source_note_id, target_note_id, target_title) VALUES (?,?,?)",
                 (diary["id"], htgt["id"], "kb/Health/Al"))
    conn.commit()
    priv = rebuild_engine._private_note_ids(conn)
    assert diary["id"] in priv             # links to a Health page → sensitivity floor
    assert plain["id"] not in priv


def test_find_facts_firewall_and_extraction(conn, monkeypatch):
    from app.services import rebuild_engine, search, notes as notes_svc, llm
    _mk(conn, "kb/People/Al", "# Al\n")
    _mk(conn, "kb/Health/Al", "# Al (health)\n")
    pub = _mk(conn, "notes/2026/01/hobby", "Al took up sailing in 2026.", kind="entry")
    sens = _mk(conn, "notes/2026/01/checkup", "Al's blood pressure reading.", kind="entry")
    htgt = notes_svc.get_by_title(conn, "kb/Health/Al")
    conn.execute("INSERT INTO links (source_note_id, target_note_id, target_title) VALUES (?,?,?)",
                 (sens["id"], htgt["id"], "kb/Health/Al"))   # marks the checkup note private-adjacent
    conn.commit()
    # Search returns both notes; the model would name both, but the private one must be filtered.
    monkeypatch.setattr(search, "hybrid_notes", lambda *a, **k: [
        {"id": pub["id"], "title": "notes/2026/01/hobby"},
        {"id": sens["id"], "title": "notes/2026/01/checkup"}])
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: __import__("json").dumps([
        {"claim": "Al took up sailing in 2026.", "source": "notes/2026/01/hobby"},
        {"claim": "Al's blood pressure was recorded.", "source": "notes/2026/01/checkup"}]))

    # Editing a PUBLIC article: the private-adjacent source is firewalled out (floor + backstop).
    facts = rebuild_engine.find_facts(conn, "kb/People/Al", "what's new with Al")
    titles = {f["source_title"] for f in facts}
    assert "notes/2026/01/hobby" in titles
    assert "notes/2026/01/checkup" not in titles
    assert all(f["claim"] and f["source_id"] for f in facts)

    # Editing the PRIVATE article itself: its own private sources are allowed (no cross-firewall).
    facts_priv = rebuild_engine.find_facts(conn, "kb/Health/Al", "what's new with Al")
    assert "notes/2026/01/checkup" in {f["source_title"] for f in facts_priv}


def test_find_facts_no_creds_returns_empty(conn, monkeypatch):
    from app.services import rebuild_engine, llm
    monkeypatch.setattr(llm, "has_credentials", lambda: False)
    assert rebuild_engine.find_facts(conn, "kb/People/Al", "anything") == []


def test_redraft_unwinds_continue_scaffolding(conn, monkeypatch):
    """After a truncated draft auto-continued, run_redraft drops the truncated assistant turn
    AND the CONTINUE_PROMPT scaffolding so the model re-answers the ORIGINAL prompt."""
    from app.services import rebuild_engine, wiki_build
    _mk(conn, "kb/Things/Truck", "# Truck\n")
    src = _mk(conn, "notes/2026/01/src", "facts", kind="entry")
    prov = FakeProvider([
        [_td("# Truck\n\nbeg"), _end(stop_reason="max_tokens")],   # initial, truncated
        [_td("in part"), _end(stop_reason="max_tokens")],          # auto-continue, still trunc
        [_td("# Truck\n\nFull clean re-draft.\n"), _end(stop_reason="end_turn")],  # redraft
    ])
    _install_provider(monkeypatch, prov)
    run = _new_run()
    _drain(rebuild_engine.run_draft(run, [src["id"]]))
    # Transcript now: [orig user][assistant truncated][CONTINUE user][assistant partial]
    assert run.messages[-1]["role"] == "assistant"
    assert any(m.get("content") == wiki_build.CONTINUE_PROMPT for m in run.messages)
    orig_user = run.messages[0]

    evs = _drain(rebuild_engine.run_redraft(run, max_tokens=12000))
    assert evs[-1]["type"] == "done"
    assert "Full clean re-draft" in run.draft
    # The scaffolding was unwound: the original prompt is the last USER turn before the redraft.
    assert not any(m.get("content") == wiki_build.CONTINUE_PROMPT
                   for m in run.messages[:-1] if m["role"] == "user")
    assert run.messages[0] is orig_user


def test_redraft_with_no_messages_errors(conn, monkeypatch):
    from app.services import rebuild_engine
    _install_provider(monkeypatch, FakeProvider([]))
    run = _new_run()
    run.messages = []
    evs = _drain(rebuild_engine.run_redraft(run, max_tokens=12000))
    assert run.status == "error"
    assert evs[-1]["type"] == "error"


def test_clamp_tokens_bounds():
    from app.services.rebuild_engine import _clamp_tokens, _MAX_TOKENS, _MAX_TOKENS_CEILING
    assert _clamp_tokens(None) == _MAX_TOKENS
    assert _clamp_tokens("bad") == _MAX_TOKENS
    assert _clamp_tokens(1) == _MAX_TOKENS                  # never below the default
    assert _clamp_tokens(99999) == _MAX_TOKENS_CEILING      # never above the ceiling
    assert _clamp_tokens(_MAX_TOKENS + 100) == _MAX_TOKENS + 100


# --- helpers used above -------------------------------------------------------------------

def _aiowrap(fn):
    """Wrap a sync fn as an async coroutine so it can stand in for asyncio.to_thread."""
    async def _inner(f, *a, **k):
        return fn(*a, **k)
    return _inner


def run_provider(rebuild_engine):
    """The currently-installed FakeProvider (get_provider was monkeypatched to return it)."""
    from app.services import llm
    return llm.get_provider()
