"""Review inbox API: list pending review items, count, dismiss, manual create."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import reviews as reviews_svc

router = APIRouter(prefix="/api/reviews", tags=["reviews"], dependencies=[CurrentUser])


class ReviewIn(BaseModel):
    """Input body for manually creating a review item."""

    title: str
    message: str = ""
    link_title: str | None = None


@router.get("")
def list_reviews(status: str = "pending"):
    """List review items filtered by status (default: pending).

    Args:
        status: Filter value; one of 'pending' or 'dismissed'.

    Returns:
        List of review item dicts, newest first, up to 200.
    """
    rows = get_conn().execute(
        "SELECT id, workflow_id, title, message, link_slug, kind, payload_json, status, created_at "
        "FROM review_items WHERE status = ? ORDER BY created_at DESC LIMIT 200",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/count")
def count():
    """Return the count of pending review items.

    Returns:
        Dict with key 'pending' containing the integer count.
    """
    return {"pending": reviews_svc.pending_count(get_conn())}


@router.get("/history")
def history():
    """List dismissed review items from the last 24 hours, newest first.

    Returns:
        List of dismissed review item dicts (up to 200), ordered by dismissed_at DESC.
    """
    # Notifications the user dismissed in the last 24h, newest first. Both sides of
    # the comparison are UTC (dismissed_at is written with datetime('now')), so the
    # window is correct; pending rows (dismissed_at IS NULL) drop out naturally.
    rows = get_conn().execute(
        "SELECT id, workflow_id, title, message, link_slug, status, created_at, dismissed_at "
        "FROM review_items WHERE status = 'dismissed' AND dismissed_at >= datetime('now', '-1 day') "
        "ORDER BY dismissed_at DESC LIMIT 200"
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("")
def create(body: ReviewIn):
    """Manually create a review item, optionally linking to a note by title.

    Args:
        body: Title, optional message, and optional note link title.

    Returns:
        Dict with key 'id' containing the new review item's integer id.
    """
    conn = get_conn()
    link_slug = None
    if body.link_title:
        n = conn.execute(
            "SELECT slug FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL",
            (body.link_title,),
        ).fetchone()
        link_slug = n["slug"] if n else None
    rid = reviews_svc.create_review_item(conn, None, body.title, body.message, link_slug)
    conn.commit()
    return {"id": rid}


@router.post("/{review_id}/dismiss")
def dismiss(review_id: int):
    """Dismiss a pending review item.

    Args:
        review_id: ID of the review item to dismiss.

    Returns:
        Dict with key 'ok' set to True.

    Raises:
        HTTPException: 404 if no pending item with the given id exists.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM review_items WHERE id = ? AND status = 'pending'", (review_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pending review item not found")
    conn.execute(
        "UPDATE review_items SET status = 'dismissed', dismissed_at = datetime('now') WHERE id = ?",
        (review_id,),
    )
    conn.commit()
    return {"ok": True}


