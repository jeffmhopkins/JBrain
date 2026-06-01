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
               (SELECT COUNT(*) FROM links l WHERE l.target_note_id = n.id) AS in_degree
        FROM notes n WHERE n.deleted_at IS NULL
        """
    ).fetchall()
    edges = conn.execute(
        "SELECT DISTINCT source_note_id AS source, target_note_id AS target "
        "FROM links WHERE target_note_id IS NOT NULL"
    ).fetchall()
    return {
        "nodes": [
            {"id": r["id"], "title": r["title"], "slug": r["slug"],
             "kind": r["kind"] or "entry", "val": r["in_degree"] + 1}
            for r in nodes
        ],
        "links": [{"source": r["source"], "target": r["target"]} for r in edges],
    }
