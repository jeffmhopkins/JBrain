"""Review items: workflow-posted cards surfaced in the PWA Review inbox."""
from __future__ import annotations


def create_review_item(
    conn, workflow_id: int | None, title: str, message: str = "", link_slug: str | None = None,
    kind: str | None = None, payload_json: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO review_items (workflow_id, title, message, link_slug, kind, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (workflow_id, title, message or "", link_slug, kind, payload_json),
    )
    return cur.lastrowid


def pending_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM review_items WHERE status = 'pending'"
    ).fetchone()["c"]
