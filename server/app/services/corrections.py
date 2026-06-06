"""Source-of-truth correction promotion.

When the owner adds a talk item of kind 'correction' to a wiki article, that item is a
factual fix the owner has verified ("the name is spelled Bjørn, not Bjorn"). We promote it
into the TRUTH LAYER — a real dated entry note — so the fact is durable, propagates the way
any captured note does, and survives renames and full rebuilds. The talk row is linked to
that note via article_talk.source_note_id; the next maintenance pass feeds the note in as a
source and the existing SUPERSEDE-by-recency rule rewrites the article from it.

Design decisions (owner-confirmed):
  - Only the 'correction' kind promotes — never note/directive/question/todo (no AI gate on
    the request path, so a real correction is never silently dropped).
  - Source entries are NEVER modified; raw captures stay immutable ground truth. Propagation
    is recency-supersede only.
  - Re-submitting the same correction does not spawn a second note (dedup, below).

Promotion is deterministic and fast — no LLM on the talk endpoint.
"""
from __future__ import annotations

import logging
import re

from . import clock, notes as notes_svc
from .article_talk import _norm

log = logging.getLogger("jbrain")

# High-precision (name/spelling-gated) extraction of the corrected value from a correction
# body. Gated on an explicit name/spelling keyword so a non-name fix ("the year is 1990, not
# 1989") never matches and mis-sets an entity name. A miss degrades gracefully: no entity
# override is recorded, but the article still heals via the promoted note (Phase 1).
_NAME_CORR = re.compile(
    r"\b(?:spelled|spelling\s+is|spelling\s+should\s+be|name\s+is|named|called|known\s+as)\s+"
    r"([^,.;\n]+?)(?:\s*,?\s+not\b|[,.;\n]|$)",
    re.IGNORECASE,
)


def extract_corrected_name(body: str) -> str | None:
    """The corrected name asserted by a name/spelling correction, or None. Conservative:
    requires a name keyword and a plausible (≤6-word, has-a-letter) value."""
    m = _NAME_CORR.search(body or "")
    if not m:
        return None
    name = m.group(1).strip().strip("\"'")
    # "name is spelled X" matches the "name is" branch and captures "spelled X" — drop a
    # leading filler word so we keep just the value.
    name = re.sub(r"^(?:spelled|spelt|spelling|actually|really|correctly|now)\s+", "",
                  name, flags=re.IGNORECASE).strip()
    if not name or len(name.split()) > 6 or not re.search(r"[^\W\d_]", name):
        return None
    return name


def maybe_promote(conn, talk_id: int, article_title: str, body: str) -> dict | None:
    """Promote a 'correction' talk item to a dated entry note and link it back.

    Runs inside the talk endpoint's transaction (before commit). Returns the new note's
    {slug, title}, or None if nothing was promoted (empty body / duplicate). The caller
    commits, then flushes entry events so the note's analysis/auto-tag hooks fire.
    """
    body = (body or "").strip()
    if not body:
        return None

    # Dedup: if an earlier OPEN correction with the same normalized body already exists on
    # this article, the re-submit is redundant — drop the just-added row and don't spawn a
    # second truth note. (The router inserts via article_talk.add(), which does NOT dedup.)
    norm = _norm(body)
    prior = conn.execute(
        "SELECT body FROM article_talk WHERE article_title=? AND kind='correction' "
        "AND resolved_at IS NULL AND id<>?",
        (article_title, talk_id),
    ).fetchall()
    if any(_norm(r["body"]) == norm for r in prior):
        conn.execute("DELETE FROM article_talk WHERE id=?", (talk_id,))
        return None

    # Create the dated entry note in the standard capture tree (notes/YYYY/MM/DD/NN). The
    # "CORRECTION (source of truth):" header is what the wiki_maintain prompt keys on to
    # treat it as authoritative; the [[article]] link is for backlinks/display only (it is
    # NOT the routing mechanism — routing is via the talk row's source_note_id).
    title = notes_svc.next_dated_title(conn, clock.today_local())
    content = (
        f"CORRECTION (source of truth): {body}\n\n"
        f"Applies to [[{article_title}]]\n"
    )
    note_id = notes_svc.upsert_note(
        conn, title, content,
        source="user", kind="entry", fire_events=False,
        version_note=f"source-of-truth correction for {article_title}",
    )

    conn.execute(
        "UPDATE article_talk SET is_correction=1, source_note_id=? WHERE id=?",
        (note_id, talk_id),
    )

    # Durable entity healing (Phase 2): if this is a name/spelling fix, record an owner
    # override so the entity index displays the corrected name and resolves both spellings —
    # and survives the nightly rebuild that would otherwise pick the frequency-derived name.
    name = extract_corrected_name(body)
    if name:
        from . import entity_index
        entity_index.set_canonical_override(conn, article_title, name, source_note_id=note_id)

    log.info("corrections: promoted talk %s -> note %s (%s) for %s",
             talk_id, note_id, title, article_title)
    row = conn.execute("SELECT slug, title FROM notes WHERE id=?", (note_id,)).fetchone()
    return dict(row) if row else None
