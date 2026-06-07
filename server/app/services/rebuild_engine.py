"""The two-stage "Rebuild page now" engine.

Stage 1 — GATHER (tool-driven, cheap model, no thinking): an agent seeds from the page's
deterministic sources (prior citations ∪ entity index) and uses search to find more of the
owner's notes, then PROPOSES a candidate source set with a one-line reason each. Its tool
calls stream live (visible on Grok too, since tool use is provider-neutral). The user then
curates the set (a separate screen) — unchecking, adding, or re-gathering.

Stage 2 — DRAFT (tool-less, synthesis model, thinking on): writes the article from ONLY the
curated sources, streaming the reasoning + the body. Never touches the live note until Accept.
Guide re-drafts from the SAME loaded context.

Keeping gather (tools, no thinking) and draft (thinking, no tools) on separate transcripts
means the draft/Guide resume has no tool_use blocks to preserve — trivially safe.
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from . import llm

log = logging.getLogger("jbrain")

# Stage 2 output cap (adaptive thinking tokens also count toward this).
_MAX_TOKENS = 6000
# Stage 1 bounds.
_GATHER_MAX_ITER = 5
_GATHER_MAX_TOKENS = 1500
_GATHER_SEARCH_LIMIT = 8

_GATHER_TOOLS = [
    llm.ToolDef(
        "search_notes",
        "Search the owner's personal notes for material relevant to this article. "
        "Returns matching note titles with their dates. Call it a few times with different "
        "queries to find everything relevant, then finish with propose_sources.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    llm.ToolDef(
        "propose_sources",
        "Finish gathering: propose the final set of source notes to write the article from "
        "(reference each by its EXACT title). Put clearly-irrelevant finds in `skipped`.",
        {"type": "object", "properties": {
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "reason": {"type": "string"}}, "required": ["title", "reason"]}},
            "skipped": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "reason": {"type": "string"}}}},
        }, "required": ["sources"]},
    ),
]


def _notes_meta(conn, ids) -> list[dict]:
    ids = [int(i) for i in ids if i]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, created_at FROM notes WHERE id IN ({q}) AND deleted_at IS NULL", ids
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "date": (r["created_at"] or "")[:10]} for r in rows]


def _gather_system(title: str, seed_titles: list[str], hint: str | None) -> str:
    seed = "\n".join(f"- {t}" for t in seed_titles) or "(none yet)"
    extra = f"\n\nThe owner asks specifically: {hint.strip()}" if (hint or "").strip() else ""
    return (
        f"You select the SOURCE NOTES to rewrite the knowledge-base article \"{title}\" from the "
        f"owner's personal notes (entries, daily logs, lists, places) — never from other kb/ "
        f"articles. The obvious starting sources are:\n{seed}\n\n"
        f"Use search_notes to find any OTHER relevant notes, then call propose_sources with the "
        f"final set (keep the obvious ones worth keeping) and a short reason for each. Be "
        f"selective — only notes that genuinely inform this article.{extra}"
    )


async def run_gather(run, hint: str | None = None, append: bool = False) -> AsyncGenerator[dict, None]:
    """Stage 1: stream the gather agent's tool use and emit the proposed candidate sources."""
    from ..db import get_conn
    from . import search, wiki_build, wiki_guides

    conn = get_conn()
    run.status = "gathering"
    if not llm.has_credentials():
        run.status = "error"
        yield {"type": "error", "message": "No LLM credentials configured."}
        return

    art, _instr, _prior = wiki_build.rebuild_sources(conn, run.title)
    run.known = wiki_build._known_titles(conn)
    pool: dict[str, dict] = {}            # title(lower) -> {id,title,date}
    seed_meta = _notes_meta(conn, art.get("sources") or [])
    for m in seed_meta:
        pool[m["title"].lower()] = m
    seed_titles = [m["title"] for m in seed_meta]

    model = llm.model_for("cheap")
    provider = llm.get_provider(model)
    system = _gather_system(run.title, seed_titles, hint)
    msgs = [{"role": "user", "content": "Gather the source notes for this article."}]
    proposal: dict | None = None

    try:
        for _ in range(_GATHER_MAX_ITER):
            if run.cancelled:
                return
            calls: list[llm.ToolCall] = []
            async for ev in provider.stream_turn(msgs, system=system, tools=_GATHER_TOOLS,
                                                  model=model, max_tokens=_GATHER_MAX_TOKENS, thinking=False):
                if run.cancelled:
                    return
                if isinstance(ev, llm.ToolCallEvent):
                    calls.append(ev.call)
                # No thinking in Stage 1; any stray text is ignored (the agent should call tools).
            if not calls:
                break
            results = []
            for call in calls:
                if call.name == "search_notes":
                    qy = str(call.args.get("query") or "").strip()
                    yield {"type": "tool_use", "tool": "search_notes", "query": qy}
                    hits = [h for h in search.hybrid_notes(conn, qy, _GATHER_SEARCH_LIMIT)
                            if not h["title"].lower().startswith("kb/")]
                    meta = _notes_meta(conn, [h["id"] for h in hits])
                    for m in meta:
                        pool[m["title"].lower()] = m
                    yield {"type": "tool_result", "tool": "search_notes",
                           "summary": f"{len(meta)} match{'es' if len(meta) != 1 else ''}",
                           "items": [m["title"] for m in meta]}
                    body = "\n".join(f"- {m['title']} ({m['date']})" for m in meta) or "(no matches)"
                    results.append(llm.ToolResult(call.id, body))
                elif call.name == "propose_sources":
                    proposal = call.args if isinstance(call.args, dict) else None
                    yield {"type": "tool_use", "tool": "propose_sources"}
                    results.append(llm.ToolResult(call.id, "ok"))
                else:
                    results.append(llm.ToolResult(call.id, "unknown tool"))
            provider.append_tool_results(msgs, results)
            if proposal is not None:
                break
    except Exception as exc:  # noqa: BLE001 — fall back to the deterministic seed set
        log.warning("rebuild gather failed (%s); falling back to seed sources", exc)
        proposal = None

    cands, skipped = _build_candidates(conn, run.title, pool, proposal, seed_titles, wiki_guides)
    if append and run.candidates:
        have = {c["note_id"] for c in run.candidates}
        run.candidates += [c for c in cands if c["note_id"] not in have]
        skip_have = {s["note_id"] for s in run.skipped}
        run.skipped += [s for s in skipped if s["note_id"] not in skip_have and s["note_id"] not in have]
    else:
        run.candidates, run.skipped = cands, skipped
    run.status = "sources_ready"
    yield {"type": "sources_proposed", "candidates": run.candidates, "skipped": run.skipped}


