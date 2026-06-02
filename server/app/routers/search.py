"""Hybrid search over notes AND attachments: FTS5 keyword + sqlite-vec semantic."""
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
    # Keyed by a string composite so note and attachment hits never collide.
    results: dict[str, dict] = {}

    def bump(key: str, base: dict, rank_index: int):
        results.setdefault(key, {**base, "score": 0.0})
        results[key]["score"] += 1.0 / (rank_index + 1)

    if not q.strip():
        return []

    if mode in ("hybrid", "keyword"):
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

    if mode in ("hybrid", "semantic"):
        for i, r in enumerate(embeddings.semantic_search(conn, q, limit)):
            bump(f"note:{r['id']}", {
                "kind": "note", "id": r["id"], "title": r["title"], "slug": r["slug"],
                "distance": r["distance"],
            }, i)
        for i, r in enumerate(embeddings.semantic_search_attachments(conn, q, limit)):
            bump(f"att:{r['attachment_id']}", {
                "kind": "attachment", "attachment_id": r["attachment_id"],
                "note_id": r["note_id"], "filename": r["filename"],
                "title": r["title"], "slug": r["slug"], "snippet": r["snippet"],
                "distance": r["distance"],
            }, i)

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
