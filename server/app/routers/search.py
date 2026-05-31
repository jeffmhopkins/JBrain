"""Hybrid search: FTS5 keyword + sqlite-vec semantic, merged and ranked."""
from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import get_conn
from ..services import embeddings

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[CurrentUser])


def _fts_escape(q: str) -> str:
    # Wrap each token as a prefix query; quote to neutralise FTS operators.
    tokens = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in tokens) or '""'


@router.get("")
def search(q: str, mode: str = "hybrid", limit: int = 20):
    conn = get_conn()
    results: dict[int, dict] = {}

    if mode in ("hybrid", "keyword") and q.strip():
        try:
            rows = conn.execute(
                """
                SELECT f.note_id, n.title, n.slug, bm25(notes_fts) AS rank
                FROM notes_fts f JOIN notes n ON n.id = f.note_id
                WHERE notes_fts MATCH ? AND n.deleted_at IS NULL
                ORDER BY rank LIMIT ?
                """,
                (_fts_escape(q), limit),
            ).fetchall()
            for i, r in enumerate(rows):
                results.setdefault(r["note_id"], {
                    "id": r["note_id"], "title": r["title"], "slug": r["slug"], "score": 0.0,
                })
                # Lower bm25 rank = better; convert to a descending contribution.
                results[r["note_id"]]["score"] += 1.0 / (i + 1)
        except Exception:
            pass  # malformed FTS query — fall back to semantic only

    if mode in ("hybrid", "semantic") and q.strip():
        for i, r in enumerate(embeddings.semantic_search(conn, q, limit)):
            results.setdefault(r["id"], {
                "id": r["id"], "title": r["title"], "slug": r["slug"], "score": 0.0,
            })
            results[r["id"]]["score"] += 1.0 / (i + 1)

    ranked = sorted(results.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]
