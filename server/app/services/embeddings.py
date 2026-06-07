"""Local text embeddings via fastembed (no external API key required).

The model is loaded lazily on first use and cached for the process lifetime.
"""
from __future__ import annotations

import threading
from typing import Iterable

import sqlite_vec

# bge-small-en-v1.5 produces 384-dim vectors. If you change the model, update
# this to match its output dimension (the vec table is sized from it).
EMBEDDING_DIM = 384

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding

                from ..config import get_settings

                _model = TextEmbedding(model_name=get_settings().embedding_model)
    return _model


def embed(text: str) -> list[float]:
    """Embed a single string."""
    return embed_many([text])[0]


def embed_many(texts: Iterable[str]) -> list[list[float]]:
    model = _get_model()
    return [vec.tolist() for vec in model.embed(list(texts))]


def upsert_note_embedding(conn, note_id: int, title: str, content_md: str) -> None:
    """(Re)compute and store BOTH representations of a note:
      - vec_notes: one whole-note vector (research_scope reads this directly), and
      - vec_note_chunks: one vector per content window, so a long note's body — past
        the embedder's ~512-token truncation — is still reachable by semantic_search.
    """
    full = f"{title}\n\n{content_md}".strip()
    vec = embed(full)
    conn.execute("DELETE FROM vec_notes WHERE note_id = ?", (note_id,))
    conn.execute(
        "INSERT INTO vec_notes (note_id, embedding) VALUES (?, ?)",
        (note_id, sqlite_vec.serialize_float32(vec)),
    )
    upsert_note_chunk_embeddings(conn, note_id, full)


