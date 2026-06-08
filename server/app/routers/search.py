"""Hybrid search over notes, attachments AND canonical entities: FTS5 keyword +
sqlite-vec semantic (entities by name/alias)."""
import logging

from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import get_conn
from ..services import embeddings, entity_index

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[CurrentUser])


def _fts_escape(q: str) -> str:
    """Escape a query string for FTS5, wrapping each token as a quoted prefix query.

    Args:
        q: Raw user query string.

    Returns:
        FTS5-safe query string with each token quoted and suffixed with '*'.
    """
    # Wrap each token as a prefix query; quote to neutralise FTS operators.
    tokens = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in tokens) or '""'


@router.get("")
def search(q: str, mode: str = "hybrid", limit: int = 20):
    """Search notes, attachments, and canonical entities using FTS5 and/or semantic search.

    'entities' mode scopes results to entities only. 'hybrid' and 'keyword' use FTS5;
    'hybrid' and 'semantic' use vector similarity. Semantic search degrades gracefully
    to keyword results when the embedding model is unavailable.

    Args:
        q: Search query string.
        mode: Search mode — 'hybrid' (default), 'keyword', 'semantic', or 'entities'.
        limit: Maximum number of results to return (default 20).

    Returns:
        List of result dicts ranked by relevance; each has a 'kind' field
        ('note', 'attachment', or 'entity').
    """
    conn = get_conn()
    # Keyed by a string composite so note and attachment hits never collide.
    results: dict[str, dict] = {}

    def bump(key: str, base: dict, rank_index: int):
        """Add a reciprocal-rank contribution for a result, initializing it if new.

        Args:
            key: Unique string key for this result (e.g. 'note:42').
            base: Seed dict for a new result entry.
            rank_index: 0-based rank position from this source.
        """
        results.setdefault(key, {**base, "score": 0.0})
        results[key]["score"] += 1.0 / (rank_index + 1)

    if not q.strip():
        return []

    # 'entities' is a SCOPE (entities only); hybrid/keyword/semantic are ranking methods.
    entity_only = mode == "entities"
    do_keyword = mode in ("hybrid", "keyword", "entities")
    do_semantic = mode in ("hybrid", "semantic", "entities")

    if do_keyword and not entity_only:
        try:
            for i, r in enumerate(conn.execute(
                "SELECT f.note_id, n.title, n.slug, bm25(notes_fts) AS rank "
                "FROM notes_fts f JOIN notes n ON n.id = f.note_id "
                "WHERE notes_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
                (_fts_escape(q), limit),
            ).fetchall()):
                bump(f"note:{r['note_id']}", {
                    "kind": "note", "id": r["note_id"], "title": r["title"], "slug": r["slug"],
                }, i)
        except Exception:
            pass

        try:
            for i, r in enumerate(conn.execute(
                "SELECT af.attachment_id, af.note_id, af.filename, n.title, n.slug, "
                "bm25(attachments_fts) AS rank "
                "FROM attachments_fts af JOIN notes n ON n.id = af.note_id "
                "WHERE attachments_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
                (_fts_escape(q), limit),
            ).fetchall()):
                bump(f"att:{r['attachment_id']}", {
                    "kind": "attachment", "attachment_id": r["attachment_id"],
                    "note_id": r["note_id"], "filename": r["filename"],
                    "title": r["title"], "slug": r["slug"],
                }, i)
        except Exception:
            pass

    # Canonical entities (people, orgs, places, things, pets…) by NAME/alias (keyword) and
    # by MEANING (their embedding). Both fuse into the same ranking with notes/attachments;
    # in 'entities' scope they're all that's returned.
    if do_keyword:
        try:
            for i, r in enumerate(entity_index.index(conn, q=q, limit=limit)):
                bump(f"entity:{r['id']}", {
                    "kind": "entity", "id": r["id"], "name": r["canonical_name"],
                    "entity_type": r["type"], "note_count": r["note_count"],
                    "article_title": r["article_title"],
                }, i)
        except Exception:
            pass

    if do_semantic and not entity_only:
        # Semantic search loads the local embedding model (embeddings._get_model), which
        # blocks while warming and raises if it failed/unavailable. Degrade to the keyword
        # hits already collected above instead of 500-ing the whole search request.
        try:
            for i, r in enumerate(embeddings.semantic_search(conn, q, limit)):
                bump(f"note:{r['id']}", {
                    "kind": "note", "id": r["id"], "title": r["title"], "slug": r["slug"],
                    "distance": r["distance"],
                }, i)
        except Exception:
            log.debug("semantic_search degraded to keyword", exc_info=True)
        try:
            for i, r in enumerate(embeddings.semantic_search_attachments(conn, q, limit)):
                bump(f"att:{r['attachment_id']}", {
                    "kind": "attachment", "attachment_id": r["attachment_id"],
                    "note_id": r["note_id"], "filename": r["filename"],
                    "title": r["title"], "slug": r["slug"], "snippet": r["snippet"],
                    "distance": r["distance"],
                }, i)
        except Exception:
            log.debug("semantic_search_attachments degraded to keyword", exc_info=True)

    if do_semantic:
        try:
            for i, r in enumerate(embeddings.semantic_search_entities(conn, q, limit)):
                bump(f"entity:{r['id']}", {
                    "kind": "entity", "id": r["id"], "name": r["canonical_name"],
                    "entity_type": r["type"], "note_count": r["note_count"],
                    "article_title": r["article_title"], "distance": r["distance"],
                }, i)
        except Exception:
            pass

    # Mark note hits that CARRY attachments, so the card shows a clip deterministically
    # — not only when the attachment's own text happened to match (one batched query).
    note_ids = [v["id"] for v in results.values() if v["kind"] == "note"]
    if note_ids:
        qmarks = ",".join("?" * len(note_ids))
        counts = {r["note_id"]: r["c"] for r in conn.execute(
            f"SELECT note_id, COUNT(*) c FROM attachments WHERE note_id IN ({qmarks}) GROUP BY note_id",
            note_ids,
        ).fetchall()}
        for v in results.values():
            if v["kind"] == "note":
                v["attachments"] = counts.get(v["id"], 0)

    if mode == "semantic":
        # Pure semantic search: order by true vector similarity (smaller distance =
        # more similar) so the order matches the relevance weight shown in the UI.
        # The rank-fusion score is for blending keyword+semantic in hybrid mode.
        ranked = sorted(results.values(), key=lambda x: (x.get("distance", float("inf")), -x["score"]))
    else:
        ranked = sorted(results.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]
