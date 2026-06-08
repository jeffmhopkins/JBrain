"""Review items: workflow-posted cards surfaced in the PWA Review inbox."""
from __future__ import annotations


def create_review_item(
    conn, workflow_id: int | None, title: str, message: str = "", link_slug: str | None = None,
    kind: str | None = None, payload_json: str | None = None
) -> int:
    """Insert a new review inbox card and return its id.

    Args:
        conn: Database connection.
        workflow_id: The workflow that triggered this item, or None for system/AI items.
        title: Short card title shown in the inbox.
        message: Longer description text shown on the card.
        link_slug: Note slug or magic slug (e.g. '__shares__') for the card's deep-link.
        kind: Optional categorization string for filtering.
        payload_json: Optional JSON blob attached to the item for structured actions.

    Returns:
        The new review_items.id.
    """
    cur = conn.execute(
        "INSERT INTO review_items (workflow_id, title, message, link_slug, kind, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (workflow_id, title, message or "", link_slug, kind, payload_json),
    )
    return cur.lastrowid


def pending_count(conn) -> int:
    """Return the count of pending (unactioned) review inbox items.

    Args:
        conn: Database connection.

    Returns:
        Integer count of pending review_items rows.
    """
    return conn.execute(
        "SELECT COUNT(*) AS c FROM review_items WHERE status = 'pending'"
    ).fetchone()["c"]