def _build_candidates(conn, title, pool, proposal, seed_titles, wiki_guides):
    target_private = wiki_guides.is_private_title(title)
    seen: set[int] = set()

    def mk(t: str, reason: str, added: bool = False):
        m = pool.get((t or "").lower())
        if not m:
            row = conn.execute(
                "SELECT id, title, created_at FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                (t,)).fetchone()
            if not row:
                return None
            m = {"id": row["id"], "title": row["title"], "date": (row["created_at"] or "")[:10]}
        priv = wiki_guides.is_private_title(m["title"])
        # On a non-private page, flag private-domain notes and leave them OFF by default.
        on = not (priv and not target_private)
        return {"note_id": m["id"], "title": m["title"], "date": m["date"],
                "reason": reason, "on": on, "private": priv, "added": added}

    cands: list[dict] = []
    for s in ((proposal or {}).get("sources") or []):
        c = mk(str(s.get("title", "")), str(s.get("reason", "") or "Relevant source"))
        if c and c["note_id"] not in seen:
            cands.append(c)
            seen.add(c["note_id"])
    if not cands:  # gather produced nothing usable → deterministic seed
        for t in seed_titles:
            c = mk(t, "Linked source / entity match")
            if c and c["note_id"] not in seen:
                cands.append(c)
                seen.add(c["note_id"])

    skipped: list[dict] = []
    for s in ((proposal or {}).get("skipped") or []):
        c = mk(str(s.get("title", "")), str(s.get("reason", "") or ""))
        if c and c["note_id"] not in seen:
            cands_skip = {"note_id": c["note_id"], "title": c["title"], "date": c["date"], "reason": c["reason"]}
            skipped.append(cands_skip)
            seen.add(c["note_id"])
    return cands, skipped


async def _generate(run, conn) -> AsyncGenerator[dict, None]:
    """Stream ONE drafting turn from run.messages: thinking + the article body, then lint,
    stage on the run, and emit `done`. Shared by the initial draft and Guide."""
    from . import wiki_build, wiki_guides

    provider = llm.get_provider(run.model)
    run.status = "streaming"
    run.draft = ""
    parts: list[str] = []
    produced = False
    use_thinking = True
    # Retry once without thinking if the thinking config is rejected before any output.
    for attempt in range(2):
        try:
            async for ev in provider.stream_turn(run.messages, system=None, tools=[],
                                                  model=run.model, max_tokens=_MAX_TOKENS, thinking=use_thinking):
                if run.cancelled:
                    return
                if isinstance(ev, llm.ThinkingDelta):
                    if ev.text:
                        produced = True
                        run.thoughts += ev.text
                        yield {"type": "thinking_delta", "text": ev.text}
                elif isinstance(ev, llm.TextDelta):
                    produced = True
                    parts.append(ev.text)
                    yield {"type": "content_delta", "text": ev.text}
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 0 and use_thinking and not produced:
                log.warning("rebuild: retrying draft without extended thinking (%s)", exc)
                use_thinking = False
                parts = []
                continue
            log.exception("rebuild draft failed for %s", run.title)
            run.status = "error"
            run.error = str(exc)
            yield {"type": "error", "message": "The rebuild failed while generating. Please try again."}
            return
    if run.cancelled:
        return

    raw = "".join(parts)
    draft, talk = wiki_build._extract_talk(wiki_build._strip_fence(raw))
    allowed = set(run.known) | {run.title}
    bad = wiki_build._bad_links(conn, draft, allowed)
    if bad:
        draft = wiki_build._neutralize_links(draft, set(bad))
        talk = list(talk) + [
            {"kind": "note", "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
            for t in bad
        ]
        yield {"type": "lint", "ok": False,
               "message": f"Removed {len(bad)} dead link{'s' if len(bad) != 1 else ''} during cleanup."}

    v = wiki_guides.validate_structure(run.title, draft)
    run.draft = draft
    run.talk = talk
    run.status = "ready"
    yield {"type": "done", "draft": draft, "truncated": False,
           "lint": {"ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "stub": v["stub"]}}


async def run_draft(run, source_ids: list[int]) -> AsyncGenerator[dict, None]:
    """Stage 2: write the article from ONLY the curated source ids."""
    from ..db import get_conn
    from . import wiki_build

    conn = get_conn()
    ids = [int(i) for i in source_ids if i]
    if not ids:
        run.status = "error"
        yield {"type": "error", "message": "Select at least one source to draft from."}
        return
    art, instr, _prior = wiki_build.rebuild_sources(conn, run.title)
    art["sources"] = ids
    subject = f"{run.title.rsplit('/', 1)[-1]} {art.get('scope') or ''}".strip()
    srcs = wiki_build._load_sources(conn, ids, query=subject)
    if not srcs:
        run.status = "error"
        yield {"type": "error", "message": "Those sources couldn't be loaded."}
        return
    if not run.known:
        run.known = wiki_build._known_titles(conn)
    run.messages = [{"role": "user", "content": wiki_build.build_write_prompt(conn, art, srcs, instr, run.known)}]
    async for ev in _generate(run, conn):
        yield ev


async def run_guide(run, instruction: str) -> AsyncGenerator[dict, None]:
    """Steer a revision: append guidance and re-stream from the SAME loaded context."""
    from ..db import get_conn

    conn = get_conn()
    run.status = "guiding"
    steer = (
        "Revise the article per this guidance, using ONLY the sources already provided "
        "earlier in this conversation (do not ask for or invent new sources). Output the "
        "COMPLETE revised article in the same Markdown format.\n\n"
        f"Guidance: {instruction.strip()}"
    )
    run.messages.append({"role": "user", "content": steer})
    async for ev in _generate(run, conn):
        yield ev
