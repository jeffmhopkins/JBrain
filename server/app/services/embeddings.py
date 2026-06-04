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
    sql = ("SELECT id, title, content_md FROM notes WHERE deleted_at IS NULL "
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


def upsert_attachment_embeddings(conn, attachment_id: int, note_id: int | None, chunks: list[str]) -> None:
    """Store one vector per chunk for an attachment (multi-vector)."""
    delete_attachment_embeddings(conn, attachment_id)
    if not chunks:
        return
    vectors = embed_many(chunks)
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


def delete_attachment_embeddings(conn, attachment_id: int) -> None:
    # vec_chunks is a virtual table: the FK CASCADE on attachment_chunks won't
    # reach it, so delete the vectors explicitly first.
    for r in conn.execute(
        "SELECT id FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,)
    ).fetchall():
        conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (r["id"],))
    conn.execute("DELETE FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,))


def semantic_search_attachments(conn, query: str, limit: int = 10) -> list[dict]:
    """Semantic search over attachment chunks, collapsed to best chunk per attachment."""
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
                "snippet": r["text"][:200],
            }
    return list(best.values())


def store_entity_vector(conn, entity_id: int, vec: list[float]) -> None:
    """Replace one canonical entity's semantic vector (vec_entities)."""
    conn.execute("DELETE FROM vec_entities WHERE entity_id = ?", (entity_id,))
    conn.execute("INSERT INTO vec_entities (entity_id, embedding) VALUES (?, ?)",
                 (entity_id, sqlite_vec.serialize_float32(vec)))


def delete_entity_embedding(conn, entity_id: int) -> None:
    conn.execute("DELETE FROM vec_entities WHERE entity_id = ?", (entity_id,))


def semantic_search_entities(conn, query: str, limit: int = 10) -> list[dict]:
    """Canonical entities most similar in meaning to the query (vec_entities). Entities are
    embedded from their name + type + aliases + KB-article lead, so a descriptive query
    ('my dog') can surface a named entity ('Buddy'). Returns rows with distance."""
    qvec = embed(query)
    rows = conn.execute(
        "SELECT e.id, e.type, e.canonical_name, e.note_count, e.article_title, v.distance "
        "FROM vec_entities v JOIN entities e ON e.id = v.entity_id "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (sqlite_vec.serialize_float32(qvec), max(1, int(limit))),
    ).fetchall()
    return [dict(r) for r in rows]


def semantic_search(conn, query: str, limit: int = 10) -> list[dict]:
    """Return notes most similar in meaning to the query, collapsed to each note's
    BEST-matching chunk. Searching chunks (not the whole-note vector) means a long
    note surfaces on the part that matches, instead of being judged only by the
    truncated head the embedder saw. Returns [{id, title, slug, distance}]."""
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
