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
    """(Re)compute and store the embedding for a note."""
    vec = embed(f"{title}\n\n{content_md}".strip())
    conn.execute("DELETE FROM vec_notes WHERE note_id = ?", (note_id,))
    conn.execute(
        "INSERT INTO vec_notes (note_id, embedding) VALUES (?, ?)",
        (note_id, sqlite_vec.serialize_float32(vec)),
    )


def delete_note_embedding(conn, note_id: int) -> None:
    conn.execute("DELETE FROM vec_notes WHERE note_id = ?", (note_id,))


def semantic_search(conn, query: str, limit: int = 10) -> list[dict]:
    """Return notes most similar in meaning to the query."""
    qvec = embed(query)
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.slug, v.distance
        FROM vec_notes v
        JOIN notes n ON n.id = v.note_id
        WHERE v.embedding MATCH ? AND k = ?
          AND n.deleted_at IS NULL
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(qvec), limit),
    ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "slug": r["slug"], "distance": r["distance"]}
        for r in rows
    ]
