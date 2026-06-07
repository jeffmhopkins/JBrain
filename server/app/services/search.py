"""Shared note search: FTS keyword + vector semantic, fused by reciprocal rank.

Used by the architect's search_notes tool so the agent gets keyword AND meaning in a
single call (exact terms the embedding might rank low still surface, and vice-versa)
— no second query_sql round-trip.

Results are NOTES, but the corpus spans each note's ATTACHMENTS too: a hit on an
attachment (most importantly the AI image-analysis sidecar in attachments.analysis_md,
which is indexed into attachments_fts and embedded into vec_chunks) credits its parent
note. So a fact that lives only in a photo's vision summary — e.g. a storefront address
read off a Messenger image — surfaces here, not just via the separate search_attachments
tool. The public Search router has its own fusion that returns attachments as their own
hits; this one collapses them onto the owning note.
"""
from __future__ import annotations

from . import embeddings


def _fts_query(q: str) -> str:
    # Prefix-match each token; quote to neutralise FTS operators.
    toks = [t for t in (q or "").replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in toks) or '""'


def hybrid_notes(conn, q: str, limit: int = 8, *, entity_expand: bool = False) -> list[dict]:
    """Notes matching `q` by keyword AND meaning — across both the note body and its
    attachments (image-analysis sidecars, PDFs, transcripts) — reciprocal-rank fused,
    deduped, best-first. Returns [{id, title, slug}]. Degrades to whichever sources work
    if others error (e.g. embeddings unavailable).

    `entity_expand` (OWNER-CONTEXT ONLY — never a recipient/share path, which would leak
    notes past their scope): blend a resolved entity's mention notes into the fusion. Two
    additive resolutions, both alias-aware so they reach notes the FTS/vector halves can't:
      • WHOLE-QUERY: the entire query resolves to a known entity OR alias ("Jeff" → notes
        filed under "Jeffrey").
      • SPAN (entity_index.span_entity_note_ids): a known NAME mentioned INSIDE a sentence
        ("what did jeff hopkins say about taxes" → that person's notes). Fires only on the
        SAFE surface set — real entity keys, multi-token aliases, or user-decided single
        names — and never on an ambiguous term, so common words / bare heuristic first names
        do NOT expand into a corpus.
    Both share the ambiguity guard and the same modest fixed-rank bump (surfaces a note but
    can't dominate a genuine FTS/vector hit), and a no-name sentence expands nothing."""
    q = (q or "").strip()
    if not q:
        return []
    # Each half fetches a WIDER pool than `limit` before fusion, so a hit ranked just
    # outside the top-`limit` in one half (e.g. a long note BM25 penalises) can still
    # win overall once the other half lifts it. Output is sliced back to `limit`.
    pool = max(limit * 3, 24)
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}

    def bump(nid, title: str, slug: str, rank: int) -> None:
        if nid is None:          # unattached attachment (no owning note) → nothing to surface
            return
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (rank + 1)
        meta.setdefault(nid, {"id": nid, "title": title, "slug": slug})

    try:  # keyword over note bodies (FTS / bm25 order)
        rows = conn.execute(
            "SELECT f.note_id, n.title, n.slug FROM notes_fts f JOIN notes n ON n.id = f.note_id "
            "WHERE notes_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
            (_fts_query(q), pool),
        ).fetchall()
        for i, r in enumerate(rows):
            bump(r["note_id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    try:  # semantic over note bodies (vector similarity order)
        for i, r in enumerate(embeddings.semantic_search(conn, q, pool)):
            bump(r["id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    try:  # keyword over ATTACHMENTS (incl. AI image-analysis sidecars) → credit the parent note
        rows = conn.execute(
            "SELECT af.note_id, n.title, n.slug FROM attachments_fts af JOIN notes n ON n.id = af.note_id "
            "WHERE attachments_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
            (_fts_query(q), pool),
        ).fetchall()
        for i, r in enumerate(rows):
            bump(r["note_id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    try:  # semantic over attachment chunks (image analysis / PDF / transcript) → parent note
        for i, r in enumerate(embeddings.semantic_search_attachments(conn, q, pool)):
            bump(r["note_id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    if entity_expand:  # alias-aware: blend the resolved entity's notes into the fusion
        try:
            from . import entity_index
            # Don't expand an AMBIGUOUS name (maps to ≥2 entities) — we can't safely pick one,
            # and blending a same-named person's corpus would be wrong. This mirrors the linker's
            # ambiguity guard so the search channel and the linker agree on what an alias resolves to.
            qn = entity_index.normalize(q)
            ambiguous = {str(t.get("term", "")).lower() for t in entity_index.ambiguous_terms(conn)}
            ids = [] if (not qn or qn in ambiguous) else entity_index.note_ids_for_name(conn, q)
            # Span detection (strictly additive, same owner-only gate): also reach notes for a
            # KNOWN name MENTIONED INSIDE a sentence query — "what did jeff hopkins say about
            # taxes" → notes filed under "Jeffrey" — which the whole-query test above can't. It
            # only fires on the safe surface set (real keys / multi-token or user-decided
            # aliases, ambiguous dropped), so common words and bare heuristic first names don't
            # expand. Union with the whole-query ids; bump() dedups so no note is double-counted.
            span_ids = entity_index.span_entity_note_ids(conn, q)
            seen = set(ids)
            ids = ids + [i for i in span_ids if not (i in seen or seen.add(i))]
            if ids:
                qmarks = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT id, title, slug FROM notes WHERE id IN ({qmarks}) AND deleted_at IS NULL "
                    f"ORDER BY created_at DESC LIMIT ?", [*ids, pool],
                ).fetchall()
                for r in rows:                       # modest fixed-rank bump → surfaces but doesn't dominate
                    bump(r["id"], r["title"], r["slug"], 2)
        except Exception:  # noqa: BLE001
            pass

    return sorted(meta.values(), key=lambda m: scores[m["id"]], reverse=True)[:limit]
