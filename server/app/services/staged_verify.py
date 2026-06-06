"""Bring interactively-staged article writes up to the NIGHTLY pipeline's link/citation bar.

When a user asks the assistant to create/update an article, the staged content is written
straight to a note — historically with none of the hygiene the nightly KB synthesis enforces
(dead-link stripping, citation integrity). This module reuses the SAME nightly primitives
(`wiki_build._bad_links` / `_neutralize_links`, `pipeline.citation_issues`) so:
  - `verify_content` is the hard backstop applied at APPLY time: a [[Title]] that resolves to no
    live note is neutralized to plain text (exactly as the nightly save does), so a fabricated link
    can never reach a saved note.
  - `warnings` is the read-only, warn-not-block layer surfaced to the model at PROPOSE time and to
    the user in the approval preview, so a stripped link / missing LINK target / broken citation is
    visible BEFORE approval rather than a silent rewrite on save.

Lives in services (not the staging router) so both the router and architect can use it without a
circular import.
"""
from __future__ import annotations

from . import notes as notes_svc


def verify_content(conn, content: str) -> tuple[str, list[str]]:
    """Neutralize every [[Title]] in `content` that resolves to no live note — the nightly
    dead-link backstop, applied to interactively-staged content. Returns (clean, dropped_titles)."""
    from . import wiki_build
    if not content:
        return content, []
    bad = wiki_build._bad_links(conn, content, allowed=set())   # allowed=∅ → "live notes only"
    if not bad:
        return content, []
    return wiki_build._neutralize_links(content, set(bad)), bad


def _is_kb(conn, title: str, payload: dict) -> bool:
    if (payload.get("kind") or "") == "kb" or (title or "").lower().startswith("kb/"):
        return True
    note = notes_svc.get_by_title(conn, (title or "").strip()) if title else None
    return bool(note and note["kind"] == "kb")


def warnings(conn, action_type: str, payload: dict) -> list[str]:
    """Read-only warnings for a staged action (never blocks): links that will be stripped on save
    because their target doesn't exist, a LINK to a missing target (apply refuses it), and — for a
    kb/ article — footnote/citation integrity. Surfaced to the model (propose) and the user (preview)."""
    out: list[str] = []
    if action_type in ("CREATE", "UPDATE"):
        content = payload.get("content") or ""
        _, dead = verify_content(conn, content)
        if dead:
            out.append(f"{len(dead)} link(s) point to notes that don't exist and will be removed on "
                       f"save: " + ", ".join(dead[:6]))
        if content and _is_kb(conn, (payload.get("title") or "").strip(), payload):
            from . import pipeline
            out += pipeline.citation_issues(content)
    elif action_type == "LINK":
        tt = (payload.get("target_title") or "").strip()
        if tt and notes_svc.get_by_title(conn, tt) is None:
            out.append(f"Can't link to “{tt}” — no such note exists (this action will be refused).")
    return out
