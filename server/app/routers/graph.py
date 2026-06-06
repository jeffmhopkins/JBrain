"""Knowledge-graph data: nodes (notes) + edges (resolved wiki-links)."""
from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/graph", tags=["graph"], dependencies=[CurrentUser])


@router.get("")
def graph():
    conn = get_conn()
    # Hide protected/system pages (any path segment starts with '_', e.g. kb/_index)
    # from the graph — as nodes and as either end of an edge, so no phantom node is
    # materialized by a link to a hidden page.
    node_hidden = notes_svc.protected_title_sql("n.title")
    src_hidden = notes_svc.protected_title_sql("s.title")
    tgt_hidden = notes_svc.protected_title_sql("t.title")
    nodes = conn.execute(
        f"""
        SELECT n.id, n.title, n.slug, n.kind,
               (SELECT COUNT(*) FROM links l JOIN notes s ON s.id = l.source_note_id
                WHERE l.target_note_id = n.id AND s.deleted_at IS NULL) AS in_degree
        FROM notes n WHERE n.deleted_at IS NULL AND n.redirect_to IS NULL AND NOT {node_hidden}
        """
    ).fetchall()
    # Only edges between two LIVE, non-hidden, non-redirect notes — never a dangling edge to a
    # deleted/hidden/redirect note (which would otherwise materialize a phantom node).
    edges = conn.execute(
        "SELECT DISTINCT l.source_note_id AS source, l.target_note_id AS target "
        "FROM links l "
        f"JOIN notes s ON s.id = l.source_note_id AND s.deleted_at IS NULL AND s.redirect_to IS NULL AND NOT {src_hidden} "
        f"JOIN notes t ON t.id = l.target_note_id AND t.deleted_at IS NULL AND t.redirect_to IS NULL AND NOT {tgt_hidden} "
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
