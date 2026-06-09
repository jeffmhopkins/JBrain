"""Knowledge-base BUILD engine (component 2) — the automatic rebuild.

Composition lives in the wiki_build action recipe; the heavy lifting is here:

  reset()         soft-delete every kb/ article EXCEPT protected kb/_* pages, and clear
                  the synthesis watermark/markers so a fresh build re-reads everything.
  corpus_digest() the compact survey the outline reads — gist + domain + entities per
                  note, from the analysis sidecar (falling back to a content snippet).
  outline()       LLM → the entity-first taxonomy: articles (kb/<Domain>/<Name>) each
                  with a scope and the source-note ids assigned to it, plus the _index.
  write_one()     write ONE article from its raw source notes + the domain guide, then a
                  single self-critique/revise pass against the structure lint.
  write_batch()   write every article; split into valid (saved by the recipe) vs
                  quarantined (failed the lint — surfaced, not saved).

Raw source notes stay the ground truth — the analysis sidecar only guides organisation.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from . import llm, prompts, wiki_guides, wikilinks

log = logging.getLogger("jbrain")

# Provider finish reasons that mean the output was cut off at the token cap (Anthropic
# "max_tokens", xAI "length"). The batch writers retry once at a bigger cap, then FAIL —
# never save a half-written article whose trailing ## References was the first casualty.
_TRUNCATED = ("max_tokens", "length")

_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_TALK_RE = re.compile(r"\n?```talk\s*\n(.*?)```[ \t]*\n?", re.DOTALL)

# Auto-continue is LIVE-ONLY. When a draft is cut off at the token cap, the live, streaming
# rebuild engine (rebuild_engine._generate) asks the model to RESUME exactly where it stopped,
# stitches the RAW partial + RAW remainder with _join_continuation, and runs
# _strip_fence/_extract_talk ONCE on the JOINED string. It is capped at ONE continuation and
# surfaces truncated=True/lint ok=False, so a human reviews and ACCEPTS the stitched draft
# before it is saved — a bad stitch is caught at the gate. The BATCH writers (write_one,
# maintain_one) auto-save with no such gate, so they DELIBERATELY never auto-continue: they
# retry once at a bigger cap, then quarantine (write_one ok=False / maintain_one changed=False).
CONTINUE_PROMPT = (
    "Your previous message was cut off at the length limit before you finished. "
    "Continue the article from EXACTLY where you stopped — resume mid-word/mid-line if "
    "that is where it ended — and output ONLY the remaining text. Do NOT restate, repeat, "
    "or re-introduce anything you already wrote, and do NOT wrap your continuation in a code "
    "fence. Pick up precisely at the cutoff and finish the article (including the trailing "
    "```talk block if it was not yet emitted)."
)


_OVERLAP_WINDOW = 400          # max chars of partial-suffix / remainder-prefix we compare
_OVERLAP_FLOOR = 12            # an overlap shorter than this is too coincidental to act on


def _trim_restated_overlap(partial: str, remainder: str) -> str:
    """Drop a restated prefix from remainder when the continuation re-emitted whole trailing lines.

    Restricted to an EDGE-ANCHORED EXACT restatement: the overlap must be one-or-more WHOLE
    trailing LINES of the partial (a candidate suffix that begins exactly at a line boundary of
    the partial — index 0 or just after a '\\n') that the remainder reproduces verbatim at its
    start. The model clearly re-emitted the last complete line(s) it had written.

    This is deliberately NOT a free mid-phrase substring trim. The previous version stripped
    ANY >=12-char suffix overlap, which would silently DELETE a COINCIDENTALLY recurring phrase
    across the seam (verified: '…the city of Washington' + 'the city of Washington remains…'
    lost the phrase, because the shared run was a mid-line substring, not a restated whole line).
    Anchoring to a line boundary means an incidental recurring phrase mid-line can't trigger a
    trim — when in doubt we leave the text un-trimmed and the human sees the (visible) dup rather
    than us silently dropping real content. A genuine mid-word resume has no line-anchored
    overlap and is untouched.

    Args:
        partial: The already-produced portion of the truncated draft.
        remainder: The continuation text returned by the model.

    Returns:
        remainder with its restated prefix removed, or remainder unchanged.
    """
    if not partial or not remainder:
        return remainder
    window = partial[-_OVERLAP_WINDOW:]
    # Candidate overlaps: each WHOLE trailing line of the partial, i.e. the suffix of `window`
    # starting at a line boundary (offset 0, or right after a '\n'). Longest first so a
    # multi-line restatement is trimmed in one go.
    starts = [0] + [i + 1 for i, ch in enumerate(window) if ch == "\n"]
    for s in starts:                                   # ascending offset == descending length
        cand = window[s:]
        if len(cand) >= _OVERLAP_FLOOR and remainder.startswith(cand):
            return remainder[len(cand):]
    return remainder


_LEADING_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _drop_duplicate_title(partial: str, remainder: str) -> str:
    """Drop a duplicate H1 heading when the continuation re-introduces the article title.

    Only fires when the partial opened with an H1 ("# Title") and the remainder RESTARTS with
    the same heading — a model that re-introduced the article. A legitimate new "## Section"
    or a different heading is never removed.

    Args:
        partial: The already-produced portion of the truncated draft.
        remainder: The continuation text returned by the model.

    Returns:
        remainder with the duplicated H1 stripped, or remainder unchanged.
    """
    pm = _LEADING_H1_RE.search(partial or "")
    if not pm:
        return remainder
    rstrip = (remainder or "").lstrip("\n")
    rm = re.match(r"#\s+(.+?)\s*(?:\n|$)", rstrip)
    if rm and rm.group(1).strip() == pm.group(1).strip():
        return rstrip[rm.end():]
    return remainder


_BLOCK_START_RE = re.compile(r"(?:#{1,6}\s|[-*>]\s|\d+\.\s|\||```)")


def _seam_separator(partial: str, remainder: str) -> str:
    """Pick the glue string between partial and remainder at a continuation seam.

    The model is told to resume EXACTLY at the cutoff, so we glue with NOTHING by default — a
    mid-word/mid-fence seam ("Refe" + "rences") MUST re-form, and that safe behaviour is what
    the whole design relies on. A provider that .strip()s each segment (xAI does) can drop the
    whitespace that lived at the seam, but we can only safely repair the ONE unambiguous case:
    the remainder begins a line-anchored markdown BLOCK ("## References", "- item", "1. ",
    "> ", "|", a fenced block). A genuine mid-word resume never starts with such a marker, so
    inserting a newline there is a zero-false-positive fix that keeps a stripped "## References"
    heading on its own line.

    We deliberately do NOT insert a SPACE for a plain word/word seam ("in" + "2026"): that case
    is genuinely indistinguishable from a real mid-word resume ("Refe" + "rences"), so guessing a
    space risks corrupting content. We fall back to the safe glue-raw behaviour — a rare cosmetic
    "in2026" under xAI is preferable to splitting a word.

    Args:
        partial: The already-produced portion of the truncated draft.
        remainder: The continuation text returned by the model.

    Returns:
        "\\n" when both sides touch non-whitespace and remainder opens a markdown block,
        "" otherwise.
    """
    if not partial or not remainder:
        return ""
    if partial[-1].isspace() or remainder[0].isspace():
        return ""
    if _BLOCK_START_RE.match(remainder):
        return "\n"
    return ""


def _join_continuation(partial: str, remainder: str) -> str:
    """Stitch a truncated draft's RAW partial with its RAW continuation.

    The model is told to resume EXACTLY at the cutoff, so we glue with no separator by default —
    but we first guard two real hazards:
      * DUPLICATION — a model that RESTATES instead of resuming. _drop_duplicate_title removes a
        repeated "# Title" heading; _trim_restated_overlap then drops the longest restated
        run (capped at _OVERLAP_WINDOW) so the join isn't garbled/doubled.
      * STRIPPED SEAM — a provider that .strip()s each segment (xAI) drops the whitespace that
        lived at the cutoff; _seam_separator re-inserts a newline before a markdown block so a
        stripped "## References" heading keeps its own line (a word/word seam is left raw — see
        _seam_separator for why a space there would risk splitting a mid-word resume).
    Extraction (_strip_fence / _extract_talk) still runs ONCE on this joined string in the
    CALLER, so a fence split across the boundary re-forms before parsing.

    Args:
        partial: The already-produced portion of the truncated draft.
        remainder: The raw continuation text returned by the model.

    Returns:
        The stitched full draft string.
    """
    partial = partial or ""
    remainder = remainder or ""
    remainder = _drop_duplicate_title(partial, remainder)
    remainder = _trim_restated_overlap(partial, remainder)
    return partial + _seam_separator(partial, remainder) + remainder


def _extract_talk(text: str):
    """Pull a trailing ```talk JSON block out of the writer's output.

    Args:
        text: Raw writer output, possibly containing a trailing ```talk block.

    Returns:
        Tuple of (article_text_without_block, list_of_talk_entry_dicts).
    """
    m = _TALK_RE.search(text or "")
    if not m:
        return text, []
    body = text[:m.start()] + text[m.end():]
    try:
        data = json.loads(m.group(1))
        entries = [e for e in data if isinstance(e, dict) and e.get("body")] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        entries = []
    return body, entries


def reset(conn) -> dict:
    """Soft-delete all kb/ articles except protected kb/_* pages, and clear synthesis markers.

    Clears the synthesis watermark + per-entry evaluated markers so a fresh build re-reads
    everything. Undoable (soft delete + versioning). Disambiguation pages (kb/_disambig/*) are
    DERIVED build artifacts, not static guides, so they're cleared here too (and regenerated).

    Args:
        conn: SQLite connection.

    Returns:
        Dict with keys ``deleted`` and ``kept``.
    """
    from . import notes as notes_svc
    rows = conn.execute(
        "SELECT id, title, redirect_to FROM notes WHERE kind = 'kb' AND deleted_at IS NULL"
    ).fetchall()
    deleted = kept = 0
    for r in rows:
        # Redirects are durable: a merged-away page must survive a full rebuild so old
        # [[links]] keep resolving. Keep them (don't soft-delete) and count as kept.
        if r["redirect_to"]:
            kept += 1
            continue
        derived = r["title"].startswith("kb/_disambig/")
        if wiki_guides.is_protected(r["title"]) and not derived:
            kept += 1
            continue
        notes_svc.soft_delete(conn, r["id"])
        deleted += 1
    conn.execute("DELETE FROM meta WHERE key LIKE 'wiki_synth:evaluated:%'")
    conn.execute("DELETE FROM meta WHERE key = 'wiki_synth:since'")
    conn.commit()
    return {"deleted": deleted, "kept": kept}


def corpus_digest(conn, limit: int = 3000) -> list[dict]:
    """Build the compact survey the outline reads: one record per entry/daily note.

    Each record is {id, title, gist, domain, entities}. Uses the analysis sidecar; falls
    back to a content snippet for notes not yet analyzed.

    Args:
        conn: SQLite connection.
        limit: Maximum number of notes to include, ordered by most recently updated.

    Returns:
        List of compact note-summary dicts.
    """
    rows = conn.execute(
        "SELECT n.id, n.title, n.content_md, a.gist, a.domain, a.entities_json "
        "FROM notes n LEFT JOIN note_analysis a ON a.note_id = n.id "
        "WHERE n.deleted_at IS NULL AND n.kind IN ('entry','daily') AND n.kb_ingest = 1 "
        "ORDER BY n.updated_at DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for r in rows:
        gist = (r["gist"] or "").strip()
        if not gist:
            gist = re.sub(r"\s+", " ", (r["content_md"] or "")).strip()[:160]
        ents = []
        try:
            ents = [e.get("name") for e in json.loads(r["entities_json"] or "[]")
                    if isinstance(e, dict) and e.get("name")]
        except Exception:  # noqa: BLE001
            pass
        out.append({"id": r["id"], "title": r["title"], "gist": gist,
                    "domain": r["domain"] or "Unsure", "entities": ents[:8]})
    return out


def _survey_text(digest: list[dict], cap: int = 800) -> str:
    """Render a corpus digest as a plain-text survey for the outline prompt.

    Args:
        digest: Output of corpus_digest.
        cap: Maximum number of entries to include.

    Returns:
        Newline-joined survey string, one line per note.
    """
    lines = []
    for d in digest[:cap]:
        ents = ", ".join(d.get("entities") or [])
        line = f"#{d['id']} [{d.get('domain', '?')}] {d['gist']}"
        if ents:
            line += f" — entities: {ents}"
        lines.append(line)
    return "\n".join(lines)


def build_index_md(articles: list[dict]) -> str:
    """Build the kb/_index org map: articles grouped by domain with wikilinks.

    Protected, so the rebuild never deletes it and synthesis never feeds it back.

    Args:
        articles: List of article dicts with ``title``, ``domain``, and ``scope`` keys.

    Returns:
        Markdown string for the kb/_index page.
    """
    by_dom: dict[str, list[dict]] = {}
    for a in articles:
        by_dom.setdefault(a.get("domain") or "Other", []).append(a)
    lines = ["# Knowledge base index", "",
             "Auto-generated map of the knowledge base, organised by domain. "
             "Rebuilt whenever the knowledge base is rebuilt.", ""]
    ordered = [*wiki_guides.DOMAINS, *(d for d in by_dom if d not in wiki_guides.DOMAINS)]
    for dom in ordered:
        arts = by_dom.get(dom)
        if not arts:
            continue
        lines.append(f"## {dom}")
        for a in sorted(arts, key=lambda x: x["title"]):
            scope = (a.get("scope") or "").strip()
            lines.append(f"- [[{a['title']}]]" + (f" — {scope}" if scope else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _isint(x) -> bool:
    """Return True if x can be converted to int, False otherwise."""
    try:
        int(x); return True
    except (TypeError, ValueError):
        return False


def outline(conn, digest: list[dict], instructions: str | None = None) -> dict:
    """Derive the entity-first KB taxonomy from a corpus digest via the LLM.

    The canonical entity roster (recurring people/orgs/places + co-occurrence) is fed in
    alongside the per-note survey so the outline reliably makes one article per entity and
    clusters co-occurring people into Groups; entity mentions then backfill each article's
    sources so no note about an entity is missed.

    Args:
        conn: SQLite connection.
        digest: Output of corpus_digest — compact per-note survey.
        instructions: Optional freeform guidance appended to the outline prompt.

    Returns:
        Dict with keys ``articles`` (list of {title, domain, scope, sources}),
        ``index_md`` (pre-built index markdown), and ``dropped`` (count of
        ungrounded articles removed).
    """
    if not llm.has_credentials() or not digest:
        return {"articles": [], "index_md": ""}
    from . import entity_index
    extra = f"\nAdditional guidance: {instructions}\n" if instructions else ""
    roster = entity_index.roster(conn) or "(none yet)"
    saved_places = ", ".join(
        r["name"] for r in conn.execute("SELECT name FROM places ORDER BY name").fetchall()) or "(none)"
    from . import people
    prompt = (prompts.get("actions.wiki_outline", "")
              .replace("{owner}", people.owner_name(conn))
              .replace("{survey}", _survey_text(digest))
              .replace("{roster}", roster)
              .replace("{saved_places}", saved_places)
              .replace("{instructions}", extra))
    try:
        text = llm.complete([{"role": "user", "content": prompt}], max_tokens=4000)
    except Exception as exc:  # noqa: BLE001
        log.info("wiki_outline failed: %s", exc)
        return {"articles": [], "index_md": ""}

    from .workflows import _parse_json_array
    articles, seen = [], set()
    for a in _parse_json_array(text):
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        if not title.lower().startswith("kb/") or title.lower() in seen:
            continue
        if wiki_guides.is_protected(title):       # never let the outline target a system page
            continue
        seen.add(title.lower())
        domain = a.get("domain") or wiki_guides.domain_for_title(title)
        sources = [int(x) for x in (a.get("sources") or []) if _isint(x)]
        articles.append({"title": title, "domain": domain,
                         "scope": str(a.get("scope") or "").strip(), "sources": sources})

    # Assignment safety net: if an article's name matches a canonical entity, make sure
    # EVERY note that mentions that entity is in its sources — catching any the LLM missed.
    for art in articles:
        leaf = art["title"].split("/")[-1]
        ids = entity_index.note_ids_for_name(conn, leaf)
        if ids:
            art["sources"] = sorted(set(art["sources"]) | set(ids))
    # Scope floor: drop any article with no source note — it isn't grounded in your notes
    # (a >1-hop / general-knowledge article). Keeps the wiki to "1 hop out from the notes".
    grounded = [a for a in articles if a["sources"]]
    dropped = len(articles) - len(grounded)
    return {"articles": grounded, "index_md": build_index_md(grounded), "dropped": dropped}


SOURCE_BUDGET = 2000   # chars of note body fed to the writer per source note


def _load_sources(conn, ids: list[int], query: str = "") -> list[dict]:
    """Load source notes by id, trimming each to SOURCE_BUDGET chars for the writer.

    Passes RAW content (never expands @t[...] tokens) so the writer can carry live tokens
    through into the evergreen article. Uses embeddings to pick relevant passages when a
    subject query is given and the note exceeds the budget. Folds in attachment text
    (image summaries, transcripts, document text) after the body.

    Args:
        conn: SQLite connection.
        ids: Note ids to load; non-integer values are silently dropped.
        query: Subject query used to relevance-select passages from long notes.

    Returns:
        List of {title, date, content} dicts, one per resolved live note.
    """
    ids = [int(i) for i in ids if _isint(i)]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, content_md, created_at FROM notes "
        f"WHERE id IN ({q}) AND deleted_at IS NULL ORDER BY created_at",
        ids,
    ).fetchall()
    from . import attachments as att_svc
    from . import embeddings
    out = []
    for r in rows:
        # Pass RAW content (do NOT expand @t[...] tokens): the writer must see the live
        # tokens so it can carry them through into the evergreen article — expanding
        # here would freeze "Jeff is @t[age:1986-03-15]" into a literal that rots.
        body = r["content_md"] or ""
        # For a note longer than the budget, a blind head slice can miss a subject-relevant
        # fact buried past the cut. When we have a subject (`query`), pick the relevant
        # passages from the note's chunks instead — local embeddings, no extra LLM call. The
        # helper is a safety-net: it returns None (→ head slice) unless a buried passage is
        # genuinely more on-subject than the head, so short/already-relevant notes are unchanged.
        content = None
        if query and len(body) > SOURCE_BUDGET:
            content = embeddings.relevant_note_excerpt(conn, r["id"], query, SOURCE_BUDGET)
        if content is None:
            content = body[:SOURCE_BUDGET]
        # Fold in attachment text (own bounded budget, after the body): image summaries,
        # audio/video transcripts, and document (PDF/text/office) text — all keep feeding
        # synthesis now that they live beside the note rather than in its body.
        att = att_svc.context_block_for_note(conn, r["id"], cap=1200)
        if att:
            content = (content + "\n\n" + att) if content else att
        out.append({"title": r["title"], "date": (r["created_at"] or "")[:10], "content": content})
    return out


def _sources_text(srcs: list[dict]) -> str:
    """Render loaded source notes as a single block for inclusion in a writer prompt.

    Args:
        srcs: List of {title, date, content} dicts from _load_sources.

    Returns:
        Double-newline-separated string with each note prefixed by its title and date.
    """
    return "\n\n".join(f"[{s['title']}] ({s['date']})\n{s['content']}" for s in srcs)


def _strip_fence(text: str) -> str:
    """Strip a wrapping ```markdown or ``` code fence from model output, if present.

    Args:
        text: Raw model output string.

    Returns:
        The content inside the fence, or the original text stripped if no fence matched.
    """
    m = _FENCE_RE.match(text or "")
    return (m.group(1) if m else (text or "")).strip()


_WRAPPER_OPEN_RE = re.compile(r"^\s*```(?:markdown|md)?[ \t]*\n", re.IGNORECASE)


def _clean_wrapper_fence(text: str) -> str:
    """Remove a leftover ```markdown wrapper-fence artifact from an auto-continued live draft.

    Run AFTER the single _strip_fence. The only artifact we touch is the verified leak case (b):
    the partial CLOSED the whole-document wrapper at the cap, the appended remainder then pushed
    text PAST the trailing ```, so the closing fence is no longer terminal and the anchored
    _FENCE_RE leaves the WHOLE wrapper (a leading ```/```markdown/```md opener plus its
    now-orphaned ``` closer) in the body. We drop that leading opener line AND the matching
    orphaned closer so the wrapper doesn't half-survive.

    Deliberately conservative — anchored to the DOCUMENT EDGE only:
      * We act ONLY when the document STARTS with a bare ```/```markdown/```md opener (the
        wrapper's own edge), never on a close-then-reopen pair in mid-document prose. A
        mid-document ```\\n```markdown can be two LEGITIMATE adjacent code blocks in prose that
        is ABOUT fences, so collapsing it could silently merge real content — we leave it for
        the human to catch in live review instead.
      * A document opening with a fenced *code* block (```python …) is untouched: we only match
        a bare ``` or ```markdown / ```md opener (no language tag), and only at the edge.
      * We drop the orphaned closer ONLY when the fences before it are balanced and none follow
        it, so we never break a real inner code block; if we can't be sure, we leave it.

    Args:
        text: Article text after _strip_fence.

    Returns:
        Text with the wrapper fence artifact removed, or text stripped if no artifact.
    """
    if not text:
        return text
    m = _WRAPPER_OPEN_RE.match(text)
    if not m:
        return text.strip()                              # no wrapper opener at the edge → leave as-is
    rest = text[m.end():]
    # Find the orphaned wrapper closer: the LAST line that is a bare ``` (the wrapper's own
    # close). Only treat it as the wrapper closer when removing it leaves balanced fences in
    # the remaining body (so we never break a real inner code block).
    lines = rest.split("\n")
    close_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "```":
            close_idx = i
            break
    if close_idx is not None:
        # Fences BEFORE the closer, excluding it: must be balanced (even) so the closer we
        # drop is the wrapper's, not an inner block's open/close.
        before = sum(1 for ln in lines[:close_idx] if ln.strip().startswith("```"))
        after = sum(1 for ln in lines[close_idx + 1:] if ln.strip().startswith("```"))
        if before % 2 == 0 and after == 0:
            del lines[close_idx]
            rest = "\n".join(lines)
    return rest.strip()


def _bad_links(conn, content: str, allowed: set[str]) -> list[str]:
    """Return [[wikilink]] targets in content that resolve to nothing.

    A target is dead if it is neither in the allowed set nor an existing live note.
    These are the dead links we refuse to save.

    Args:
        conn: SQLite connection.
        content: Article markdown to scan.
        allowed: Set of article titles that are valid targets for this build.

    Returns:
        List of dead-link target strings.
    """
    bad = []
    for t in wikilinks.extract_links(content):
        if t in allowed:
            continue
        if conn.execute("SELECT 1 FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                        (t,)).fetchone():
            continue
        bad.append(t)
    return bad


def _neutralize_links(content: str, bad: set[str]) -> str:
    """Unwrap dead [[wikilinks]] to plain text so they can't reach a saved article.

    A dead link in a FOOTNOTE DEFINITION (a citation to a now-missing source) drops the whole
    definition line — and any inline [^marker] left without a definition is then stripped —
    rather than leaving a mangled '[^s1]: Ghost — DATE'. Dead links in PROSE unwrap to plain
    text. Live links and valid footnotes are untouched.

    Args:
        content: Article markdown to clean.
        bad: Set of dead-link target strings from _bad_links.

    Returns:
        Content with dead links unwrapped and orphaned footnote markers stripped.
    """
    def repl(m):
        """Unwrap a dead [[link]] to its display text or leaf name."""
        target = m.group(1).strip()
        if target not in bad:
            return m.group(0)
        inner = m.group(0)[2:-2]
        return inner.split("|", 1)[1].strip() if "|" in inner else target.split("/")[-1]

    kept = []
    for line in content.split("\n"):
        if re.match(r"^\s*\[\^[^\]]+\]:", line) and any(b in line for b in bad):
            continue                                        # a citation to a dead source → drop the footnote
        kept.append(line)
    out = wikilinks.WIKILINK_RE.sub(repl, "\n".join(kept))
    defined = set(re.findall(r"(?m)^\s*\[\^([^\]]+)\]:", out))
    # Strip inline footnote markers whose definition we just removed.
    return re.sub(r"\[\^([^\]]+)\]", lambda m: m.group(0) if m.group(1) in defined else "", out)


def _repair_citation_titles(content: str, bad: list[str], source_titles: list[str]):
    """Correct a writer-typo citation title before _neutralize_links would delete it.

    A footnote [[title]] that is a NORMALISED near-miss of exactly one CURATED source title is
    rewritten to that exact title (so a single mistyped source can't leave a bare
    '## References' heading). Conservative: only rewrites a target already deemed 'dead', only
    against THIS run's curated sources, and refuses when ≥2 distinct sources normalise-equal
    (ambiguous).

    Args:
        content: Article markdown to repair.
        bad: Dead-link targets from _bad_links.
        source_titles: Titles of the source notes used for this article.

    Returns:
        Tuple of (repaired_content, still_bad_list).
    """
    from . import entity_index
    if not bad or not source_titles:
        return content, bad
    norm_map: dict[str, set[str]] = {}
    for t in source_titles:
        norm_map.setdefault(entity_index.normalize(t), set()).add(t)
    fixes: dict[str, str] = {}
    still_bad: list[str] = []
    for b in bad:
        cands = norm_map.get(entity_index.normalize(b))
        if cands and len(cands) == 1:
            exact = next(iter(cands))
            if exact != b:
                fixes[b] = exact
            else:
                still_bad.append(b)   # already exact (shouldn't be 'bad', but don't drop it)
        else:
            still_bad.append(b)
    if not fixes:
        return content, still_bad

    def repl(m):
        """Rewrite a dead [[link]] target to its corrected source title."""
        inner = m.group(0)[2:-2]
        target, sep, disp = inner.partition("|")
        if target.strip() in fixes:
            return f"[[{fixes[target.strip()]}{sep}{disp}]]"
        return m.group(0)

    return wikilinks.WIKILINK_RE.sub(repl, content), still_bad


def _apply_link_props(body: str, props: list[dict]) -> str:
    """Insert [[target|surface]] wikilinks into body at the given character offsets.

    Applies right-to-left so earlier offsets stay valid after each insertion.

    Args:
        body: Article text to modify.
        props: List of {target, surface, at} dicts; ``at`` is the character offset.

    Returns:
        Body with all wikilinks inserted.
    """
    for p in sorted(props, key=lambda x: -x["at"]):
        s, e = p["at"], p["at"] + len(p["surface"])
        body = body[:s] + f"[[{p['target']}|{p['surface']}]]" + body[e:]
    return body


def _alias_link_props(conn, body: str, title: str, surface_map: dict) -> list[dict]:
    """Build link props for prose surfaces that match a registered entity alias.

    Links the matched surface to the alias's canonical article, displaying the matched prose
    text ([[canonical|Jeff Hopkins]]). The match is round-trip verified —
    normalize(surface) == alias_norm — so the label-hygiene allow-list protects exactly these
    links. Masks code/links/footnotes; never re-links an already-linked target; one link per
    canonical target. No DB write.

    Args:
        conn: SQLite connection (passed through to entity_index).
        body: Article text to scan.
        title: Title of the article being written (to avoid self-linking).
        surface_map: Output of entity_index.alias_surface — {alias_norm: (article, display)}.

    Returns:
        List of {target, surface, at} props for _apply_link_props.
    """
    from . import entity_index
    spans = _mask_spans(body)
    linked = {x.lower() for x in wikilinks.extract_links(body)}
    props: list[dict] = []
    seen_targets: set[str] = set()           # one link per canonical target, even across aliases
    for alias_norm, (art, disp) in surface_map.items():
        if not disp or art == title or art.lower() in linked or art in seen_targets:
            continue
        pat = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in disp.split()) + r"(?!\w)"
        for m in re.finditer(pat, body, re.IGNORECASE):
            if any(s <= m.start() < e for s, e in spans):
                continue
            surface = m.group(0)
            if len(surface) < 4 or entity_index.normalize(surface) != alias_norm:
                continue
            props.append({"target": art, "surface": surface, "at": m.start()})
            seen_targets.add(art)
            break
    return props


def add_links_to_content(conn, title: str, body: str):
    """Deterministically link bare mentions in body to their kb articles (in-memory only).

    The in-memory half of check_needed_links — performs NO DB write so a caller (the live
    rebuild engine / write_one) can apply it to a staged draft and persist it with the single
    article write. Two passes: (1) EXACT article-leaf matches; (2) registered ALIAS matches
    (e.g. prose "Jeff Hopkins" → [[kb/People/Jeffrey Hopkins|Jeff Hopkins]]).

    Self-guarding for the PII firewall: refuses entirely on a Reference or private (Health/
    Finance) TARGET article (those must stay unnamed / never link People), and never offers a
    private-domain target. Reuses ambiguity/short/common-word refusals and code/link/footnote
    masking, so it can't link a stop-word, an ambiguous leaf/alias, or inside a citation.

    Args:
        conn: SQLite connection.
        title: Title of the article being written.
        body: Draft article text to scan and link.

    Returns:
        Tuple of (new_body_with_links, proposals_list).
    """
    from . import entity_index
    if wiki_guides.is_private_title(title) or wiki_guides.domain_for_title(title) == "Reference":
        return body, []
    titles = _known_titles(conn)
    leafmap: dict[str, list[str]] = {}
    for t in titles:
        if wiki_guides.is_private_title(t):
            continue
        leafmap.setdefault(t.split("/")[-1].strip().lower(), []).append(t)
    ambiguous = {k for k, v in leafmap.items() if len(v) > 1}
    try:
        for amb in entity_index.ambiguous_terms(conn):
            ambiguous.add(str(amb.get("term", "")).lower())
    except Exception:  # noqa: BLE001
        pass

    def linkable(leaf_lower: str, leaf: str) -> bool:
        """Return True if leaf is safe to auto-link (unambiguous, long enough, not a stop word)."""
        if leaf_lower in ambiguous or len(leaf) < 4:
            return False
        if " " not in leaf and leaf_lower in _STOP_LEAVES:
            return False
        return True

    # Pass 1 — canonical article leaves.
    spans = _mask_spans(body)
    linked = {x.lower() for x in wikilinks.extract_links(body)}
    leaf_props: list[dict] = []
    for leaf_lower, cands in leafmap.items():
        tgt = cands[0]
        if tgt == title or tgt.lower() in linked:
            continue
        leaf = tgt.split("/")[-1]
        if not linkable(leaf_lower, leaf):
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(leaf) + r"(?!\w)", body, re.IGNORECASE):
            if not any(s <= m.start() < e for s, e in spans):
                leaf_props.append({"target": tgt, "surface": m.group(0), "at": m.start()})
                break
    body = _apply_link_props(body, leaf_props)

    # Pass 2 — registered aliases (re-mask against the post-leaf body so we never nest links).
    try:
        surface_map = entity_index.alias_surface(conn)
    except Exception:  # noqa: BLE001 — alias surfacing must never break linking
        surface_map = {}
    alias_props = _alias_link_props(conn, body, title, surface_map)
    body = _apply_link_props(body, alias_props)

    return body, [{"target": p["target"], "surface": p["surface"]} for p in (leaf_props + alias_props)]


def build_write_prompt(conn, art: dict, srcs: list[dict], instructions: str | None = None,
                       known_titles: list[str] | None = None) -> str:
    """Assemble the actions.wiki_write prompt for one article.

    Single source of truth for the writer prompt, shared by write_one (single-shot) and the
    live streaming rebuild engine. Mirror any change to the prompt assembly in write_one below.

    Args:
        conn: SQLite connection.
        art: Article dict with ``title``, ``domain``, ``scope``, and ``sources`` keys.
        srcs: Already-loaded source notes from _load_sources.
        instructions: Optional per-article guidance appended to the prompt.
        known_titles: Full set of planned article titles for cross-link validation.

    Returns:
        Filled prompt string ready to send to the LLM.
    """
    from . import people
    title = str(art.get("title") or "").strip()
    domain = art.get("domain") or wiki_guides.domain_for_title(title)
    scope = str(art.get("scope") or "")
    owner = people.owner_name(conn)
    general = wiki_guides.guide_text(None)
    dguide = wiki_guides.guide_text(domain)
    others = scoped_known_titles(conn, title, known_titles, source_ids=art.get("sources"))
    known_block = "\n".join(others) if others else "(no other articles yet)"
    alias_block = known_aliases_block(conn, others)
    guidance = f"\nADDITIONAL GUIDANCE — follow this too:\n{instructions.strip()}\n" if (instructions or "").strip() else ""
    return (prompts.get("actions.wiki_write", "")
            .replace("{owner}", owner)
            .replace("{general_guide}", general).replace("{domain_guide}", dguide)
            .replace("{domain}", domain or "").replace("{title}", title)
            .replace("{known_titles}", known_block).replace("{known_aliases}", alias_block)
            .replace("{instructions}", guidance)
            .replace("{scope}", scope).replace("{sources}", _sources_text(srcs)))


def build_suggest_prompt(conn, title: str, base: str, srcs: list[dict], backlinks: list[dict],
                         instruction: str | None, known_titles: list[str] | None = None,
                         source_ids: list[int] | None = None) -> str:
    """Assemble the actions.wiki_suggest prompt for a conversational targeted edit.

    Mirrors build_write_prompt but seeds the CURRENT article (``base``) as the thing being
    edited and adds read-only backlink context plus the owner's guidance. Used by the live
    "Suggest revisions" engine for the first edit turn.

    Args:
        conn: SQLite connection.
        title: KB article title being edited.
        base: The current (live) article body to edit FROM.
        srcs: Curated source notes from _load_sources (may be empty).
        backlinks: Read-only backlink context dicts ({title, excerpt}).
        instruction: The owner's guidance for this turn (empty → a tidy-only default).
        known_titles: Full set of article titles for cross-link validation.
        source_ids: Curated source-note IDs (seed mentioned-entity articles into known_titles).

    Returns:
        Filled prompt string ready to send to the LLM.
    """
    from . import people
    domain = wiki_guides.domain_for_title(title)
    owner = people.owner_name(conn)
    general = wiki_guides.guide_text(None)
    dguide = wiki_guides.guide_text(domain)
    others = scoped_known_titles(conn, title, known_titles, source_ids=source_ids)
    known_block = "\n".join(others) if others else "(no other articles yet)"
    alias_block = known_aliases_block(conn, others)
    bl = "\n\n".join(f"[{b['title']}]\n{b['excerpt']}" for b in (backlinks or [])) or "(none)"
    guidance = (instruction or "").strip() or (
        "Review the article and tidy any formatting or linking issues; make no factual change "
        "without a source.")
    return (prompts.get("actions.wiki_suggest", "")
            .replace("{owner}", owner)
            .replace("{general_guide}", general).replace("{domain_guide}", dguide)
            .replace("{domain}", domain or "").replace("{title}", title)
            .replace("{known_titles}", known_block).replace("{known_aliases}", alias_block)
            .replace("{backlinks}", bl)
            .replace("{sources}", _sources_text(srcs) if srcs else "(none)")
            .replace("{base}", base or "(empty)")
            .replace("{instructions}", guidance))


def write_one(conn, art: dict, instructions: str | None = None,
              known_titles: list[str] | None = None) -> dict:
    """Write one kb article from its raw source notes and apply a structure lint/revise pass.

    Performs ONE self-critique/revise pass against the structure lint. ``known_titles`` is the
    set of articles that will exist, so cross-links target real pages instead of inventing dead
    ones. Retries once at a doubled token cap on truncation, then quarantines — never saves a
    half-written article.

    Args:
        conn: SQLite connection.
        art: Article dict with ``title``, ``domain``, ``scope``, and ``sources`` keys.
        instructions: Optional per-article guidance appended to the writer prompt.
        known_titles: Full planned article-title set for dead-link validation.

    Returns:
        Dict with keys ``title``, ``domain``, ``content_md``, ``ok``, ``errors``,
        ``warnings``, ``stub``, and ``talk``.
    """
    title = str(art.get("title") or "").strip()
    domain = art.get("domain") or wiki_guides.domain_for_title(title)
    scope = str(art.get("scope") or "")
    # Subject query for relevance-selecting long sources: the article's leaf name + its scope.
    subject = f"{title.rsplit('/', 1)[-1]} {scope}".strip()
    srcs = _load_sources(conn, art.get("sources") or [], query=subject)
    base = {"title": title, "domain": domain, "content_md": "", "ok": False,
            "errors": [], "warnings": [], "stub": False, "talk": []}
    if not srcs:
        base["errors"] = ["no source notes resolved"]
        return base
    if not llm.has_credentials():
        base["errors"] = ["no LLM credentials"]
        return base

    from . import people
    owner = people.owner_name(conn)
    general = wiki_guides.guide_text(None)
    dguide = wiki_guides.guide_text(domain)
    # Only let the writer cross-link articles that will exist (minus this one), so it
    # can't invent a dead [[kb/People/Someone]] link. Capped to bound prompt size.
    others = scoped_known_titles(conn, title, known_titles, source_ids=art.get("sources"))
    known_block = "\n".join(others) if others else "(no other articles yet)"
    alias_block = known_aliases_block(conn, others)
    # Per-article guidance (e.g. open directives carried in by rebuild_article). Empty for
    # an ordinary build. Without the placeholder in the prompt this was silently ignored.
    guidance = f"\nADDITIONAL GUIDANCE — follow this too:\n{instructions.strip()}\n" if (instructions or "").strip() else ""
    prompt = (prompts.get("actions.wiki_write", "")
              .replace("{owner}", owner)
              .replace("{general_guide}", general).replace("{domain_guide}", dguide)
              .replace("{domain}", domain or "").replace("{title}", title)
              .replace("{known_titles}", known_block).replace("{known_aliases}", alias_block)
              .replace("{instructions}", guidance)
              .replace("{scope}", scope).replace("{sources}", _sources_text(srcs)))
    msgs = [{"role": "user", "content": prompt}]
    try:
        # Surface the finish reason so a draft cut off at the token cap (its trailing
        # ## References is the first casualty) is retried once at a bigger cap, then
        # QUARANTINED (ok=False) — never silently saved half-written. The batch path has NO
        # human Accept gate, so it never auto-continues/stitches a truncated draft (a bad
        # stitch would auto-save uncaught); auto-continue lives only in the live engine.
        cap = 3000
        text, stop = llm.complete_with_meta(msgs, max_tokens=cap)
        if stop in _TRUNCATED:
            cap = min(cap * 2, 6000)
            text, stop = llm.complete_with_meta(msgs, max_tokens=cap)
        if stop in _TRUNCATED:
            base["errors"] = ["draft truncated at the token limit"]
            return base
        draft, talk = _extract_talk(_strip_fence(text))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"write failed: {exc}"]
        return base

    allowed = {t for t in (known_titles or [])} | {title}
    source_titles = [s["title"] for s in srcs]
    draft, _ = _repair_citation_titles(draft, _bad_links(conn, draft, allowed), source_titles)
    v = wiki_guides.validate_structure(title, draft)
    bad = _bad_links(conn, draft, allowed)
    # Bounded revise loop (§10 gate model: fail → a bounded, NON-REGRESSING revise). Up to
    # two passes; a pass is kept only if it doesn't regress the lint/links, and we only take
    # a second pass when the first STRICTLY improved — so it converges and can't oscillate.
    for _ in range(2):
        if not (((v["errors"] or v["warnings"]) and not v["stub"]) or bad):
            break
        link_issues = [f"Dead link [[{t}]] — not a real article; link a listed article instead, or write it as plain text"
                       for t in bad]
        issues = "\n".join(f"- {x}" for x in (v["errors"] + v["warnings"] + link_issues))
        rprompt = (prompts.get("actions.wiki_revise", "")
                   .replace("{issues}", issues).replace("{general_guide}", general)
                   .replace("{domain_guide}", dguide).replace("{domain}", domain or "")
                   .replace("{known_titles}", known_block).replace("{draft}", draft))
        try:
            revised, rtalk = _extract_talk(_strip_fence(
                llm.complete([{"role": "user", "content": rprompt}], max_tokens=2200)))
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_revise failed for %s: %s", title, exc)
            break
        revised, _ = _repair_citation_titles(revised, _bad_links(conn, revised, allowed), source_titles)
        v2 = wiki_guides.validate_structure(title, revised)
        bad2 = _bad_links(conn, revised, allowed)
        prev, cur = len(v["errors"]) + len(bad), len(v2["errors"]) + len(bad2)
        if cur <= prev:                                 # non-regressing → adopt the revision
            draft, v, talk, bad = revised, v2, (rtalk or talk), bad2
        if cur >= prev:                                 # no strict improvement → stop looping
            break

    # Backstop guarantee: whatever the model left, no dead link survives into the saved
    # article — unwrap it to plain text and note it on the article's talk.
    if bad:
        draft = _neutralize_links(draft, set(bad))
        talk = list(talk) + [{"kind": "note",
                              "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
                             for t in bad]

    # Deterministic add-link backstop (in memory): link any bare mention matching an existing
    # kb leaf the writer left plain. Self-guards Reference/private targets (PII firewall).
    draft, _added = add_links_to_content(conn, title, draft)

    return {"title": title, "domain": domain, "content_md": draft, "talk": talk,
            "ok": v["ok"], "errors": v["errors"], "warnings": v["warnings"], "stub": v["stub"]}


def dead_links(conn) -> list[dict]:
    """Return all dangling [[links]] from kb articles to targets that no longer exist.

    Covers real articles AND the derived nav pages (kb/_index, kb/_disambig/*); only the
    static guides are skipped (they carry no article cross-links).

    Args:
        conn: SQLite connection.

    Returns:
        List of {source_title, source_slug, target_title} dicts.
    """
    rows = conn.execute(
        "SELECT s.title AS source_title, s.slug AS source_slug, l.target_title "
        "FROM links l JOIN notes s ON s.id = l.source_note_id AND s.deleted_at IS NULL "
        "WHERE l.target_note_id IS NULL AND s.kind='kb' AND s.redirect_to IS NULL AND ("
        "  s.title NOT LIKE 'kb/\\_%' ESCAPE '\\' "
        "  OR s.title = 'kb/_index' OR s.title LIKE 'kb/\\_disambig/%' ESCAPE '\\') "
        "ORDER BY s.title, l.target_title",
    ).fetchall()
    return [dict(r) for r in rows]


def flag_dead_links(conn) -> dict:
    """Neutralize dead cross-links in saved articles and log each fix as a resolved talk entry.

    Runs after every article is saved, so it has ground truth: any [[link]] whose target still
    doesn't exist (e.g. the target article was planned but quarantined) is unwrapped to plain
    text and recorded as a RESOLVED note on the article's talk — the AI handled it, so it's a
    log entry, never an open item awaiting a click. Also closes any dead-link items left open
    by an earlier build/model.

    Args:
        conn: SQLite connection.

    Returns:
        Dict with keys ``dead_links``, ``articles``, and ``fixed``.
    """
    from . import article_talk
    from . import notes as notes_svc
    items = dead_links(conn)
    by_src: dict[str, set[str]] = {}
    for it in items:
        by_src.setdefault(it["source_title"], set()).add(it["target_title"])
    fixed = 0
    for src, targets in by_src.items():
        row = conn.execute(
            "SELECT content_md FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL",
            (src,)).fetchone()
        if row:
            new = _neutralize_links(row["content_md"] or "", set(targets))
            if new != (row["content_md"] or ""):
                notes_svc.upsert_note(conn, src, new, kind="kb", version_note="unlinked dead references")
                fixed += 1
        article_talk.record(conn, src, [
            {"kind": "note", "body": f"Unlinked dead reference [[{t}]] — no such article; kept as plain text."}
            for t in sorted(targets)], author="ai")
    # Dead-link entries are completed actions, not open work — resolve them (and any left
    # open by older builds) so they live in the log, not the "needs attention" list.
    conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now') WHERE resolved_at IS NULL "
        "AND (body LIKE 'Dead link%' OR body LIKE 'Unlinked dead reference%')")
    conn.commit()
    return {"dead_links": len(items), "articles": len(by_src), "fixed": fixed}


def link_owner(conn) -> dict:
    """Connect the note owner to their kb/People article and store the matched slug.

    Matches a LIVE (non-redirect) kb/People page by three strategies in order:
    (1) exact normalized name/declared-alias; (2) a nickname-aware match (jeff↔jeffrey) gated
    exactly like the entity merge — shared distinctive surname token, no generational-suffix
    conflict, nickname-mapped token-subset — and only when EXACTLY ONE page matches (never
    guess among several); (3) generic placeholder leaves an unnamed owner page uses
    ('Owner'/'Me'). Never guesses — returns linked:None rather than risk attaching 'me' to a
    family member's page.

    Args:
        conn: SQLite connection.

    Returns:
        Dict with key ``linked`` (matched article title or None), and ``person`` when matched.
    """
    from . import people, entity_index, nickname_lexicon
    o = people.owner(conn)
    if not o:
        return {"linked": None}
    name = (o["name"] or "").strip()
    owner_norm = entity_index.normalize(name) if name and name.lower() != "me" else ""
    alias_norms = {entity_index.normalize(a) for a in (o["aliases"] or "").split(",") if a.strip()}
    alias_norms.discard("")
    rows = [dict(r) for r in conn.execute(
        "SELECT slug, title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL "
        "AND title LIKE 'kb/People/%'").fetchall()]
    leaf_norm = lambda t: entity_index.normalize(t.split("/")[-1])

    # 1) Exact normalized match on the owner's name or a declared alias.
    strong = ({owner_norm} if owner_norm else set()) | alias_norms
    target = next((r for r in rows if leaf_norm(r["title"]) in strong), None) if strong else None

    # 2) Nickname-aware, surname-gated, exactly-one-match (mirrors entity_index._merge_map).
    if target is None and owner_norm:
        otoks = set(owner_norm.split())
        odist = {t for t in otoks if len(t) >= 3}
        omap = nickname_lexicon.map_tokens(otoks)
        osuf = nickname_lexicon.suffixes(otoks)
        cands = []
        for r in rows:
            ltoks = set(leaf_norm(r["title"]).split())
            if not (odist & {t for t in ltoks if len(t) >= 3}):     # require a shared real token
                continue
            if osuf != nickname_lexicon.suffixes(ltoks):            # Jr/Sr conflict → different person
                continue
            lmap = nickname_lexicon.map_tokens(ltoks)
            if omap <= lmap or lmap <= omap:
                cands.append(r)
        if len(cands) == 1:                                          # never guess among ≥2
            target = cands[0]

    # 3) Generic placeholder pages for an unnamed owner.
    if target is None:
        weak = {"owner", "me", "the owner"} | ({owner_norm} if owner_norm else set())
        target = next((r for r in rows if leaf_norm(r["title"]) in weak), None)

    if not target:
        return {"linked": None}
    conn.execute("UPDATE people SET note_slug=? WHERE id=?", (target["slug"], o["id"]))
    conn.commit()
    return {"linked": target["title"], "person": o["name"]}


def reconcile_owner(conn) -> dict:
    """Bind the owner to their People article and seed their name/aliases as entity decisions.

    E1+E2: runs link_owner AND registers the owner's display name + declared aliases as
    DURABLE entity_decisions('alias') of that article's entity, so prose using a nickname
    ("Jeff Hopkins") links to the canonical page. Split-gated (never re-binds across a user
    split) and idempotent (entity_decisions.add de-dupes). The seeded aliases materialize into
    entity_aliases on the next entity_index.rebuild. Safe on the request path (no LLM/embeddings).

    Args:
        conn: SQLite connection.

    Returns:
        Dict with keys ``linked``, ``person``, and ``aliases_seeded``.
    """
    from . import people, entity_index, entity_decisions
    res = link_owner(conn)
    title = res.get("linked")
    if not title:
        return {**res, "aliases_seeded": 0}
    o = people.owner(conn)
    name = (o["name"] or "").strip()
    if not name:
        return {**res, "aliases_seeded": 0}
    # Anchor the alias decisions on the bound entity's key when one exists, else on the bound
    # article's leaf norm (a People article's leaf IS the person's name, so this equals the
    # entity's normalized_key). Leaf-norm anchoring means we record the decisions even before
    # the entity index has built/bound the entity — they fold onto the canonical entity on the
    # next rebuild — closing the "seeds nothing on a young KB and never retries" gap.
    ent = conn.execute("SELECT normalized_key FROM entities WHERE article_title=?", (title,)).fetchone()
    canon = ent["normalized_key"] if ent else entity_index.normalize(title.rsplit("/", 1)[-1])
    if not canon:
        return {**res, "aliases_seeded": 0}
    splits = entity_decisions.load_splits(conn)
    seeded = 0
    for form in [name, *[a.strip() for a in (o["aliases"] or "").split(",") if a.strip()]]:
        an = entity_index.normalize(form)
        if not an or an == canon or frozenset({canon, an}) in splits:
            continue
        entity_decisions.add(conn, kind="alias", type="person", norm_a=an, norm_b=canon, display_a=form)
        seeded += 1
    conn.commit()
    return {**res, "aliases_seeded": seeded}


_AKA_LINE_RE = re.compile(r"^\*Also known as:.*\*$")
_MD_ESCAPE_RE = re.compile(r"([\\*_`\[\]])")


def _md_escape(s: str) -> str:
    """Escape markdown emphasis/link characters in an alias display string.

    Prevents a name like 'Bob *x*' from breaking the AKA line's own emphasis.

    Args:
        s: Alias display string to escape.

    Returns:
        Escaped string safe for embedding in markdown emphasis.
    """
    return _MD_ESCAPE_RE.sub(r"\\\1", s or "")


def _apply_aka_line(body: str, aliases: list[str]) -> str:
    """Idempotently set or remove the '*Also known as: ...*' line under the article's H1.

    Deterministic — rebuilds the H1/AKA/blank layout each call, so repeated runs don't
    accumulate blanks. Robust to leading frontmatter (finds the first H1 by scanning, not
    line 0) and to code fences (never strips an AKA-looking line inside a ``` block). If
    there's no H1 at all, the body is left unchanged (we never synthesize a top-of-file AKA
    above frontmatter). The single source of truth for AKA display.

    Args:
        body: Article markdown to update.
        aliases: Alias display strings; empty list removes any existing AKA line.

    Returns:
        Updated article body.
    """
    body = body or ""
    lines = body.split("\n")
    h1 = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("# ")), None)
    if h1 is None:
        return body
    head, rest = lines[: h1 + 1], lines[h1 + 1:]
    # Drop our prior AKA line(s) from the body, but never one inside a fenced code block.
    cleaned, in_fence = [], False
    for ln in rest:
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _AKA_LINE_RE.match(s):
            continue
        cleaned.append(ln)
    while cleaned and not cleaned[0].strip():          # normalize blanks right under the H1
        cleaned.pop(0)
    aka = None
    if aliases:
        aka = "*Also known as: " + ", ".join(_md_escape(a) for a in aliases) + ".*"
    parts = head + [""] + ([aka, ""] if aka else []) + cleaned
    return "\n".join(parts)


def surface_aliases(conn) -> dict:
    """Ensure each person/animal article shows a current '*Also known as:*' line under its H1.

    Deterministic, no LLM — the AKA source of truth. Idempotent: updates the line to the
    current alias set and removes it when there are none. Aliases that equal the article's own
    leaf name are skipped.

    Args:
        conn: SQLite connection.

    Returns:
        Dict with key ``updated`` (count of articles whose body changed).
    """
    from . import notes as notes_svc, entity_index
    updated = 0
    for e in conn.execute(
        "SELECT id, canonical_name, article_title FROM entities "
        "WHERE type IN ('person','animal') AND article_title IS NOT NULL"
    ).fetchall():
        note = conn.execute(
            "SELECT id, content_md FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL "
            "AND redirect_to IS NULL", (e["article_title"],)).fetchone()
        if not note:
            continue
        leaf_norm = entity_index.normalize(e["article_title"].split("/")[-1])
        aliases = [a["alias_display"] for a in conn.execute(
            "SELECT alias_display, alias_norm FROM entity_aliases WHERE entity_id=? "
            "AND alias_display IS NOT NULL ORDER BY alias_display", (e["id"],)).fetchall()
            if entity_index.normalize(a["alias_display"]) != leaf_norm]
        new_body = _apply_aka_line(note["content_md"] or "", aliases)
        if new_body != (note["content_md"] or ""):
            notes_svc.upsert_note(conn, e["article_title"], new_body, kind="kb",
                                  source="aka", version_note="updated also-known-as")
            updated += 1
    conn.commit()
    return {"updated": updated}


_MAINTAIN_RE = re.compile(r"\n?```maintain\s*\n(.*?)```[ \t]*\n?", re.DOTALL)
_ARTICLE_RE = re.compile(r"```article\s*\n(.*?)\n```", re.DOTALL)
_H1_RE = re.compile(r"(?m)^#\s")


def _extract_maintain(text: str):
    """Extract the article body and resolution payload from a maintenance reply.

    The body is the content of a ```article fence when present, so any preamble or commentary
    OUTSIDE the two fences ("here's the article", "I reconciled X…") is discarded by
    construction — the model can't leak talk into the saved article. Falls back, for older
    un-fenced output, to the text minus the maintain block; as a backstop it drops a
    non-article preamble before the first top-level "# " heading, but ONLY when an H1 exists
    (never deletes an article that merely lacks one).

    Args:
        text: Raw maintenance reply from the LLM.

    Returns:
        Tuple of (article_body_str, payload_dict) where payload has ``resolved`` and ``new``.
    """
    text = text or ""
    data: dict = {}
    m = _MAINTAIN_RE.search(text)
    if m:
        try:
            d = json.loads(m.group(1))
            data = d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            data = {}
        text = text[:m.start()] + text[m.end():]
    a = _ARTICLE_RE.search(text)
    if a:
        return a.group(1).strip(), data
    h = _H1_RE.search(text)
    if h and h.start() > 0:
        log.info("wiki_maintain: dropped %d chars of preamble before the article heading", h.start())
        return text[h.start():].strip(), data
    return text.strip(), data


def maintain_one(conn, article_title: str, known_titles: list[str] | None = None,
                 extra_source_ids: list[int] | None = None, removed_titles: list[str] | None = None) -> dict:
    """Update one article to be faithful and current (component 3 of the KB pipeline).

    Addresses open talk items, integrates NEW/CHANGED sources (extra_source_ids), and purges
    claims that relied on REMOVED (deleted) sources (removed_titles). Pure — the caller
    applies/records the result.

    Args:
        conn: SQLite connection.
        article_title: Title of the kb article to maintain.
        known_titles: Full article-title set for dead-link validation.
        extra_source_ids: Note ids of new/changed sources to integrate.
        removed_titles: Titles of deleted source notes whose claims should be purged.

    Returns:
        Dict with keys ``title``, ``ok``, ``changed``, ``content_md``, ``resolved``,
        ``new``, ``errors``, and ``warnings``.
    """
    from . import article_talk
    base = {"title": article_title, "ok": False, "changed": False,
            "content_md": "", "resolved": [], "new": [], "errors": [], "warnings": []}
    note = conn.execute(
        "SELECT content_md FROM notes WHERE title=? AND kind='kb' AND deleted_at IS NULL",
        (article_title,)).fetchone()
    if not note:
        base["errors"] = ["no such article"]
        return base
    open_items = [it for it in article_talk.open_for(conn, article_title)
                  if it["kind"] in ("conflict", "question", "todo", "directive", "correction")]
    extra_source_ids = [int(i) for i in (extra_source_ids or [])]
    removed_titles = [t for t in (removed_titles or []) if t]
    if not (open_items or extra_source_ids or removed_titles):
        return {**base, "ok": True}                         # nothing to do
    if not llm.has_credentials():
        base["errors"] = ["no LLM credentials"]
        return base

    from . import people
    content = note["content_md"] or ""
    domain = wiki_guides.domain_for_title(article_title) or ""
    # The article's own evidence base: the source notes it already cites (live ones only).
    cited = [t for t in wikilinks.extract_links(content) if not t.lower().startswith("kb/")]
    ids = []
    for t in cited:
        r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (t,)).fetchone()
        if r:
            ids.append(r["id"])
    # Subject query for relevance-selecting long sources: the article's leaf name + its lead.
    subject = f"{article_title.rsplit('/', 1)[-1]} {content.splitlines()[0].lstrip('# ') if content.strip() else ''}".strip()
    srcs = _load_sources(conn, ids, query=subject)
    new_srcs = _load_sources(conn, extra_source_ids, query=subject)
    # Fold the owner's replies under their item — the maintenance conversation. A reply is
    # the latest, authoritative steer on that item (see the wiki_maintain prompt).
    reps = article_talk.replies_for(conn, [it["id"] for it in open_items])

    def _item_line(it: dict) -> str:
        """Format one open talk item (with any owner replies) as a prompt line."""
        head = (f"[{it['id']}] ("
                f"{'correction — SOURCE OF TRUTH, authoritative' if it.get('is_correction') else it['kind']}, "
                f"by {it['author']}) {it['body']}")
        for rp in reps.get(it["id"], []):
            head += f"\n    ↳ reply ({rp['author']}): {rp['body']}"
        return head

    items_text = "\n".join(_item_line(it) for it in open_items) or "(none)"
    new_block = _sources_text(new_srcs) or "(none)"
    removed_block = "\n".join(f"- [[{t}]]" for t in removed_titles) or "(none)"
    others = scoped_known_titles(conn, article_title, known_titles)
    known_block = "\n".join(others) if others else "(no other articles)"
    alias_block = known_aliases_block(conn, others)

    prompt = (prompts.get("actions.wiki_maintain", "")
              .replace("{owner}", people.owner_name(conn))
              .replace("{general_guide}", wiki_guides.guide_text(None))
              .replace("{domain_guide}", wiki_guides.guide_text(domain)).replace("{domain}", domain)
              .replace("{title}", article_title).replace("{article}", content)
              .replace("{items}", items_text).replace("{new_sources}", new_block)
              .replace("{removed_sources}", removed_block).replace("{known_titles}", known_block)
              .replace("{known_aliases}", alias_block)
              .replace("{sources}", _sources_text(srcs)))
    msgs = [{"role": "user", "content": prompt}]
    try:
        # Retry once at a bigger cap on truncation, then FAIL (changed=False → don't save)
        # rather than persist a maintain output cut off at the token limit. maintain is a
        # BATCH path with no human Accept gate, so it never auto-continues/stitches (a bad
        # stitch would auto-save uncaught) — auto-continue lives only in the live engine.
        cap = 3500
        text, stop = llm.complete_with_meta(msgs, max_tokens=cap)
        if stop in _TRUNCATED:
            cap = min(cap * 2, 6000)
            text, stop = llm.complete_with_meta(msgs, max_tokens=cap)
        if stop in _TRUNCATED:
            base["errors"] = ["maintain output truncated"]
            return base
        revised, payload = _extract_maintain(_strip_fence(text))
    except Exception as exc:  # noqa: BLE001
        base["errors"] = [f"maintain failed: {exc}"]
        return base

    revised = revised.strip()
    if not revised:
        base["errors"] = ["empty revision"]
        return base
    # Same dead-link guarantee as the writer: never save a dead link.
    allowed = {t for t in (known_titles or [])} | {article_title}
    bad = _bad_links(conn, revised, allowed)
    if bad:
        revised = _neutralize_links(revised, set(bad))
    v = wiki_guides.validate_structure(article_title, revised)
    # Bounded self-critique/revise loop against the lint (mirrors write_one's §10 gate):
    # up to two non-regressing passes, second only on strict improvement — so a structural
    # slip (e.g. a deletion-purge that dropped a required section) gets a couple of chances
    # to recover instead of silently no-op'ing, without oscillating.
    for _ in range(2):
        if not ((v["errors"] or v["warnings"]) and not v["stub"]):
            break
        issues = "\n".join(f"- {x}" for x in (v["errors"] + v["warnings"]))
        rprompt = (prompts.get("actions.wiki_revise", "")
                   .replace("{issues}", issues).replace("{general_guide}", wiki_guides.guide_text(None))
                   .replace("{domain_guide}", wiki_guides.guide_text(domain)).replace("{domain}", domain)
                   .replace("{known_titles}", known_block).replace("{draft}", revised))
        try:
            r2, _ = _extract_maintain(_strip_fence(
                llm.complete([{"role": "user", "content": rprompt}], max_tokens=2600)))
        except Exception as exc:  # noqa: BLE001
            log.info("wiki_maintain revise failed for %s: %s", article_title, exc)
            break
        r2 = r2.strip()
        b2 = _bad_links(conn, r2, allowed)
        if b2:
            r2 = _neutralize_links(r2, set(b2))
        v2 = wiki_guides.validate_structure(article_title, r2)
        prev, cur = len(v["errors"]), len(v2["errors"])
        if r2 and cur <= prev:
            revised, v = r2, v2
        if not r2 or cur >= prev:
            break
    by_id = {it["id"]: it for it in open_items}
    resolved = [{"id": int(r["id"]), "kind": by_id[int(r["id"])]["kind"],
                 "outcome": str(r.get("outcome") or "").strip().lower(),
                 "how": str(r.get("how") or "")}
                for r in (payload.get("resolved") or [])
                if isinstance(r, dict) and _isint(r.get("id")) and int(r["id"]) in by_id]
    new = [{"kind": str(n.get("kind") or "note"), "body": str(n.get("body") or "").strip()}
           for n in (payload.get("new") or []) if isinstance(n, dict) and str(n.get("body") or "").strip()]
    return {"title": article_title, "ok": v["ok"], "changed": revised != content.strip(),
            "content_md": revised, "resolved": resolved, "new": new,
            "errors": v["errors"], "warnings": v["warnings"]}


def _cited_source_len(conn, content: str) -> int:
    """Return the total character length of source notes cited by a kb article.

    Args:
        conn: SQLite connection.
        content: Article markdown (wikilinks to source notes are extracted from here).

    Returns:
        Sum of content_md lengths across all live cited non-kb notes.
    """
    total = 0
    for t in wikilinks.extract_links(content):
        if t.lower().startswith("kb/"):
            continue
        r = conn.execute("SELECT content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                         (t,)).fetchone()
        if r:
            total += len(r["content_md"] or "")
    return total


def flag_ungrounded_reference(conn, ratio: float = 3.0, min_body: int = 500) -> dict:
    """Flag Reference articles whose body far outweighs their cited sources.

    A body-to-sources ratio exceeding `ratio` is the signature of the model padding with
    general 'common knowledge' from training instead of the owner's notes. Records a note (the
    worklist for an approved external fill) and returns counts. Forward-looking: the GROUNDING
    rule keeps new articles honest; this audits what's already there.

    Args:
        conn: SQLite connection.
        ratio: Body-chars / cited-source-chars threshold above which an article is flagged.
        min_body: Minimum body length to consider; stubs below this are fine.

    Returns:
        Dict with keys ``scanned`` and ``flagged``.
    """
    from . import article_talk
    # Reclassify any flags left as 'todo' by an earlier build so they stop driving maintenance.
    conn.execute("UPDATE article_talk SET kind='note' WHERE kind='todo' AND resolved_at IS NULL "
                 "AND body LIKE 'External reference needed%'")
    rows = conn.execute(
        "SELECT title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND title LIKE 'kb/Reference/%'").fetchall()
    scanned = flagged = 0
    for r in rows:
        if wiki_guides.is_protected(r["title"]):
            continue
        scanned += 1
        body = r["content_md"] or ""
        core = re.split(r"\n##\s+References", body, maxsplit=1)[0]
        blen = len(re.sub(r"\s+", " ", core).strip())
        if blen < min_body:
            continue                                        # a stub is fine — that's the goal
        slen = _cited_source_len(conn, body)
        if slen == 0 or blen / slen > ratio:
            article_talk.record(conn, r["title"], [{"kind": "note",
                "body": "External reference needed — the general content here isn't grounded in "
                        "your notes; awaiting an approved external reference."}], author="ai")
            flagged += 1
    conn.commit()
    return {"scanned": scanned, "flagged": flagged}


def _known_titles(conn) -> list[str]:
    """Return a sorted list of all live non-protected kb article titles."""
    return sorted({r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL "
        r"AND title NOT LIKE 'kb/\_%' ESCAPE '\'").fetchall()})


def known_aliases_block(conn, title_set) -> str:
    """Build the {known_aliases} writer-prompt block for the given title set.

    For each registered alias whose canonical article is in ``title_set``, emits a line
    '"alias" → article'. Advisory — it tells the model that prose using a nickname should link
    to the canonical article keeping the nickname as display ([[article|alias]]); the
    deterministic add_links_to_content pass still guarantees the link. Reuses the SAME
    entity_index.alias_surface the linker and hygiene allow-list use, so the offered set never
    drifts from what's linkable.

    Args:
        conn: SQLite connection.
        title_set: Iterable of article titles in scope for this writer call.

    Returns:
        Formatted alias block string, or "(none)" when empty.
    """
    from . import entity_index
    title_set = {t for t in (title_set or [])}
    if not title_set:
        return "(none)"
    try:
        surface = entity_index.alias_surface(conn)
    except Exception:  # noqa: BLE001 — never let alias surfacing break prompt assembly
        return "(none)"
    by_art: dict[str, set] = {}
    for _an, (art, disp) in surface.items():
        if art in title_set and disp:
            by_art.setdefault(art, set()).add(disp)
    lines = [f'- "{disp}" → {art}' for art in sorted(by_art) for disp in sorted(by_art[art])]
    return "\n".join(lines) if lines else "(none)"


def scoped_known_titles(conn, title: str, all_titles, budget: int = 600,
                        source_ids: list[int] | None = None) -> list[str]:
    """Return a relevance-scoped cross-link candidate list for a writer prompt.

    Replaces the old blind alphabetical ``[:600]`` cap. When the whole KB fits in ``budget``
    returns everything. Past it, prioritises a relevant neighbourhood — articles that link to
    this one (backlinks), this article's own current link targets, same-folder siblings, and
    the kb pages of entities MENTIONED IN THE CHOSEN SOURCES (so the link vocabulary follows
    the sources) — then tops up to ``budget`` so we never offer FEWER candidates than the old
    cap but the ones kept when truncating are the relevant ones. Deterministic, no LLM.

    Args:
        conn: SQLite connection.
        title: Title of the article being written.
        all_titles: Full set of planned and existing article titles.
        budget: Maximum number of titles to return.
        source_ids: Source note ids used to find entity-linked articles.

    Returns:
        Sorted list of up to ``budget`` cross-link candidate titles.
    """
    others = [t for t in (all_titles or []) if t and t != title]
    if len(others) <= budget:
        return others
    others_set = set(others)
    keep: set[str] = set()
    # Entities mentioned in the chosen source notes → their articles. Skipped for a Reference
    # or private TARGET (those must not reference People/Groups — the PII firewall), matching
    # add_links_to_content's guard so the writer is never even offered a forbidden link.
    if (source_ids and not wiki_guides.is_private_title(title)
            and wiki_guides.domain_for_title(title) != "Reference"):
        ids = [int(i) for i in source_ids if i]
        if ids:
            q = ",".join("?" * len(ids))
            for r in conn.execute(
                f"SELECT DISTINCT e.article_title FROM entity_mentions m "
                f"JOIN entities e ON e.id = m.entity_id "
                f"WHERE m.note_id IN ({q}) AND e.article_title IS NOT NULL", ids):
                if r["article_title"]:
                    keep.add(r["article_title"])
    # Backlinks: kb articles that link TO this title (target_title survives soft-delete).
    for r in conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL", (title,)):
        keep.add(r["title"])
    # This article's own outward kb link targets (co-relevant neighbours it already cites).
    row = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                       (title,)).fetchone()
    if row:
        for r in conn.execute(
            "SELECT DISTINCT t.title FROM links l JOIN notes t ON t.id=l.target_note_id "
            "WHERE l.source_note_id=? AND t.kind='kb' AND t.deleted_at IS NULL", (row["id"],)):
            keep.add(r["title"])
    # Same-folder siblings (shared kb/<Domain>/<Sub>/ parent prefix).
    if "/" in title:
        parent = title.rsplit("/", 1)[0]
        for r in conn.execute(
            "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title LIKE ?",
            (parent + "/%",)):
            keep.add(r["title"])
    keep &= others_set
    keep.discard(title)
    result = sorted(keep)[:budget]
    if len(result) < budget:                       # top up with the rest so count >= old cap
        seen = set(result)
        for t in others:
            if t not in seen:
                result.append(t)
                if len(result) >= budget:
                    break
    return result


# ---- KB write lock (advisory, atomic claim) ------------------------------------------
# Re-entrancy depth per thread/key: a nightly batch can hold the lock and still call
# create_article (which also acquires it) without self-deadlocking, while a SEPARATE
# thread (e.g. a manual run) still sees the DB row and is correctly excluded.
_kb_lock_depth = threading.local()


def kb_lock_acquire(conn, key: str = "kb_write", ttl_s: int = 1800) -> bool:
    """Claim the KB write lock so concurrent ops can't interleave mutations.

    Atomic: INSERT OR IGNORE on a unique key — exactly one caller wins. A holder older than
    ttl_s is reclaimed so a crash can't wedge the KB. Re-entrant within a thread: a nested
    acquire by the same holder succeeds without a new DB claim (balanced by a nested release).

    Args:
        conn: SQLite connection.
        key: Lock name (default "kb_write").
        ttl_s: Seconds after which an unclaimed lock is reclaimed.

    Returns:
        True if the lock was acquired (or re-entered), False if held by another connection.
    """
    counts = getattr(_kb_lock_depth, "counts", None)
    if counts is None:
        counts = _kb_lock_depth.counts = {}
    if counts.get(key, 0) > 0:
        counts[key] += 1
        return True
    conn.execute("CREATE TABLE IF NOT EXISTS kb_locks (key TEXT PRIMARY KEY, held_at INTEGER NOT NULL)")
    conn.execute("DELETE FROM kb_locks WHERE key=? AND held_at < CAST(strftime('%s','now') AS INTEGER) - ?",
                 (key, int(ttl_s)))
    cur = conn.execute(
        "INSERT OR IGNORE INTO kb_locks(key, held_at) VALUES (?, CAST(strftime('%s','now') AS INTEGER))", (key,))
    conn.commit()
    if cur.rowcount == 1:
        counts[key] = 1
        return True
    return False


def kb_lock_release(conn, key: str = "kb_write") -> None:
    """Release the KB write lock (or decrement the re-entrancy counter).

    Args:
        conn: SQLite connection.
        key: Lock name (must match the corresponding acquire call).
    """
    counts = getattr(_kb_lock_depth, "counts", None) or {}
    if counts.get(key, 0) > 1:
        counts[key] -= 1       # inner (re-entrant) release: keep the DB claim
        return
    counts.pop(key, None)
    conn.execute("DELETE FROM kb_locks WHERE key=?", (key,))
    conn.commit()


def rebuild_sources(conn, title: str, instructions: str | None = None):
    """Resolve the primary sources, merged instructions, and scope for rebuilding an article.

    Sources = the article's prior citations (non-kb) ∪ the entity index for its subject
    (search is never a seed); open directives/conflicts are carried into the writer. Shared by
    the nightly rebuild_article and the live streaming rebuild so both pick identical inputs.

    Args:
        conn: SQLite connection.
        title: Title of the kb article to rebuild.
        instructions: Optional caller-supplied guidance merged with any open directives.

    Returns:
        Tuple of (art_dict, merged_instructions, prior_content) where art_dict has
        ``title``, ``domain``, ``scope``, and ``sources``.
    """
    from . import entity_index, article_talk, notes as notes_svc
    title = (title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    prior = (note["content_md"] if note else "") or ""
    ids: set[int] = set()
    for t in wikilinks.extract_links(prior):
        if t.lower().startswith("kb/"):
            continue
        r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                         (t,)).fetchone()
        if r:
            ids.add(int(r["id"]))
    ids |= {int(i) for i in entity_index.note_ids_for_name(conn, title.rsplit("/", 1)[-1])}
    opens = article_talk.open_for(conn, title)
    directives = "; ".join(o["body"] for o in opens
                           if o.get("kind") in ("directive", "conflict") and o.get("body"))
    instr = (instructions or "").strip()
    if directives:
        instr = (instr + "\nHonor these standing directives / unresolved conflicts: " + directives).strip()
    scope = prior.splitlines()[0].lstrip("# ").strip() if prior.strip() else ""
    art = {"title": title, "domain": wiki_guides.domain_for_title(title),
           "scope": scope, "sources": sorted(ids)}
    return art, instr, prior


def promote(conn) -> dict:
    """Run the network-free promotion suite after a single-article rebuild/Accept.

    Brings the live Accept path and the nightly per-article rebuild (which share
    finalize_rebuild) to formatting/promotion parity with the full build's per-article
    steps — without adding a network dependency to the interactive Accept. Runs the four
    deterministic, idempotent steps: bind the owner to their People page (link_owner),
    refresh each person's "Also known as" line (surface_aliases), normalize KB-wide link
    labels (wikilinks.normalize_all_link_labels), and flag ungrounded Reference articles
    (flag_ungrounded_reference). Each is a no-op when nothing changed and talk notes
    dedupe, so running it on every Accept can't duplicate work or churn versions.

    The two NETWORK-bound build steps are deliberately excluded here — link_medications
    (RxNorm/MedlinePlus) and link_places (reverse-geocode) stay on the full build /
    nightly maintenance path so an Accept never blocks on an external service.

    Args:
        conn: SQLite connection (the caller commits; the steps also commit internally).

    Returns:
        Dict of per-step result summaries (keys: owner, aliases, labels, grounding).
    """
    return {
        "owner": link_owner(conn),
        "aliases": surface_aliases(conn),
        "labels": wikilinks.normalize_all_link_labels(conn),
        "grounding": flag_ungrounded_reference(conn),
    }


def finalize_rebuild(conn, title: str, content_md: str, talk=None, *,
                     prior_note_id: int | None = None, rename_to: str | None = None) -> None:
    """Persist a rebuilt article and re-link it fully into the KB.

    Upserts revive-in-place (keeping slug + version history), reconnects inbound links, records
    talk, rebuilds the entity index + disambiguation pages, sweeps dead links, and runs the
    network-free promotion suite (owner-link, AKA lines, link-label hygiene, ungrounded-Reference
    flagging — see promote). The caller must hold the KB write lock and commit afterwards. Shared
    by the nightly rebuild_article and the live Accept path so they can't drift.

    ``rename_to`` (live Accept only, with the user's explicit approval) retitles the article in
    place: an id-targeted write changes the slug and rewrites inbound [[links]], exactly as the
    notes rename (PUT) path does.

    Args:
        conn: SQLite connection.
        title: Current title of the article.
        content_md: New article body.
        talk: Optional list of talk entry dicts to record.
        prior_note_id: Row id of the soft-deleted prior version (for revive-in-place).
        rename_to: New title when the user approved a rename (live Accept only).
    """
    from . import entity_index, article_talk, notes as notes_svc
    new_title = (rename_to or "").strip() or title
    if new_title.lower() != title.lower() and prior_note_id is not None:
        notes_svc.upsert_note(conn, new_title, content_md, note_id=prior_note_id, kind="kb",
                              source="rebuild", version_note="rebuilt from sources (renamed)")
    else:
        notes_svc.upsert_note(conn, new_title, content_md, kind="kb",
                              source="rebuild", version_note="rebuilt from sources")
    # soft_delete (nightly path) nulled inbound links' target_note_id; re-point them at the
    # revived/renamed row so other articles' [[links]] to this one reconnect (as restore() does).
    row = notes_svc.get_by_title(conn, new_title)
    nid = row["id"] if row else prior_note_id
    if nid is not None:
        wikilinks.resolve_dangling_links(conn, nid, new_title)
    if talk:
        article_talk.record(conn, new_title, talk)
    entity_index.rebuild(conn)                 # relink entity → fresh article
    entity_index.write_disambiguation_pages(conn)
    flag_dead_links(conn)                      # sweep any dangling cross-links
    promote(conn)                              # formatting/promotion parity (network-free steps)


def rebuild_article(conn, title: str, instructions: str | None = None) -> dict:
    """Regenerate one existing kb article from scratch from its primary sources.

    REGENERATE-IN-PLACE, never a wipe: revives the SAME row, so slug + version history +
    inbound links + the AI-talk ledger all survive. Sources = the article's prior citations ∪
    the entity index for its subject (search is never a seed). Open directives/conflicts are
    carried into the writer. On a quarantine (lint fail) the prior version is restored and an
    open todo is recorded — a failed rebuild never leaves a hole. Runs under the KB write lock.

    Args:
        conn: SQLite connection.
        title: Title of the kb article to rebuild.
        instructions: Optional guidance forwarded to the writer.

    Returns:
        Dict with keys ``ok``, ``title``, and optionally ``reason`` and ``quarantined``.
    """
    from . import notes as notes_svc, article_talk
    title = (title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "title": title, "reason": "no such kb article"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "title": title,
                "reason": "KB is busy (another build/maintain is running) — try again shortly"}
    try:
        nid = note["id"]
        art, instr, _prior = rebuild_sources(conn, title, instructions)
        # Soft-delete this one article, regenerate, revive the SAME row on success.
        notes_svc.soft_delete(conn, nid)
        out = write_one(conn, art, instr, known_titles=_known_titles(conn))
        if out.get("ok") and out.get("content_md"):
            finalize_rebuild(conn, title, out["content_md"], out.get("talk"), prior_note_id=nid)
            conn.commit()
            return {"ok": True, "title": title}
        # Quarantine: restore the prior version (never leave a hole) + surface an open todo.
        notes_svc.restore(conn, nid)
        reason = "; ".join(out.get("errors") or []) or "structure lint failed"
        article_talk.add(conn, title, "todo", f"Rebuild quarantined ({reason}) — manual review.")
        conn.commit()
        return {"ok": False, "title": title, "quarantined": True, "reason": reason}
    finally:
        kb_lock_release(conn)


def maintain_now(conn, title: str) -> dict:
    """Perform immediate surgical maintenance of one article on owner demand.

    The on-demand twin of the maintain_batch loop body, so an owner reply / new directive
    doesn't have to wait for the nightly pass. Folds in the article's open items + their owner
    replies, integrates any promoted source-of-truth corrections, and runs under the KB write
    lock so it can't interleave with the batch. NOT a full rebuild (rebuild_article) — it edits
    in place from the open items, exactly like the scheduled pass.

    Args:
        conn: SQLite connection.
        title: Title of the kb article to maintain.

    Returns:
        Dict with keys ``ok``, ``title``, ``changed``, ``resolved``, ``examined``,
        ``kept_open``, and optionally ``reason``.
    """
    from . import notes as notes_svc
    title = (title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "title": title, "reason": "no such kb article"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "title": title,
                "reason": "KB is busy (another build/maintain is running) — try again shortly"}
    try:
        # Feed promoted source-of-truth correction notes in as NEW sources (same as the batch).
        corr_ids = [r["source_note_id"] for r in conn.execute(
            "SELECT source_note_id FROM article_talk WHERE article_title=? AND kind='correction' "
            "AND resolved_at IS NULL AND source_note_id IS NOT NULL", (title,)).fetchall()]
        out = maintain_one(conn, title, _known_titles(conn), extra_source_ids=corr_ids or None)
        if not out["ok"]:
            return {"ok": False, "title": title,
                    "reason": "; ".join(out.get("errors") or []) or "maintenance failed"}
        did_change, n_closed = _apply_maintain(conn, out, "maintenance (on-demand)")
        conn.commit()
        return {"ok": True, "title": title, "changed": did_change, "resolved": n_closed,
                "examined": len(out["resolved"]), "kept_open": len(out["resolved"]) - n_closed}
    finally:
        kb_lock_release(conn)


# Common short/general leaves we never auto-link (too many false positives as bare words).
_STOP_LEAVES = {
    "home", "work", "car", "list", "notes", "note", "care", "team", "group", "family",
    "house", "office", "school", "money", "health", "food", "travel", "today", "people",
}


def _mask_spans(text: str) -> list[tuple[int, int]]:
    """Return character ranges where a wikilink must NOT be inserted.

    Masks fenced code, inline code, existing [[wikilinks]], and footnote-definition lines
    (`[^sN]: …` — citations).

    Args:
        text: Article text to scan.

    Returns:
        List of (start, end) character-offset pairs to exclude from linking.
    """
    spans: list[tuple[int, int]] = []
    for pat, flags in ((r"```.*?```", re.DOTALL), (r"`[^`]+`", 0),
                       (r"\[\[.*?\]\]", 0), (r"(?m)^\[\^[^\]]+\]:.*$", 0)):
        spans += [m.span() for m in re.finditer(pat, text, flags)]
    return spans


def check_needed_links(conn, title: str | None = None, mode: str = "propose") -> dict:
    """Find bare mentions that should be wikilinks and propose or apply them.

    Deterministic ADD-link backstop (the complement to flag_dead_links's remove): finds
    mentions in an article that EXACTLY match an existing kb article's leaf name but aren't
    linked, and links them. Refuses ambiguous leaves (map to ≥2 articles, or in the entity
    index's ambiguous_terms) and short/common-word leaves; masks code, existing links, and
    footnote-citation lines. No See-also, no reciprocal edits.

    Args:
        conn: SQLite connection.
        title: Specific article to check; None checks all live articles.
        mode: "propose" (default) returns proposals for a Review card; "auto" writes them
            (versioned).

    Returns:
        Dict with keys ``ok``, ``articles`` (per-article proposal lists), and ``count``.
    """
    from . import notes as notes_svc, entity_index
    titles = _known_titles(conn)
    leafmap: dict[str, list[str]] = {}
    for t in titles:
        # Exclude private-domain targets (kb/Health/…, kb/Finance/…): they can share a person's
        # leaf, so including them would make the name "ambiguous" and suppress the legitimate
        # public link — and a shareable article must never auto-link a private page (PII firewall).
        if wiki_guides.is_private_title(t):
            continue
        leafmap.setdefault(t.split("/")[-1].strip().lower(), []).append(t)
    ambiguous = {k for k, v in leafmap.items() if len(v) > 1}
    try:
        for amb in entity_index.ambiguous_terms(conn):
            ambiguous.add(str(amb.get("term", "")).lower())
    except Exception:  # noqa: BLE001
        pass

    def linkable(leaf_lower: str, leaf: str) -> bool:
        """Return True if leaf is safe to auto-link (unambiguous, long enough, not a stop word)."""
        if leaf_lower in ambiguous or len(leaf) < 4:
            return False
        if " " not in leaf and leaf_lower in _STOP_LEAVES:
            return False
        return True

    try:
        surface_map = entity_index.alias_surface(conn)
    except Exception:  # noqa: BLE001
        surface_map = {}

    targets = [title] if title else titles
    out_articles = []
    for tt in targets:
        note = notes_svc.get_by_title(conn, tt)
        if not note or note["kind"] != "kb":
            continue
        body = note["content_md"] or ""
        # The article being edited must not name/link People from a Reference/private page.
        public_target = not (wiki_guides.is_private_title(tt) or wiki_guides.domain_for_title(tt) == "Reference")
        spans = _mask_spans(body)
        linked = {x.lower() for x in wikilinks.extract_links(body)}
        props = []
        if public_target:
            for leaf_lower, cands in leafmap.items():
                tgt = cands[0]
                if tgt == tt or tgt.lower() in linked:
                    continue
                leaf = tgt.split("/")[-1]
                if not linkable(leaf_lower, leaf):
                    continue
                for m in re.finditer(r"(?<!\w)" + re.escape(leaf) + r"(?!\w)", body, re.IGNORECASE):
                    if not any(s <= m.start() < e for s, e in spans):
                        props.append({"target": tgt, "surface": m.group(0), "at": m.start()})
                        break
        if mode == "auto" and props:                          # apply leaf links, then re-find aliases
            body = _apply_link_props(body, props)
        alias_props = _alias_link_props(conn, body, tt, surface_map) if public_target else []
        if mode == "auto" and (props or alias_props):
            nb = _apply_link_props(body, alias_props) if alias_props else body
            notes_svc.upsert_note(conn, tt, nb, kind="kb", source="user",
                                  version_note="added missing cross-links")
        out_articles.append({"title": tt, "proposals": [
            {"target": p["target"], "surface": p["surface"]} for p in (props + alias_props)]})
    if mode == "auto":
        conn.commit()
    return {"ok": True, "articles": out_articles,
            "count": sum(len(a["proposals"]) for a in out_articles)}


_TYPE_DOMAIN = {"person": "People", "animal": "People", "org": "Groups", "place": "Places",
                "thing": "Things", "work": "Things",
                "condition": "Reference", "medication": "Reference", "procedure": "Reference",
                "concept": "Reference", "event": "Reference"}
_REF_SUB = {"condition": "Medicine/Conditions", "medication": "Medicine/Medications",
            "procedure": "Medicine/Procedures", "event": "Events"}


def create_article(conn, subject: str, etype: str | None = None, min_notes: int = 2) -> dict:
    """Create a new kb article for a subject from its source notes.

    DEDUP BEFORE SPAWN: if the subject's canonical entity already has an article, or an
    existing kb leaf normalises-equal, routes there (fold) instead of minting a near-duplicate.
    Spawns only a subject with ≥ min_notes notes (else fold, don't stub). Picks
    kb/<Domain>/<Name> (Reference is foldered), writes from sources, relinks. Runs under the
    KB write lock.

    Args:
        conn: SQLite connection.
        subject: Canonical name of the subject to create an article for.
        etype: Entity type hint (e.g. "person", "org"); used for domain selection.
        min_notes: Minimum note count to spawn a new article (default 2).

    Returns:
        Dict with keys ``ok``, ``title``, and one of ``created``, ``folded``, or ``reason``.
    """
    from . import notes as notes_svc, entity_index, article_talk
    subject = (subject or "").strip()
    norm = entity_index.normalize(subject)
    if not norm:
        return {"ok": False, "reason": "empty subject"}
    ent = conn.execute("SELECT id, type, canonical_name, article_title FROM entities "
                       "WHERE normalized_key=? ORDER BY note_count DESC LIMIT 1", (norm,)).fetchone()
    if ent and ent["article_title"]:
        return {"ok": True, "folded": True, "title": ent["article_title"], "reason": "article already exists"}
    for r in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL"
    ):
        # Never fold a new subject into a private-domain page (kb/Health/…, kb/Finance/…) — it can
        # share a person's leaf but is a PII satellite, not a canonical article (would collapse the
        # public page and its private vault together).
        if wiki_guides.is_private_title(r["title"]):
            continue
        if entity_index.normalize(r["title"].split("/")[-1]) == norm:
            return {"ok": True, "folded": True, "title": r["title"], "reason": "near-duplicate title exists"}
    ids = entity_index.note_ids_for_name(conn, subject)
    if len(ids) < int(min_notes):
        return {"ok": False, "reason": f"only {len(ids)} note(s) — fold into a related article, don't spawn a stub"}
    name = (ent["canonical_name"] if ent else subject).strip().replace("/", " ").strip()
    typ = (etype or (ent["type"] if ent else "") or "").lower()
    domain = _TYPE_DOMAIN.get(typ, "Reference")
    title = (f"kb/Reference/{_REF_SUB.get(typ, 'Concepts')}/{name}" if domain == "Reference"
             else f"kb/{domain}/{name}")
    if conn.execute("SELECT 1 FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (title,)).fetchone():
        return {"ok": True, "folded": True, "title": title, "reason": "title already exists"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        art = {"title": title, "domain": domain, "scope": "", "sources": sorted(ids)}
        out = write_one(conn, art, None, known_titles=_known_titles(conn))
        if out.get("ok") and out.get("content_md"):
            notes_svc.upsert_note(conn, title, out["content_md"], kind="kb",
                                  source="create", version_note="created from sources")
            if out.get("talk"):
                article_talk.record(conn, title, out["talk"])
            entity_index.rebuild(conn)            # point the entity at its new article
            flag_dead_links(conn)
            conn.commit()
            return {"ok": True, "created": True, "title": title}
        conn.commit()
        return {"ok": False, "title": title, "reason": "; ".join(out.get("errors") or []) or "structure lint failed"}
    finally:
        kb_lock_release(conn)


def refresh_index(conn) -> int:
    """Rebuild kb/_index from the live (non-protected) articles.

    No LLM. Run after any structural op (create/merge/rename) so the org map can't go stale
    or dangle.

    Args:
        conn: SQLite connection.

    Returns:
        Number of articles included in the refreshed index.
    """
    from . import notes as notes_svc
    arts = [{"title": r["title"], "domain": wiki_guides.domain_for_title(r["title"]) or "", "scope": ""}
            for r in conn.execute(
                r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL "
                r"AND title NOT LIKE 'kb/\_%' ESCAPE '\' ORDER BY title")]
    notes_svc.upsert_note(conn, "kb/_index", build_index_md(arts), kind="kb",
                          source="index", version_note="index refreshed")
    return len(arts)


def _structure_log(conn, op: str, pair_key: str) -> None:
    """Record a structural KB operation (merge/split) for hysteresis tracking.

    Args:
        conn: SQLite connection.
        op: Operation name, e.g. "merge" or "split".
        pair_key: Canonical key identifying the article pair (pipe-joined sorted titles).
    """
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    conn.execute("INSERT INTO kb_structure_log(op, pair_key, at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER))",
                 (op, pair_key))


def _structure_recent(conn, op: str, pair_key: str, k_days: int = 3) -> bool:
    """Return True if a structural operation on pair_key occurred within k_days.

    Used for merge/split hysteresis — blocks the inverse operation if it was done too recently.

    Args:
        conn: SQLite connection.
        op: Operation name to check.
        pair_key: Canonical key identifying the article pair.
        k_days: Look-back window in days.

    Returns:
        True if such an operation was logged within the window.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    return conn.execute(
        "SELECT 1 FROM kb_structure_log WHERE op=? AND pair_key=? "
        "AND at > CAST(strftime('%s','now') AS INTEGER) - ? LIMIT 1",
        (op, pair_key, int(k_days) * 86400)).fetchone() is not None


def recategorize_article(conn, title: str, new_title: str) -> dict:
    """Move or rename a kb article (e.g. fold it into a subcategory).

    Reuses upsert_note's rename path, which rewrites inbound [[old]]→[[new]] links itself
    (never relies on the dead-link sweep, which would unwrap them). Refreshes the index.
    Runs under the KB lock.

    Args:
        conn: SQLite connection.
        title: Current article title.
        new_title: Target title (must be a normal kb/… path, not kb/_…).

    Returns:
        Dict with keys ``ok``, ``from``, ``to``, or ``reason`` on failure.
    """
    from . import notes as notes_svc, entity_index
    title, new_title = (title or "").strip(), (new_title or "").strip()
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "reason": "no such kb article"}
    if not new_title.startswith("kb/") or new_title.lower().startswith("kb/_"):
        return {"ok": False, "reason": "new title must be a normal kb/… path"}
    if notes_svc.get_by_title(conn, new_title):
        return {"ok": False, "reason": "target title already exists"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        notes_svc.upsert_note(conn, new_title, note["content_md"], note_id=note["id"], kind="kb",
                              source="recategorize", version_note=f"recategorized from {title}")
        # article_talk and entity_overrides are keyed by title (not a FK), so carry them —
        # including source-of-truth corrections — to the new title or they'd be orphaned.
        conn.execute("UPDATE article_talk SET article_title=? WHERE article_title=?", (new_title, title))
        conn.execute("UPDATE OR IGNORE entity_overrides SET article_title=? WHERE article_title=?",
                     (new_title, title))
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "from": title, "to": new_title}
    finally:
        kb_lock_release(conn)


def _resolve_redirect_chain(conn, title: str, _seen: set[str] | None = None) -> str:
    """Follow redirect_to chains from title to the final non-redirect target.

    Cycle-safe: stops and returns the last title if it loops back. Case-insensitive lookup.

    Args:
        conn: SQLite connection.
        title: Starting title to resolve.
        _seen: Internal cycle-detection set (do not pass externally).

    Returns:
        The final non-redirect title.
    """
    seen = _seen if _seen is not None else set()
    cur = title
    while True:
        key = cur.lower()
        if key in seen:
            return cur
        seen.add(key)
        row = conn.execute(
            "SELECT redirect_to FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
            (cur,)).fetchone()
        if not row or not row["redirect_to"]:
            return cur
        cur = row["redirect_to"]


def create_redirect(conn, from_title: str, to_title: str) -> dict:
    """Make from_title a redirect to to_title, keeping old links and URLs resolving.

    The target is resolved through any existing redirect chain to a FINAL target, so we never
    point a redirect at a redirect. Refuses a self-redirect or a cycle. Runs under the KB lock
    (re-entrant).

    If a row titled from_title already exists, it's CONVERTED IN PLACE: kept live
    (deleted_at NULL) with the same slug, redirect_to set, kind='kb', body replaced with a
    one-line marker — because notes.title is UNIQUE and a soft-deleted source would keep the
    title slot, blocking a fresh redirect row. Otherwise a new kb redirect row is inserted.
    Inbound dangling [[from]] links are re-pointed at the (possibly revived) row. Does NOT
    refresh the index — callers do.

    Args:
        conn: SQLite connection.
        from_title: Title to redirect away from.
        to_title: Target title (resolved through any existing redirect chain).

    Returns:
        Dict with keys ``ok``, ``from``, ``to``, or ``reason`` on failure.
    """
    from . import embeddings
    from_title = (from_title or "").strip()
    to_title = (to_title or "").strip()
    if not from_title or not to_title:
        return {"ok": False, "reason": "need both from and to titles"}
    target = _resolve_redirect_chain(conn, to_title)
    if target.lower() == from_title.lower():
        return {"ok": False, "reason": "refused: self-redirect / cycle"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        marker = f"Redirects to [[{target}]]."
        existing = conn.execute(
            "SELECT id FROM notes WHERE lower(title)=lower(?)", (from_title,)).fetchone()
        if existing:
            nid = existing["id"]
            conn.execute(
                "UPDATE notes SET content_md=?, kind='kb', redirect_to=?, deleted_at=NULL, "
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                (marker, target, nid))
            # The redirect row must not surface as a keyword hit on the old title, and its own
            # marker shouldn't manufacture spurious links: drop FTS + outgoing links.
            conn.execute("DELETE FROM notes_fts WHERE note_id=?", (nid,))
            conn.execute("DELETE FROM links WHERE source_note_id=?", (nid,))
            embeddings.delete_note_embedding(conn, nid)
        else:
            slug = _unique_redirect_slug(conn, from_title)
            cur = conn.execute(
                "INSERT INTO notes (title, slug, content_md, kind, redirect_to, updated_at) "
                "VALUES (?, ?, ?, 'kb', ?, strftime('%Y-%m-%d %H:%M:%f','now'))",
                (from_title, slug, marker, target))
            nid = cur.lastrowid
        # Old [[from_title]] links (now dangling) re-point at this row so they keep resolving.
        wikilinks.resolve_dangling_links(conn, nid, from_title)
        return {"ok": True, "from": from_title, "to": target}
    finally:
        kb_lock_release(conn)


def _unique_redirect_slug(conn, title: str) -> str:
    """Generate a unique slug for a redirect row, appending a counter if needed.

    Args:
        conn: SQLite connection.
        title: Redirect source title to slugify.

    Returns:
        A slug string not already used in the notes table.
    """
    from . import notes as notes_svc
    base = notes_svc.slugify(title)
    slug, i = base, 2
    while conn.execute("SELECT 1 FROM notes WHERE slug=?", (slug,)).fetchone():
        slug = f"{base}-{i}"
        i += 1
    return slug


def merge_articles(conn, sources: list[str], into: str) -> dict:
    """Fold one or more kb articles into another, rewriting from the union of their sources.

    Soft-deletes the source articles and rewrites every inbound [[source]]→[[into]] link via
    _rename_inbound_links (NOT the dead-link sweep, which would unwrap them). Inverse-pair
    hysteresis blocks merging titles a recent split produced. Runs under the KB lock.

    Args:
        conn: SQLite connection.
        sources: List of kb article titles to fold in.
        into: Title of the target article that absorbs the sources.

    Returns:
        Dict with keys ``ok``, ``into``, ``merged``, or ``reason`` on failure.
    """
    from . import notes as notes_svc, entity_index, article_talk
    into = (into or "").strip()
    sources = [s.strip() for s in (sources or []) if s and s.strip() and s.strip() != into]
    if not into or not sources:
        return {"ok": False, "reason": "need source(s) + a distinct `into`"}
    if _structure_recent(conn, "split", "|".join(sorted(sources))):
        article_talk.add(conn, into, "todo",
                         f"Merge of {sources} blocked — they were split apart recently; review by hand.")
        return {"ok": False, "reason": "blocked by hysteresis (recently split)"}
    by = {t: notes_svc.get_by_title(conn, t) for t in [into] + sources}
    if not by[into] or by[into]["kind"] != "kb":
        return {"ok": False, "reason": "`into` is not a kb article"}
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        ids: set[int] = set()
        for t, n in by.items():
            if not n:
                continue
            for x in wikilinks.extract_links(n["content_md"] or ""):
                if not x.lower().startswith("kb/"):
                    r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                                     (x,)).fetchone()
                    if r:
                        ids.add(int(r["id"]))
            ids |= {int(i) for i in entity_index.note_ids_for_name(conn, t.rsplit("/", 1)[-1])}
        out = write_one(conn, {"title": into, "domain": wiki_guides.domain_for_title(into),
                               "scope": "", "sources": sorted(ids)}, None, known_titles=_known_titles(conn))
        if not (out.get("ok") and out.get("content_md")):
            conn.commit()
            return {"ok": False, "reason": "; ".join(out.get("errors") or []) or "merged draft failed the lint"}
        into_id = by[into]["id"]
        notes_svc.upsert_note(conn, into, out["content_md"], note_id=into_id, kind="kb",
                              source="merge", version_note=f"merged {', '.join(sources)}")
        wikilinks.resolve_dangling_links(conn, into_id, into)
        for s in sources:
            sn = by[s]
            if not sn:
                continue
            # CONVERT the source to a redirect IN PLACE (keeps its UNIQUE title slot live) so
            # old [[old]] links / external URLs still resolve — then still rewrite inbound
            # [[old]]→[[into]] so live articles point straight at the canonical page.
            create_redirect(conn, s, into)
            notes_svc._rename_inbound_links(conn, s, into, into_id)   # [[old]]→[[into]], never unwrap
            # Carry the source article's talk + entity override (incl. source-of-truth
            # corrections) into the merge target — both are title-keyed, so they'd otherwise
            # be orphaned. OR IGNORE: keep the target's own override if it already has one.
            conn.execute("UPDATE article_talk SET article_title=? WHERE article_title=?", (into, s))
            conn.execute("UPDATE OR IGNORE entity_overrides SET article_title=? WHERE article_title=?",
                         (into, s))
        if out.get("talk"):
            article_talk.record(conn, into, out["talk"])
        _structure_log(conn, "merge", "|".join(sorted(sources)))
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "into": into, "merged": sources}
    finally:
        kb_lock_release(conn)


_RESEARCH_TAU = 0.55


def research_article(conn, title: str, mode: str = "propose") -> dict:
    """Surface existing kb/Reference/* articles semantically related to this article.

    Catches relationships check_needed_links misses because the body never names the leaf.
    Gauntlet-constrained: PROPOSE-ONLY (never auto-writes, never adds prose — the owner's
    accept/reject is the non-colluding adversary). A candidate must be embedding-near
    (distance < tau) AND corroborated by the deterministic neighbourhood (shared
    entity/citation/sibling) — embedding-alone nomination is the sprawl we refuse.

    Args:
        conn: SQLite connection.
        title: Title of the kb article to research.
        mode: Reserved (only "propose" is implemented; included for API symmetry).

    Returns:
        Dict with keys ``ok`` and ``proposals`` (list of {target} dicts).
    """
    from . import notes as notes_svc, embeddings
    note = notes_svc.get_by_title(conn, title)
    if not note or note["kind"] != "kb":
        return {"ok": False, "reason": "no such kb article"}
    body = note["content_md"] or ""
    lines = body.splitlines()
    query = " ".join(([lines[0]] if lines else []) + [ln for ln in lines if ln.startswith("##")]).strip()[:600] \
        or title.rsplit("/", 1)[-1]
    nominated: dict[str, float] = {}
    try:
        for hit in embeddings.semantic_search(conn, query, 25):
            t = hit.get("title") or ""
            if t.startswith("kb/Reference/") and t != title and float(hit.get("distance", 1.0)) < _RESEARCH_TAU:
                nominated[t] = float(hit.get("distance", 1.0))
    except Exception:  # noqa: BLE001
        pass
    neigh = set(scoped_known_titles(conn, title, _known_titles(conn), budget=100000))
    linked = {x.lower() for x in wikilinks.extract_links(body)}
    candidates = [t for t in nominated if t in neigh and t.lower() not in linked]   # corroborated
    if not candidates:
        return {"ok": True, "proposals": []}
    proposals = list(candidates)
    if llm.has_credentials():                       # adjudicate, fed candidate LEADS (new info)
        leads = []
        for t in candidates[:12]:
            r = conn.execute("SELECT content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                             (t,)).fetchone()
            head = (r["content_md"].splitlines()[0] if r and r["content_md"] else "")
            leads.append(f"- {t}: {head[:160]}")
        prompt = (prompts.get("actions.research_article", "")
                  .replace("{title}", title).replace("{salient}", query)
                  .replace("{candidates}", "\n".join(leads)))
        try:
            picked = json.loads(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=400)))
            chosen = {p.get("reference_title") for p in picked if isinstance(p, dict)}
            proposals = [t for t in candidates if t in chosen]      # validate membership (anti-hallucination)
        except Exception:  # noqa: BLE001
            proposals = list(candidates)
    return {"ok": True, "proposals": [{"target": t} for t in proposals]}


def split_article(conn, parent: str, child: str, child_sources: list[str]) -> dict:
    """Spin a child article off a parent using an explicit source partition.

    Writes ``child`` from the given source notes, then re-writes the parent from its remaining
    sources. The partition is explicit (the owner or a restructure hint supplies which notes go
    to the child) — deterministic, no auto-partition. Inverse-pair hysteresis blocks splitting
    a pair a recent merge produced. Runs under the KB lock.

    Args:
        conn: SQLite connection.
        parent: Title of the existing parent article.
        child: New title for the child article (must be a new kb/… path).
        child_sources: Titles of source notes assigned to the child.

    Returns:
        Dict with keys ``ok``, ``parent``, ``child``, or ``reason`` on failure.
    """
    from . import notes as notes_svc, entity_index, article_talk
    parent, child = (parent or "").strip(), (child or "").strip()
    pnote = notes_svc.get_by_title(conn, parent)
    if not pnote or pnote["kind"] != "kb":
        return {"ok": False, "reason": "no such parent kb article"}
    if not child.startswith("kb/") or child.lower().startswith("kb/_") or notes_svc.get_by_title(conn, child):
        return {"ok": False, "reason": "child must be a new kb/… title"}
    pair = "|".join(sorted([parent, child]))
    if _structure_recent(conn, "merge", pair):
        article_talk.add(conn, parent, "todo", f"Split of {child} blocked — recently merged; review by hand.")
        return {"ok": False, "reason": "blocked by hysteresis (recently merged)"}
    child_ids: set[int] = set()
    for t in (child_sources or []):
        r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (t,)).fetchone()
        if r:
            child_ids.add(int(r["id"]))
    if not child_ids:
        return {"ok": False, "reason": "no child source notes resolved"}
    parent_ids: set[int] = set()
    for x in wikilinks.extract_links(pnote["content_md"] or ""):
        if not x.lower().startswith("kb/"):
            r = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (x,)).fetchone()
            if r:
                parent_ids.add(int(r["id"]))
    parent_ids |= {int(i) for i in entity_index.note_ids_for_name(conn, parent.rsplit("/", 1)[-1])}
    remaining = sorted(parent_ids - child_ids)
    if not kb_lock_acquire(conn):
        return {"ok": False, "reason": "KB busy — try again shortly"}
    try:
        cout = write_one(conn, {"title": child, "domain": wiki_guides.domain_for_title(child),
                                "scope": "", "sources": sorted(child_ids)}, None, known_titles=_known_titles(conn))
        if not (cout.get("ok") and cout.get("content_md")):
            conn.commit()
            return {"ok": False, "reason": "; ".join(cout.get("errors") or []) or "child draft failed the lint"}
        notes_svc.upsert_note(conn, child, cout["content_md"], kind="kb", source="split",
                              version_note=f"split from {parent}")
        if cout.get("talk"):
            article_talk.record(conn, child, cout["talk"])
        if remaining:                                  # trim the parent to its remaining sources
            pout = write_one(conn, {"title": parent, "domain": wiki_guides.domain_for_title(parent),
                                    "scope": "", "sources": remaining}, None, known_titles=_known_titles(conn))
            if pout.get("ok") and pout.get("content_md"):
                notes_svc.upsert_note(conn, parent, pout["content_md"], note_id=pnote["id"], kind="kb",
                                      source="split", version_note=f"trimmed — split out {child}")
        _structure_log(conn, "split", pair)
        entity_index.rebuild(conn)
        flag_dead_links(conn)
        refresh_index(conn)
        conn.commit()
        return {"ok": True, "parent": parent, "child": child}
    finally:
        kb_lock_release(conn)


# An item CLOSES only on a genuine settlement outcome; anything else (incl. a missing or
# unrecognized outcome) leaves it OPEN. answered/applied/reconciled also require the article
# to have actually changed this pass — a claimed fix with no edit is downgraded and kept open
# so "no source / no edit needed" can never silently close work. "moot" is exempt (an item can
# legitimately already be answered, or be voided by a removed claim, with nothing left to edit).
_CLOSING = {"answered", "applied", "reconciled", "moot"}
_NEEDS_EDIT = {"answered", "applied", "reconciled"}


def _apply_maintain(conn, out: dict, version_note: str) -> tuple[bool, int]:
    """Persist a maintain_one result: save the revision and close genuinely settled items.

    Saves the revision (versioned), closes the items the model GENUINELY settled (recording
    HOW), and records any new items. An item closes only when its ``outcome`` is a real
    settlement (see _CLOSING) — and answered/applied/reconciled also require the article to
    have changed, else the item stays open.

    Args:
        conn: SQLite connection.
        out: Result dict from maintain_one.
        version_note: Version note to record on the upsert.

    Returns:
        Tuple of (content_changed, number_of_items_actually_closed).
    """
    from . import article_talk
    from . import notes as notes_svc
    if not out["ok"]:
        return False, 0
    changed = bool(out["changed"] and out["content_md"])
    if changed:
        notes_svc.upsert_note(conn, out["title"], out["content_md"], kind="kb", version_note=version_note)
    closed = 0
    for r in out["resolved"]:
        outcome = r.get("outcome") or ""
        how = r["how"] or ""
        if outcome not in _CLOSING:
            continue                                    # unresolvable / unknown → stays open
        if outcome in _NEEDS_EDIT and not changed:
            continue                                    # claimed a fix but made no edit → stays open
        if outcome == "moot" and not changed:
            how = ("(moot — no edit needed) " + how).strip()
        article_talk.resolve_with(conn, r["id"], how)
        closed += 1
    if out["new"]:
        article_talk.record(conn, out["title"], out["new"], author="ai")
    # Commit this article's writes now so the write lock is released BEFORE the batch's next
    # maintain_one() runs its (slow) LLM generation. update_batch / maintain_batch loop over
    # many articles within a single pipeline primitive — without this, each article's writes
    # stayed in an open transaction across the next article's model call, pinning SQLite's
    # write lock for the batch's whole duration and blocking interactive chat writes. The
    # batches are watermark-resumable, so per-article commits are safe (a crash just retries).
    conn.commit()
    return changed, closed


_MAINT_WATERMARK = "kb_maintain:since"


def _now_sec(conn) -> str:
    """Return the current UTC datetime at second precision for watermark use.

    article_talk.created_at is second-precision (datetime('now')); taking the watermark at
    the same precision makes the >= gate exact, never off by a sub-second.
    """
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S','now') AS n").fetchone()["n"]


def maintain_batch(conn, limit: int = 20) -> dict:
    """Run the scheduled maintenance pass over articles with new talk items since the last run.

    The compute gate: an article whose only open items are old unsettled ones with no new
    directive gets skipped (its sources changing is update_batch's job). So maintenance
    specifically picks up NEW owner directives / writer-raised items. Applies each valid
    revision (versioned), closes genuinely settled items (recording HOW), records new items,
    and advances the watermark.

    Watermark discipline (mirrors update_batch): advances only over the LEADING run of new
    items whose article succeeded — a failed (or deferred-by-cap) article holds the watermark
    at that item, so nothing is silently skipped; the next run retries from there. First run
    drains the whole existing backlog once, then gates on newness thereafter.

    Args:
        conn: SQLite connection.
        limit: Maximum number of distinct articles to maintain per run.

    Returns:
        Dict with keys ``articles``, ``changed``, ``resolved``, ``examined``,
        ``kept_open``, ``failed``, and optionally ``deferred`` or ``skipped``.
    """
    from ..db import get_meta, set_meta
    since = get_meta(_MAINT_WATERMARK)
    if since is None:
        since = ""                       # first run: every currently-open item qualifies
    if not llm.has_credentials():
        # Don't advance with no key, or these items would be skipped forever.
        return {"articles": 0, "changed": 0, "resolved": 0, "examined": 0,
                "kept_open": 0, "failed": 0, "skipped": "no LLM credentials"}

    # Take the KB write lock so the batch and an on-demand maintain_now / rebuild can't
    # mutate the same article (across separate connections) at once. If it's held, skip this
    # tick WITHOUT advancing the watermark — the next scheduled run retries from here.
    if not kb_lock_acquire(conn):
        return {"articles": 0, "changed": 0, "resolved": 0, "examined": 0,
                "kept_open": 0, "failed": 0, "skipped": "KB busy"}
    try:
        # Open items raised at/after the watermark second (oldest first), each tied to a live kb
        # article. `>=` (with a second-precision watermark) is at-least-once: an item created in
        # the watermark's own second is re-examined rather than risk being skipped.
        items = conn.execute(
            "SELECT t.id, t.created_at, t.article_title AS title FROM article_talk t "
            "WHERE t.resolved_at IS NULL AND t.kind IN ('conflict','question','todo','directive','correction') "
            "AND t.created_at >= ? "
            "AND EXISTS (SELECT 1 FROM notes n WHERE n.title=t.article_title AND n.kind='kb' AND n.deleted_at IS NULL) "
            "ORDER BY t.created_at, t.id",
            (since,)).fetchall()
        if not items:
            # Quiet night (no new items): no LLM spent — just advance the clock so it keeps moving.
            set_meta(conn, _MAINT_WATERMARK, _now_sec(conn)); conn.commit()
            return {"articles": 0, "changed": 0, "resolved": 0, "examined": 0, "kept_open": 0, "failed": 0}

        # Distinct articles in item-creation order, capped at `limit`; the rest are deferred and
        # (like failures) hold the watermark so they're picked up next run.
        seen, ordered = set(), []
        for it in items:
            if it["title"] not in seen:
                seen.add(it["title"]); ordered.append(it["title"])
        deferred = set(ordered[int(limit):])
        work = ordered[:int(limit)]

        known = _known_titles(conn)
        changed = resolved = examined = failed = 0
        bad = set(deferred)
        for title in work:
            # Feed in any promoted source-of-truth correction notes for this article as NEW
            # sources (the [[wikilink]] alone does NOT route them — _articles_citing is
            # reverse-direction), so the SUPERSEDE-by-recency rule rewrites the article from them.
            corr_ids = [r["source_note_id"] for r in conn.execute(
                "SELECT source_note_id FROM article_talk WHERE article_title=? AND kind='correction' "
                "AND resolved_at IS NULL AND source_note_id IS NOT NULL", (title,)).fetchall()]
            out = maintain_one(conn, title, known, extra_source_ids=corr_ids or None)
            if not out["ok"]:
                failed += 1
                bad.add(title)
                continue
            did_change, n_closed = _apply_maintain(conn, out, "maintenance pass")
            changed += 1 if did_change else 0
            resolved += n_closed
            examined += len(out["resolved"])

        # Advance over the leading run of items whose article succeeded; stop at the first item
        # belonging to a failed/deferred article so it (and everything after) is retried. On a
        # fully-clean run, jump to now so old processed items don't re-qualify next pass.
        new_wm = _now_sec(conn)
        for it in items:
            if it["title"] in bad:
                new_wm = it["created_at"]
                break
        set_meta(conn, _MAINT_WATERMARK, new_wm)
        conn.commit()
        return {"articles": len(work), "changed": changed, "resolved": resolved,
                "examined": examined, "kept_open": examined - resolved,
                "failed": failed, "deferred": len(deferred)}
    finally:
        kb_lock_release(conn)


def _articles_citing(conn, note_id: int) -> set[str]:
    """Return titles of kb articles that cite a given source note via the links table.

    Args:
        conn: SQLite connection.
        note_id: Id of the source note to look up.

    Returns:
        Set of kb article titles.
    """
    rows = conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE l.target_note_id=? AND s.kind='kb' AND s.deleted_at IS NULL", (note_id,)).fetchall()
    return {r["title"] for r in rows}


def _articles_citing_title(conn, title: str) -> set[str]:
    """Return titles of kb articles that cite a note by its title string.

    Used for DELETED sources: soft_delete nulls links.target_note_id (so the id-based lookup
    finds nothing), but it leaves target_title intact — so we match on that to still route a
    deletion to the articles that cited it and let them purge claims whose only source just
    disappeared.

    Args:
        conn: SQLite connection.
        title: Title of the (possibly deleted) source note.

    Returns:
        Set of kb article titles.
    """
    rows = conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL", (title,)).fetchall()
    return {r["title"] for r in rows}


def _articles_for_note_entities(conn, note_id: int) -> set[str]:
    """Return kb article titles whose entities are mentioned by the given note.

    Used so a new or updated note routes to the articles about its subjects.

    Args:
        conn: SQLite connection.
        note_id: Id of the changed note.

    Returns:
        Set of kb article titles.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.article_title AS t FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
        "WHERE m.note_id=? AND e.article_title IS NOT NULL", (note_id,)).fetchall()
    return {r["t"] for r in rows}


def _route_medical_to_health(conn, note_title: str, targets: set[str]) -> set[str]:
    """Redirect medical notes from a person's People article to their Health article.

    Keeps a person's medical captures out of their (now medical-free) People article once the
    health split has run: a notes/medical/… note that routes to kb/People/<X> is retargeted to
    kb/Health/<X> WHEN that health page exists. Existence-gated, so before the one-time split
    (no health page yet) behaviour is unchanged — there is never a half-split state. General
    medical that routes to a Reference article is left alone.

    Args:
        conn: SQLite connection.
        note_title: Title of the changed note.
        targets: Current routing target set.

    Returns:
        Adjusted target set with People→Health redirections applied where applicable.
    """
    if not (note_title or "").lower().startswith("notes/medical/"):
        return targets
    from . import health_split
    out: set[str] = set()
    for t in targets:
        if t.lower().startswith("kb/people/"):
            out.add(health_split.health_page_for(conn, t) or t)
        else:
            out.add(t)
    return out


_WATERMARK = "kb_incremental:since"


def update_batch(conn, limit: int = 40, new_subject_min: int = 2, max_articles: int = 25) -> dict:
    """Flow notes changed since the last pass into the existing KB (incremental update).

    Routes each change to its articles (cite-based for edits/deletes ∪ entity-based for new
    facts), refreshes each affected article once via maintain_one, nudges a Review card for
    brand-new subjects that have no article yet, and advances the watermark. Additive +
    reconciling; the full rebuild remains the source of truth. Assumes changed notes are
    already analyzed and the entity index rebuilt (the recipe does that first).

    Watermark discipline: advances only over the LEADING run of changes whose every target
    article succeeded — a failed (or deferred-by-cap) article holds the watermark at the prior
    change, so nothing is silently skipped; the next run retries from there.

    Args:
        conn: SQLite connection.
        limit: Maximum number of changed notes to process per run.
        new_subject_min: Minimum note count to auto-create a new subject article.
        max_articles: Maximum number of distinct articles to refresh per run.

    Returns:
        Dict with keys ``changes``, ``articles``, ``changed``, ``resolved``, ``examined``,
        ``kept_open``, ``failed``, ``deferred``, ``created``, and ``new_subjects``.
    """
    from ..db import get_meta, set_meta
    since = get_meta(_WATERMARK)
    if since is None:
        # First ever run: start the clock now (the full build already covered history).
        set_meta(conn, _WATERMARK, _now(conn)); conn.commit()
        return {"seeded": True, "changes": 0, "articles": 0, "changed": 0}
    if not llm.has_credentials():
        # Don't advance the watermark with no key, or changes would be skipped forever.
        return {"changes": 0, "articles": 0, "changed": 0, "skipped": "no LLM credentials"}

    changes = conn.execute(
        "SELECT id, title, COALESCE(deleted_at, updated_at) AS changed_at, "
        "(deleted_at IS NOT NULL) AS deleted FROM notes WHERE "
        "(kind='daily' OR (kind='entry' AND title NOT LIKE 'notes/daily/%')) "
        "AND kb_ingest = 1 "                          # opted-out entries don't drive incremental KB updates
        "AND COALESCE(deleted_at, updated_at) > ? ORDER BY changed_at LIMIT ?",
        (since, max(1, int(limit))),
    ).fetchall()
    if not changes:
        return {"changes": 0, "articles": 0, "changed": 0, "resolved": 0, "new_subjects": 0}

    # Route each change → affected articles, collecting new sources + removed (deleted) titles.
    # Keep per-change targets so we can hold the watermark precisely on any failure.
    affected: dict[str, dict] = {}
    change_targets: list[tuple] = []                         # (changed_at, {titles})  in order
    orphans: list[dict] = []
    for ch in changes:
        if ch["deleted"]:
            # soft_delete nulled this note's incoming links' target_note_id, so route the
            # deletion by surviving target_title instead (id-based lookup would find nothing).
            targets = _articles_citing_title(conn, ch["title"])
        else:
            targets = _articles_citing(conn, ch["id"]) | _articles_for_note_entities(conn, ch["id"])
            targets = _route_medical_to_health(conn, ch["title"], targets)
        change_targets.append((ch["changed_at"], targets))
        if not targets:
            if not ch["deleted"]:
                orphans.append(dict(ch))                     # a new/edited subject with no article
            continue
        for t in targets:
            slot = affected.setdefault(t, {"new": set(), "removed": set()})
            if ch["deleted"]:
                slot["removed"].add(ch["title"])
            else:
                slot["new"].add(ch["id"])

    # Cap the LLM fan-out: refresh at most max_articles this run; the rest are "deferred"
    # and (like failures) hold the watermark so they're picked up next run.
    items = list(affected.items())
    deferred = {t for t, _ in items[int(max_articles):]}
    items = items[:int(max_articles)]

    known = _known_titles(conn)
    changed = resolved = examined = failed = 0
    bad_articles = set(deferred)
    for title, slot in items:
        out = maintain_one(conn, title, known,
                           extra_source_ids=list(slot["new"]), removed_titles=list(slot["removed"]))
        if not out["ok"]:
            failed += 1
            bad_articles.add(title)
            continue
        did_change, n_closed = _apply_maintain(conn, out, "incremental update")
        changed += 1 if did_change else 0
        resolved += n_closed
        examined += len(out["resolved"])

    # Brand-new recurring subjects: CREATE their articles now — maintenance owns this, no
    # waiting for a full rebuild. create_article dedups/folds and refuses thin subjects; a
    # creation that fails the lint falls back to a Review card for the owner.
    subj = _create_new_subjects(conn, orphans, new_subject_min)

    # Advance over the leading run of changes whose targets all succeeded; stop at the first
    # change that touched a failed/deferred article so it (and everything after) is retried.
    new_wm = since
    for changed_at, targets in change_targets:
        if targets & bad_articles:
            break
        new_wm = changed_at
    set_meta(conn, _WATERMARK, new_wm)
    conn.commit()
    return {"changes": len(changes), "articles": len(items), "changed": changed,
            "resolved": resolved, "examined": examined, "kept_open": examined - resolved,
            "failed": failed, "deferred": len(deferred),
            "created": subj["created"], "new_subjects": subj["created"] + subj["nudged"]}


def taxonomy_health(conn, post_card: bool = True) -> dict:
    """Report KB taxonomy drift: orphaned articles and un-foldered Reference pages.

    Read-only (no LLM) — turns "rare manual Reorganize" from a guess into a triggered
    decision. Surfaces ORPHAN articles (nothing but the index links them) and un-foldered
    Reference articles (kb/Reference/<Name> — the guide says Reference is always foldered).
    Posts a single "Reorganize recommended" Review card past thresholds.

    Args:
        conn: SQLite connection.
        post_card: Whether to post a Review card when thresholds are exceeded (default True).

    Returns:
        Dict with keys ``articles``, ``orphans``, ``flat_reference``, ``orphan_titles``,
        ``flat_reference_titles``, ``reasons``, and optionally ``carded``.
    """
    from . import reviews as reviews_svc
    arts = [r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL "
        r"AND title NOT LIKE 'kb/\_%' ESCAPE '\'")]
    orphans = []
    for t in arts:
        inbound = conn.execute(
            r"SELECT 1 FROM links l JOIN notes s ON s.id=l.source_note_id "
            r"WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL "
            r"AND s.title NOT LIKE 'kb/\_%' ESCAPE '\' LIMIT 1", (t,)).fetchone()
        if not inbound:
            orphans.append(t)
    flat_ref = [t for t in arts if t.startswith("kb/Reference/") and t.count("/") == 2]
    report = {"articles": len(arts), "orphans": len(orphans), "flat_reference": len(flat_ref),
              "orphan_titles": orphans[:20], "flat_reference_titles": flat_ref[:20]}
    reasons = []
    if len(orphans) >= 5:
        reasons.append(f"{len(orphans)} orphan article(s) (nothing links to them)")
    if flat_ref:
        reasons.append(f"{len(flat_ref)} un-foldered Reference article(s)")
    if reasons and post_card:
        card = "Reorganize recommended"
        if not conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'", (card,)).fetchone():
            reviews_svc.create_review_item(conn, None, title=card,
                                           message="; ".join(reasons) + ". Run a Reorganize when convenient.")
            report["carded"] = True
        conn.commit()
    report["reasons"] = reasons
    return report


# --- Autonomous taxonomy reorg (surgical, content-preserving) -------------------------- #
# Folds un-foldered kb/Reference/<Name> pages into an EXISTING subcategory using a
# deterministic-first, LLM-only-as-tie-breaker selector, then MOVES them via
# recategorize_article (which rewrites inbound links — the article body is never
# regenerated). Bounded per run, with a cooldown plus a permanent inverse-move refusal so
# the layout converges to a stable state instead of churning night after night. Orphans are
# reported, never moved: an orphan needs inbound LINKS (a different, riskier op that edits
# other articles' prose), which we keep owner-gated.
_REORG_TAU = 0.55           # embedding-distance ceiling for a sibling to count (mirrors _RESEARCH_TAU)
_REORG_CORROBORATION = 1.0  # score a subcategory earns when it already holds a linked neighbour


def _reference_subfolders(conn) -> dict[str, list[str]]:
    """Map each existing ``kb/Reference/<Sub>`` subcategory to the live titles under it.

    These prefixes are the ONLY legal move destinations — the reorg never invents a folder.

    Args:
        conn: SQLite connection.

    Returns:
        Dict of subcategory prefix to member titles (depth >= 3 under Reference).
    """
    subs: dict[str, list[str]] = {}
    for t in _known_titles(conn):
        if t.startswith("kb/Reference/") and t.count("/") >= 3:
            subs.setdefault("/".join(t.split("/")[:3]), []).append(t)
    return subs


def _reorg_neighbourhood(conn, title: str) -> set[str]:
    """Return kb titles this article links to or that link to it (deterministic corroboration).

    Args:
        conn: SQLite connection.
        title: Article whose link neighbourhood to gather.

    Returns:
        Set of neighbouring kb titles.
    """
    neigh: set[str] = set()
    row = conn.execute("SELECT id FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                       (title,)).fetchone()
    if row:
        for r in conn.execute(
            "SELECT DISTINCT t.title FROM links l JOIN notes t ON t.id=l.target_note_id "
            "WHERE l.source_note_id=? AND t.kind='kb' AND t.deleted_at IS NULL", (row["id"],)):
            neigh.add(r["title"])
    for r in conn.execute(
        "SELECT DISTINCT s.title FROM links l JOIN notes s ON s.id=l.source_note_id "
        "WHERE lower(l.target_title)=lower(?) AND s.kind='kb' AND s.deleted_at IS NULL", (title,)):
        neigh.add(r["title"])
    return neigh


def _reorg_salient(conn, title: str) -> str:
    """Build the salient-facts query for an article (first line + ``##`` headings).

    Args:
        conn: SQLite connection.
        title: Article title.

    Returns:
        A short query string, falling back to the leaf name when the body is empty.
    """
    row = conn.execute("SELECT content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL",
                       (title,)).fetchone()
    body = (row["content_md"] if row else "") or ""
    lines = body.splitlines()
    return (" ".join(([lines[0]] if lines else []) + [ln for ln in lines if ln.startswith("##")]).strip()[:600]
            or title.rsplit("/", 1)[-1])


def _score_subfolders(conn, title: str, subfolders: dict[str, list[str]]) -> list[tuple[str, float]]:
    """Score each candidate subcategory for an un-foldered Reference page (no mutation).

    Deterministic signals only: embedding similarity to a subcategory's members (capped at
    ``_REORG_TAU``) plus a corroboration bonus when the subcategory already holds a linked
    neighbour of the page. Embedding failures degrade to the link signal alone.

    Args:
        conn: SQLite connection.
        title: The un-foldered Reference page being placed.
        subfolders: Candidate subcategories from ``_reference_subfolders``.

    Returns:
        ``(subcategory, score)`` pairs sorted high-to-low.
    """
    from . import embeddings
    scores = {sub: 0.0 for sub in subfolders}
    member_sub = {m: sub for sub, members in subfolders.items() for m in members}
    try:
        for hit in embeddings.semantic_search(conn, _reorg_salient(conn, title), 25):
            t = hit.get("title") or ""
            d = float(hit.get("distance", 1.0))
            if d < _REORG_TAU and t in member_sub:
                scores[member_sub[t]] += (1.0 - d)
    except Exception:  # noqa: BLE001 — embeddings are best-effort; the link signal still stands
        pass
    neigh = _reorg_neighbourhood(conn, title)
    for sub, members in subfolders.items():
        if any(m in neigh for m in members):
            scores[sub] += _REORG_CORROBORATION
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _reorg_llm_pick(conn, title: str, candidate_subs: list[str], subfolders: dict[str, list[str]]) -> str | None:
    """Adjudicate an ambiguous placement: pick one subcategory from a deterministic shortlist.

    The article's salient text is fenced as UNTRUSTED DATA; the model may only return one of
    the supplied subcategories (membership-validated) or null — it can never name a
    destination outside the shortlist, so an injected instruction cannot redirect a move.

    Args:
        conn: SQLite connection.
        title: The page being placed.
        candidate_subs: Shortlist of existing subcategory prefixes to choose among.
        subfolders: Subcategory to members map (for showing example members).

    Returns:
        The chosen subcategory prefix, or None when the model declines or hallucinates.
    """
    if not llm.has_credentials() or not candidate_subs:
        return None
    leads = [f"- {sub}: " + ", ".join(m.split("/")[-1] for m in subfolders.get(sub, [])[:5])
             for sub in candidate_subs]
    prompt = (prompts.get("actions.reorg_taxonomy", "")
              .replace("{title}", title)
              .replace("{salient}", _reorg_salient(conn, title))
              .replace("{candidates}", "\n".join(leads)))
    try:
        picked = json.loads(_strip_fence(llm.complete([{"role": "user", "content": prompt}], max_tokens=200)))
        sub = picked.get("subfolder") if isinstance(picked, dict) else None
        return sub if sub in candidate_subs else None      # validate membership (anti-hallucination)
    except Exception:  # noqa: BLE001
        return None


def _recat_recent(conn, title: str, k_days: int) -> bool:
    """Return True if ``title`` was a recategorize endpoint within k_days (anti-churn cooldown).

    Uses a recat-specific ``kb_structure_log`` op with a directional ``from|to`` pair_key (the
    merge/split key conventions are not reused). A just-moved page is untouchable for the
    window, which is the primary oscillation guard.

    Args:
        conn: SQLite connection.
        title: Article title to check as either endpoint.
        k_days: Look-back window in days.

    Returns:
        True if a recent recat touched this title.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    rows = conn.execute(
        "SELECT pair_key FROM kb_structure_log WHERE op='recat' "
        "AND at > CAST(strftime('%s','now') AS INTEGER) - ?", (int(k_days) * 86400,)).fetchall()
    for r in rows:
        frm, _sep, to = (r["pair_key"] or "").partition("|")
        if title in (frm, to):
            return True
    return False


def _recat_inverse_blocked(conn, frm: str, to: str) -> bool:
    """Return True if the reverse move (to -> frm) was EVER logged (permanent anti-oscillation).

    Args:
        conn: SQLite connection.
        frm: Proposed move source title.
        to: Proposed move target title.

    Returns:
        True if the inverse move is on record, making A<->B oscillation impossible.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    return conn.execute("SELECT 1 FROM kb_structure_log WHERE op='recat' AND pair_key=? LIMIT 1",
                        (f"{to}|{frm}",)).fetchone() is not None


def reorg_taxonomy(conn, *, max_moves: int = 5, max_llm: int = 3, cooldown_days: int = 7,
                   min_score: float = 0.6, margin: float = 0.15, dry_run: bool = False,
                   post_card: bool = True) -> dict:
    """Autonomously fold un-foldered Reference pages into existing subcategories (no regen).

    Deterministic-first selection; the LLM is only a confidence-gated tie-breaker over a
    shortlist of EXISTING subcategories, and low-confidence pages are left and surfaced on a
    card rather than guessed. Each move goes through ``recategorize_article`` (rewrites inbound
    links, refreshes the index — the body is never regenerated). Bounded by ``max_moves`` per
    run, an N-day cooldown, and a permanent inverse-move refusal, so the layout converges.
    Orphans are reported, never moved.

    Args:
        conn: SQLite connection.
        max_moves: Hard cap on moves applied per run.
        max_llm: Hard cap on LLM tie-break calls per run.
        cooldown_days: Skip any page recategorized within this many days.
        min_score: Minimum top-subcategory score to move without the LLM.
        margin: Minimum lead of the top subcategory over the runner-up to be "confident".
        dry_run: Plan and report intended moves without applying them.
        post_card: Post a summary Review card.

    Returns:
        Dict with keys ``moved``, ``carded``, ``skipped``, ``orphans``, ``llm_used``, ``dry_run``.
    """
    from . import reviews as reviews_svc
    health = taxonomy_health(conn, post_card=False)
    subfolders = _reference_subfolders(conn)
    flat = [t for t in _known_titles(conn) if t.startswith("kb/Reference/") and t.count("/") == 2]
    moved: list[dict] = []
    carded: list[str] = []
    skipped: list[dict] = []
    llm_used = 0
    for title in flat:
        if len(moved) >= max_moves:
            break
        if _recat_recent(conn, title, cooldown_days):
            skipped.append({"title": title, "reason": "cooldown"})
            continue
        scored = _score_subfolders(conn, title, subfolders)
        sub = None
        if scored and scored[0][1] > 0:
            top = scored[0]
            second = scored[1] if len(scored) > 1 else None
            if top[1] >= min_score and (second is None or top[1] - second[1] >= margin):
                sub = top[0]                                # confident — no LLM spend
            elif llm_used < max_llm:
                sub = _reorg_llm_pick(conn, title, [s for s, sc in scored[:4] if sc > 0], subfolders)
                llm_used += 1
        if not sub or sub not in subfolders:                # low-confidence / no fit → leave it
            carded.append(title)
            continue
        new_title = f"{sub}/{title.rsplit('/', 1)[-1]}"
        if _recat_inverse_blocked(conn, title, new_title):
            skipped.append({"title": title, "reason": "inverse-blocked"})
            continue
        if dry_run:
            moved.append({"from": title, "to": new_title, "dry_run": True})
            continue
        res = recategorize_article(conn, title, new_title)
        if res.get("ok"):
            _structure_log(conn, "recat", f"{title}|{new_title}")
            conn.commit()
            moved.append({"from": title, "to": new_title})
        else:
            skipped.append({"title": title, "reason": res.get("reason")})
    if post_card and (moved or carded):
        parts = []
        if moved:
            parts.append(f"folded {len(moved)} Reference page(s) into subcategories"
                         + (" (dry run — nothing applied)" if dry_run else ""))
        if carded:
            parts.append(f"{len(carded)} couldn't be placed confidently — file by hand")
        if health["orphans"]:
            parts.append(f"{health['orphans']} orphan(s) still need inbound links")
        card = "Knowledge base reorganized"
        if not conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'",
                            (card,)).fetchone():
            reviews_svc.create_review_item(conn, None, title=card, message="; ".join(parts) + ".")
        conn.commit()
    return {"moved": moved, "carded": carded, "skipped": skipped,
            "orphans": health["orphans"], "llm_used": llm_used, "dry_run": dry_run}


def _create_new_subjects(conn, orphans: list[dict], min_notes: int) -> dict:
    """Create articles for recurring subjects that have changed notes but no article yet.

    Uses create_article (which dedups/folds and refuses thin stubs). A creation that fails
    the lint falls back to a Review card so the owner sees it — maintenance no longer defers
    new subjects to a full rebuild.

    Args:
        conn: SQLite connection.
        orphans: Changed note dicts with no routed article (from update_batch routing).
        min_notes: Minimum mention count to attempt article creation.

    Returns:
        Dict with keys ``created`` and ``nudged``.
    """
    from . import reviews as reviews_svc
    tally: dict[tuple, int] = {}
    for ch in orphans:
        for r in conn.execute(
            "SELECT e.type, e.canonical_name FROM entity_mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE m.note_id=? AND e.article_title IS NULL", (ch["id"],)).fetchall():
            key = (r["type"], r["canonical_name"])
            tally[key] = tally.get(key, 0) + 1
    created = nudged = 0
    for (typ, name), c in tally.items():
        if c < int(min_notes):
            continue
        res = create_article(conn, name, etype=typ, min_notes=int(min_notes))
        if res.get("created"):
            created += 1
        elif res.get("folded"):
            continue
        else:
            card = f"New subject: {name}"
            if not conn.execute("SELECT 1 FROM review_items WHERE title=? AND status='pending'",
                                (card,)).fetchone():
                reviews_svc.create_review_item(
                    conn, None, title=card,
                    message=f"{c} note(s) mention {name} ({typ}) — couldn't auto-create its article "
                            f"({res.get('reason')}); add it by hand.")
                nudged += 1
    return {"created": created, "nudged": nudged}


def _now(conn) -> str:
    """Return the current UTC datetime at millisecond precision for watermark use.

    Matches the notes table's updated_at/deleted_at format so watermark comparisons
    are exact, never off-by-a-fraction.
    """
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f','now') AS n").fetchone()["n"]


def write_batch(conn, articles: list[dict], instructions: str | None = None, on_article=None) -> dict:
    """Write every article and split the results into valid vs quarantined.

    Quarantined articles failed the structure lint and are surfaced but not saved. Mirrors
    validate_citations' shape. ``on_article(index, total, title)`` is called before each
    write so the run modal can show which article is currently being written.

    Args:
        conn: SQLite connection.
        articles: List of article dicts (title, domain, scope, sources) from outline.
        instructions: Optional guidance forwarded to each write_one call.
        on_article: Optional callback(index, total, title) called before each write.

    Returns:
        Dict with keys ``valid``, ``quarantined``, ``count``, ``bad``, and ``report``.
    """
    # The titles writers may cross-link: every planned article PLUS any live kb article
    # that survives (so links resolve when adding to an existing KB, not just full rebuild).
    planned = [str(a.get("title") or "").strip() for a in articles]
    existing = [r["title"] for r in conn.execute(
        r"SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL "
        r"AND title NOT LIKE 'kb/\_%' ESCAPE '\'").fetchall()]
    known = sorted({t for t in planned + existing if t})
    valid, quarantined = [], []
    total = len(articles)
    for i, art in enumerate(articles, 1):
        if on_article:
            on_article(i, total, str(art.get("title") or "").strip())
        d = write_one(conn, art, instructions, known_titles=known)
        (valid if d["ok"] and d["content_md"] else quarantined).append(d)
    # A human-readable reason per dropped article, so the review card explains WHY each
    # was quarantined (e.g. "no source notes resolved") — not just that it was.
    report = " | ".join(
        f"{d['title']} — {'; '.join(d['errors']) or ('stub' if d['stub'] else 'empty draft')}"
        for d in quarantined)
    return {"valid": valid, "quarantined": quarantined,
            "count": len(articles), "bad": len(quarantined), "report": report}
