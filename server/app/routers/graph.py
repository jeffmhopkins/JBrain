"""Knowledge-graph data: nodes (notes) + edges (resolved wiki-links)."""
from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import get_conn

router = APIRouter(prefix="/api/graph", tags=["graph"], dependencies=[CurrentUser])


@router.get("")
def graph():
    conn = get_conn()
    nodes = conn.execute(
        """
        SELECT n.id, n.title, n.slug, n.kind,
               (SELECT COUNT(*) FROM links l JOIN notes s ON s.id = l.source_note_id
                WHERE l.target_note_id = n.id AND s.deleted_at IS NULL) AS in_degree
        FROM notes n WHERE n.deleted_at IS NULL
        """
    ).fetchall()
    # Only edges between two LIVE notes — never a dangling edge to a deleted note
    # (which would otherwise materialize a phantom node in the force graph).
    edges = conn.execute(
        "SELECT DISTINCT l.source_note_id AS source, l.target_note_id AS target "
        "FROM links l "
        "JOIN notes s ON s.id = l.source_note_id AND s.deleted_at IS NULL "
        "JOIN notes t ON t.id = l.target_note_id AND t.deleted_at IS NULL "
        "WHERE l.source_note_id != l.target_note_id"
    ).fetchall()
    return {
        "nodes": [
            {"id": r["id"], "title": r["title"], "slug": r["slug"],
             "kind": r["kind"] or "entry", "val": r["in_degree"] + 1}
            for r in nodes
        ],
        "links": [{"source": r["source"], "target": r["target"]} for r in edges],
    }