def upsert_note_chunk_embeddings(conn, note_id: int, full_text: str) -> None:
    """Replace a note's chunk vectors. `full_text` is the same 'title\\n\\ncontent'
    string used for the whole-note vector, so a one-chunk note matches identically."""
    from .attachments import chunk_text  # lazy: attachments imports this module
    delete_note_chunk_embeddings(conn, note_id)
    chunks = chunk_text(full_text)
    if not chunks:
        return
    vectors = embed_many(chunks)
    for idx, (text, vec) in enumerate(zip(chunks, vectors)):
        cur = conn.execute(
            "INSERT INTO note_chunks (note_id, chunk_index, text) VALUES (?, ?, ?)",
            (note_id, idx, text),
        )
        conn.execute(
            "INSERT INTO vec_note_chunks (chunk_id, embedding) VALUES (?, ?)",
            (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
        )


def delete_note_chunk_embeddings(conn, note_id: int) -> None:
    # vec_note_chunks is a virtual table: the FK CASCADE on note_chunks won't reach
    # it, so delete its vectors explicitly first (mirrors delete_attachment_embeddings).
    for r in conn.execute(
        "SELECT id FROM note_chunks WHERE note_id = ?", (note_id,)
    ).fetchall():
        conn.execute("DELETE FROM vec_note_chunks WHERE chunk_id = ?", (r["id"],))
    conn.execute("DELETE FROM note_chunks WHERE note_id = ?", (note_id,))


def delete_note_embedding(conn, note_id: int) -> None:
    conn.execute("DELETE FROM vec_notes WHERE note_id = ?", (note_id,))
    delete_note_chunk_embeddings(conn, note_id)


def reindex_missing_note_chunks(conn, batch: int | None = None) -> int:
    """Backfill chunk vectors for notes that have none yet (e.g. after the migration
    that introduced them). Returns how many notes were indexed. Commits if it did
    work. `batch` caps the pass; None does all remaining."""
    # Redirect rows carry only a one-line marker and must stay out of semantic search.
    sql = ("SELECT id, title, content_md FROM notes WHERE deleted_at IS NULL AND redirect_to IS NULL "
           "AND id NOT IN (SELECT DISTINCT note_id FROM note_chunks)")
    if batch:
        sql += f" LIMIT {int(batch)}"
    rows = conn.execute(sql).fetchall()
    n = 0
    for r in rows:
        full = f"{r['title']}\n\n{r['content_md']}".strip()
        upsert_note_chunk_embeddings(conn, r["id"], full)
        n += 1
    if n:
        conn.commit()
    return n


def reindex_missing_attachment_analysis(conn, batch: int | None = None) -> int:
    """Backfill chunk vectors for analyzed attachments whose sidecar was never embedded
    (e.g. images analyzed before image analysis started embedding its summary). Their text
    is already in attachments_fts, so keyword search worked all along — this adds the
    SEMANTIC half. Returns how many attachments were indexed; commits if it did work."""
    from .attachments import chunk_text  # lazy: attachments imports this module
    sql = ("SELECT id, note_id, analysis_md FROM attachments "
           "WHERE analysis_md IS NOT NULL AND analysis_md != '' "
           "AND id NOT IN (SELECT DISTINCT attachment_id FROM attachment_chunks)")
    if batch:
        sql += f" LIMIT {int(batch)}"
    rows = conn.execute(sql).fetchall()
    n = 0
    for r in rows:
        upsert_attachment_embeddings(conn, r["id"], r["note_id"], chunk_text(r["analysis_md"]))
        n += 1
    if n:
        conn.commit()
    return n


def reindex_all_chunks(conn) -> int:
    """Re-chunk and re-embed EVERY note and attachment under the CURRENT chunking
    strategy — used once after a strategy change, when existing chunk vectors (and the
    chunk_index values read_attachment trusts) were built by the old splitter. Notes are
    rebuilt from their stored title+body, attachments from their stored content_text — no
    re-extraction. Commits as it goes so a crash resumes from roughly where it stopped.
    Returns the number of items reprocessed."""
    from .attachments import chunk_text  # lazy: attachments imports this module
    n = 0
    for r in conn.execute(
        "SELECT id, title, content_md FROM notes WHERE deleted_at IS NULL AND redirect_to IS NULL"
    ).fetchall():
        full = f"{r['title']}\n\n{r['content_md']}".strip()
        upsert_note_chunk_embeddings(conn, r["id"], full)
        n += 1
        if n % 50 == 0:
            conn.commit()
    for r in conn.execute(
        "SELECT id, note_id, content_text, analysis_md FROM attachments "
        "WHERE content_text != '' OR (analysis_md IS NOT NULL AND analysis_md != '')"
    ).fetchall():
        # Prefer the enrichment sidecar (image vision summary / transcript) over extracted
        # text, mirroring the read paths — so an analyzed image embeds its analysis, not its
        # (usually empty) OCR.
        body = (r["analysis_md"] or "").strip() or (r["content_text"] or "")
        upsert_attachment_embeddings(conn, r["id"], r["note_id"], chunk_text(body))
        n += 1
        if n % 50 == 0:
            conn.commit()
    conn.commit()
    return n


def run_pending_rechunk(conn) -> int | None:
    """If a chunking-strategy migration flagged a full re-chunk ('rechunk:pending'), run
    it ONCE, clear the flag, and return the count. Returns None when nothing is pending.
    Called from the startup warm task so re-embedding the corpus never blocks boot."""
    from ..db import get_meta, set_meta
    if get_meta("rechunk:pending", conn=conn) != "1":
        return None
    n = reindex_all_chunks(conn)
    set_meta(conn, "rechunk:pending", "0")
    conn.commit()
    return n


def relevant_note_excerpt(conn, note_id: int, query: str, budget_chars: int) -> str | None:
    """A long note's SUBJECT-RELEVANT passages for feeding a writer (KB synthesis), instead
    of a blind head slice that can miss a fact buried past the cut. The note's stored chunks
    are scored against `query` (local embeddings — NO LLM call); the lead is kept for framing
    and the most on-subject buried passages are folded in, in document order, within
    `budget_chars`.

    A deliberate safety-net, not an override: returns None — caller falls back to a head
    slice — unless some buried chunk is MORE on-subject than the note's own head. That keeps
    the head's behaviour wherever it was already capturing the relevant content, so the change
    can only help the off-topic-head case it targets (and can't fire on a vocabulary mismatch
    where nothing buried stands out)."""
    import numpy as np
    from .attachments import CHUNK_MAX_CHARS  # lazy: avoid an import cycle at module load
    rows = conn.execute(
        "SELECT c.chunk_index, c.text, v.embedding FROM note_chunks c "
        "JOIN vec_note_chunks v ON v.chunk_id = c.id "
        "WHERE c.note_id = ? ORDER BY c.chunk_index",
        (note_id,),
    ).fetchall()
    if len(rows) <= 1:                       # 0/1 chunk: a head slice already is the whole note
        return None
    try:
        qv = np.asarray(embed(query), dtype=np.float32)
        qv = qv / (float(np.linalg.norm(qv)) or 1.0)
        mat = np.stack([np.frombuffer(r["embedding"], dtype="<f4") for r in rows])
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat / norms[:, None]) @ qv
    except Exception:                        # embeddings unavailable → caller head-slices
        return None
    lead_sim = float(sims[0])                # chunk_index is contiguous from 0, so row 0 is the lead
    buried = sorted((i for i in range(1, len(rows)) if float(sims[i]) > lead_sim),
                    key=lambda i: -float(sims[i]))
    if not buried:                           # head is already the most on-subject part → no change
        return None
    # Reserve enough budget that the single most-relevant buried chunk always fits beside the
    # framing, so we never end up showing LESS than a head slice would.
    lead_cap = max(budget_chars - CHUNK_MAX_CHARS, budget_chars // 3)
    lead_text = rows[0]["text"][:lead_cap]
    used, keep = len(lead_text), [0]
    for i in buried:
        if used + len(rows[i]["text"]) > budget_chars:
            break
        keep.append(i)
        used += len(rows[i]["text"])
    parts, prev_idx = [], None
    for i in sorted(keep):
        idx = rows[i]["chunk_index"]
        if prev_idx is not None and idx > prev_idx + 1:
            parts.append("[…]")             # mark elided spans so the writer knows there's a gap
        parts.append(lead_text if i == 0 else rows[i]["text"])
        prev_idx = idx
    return "\n\n".join(parts)


def embed_attachment_chunks(chunks: list[str]) -> list[list[float]]:
    """Run the (slow, CPU-bound) embedding for an attachment's chunks WITHOUT touching the DB,
    so a background worker can compute vectors BEFORE it opens its write transaction. Holding the
    single WAL write lock across multi-second fastembed inference wedges every other writer within
    busy_timeout (5s) — including the owner chat persisting its turn — so the embed must never run
    under a lock. Pair with write_attachment_embeddings(), which does the fast row writes."""
    return embed_many(chunks) if chunks else []


def write_attachment_embeddings(conn, attachment_id: int, note_id: int | None,
                                chunks: list[str], vectors: list[list[float]]) -> None:
    """Pure-SQL write of PRE-COMPUTED attachment chunk vectors (fast; safe inside a write txn).
    Replaces the attachment's chunk rows. Pair with embed_attachment_chunks() for the slow half."""
    delete_attachment_embeddings(conn, attachment_id)
    if not chunks:
        return
    for idx, (text, vec) in enumerate(zip(chunks, vectors)):
        cur = conn.execute(
            "INSERT INTO attachment_chunks (attachment_id, note_id, chunk_index, text) "
            "VALUES (?, ?, ?, ?)",
            (attachment_id, note_id, idx, text),
        )
        conn.execute(
            "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
            (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
        )


def upsert_attachment_embeddings(conn, attachment_id: int, note_id: int | None, chunks: list[str]) -> None:
    """Compute-then-write in one call (multi-vector). Convenience wrapper for callers ALREADY
    outside a hot write lock (backfills, the upload path); background workers that hold a write
    txn should instead embed_attachment_chunks() before the lock and write_attachment_embeddings()
    inside it, so the slow embed never holds the lock."""
    write_attachment_embeddings(conn, attachment_id, note_id, chunks, embed_attachment_chunks(chunks))


def delete_attachment_embeddings(conn, attachment_id: int) -> None:
    # vec_chunks is a virtual table: the FK CASCADE on attachment_chunks won't
    # reach it, so delete the vectors explicitly first.
    for r in conn.execute(
        "SELECT id FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,)
    ).fetchall():
        conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (r["id"],))
    conn.execute("DELETE FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,))


# Embed-small / return-large for attachments: a search hit folds the matched chunk's
# immediate NEIGHBOURS into its snippet, so the preview carries enough surrounding context
# to judge relevance (and to feed an LLM) without a separate read_attachment round-trip.
ATT_SNIPPET_WINDOW = 1     # neighbour chunks each side
ATT_SNIPPET_CHARS = 600    # cap on the expanded snippet


def _expanded_snippet(conn, attachment_id: int, chunk_index, matched_text: str,
                      cap: int = ATT_SNIPPET_CHARS) -> str:
    """The matched chunk WITH ±ATT_SNIPPET_WINDOW neighbours from the same attachment,
    concatenated and capped. Falls back to the matched chunk alone when it has no index."""
    if chunk_index is None:
        return matched_text[:cap]
    rows = conn.execute(
        "SELECT text FROM attachment_chunks "
        "WHERE attachment_id = ? AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index",
        (attachment_id, chunk_index - ATT_SNIPPET_WINDOW, chunk_index + ATT_SNIPPET_WINDOW),
    ).fetchall()
    joined = "\n\n".join(r["text"] for r in rows) if rows else matched_text
    return joined[:cap]


def semantic_search_attachments(conn, query: str, limit: int = 10, *,
                                require_tool_access: bool = False, require_kb_ingest: bool = False) -> list[dict]:
    """Semantic search over attachment chunks, collapsed to best chunk per attachment.
    Each hit's snippet is neighbour-expanded (see _expanded_snippet) for context. The
    governance gates mirror semantic_search: an attachment hit credits its parent note,
    so a parent the owner hid from the assistant must not surface here either."""
    qvec = embed(query)
    rows = conn.execute(
        """
        SELECT c.attachment_id, c.note_id, c.text, c.chunk_index, a.filename,
               n.title, n.slug, v.distance
        FROM vec_chunks v
        JOIN attachment_chunks c ON c.id = v.chunk_id
        JOIN attachments a ON a.id = c.attachment_id
        JOIN notes n ON n.id = c.note_id
        WHERE v.embedding MATCH ? AND k = ?
          AND n.deleted_at IS NULL
        """ + _flag_gate(require_tool_access, require_kb_ingest) + """
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(qvec), limit),
    ).fetchall()
    best: dict[int, dict] = {}
    for r in rows:
        if r["attachment_id"] not in best:
            best[r["attachment_id"]] = {
                "attachment_id": r["attachment_id"],
                "note_id": r["note_id"],
                "filename": r["filename"],
                "title": r["title"],
                "slug": r["slug"],
                "distance": r["distance"],
                "chunk_index": r["chunk_index"],
                "snippet": _expanded_snippet(conn, r["attachment_id"], r["chunk_index"], r["text"]),
            }
    return list(best.values())


def store_entity_vector(conn, entity_id: int, vec: list[float]) -> None:
    """Replace one canonical entity's semantic vector (vec_entities)."""
    conn.execute("DELETE FROM vec_entities WHERE entity_id = ?", (entity_id,))
    conn.execute("INSERT INTO vec_entities (entity_id, embedding) VALUES (?, ?)",
                 (entity_id, sqlite_vec.serialize_float32(vec)))


def delete_entity_embedding(conn, entity_id: int) -> None:
    conn.execute("DELETE FROM vec_entities WHERE entity_id = ?", (entity_id,))


# bge-small cosine distance: ~0 identical, ~0.6–0.9 related, ≳1.0 unrelated. The entity
# index is small, so a top-k vector search ALWAYS returns its nearest rows even when nothing
# is actually on topic — without a floor, a "dog" query surfaces a "CT scan" entity. Keep
# only reasonably-similar hits so embedding top-k can't inject junk into the ranking.
ENTITY_SIM_MAX_DISTANCE = 0.9


def semantic_search_entities(conn, query: str, limit: int = 10,
                             max_distance: float = ENTITY_SIM_MAX_DISTANCE) -> list[dict]:
    """Canonical entities most similar in meaning to the query (vec_entities), filtered to a
    relevance floor. Entities are embedded from their name + type + aliases + KB-article
    lead, so a descriptive query ('my dog') can surface a named entity ('Buddy'). Returns
    rows with distance, nearest first, dropping anything farther than `max_distance`."""
    qvec = embed(query)
    rows = conn.execute(
        "SELECT e.id, e.type, e.canonical_name, e.note_count, e.article_title, v.distance "
        "FROM vec_entities v JOIN entities e ON e.id = v.entity_id "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (sqlite_vec.serialize_float32(qvec), max(1, int(limit))),
    ).fetchall()
    return [dict(r) for r in rows if r["distance"] <= max_distance]


def _flag_gate(require_tool_access: bool, require_kb_ingest: bool, alias: str = "n") -> str:
    """SQL fragment to gate note rows by the per-note governance flags. Empty unless a
    caller opts in, so owner-facing search/browse is unaffected by default."""
    conds = []
    if require_tool_access:
        conds.append(f" AND {alias}.tool_access = 1")
    if require_kb_ingest:
        conds.append(f" AND {alias}.kb_ingest = 1")
    return "".join(conds)


def semantic_search(conn, query: str, limit: int = 10, *,
                    require_tool_access: bool = False, require_kb_ingest: bool = False) -> list[dict]:
    """Return notes most similar in meaning to the query, collapsed to each note's
    BEST-matching chunk. Searching chunks (not the whole-note vector) means a long
    note surfaces on the part that matches, instead of being judged only by the
    truncated head the embedder saw. Returns [{id, title, slug, distance}].

    `require_tool_access` / `require_kb_ingest`: caller-aware governance gates — the
    assistant's research tools pass tool_access; KB synthesis passes kb_ingest. Default
    off so owner-facing search is unchanged."""
    qvec = embed(query)
    # Over-fetch chunks so several distinct notes survive even when one long note
    # contributes many near-neighbour chunks; then collapse to best-per-note.
    k = max(limit * 10, 80)
    rows = conn.execute(
        """
        SELECT c.note_id AS id, n.title, n.slug, v.distance
        FROM vec_note_chunks v
        JOIN note_chunks c ON c.id = v.chunk_id
        JOIN notes n ON n.id = c.note_id
        WHERE v.embedding MATCH ? AND k = ?
          AND n.deleted_at IS NULL
        """ + _flag_gate(require_tool_access, require_kb_ingest) + """
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(qvec), k),
    ).fetchall()
    best: dict[int, dict] = {}
    for r in rows:  # rows are distance-ascending → first hit per note is its best chunk
        if r["id"] not in best:
            best[r["id"]] = {"id": r["id"], "title": r["title"], "slug": r["slug"],
                             "distance": r["distance"]}
        if len(best) >= limit:
            break
    return list(best.values())[:limit]
