"""Shared note search: FTS keyword + vector semantic, fused by reciprocal rank.

Used by the architect's search_notes tool so the agent gets keyword AND meaning in a
single call (exact terms the embedding might rank low still surface, and vice-versa)
— no second query_sql round-trip. The public Search router has its own (broader)
fusion that also spans attachments; this is the notes-only core.
"""
from __future__ import annotations

from . import embeddings


def _fts_query(q: str) -> str:
    # Prefix-match each token; quote to neutralise FTS operators.
    toks = [t for t in (q or "").replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in toks) or '""'


def hybrid_notes(conn, q: str, limit: int = 8) -> list[dict]:
    """Notes matching `q` by keyword AND meaning, reciprocal-rank fused, deduped,
    best-first. Returns [{id, title, slug}]. Degrades to whichever half works if the
    other errors (e.g. embeddings unavailable)."""
    q = (q or "").strip()
    if not q:
        return []
    # Each half fetches a WIDER pool than `limit` before fusion, so a hit ranked just
    # outside the top-`limit` in one half (e.g. a long note BM25 penalises) can still
    # win overall once the other half lifts it. Output is sliced back to `limit`.
    pool = max(limit * 3, 24)
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}

    def bump(nid: int, title: str, slug: str, rank: int) -> None:
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (rank + 1)
        meta.setdefault(nid, {"id": nid, "title": title, "slug": slug})

    try:  # keyword (FTS / bm25 order)
        rows = conn.execute(
            "SELECT f.note_id, n.title, n.slug FROM notes_fts f JOIN notes n ON n.id = f.note_id "
            "WHERE notes_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
            (_fts_query(q), pool),
        ).fetchall()
        for i, r in enumerate(rows):
            bump(r["note_id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    try:  # semantic (vector similarity order)
        for i, r in enumerate(embeddings.semantic_search(conn, q, pool)):
            bump(r["id"], r["title"], r["slug"], i)
    except Exception:  # noqa: BLE001
        pass

    return sorted(meta.values(), key=lambda m: scores[m["id"]], reverse=True)[:limit]
