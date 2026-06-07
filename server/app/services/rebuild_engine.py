"""The live "Rebuild page now" engine.

Mirrors how the KB is actually written (wiki_build.write_one): resolve the page's PRIMARY
SOURCES deterministically, then ask the model to (re)write the article body — but here we
STREAM it, surfacing the model's extended-thinking and the article tokens live, and we never
touch the live note (the draft is staged in the in-memory run until the user Accepts).

Because the writer is source-driven (no agentic tool calls), the transcript is a clean
[user prompt] / [assistant article] pair — which makes Guide resume trivially safe (no
tool_use/tool_result pairing to preserve) and removes the dangling-tool_use hazard. Guide
simply appends a steering user turn and re-streams from the SAME context.
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

log = logging.getLogger("jbrain")

# Output cap per turn. Headroom over write_one's 2200 (non-streaming) because adaptive
# thinking tokens also count toward this budget — too low and a long article truncates.
_MAX_TOKENS = 6000


async def _generate(run, conn) -> AsyncGenerator[dict, None]:
    """Stream ONE turn from run.messages: emit thinking + the article body, then lint the
    result, stage it on the run, and emit `done`. Shared by the initial rebuild and Guide."""
    from . import llm, wiki_build, wiki_guides

    provider = llm.get_provider(run.model)
    run.status = "streaming"
    run.draft = ""
    parts: list[str] = []
    produced = False        # any thinking/text streamed yet (a request-time 400 yields nothing)
    use_thinking = True
    # Up to two attempts: if the extended-thinking request is rejected before any output
    # (an SDK/model that doesn't accept the thinking config), retry once plainly. The
    # provider appends its assistant turn only on success, so run.messages is untouched on a
    # failed attempt and the retry is safe.
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
                # ToolCallEvent / TurnEnd are irrelevant here (tools=[]).
            break   # streamed to completion
        except Exception as exc:  # noqa: BLE001 — surface a clean message; detail to the log
            if attempt == 0 and use_thinking and not produced:
                log.warning("rebuild: retrying without extended thinking (%s)", exc)
                use_thinking = False
                parts = []
                continue
            log.exception("rebuild generate failed for %s", run.title)
            run.status = "error"
            run.error = str(exc)
            yield {"type": "error", "message": "The rebuild failed while generating. Please try again."}
            return
    if run.cancelled:
        return

    raw = "".join(parts)
    draft, talk = wiki_build._extract_talk(wiki_build._strip_fence(raw))
    # Deterministic dead-link backstop (no extra LLM call): never stage a [[link]] to a
    # page that doesn't exist — unwrap it to plain text and note it.
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


async def run_rebuild(run) -> AsyncGenerator[dict, None]:
    """Initial rebuild: load the page's sources, build the writer prompt, stream the rewrite."""
    from ..db import get_conn
    from . import llm, wiki_build

    conn = get_conn()
    if not llm.has_credentials():
        run.status = "error"
        yield {"type": "error", "message": "No LLM credentials configured."}
        return

    art, instr, _prior = wiki_build.rebuild_sources(conn, run.title, run.instructions)
    scope = art.get("scope") or ""
    subject = f"{run.title.rsplit('/', 1)[-1]} {scope}".strip()
    srcs = wiki_build._load_sources(conn, art.get("sources") or [], query=subject)
    if not srcs:
        run.status = "error"
        run.error = "no source notes resolved"
        yield {"type": "error", "message": "This page has no source notes to rebuild from."}
        return

    run.sources = [{"title": s["title"]} for s in srcs]
    yield {"type": "sources", "items": run.sources}

    run.known = wiki_build._known_titles(conn)
    prompt = wiki_build.build_write_prompt(conn, art, srcs, instr, run.known)
    run.messages = [{"role": "user", "content": prompt}]

    async for ev in _generate(run, conn):
        yield ev


async def run_guide(run, instruction: str) -> AsyncGenerator[dict, None]:
    """Steer a revision: append the user's guidance and re-stream from the SAME loaded
    context (no new sources / tools), per the locked design."""
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