def _load_entity_merge(conn, review_id: int, *, require_status: str | None = None) -> tuple:
    """Fetch an entity_merge card's (status, parsed payload), or raise 404/400.

    When require_status is set the card must currently be in that status, so a stale
    double-action (approve/reject/undo on an already-resolved card) is rejected rather
    than silently re-applied.

    Args:
        conn: Active database connection.
        review_id: ID of the review item to load.
        require_status: If provided, the item's status must match this value.

    Returns:
        Tuple of (status_str, payload_dict).

    Raises:
        HTTPException: 404 if the item is not found.
        HTTPException: 400 if the item is not an entity_merge kind or the payload is malformed.
        HTTPException: 409 if require_status is set and the item's status does not match.
    """
    import json
    row = conn.execute(
        "SELECT kind, status, payload_json FROM review_items WHERE id = ?", (review_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Review item not found")
    if row["kind"] != "entity_merge":
        raise HTTPException(status_code=400, detail="Not an entity-merge review item")
    if require_status is not None and row["status"] != require_status:
        raise HTTPException(status_code=409, detail=f"Review item is not {require_status}")
    try:
        return row["status"], json.loads(row["payload_json"] or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed review payload")


def _dismiss(conn, review_id: int) -> None:
    """Mark a review item as dismissed and record the dismissal timestamp.

    Args:
        conn: Active database connection.
        review_id: ID of the review item to dismiss.
    """
    conn.execute(
        "UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
        (review_id,))


@router.post("/{review_id}/approve")
def approve_merge(review_id: int):
    """Approve a PENDING 'same person?' card: record a durable MERGE (survives every rebuild),
    rebuild the index, fold the articles (with a redirect) when both sides have one, refresh
    the AKA lines, and stash the decision id so it can be undone. The entity merge always
    applies; if the (LLM) article fold fails the card is LEFT PENDING with the reason so the
    owner can retry, rather than reporting a false success."""
    import json
    from ..services import entity_decisions, entity_index, wiki_build
    conn = get_conn()
    _, p = _load_entity_merge(conn, review_id, require_status="pending")
    src, into = p.get("source") or {}, p.get("into") or {}
    if not src.get("norm") or not into.get("norm"):
        raise HTTPException(status_code=400, detail="Payload missing source/into")
    decision_id = entity_decisions.add(
        conn, "merge", type=p.get("type", "person"),
        norm_a=src["norm"], canonical=into["norm"],
        display_a=src.get("display"), display_b=into.get("display"), source="review")
    entity_index.rebuild(conn)
    # Fold the articles only when BOTH sides have their own; otherwise rebuild's alias-aware
    # article linking already points the survivor at the lone article (no fold needed).
    fold_reason = None
    if src.get("article_title") and into.get("article_title") and \
            src["article_title"] != into["article_title"]:
        m = wiki_build.merge_articles(conn, [src["article_title"]], into["article_title"])
        if not m.get("ok"):
            fold_reason = m.get("reason") or "article fold failed"
    wiki_build.surface_aliases(conn)
    p["decision_id"] = decision_id
    conn.execute("UPDATE review_items SET payload_json=? WHERE id=?", (json.dumps(p), review_id))
    if fold_reason:
        # The entities are merged (durable), but the article fold didn't apply — keep the card
        # pending so the owner can retry the fold, and report the reason instead of a false OK.
        conn.commit()
        return {"ok": False, "merged_entities": True, "decision_id": decision_id, "reason": fold_reason}
    _dismiss(conn, review_id)
    conn.commit()
    return {"ok": True, "decision_id": decision_id}


@router.post("/{review_id}/reject")
def reject_merge(review_id: int):
    """Reject a PENDING 'same person?' card: record a durable SPLIT so the pair is never
    re-proposed (and the heuristic won't auto-merge them), then dismiss the card."""
    from ..services import entity_decisions, entity_index
    conn = get_conn()
    _, p = _load_entity_merge(conn, review_id, require_status="pending")
    src, into = p.get("source") or {}, p.get("into") or {}
    if src.get("norm") and into.get("norm"):
        entity_decisions.add(conn, "split", type=p.get("type", "person"),
                             norm_a=src["norm"], norm_b=into["norm"],
                             display_a=src.get("display"), display_b=into.get("display"),
                             source="review")
        entity_index.rebuild(conn)
    _dismiss(conn, review_id)
    conn.commit()
    return {"ok": True}


@router.post("/{review_id}/undo")
def undo_merge(review_id: int):
    """Undo an APPROVED merge: delete the recorded decision and rebuild, so the entities split
    back apart. Requires a dismissed card that still carries its decision id; clears the id so
    a second undo can't act on a stale decision."""
    import json
    from ..services import entity_decisions, entity_index
    conn = get_conn()
    _, p = _load_entity_merge(conn, review_id, require_status="dismissed")
    did = p.get("decision_id")
    if not did:
        raise HTTPException(status_code=400, detail="No applied decision to undo")
    entity_decisions.remove(conn, did)
    entity_index.rebuild(conn)
    p.pop("decision_id", None)                       # so a repeat undo can't reuse a stale id
    conn.execute("UPDATE review_items SET payload_json=? WHERE id=?", (json.dumps(p), review_id))
    conn.commit()
    return {"ok": True}
