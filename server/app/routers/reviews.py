"""Review inbox API: list pending review items, count, dismiss, manual create."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import reviews as reviews_svc

router = APIRouter(prefix="/api/reviews", tags=["reviews"], dependencies=[CurrentUser])


class ReviewIn(BaseModel):
    title: str
    message: str = ""
    link_title: str | None = None


@router.get("")
def list_reviews(status: str = "pending"):
    rows = get_conn().execute(
        "SELECT id, workflow_id, title, message, link_slug, status, created_at "
        "FROM review_items WHERE status = ? ORDER BY created_at DESC LIMIT 200",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/count")
def count():
    return {"pending": reviews_svc.pending_count(get_conn())}


@router.get("/history")
def history():
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
