"""Owner-side (AUTHENTICATED) share-link management: mint, list, revoke, and
accept/reject the edit proposals that arrive via public EDIT links."""
import hashlib

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc
from ..services import share as share_svc
from .staging import _apply_action

router = APIRouter(prefix="/api/shares", tags=["shares"], dependencies=[CurrentUser])


class MintIn(BaseModel):
    title: str
    scope: str = "view"
    label: str | None = None
    ttl_days: int | None = None      # optional expiry; None = no expiry


@router.post("")
def mint(body: MintIn):
    if body.scope not in ("view", "edit"):
        raise HTTPException(status_code=400, detail="scope must be 'view' or 'edit'")
    conn = get_conn()
    note = notes_svc.get_by_title(conn, body.title.strip())
    if note is None:
        raise HTTPException(status_code=404, detail=f"No note titled '{body.title}'")
    token = share_svc.create_link(conn, note["id"], body.scope, body.label, body.ttl_days)
    conn.commit()
    return {"token": token, "url": share_svc.share_url(token), "scope": body.scope,
            "note_title": note["title"], "note_slug": note["slug"]}


@router.get("")
def list_shares():
    """Active links (+ absolute URL), pending proposals (with a diff preview), and
    the recent proposal HISTORY (accepted / rejected / superseded) so the owner can
    see the status of everything in one place."""
    conn = get_conn()
    links = conn.execute(
        "SELECT sl.id, sl.token, sl.scope, sl.label, sl.created_at, sl.last_used_at, sl.expires_at, "
        "       n.title AS note_title, n.slug AS note_slug, "
        "       (SELECT COUNT(*) FROM share_proposals p WHERE p.share_link_id = sl.id AND p.status='pending') AS pending "
        "FROM share_links sl JOIN notes n ON n.id = sl.note_id "
        "WHERE sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()
    proposals = conn.execute(
        "SELECT p.id, p.proposed_content, p.proposer_name, p.proposer_note, p.created_at, p.basis_hash, "
        "       n.title AS note_title, n.slug AS note_slug, n.kind AS note_kind, "
        "       n.content_md AS current_content, sl.label "
        "FROM share_proposals p JOIN notes n ON n.id = p.note_id JOIN share_links sl ON sl.id = p.share_link_id "
        "WHERE p.status='pending' ORDER BY p.created_at DESC"
    ).fetchall()
    history = conn.execute(
        "SELECT p.id, p.proposer_name, p.status, p.created_at, p.resolved_at, "
        "       n.title AS note_title, n.slug AS note_slug "
        "FROM share_proposals p JOIN notes n ON n.id = p.note_id "
        "WHERE p.status != 'pending' ORDER BY COALESCE(p.resolved_at, p.created_at) DESC LIMIT 50"
    ).fetchall()
    return {
        "links": [{**dict(r), "url": share_svc.share_url(r["token"])} for r in links],
        "proposals": [{**dict(r),
                       "stale": hashlib.sha256((r["current_content"] or "").encode()).hexdigest() != r["basis_hash"]}
                      for r in proposals],
        "history": [dict(r) for r in history],
    }


@router.post("/{link_id}/revoke")
def revoke(link_id: int):
    conn = get_conn()
    share_svc.revoke_link(conn, link_id)
    conn.commit()
    return {"ok": True}


@router.post("/proposals/{prop_id}/accept")
def accept_proposal(prop_id: int):
    """Apply the proposed content through the SAME staged-UPDATE path (content-hash
    basis), so it 409s instead of clobbering an intervening owner edit."""
    conn = get_conn()
    p = conn.execute("SELECT * FROM share_proposals WHERE id=? AND status='pending'", (prop_id,)).fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Pending proposal not found")
    claim = conn.execute("UPDATE share_proposals SET status='accepted', resolved_at=datetime('now') "
                         "WHERE id=? AND status='pending'", (prop_id,))
    if claim.rowcount != 1:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Proposal is no longer pending")
    note = conn.execute("SELECT title FROM notes WHERE id=? AND deleted_at IS NULL", (p["note_id"],)).fetchone()
    if note is None:
        conn.rollback()
        raise HTTPException(status_code=409, detail="The target note no longer exists.")
    payload = {"type": "UPDATE", "title": note["title"], "content": p["proposed_content"],
               "_basis": {"note_id": p["note_id"], "content_hash": p["basis_hash"]}}
    try:
        _apply_action(conn, "UPDATE", payload, conversation_id=None, source="shared")
    except HTTPException:
        conn.rollback()   # basis-stale 409 bubbles; proposal stays pending → re-propose
        raise
    if p["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (p["review_item_id"],))
    conn.commit()
    return {"ok": True}


@router.post("/proposals/{prop_id}/reject")
def reject_proposal(prop_id: int):
    """Reject but LEAVE THE LINK ACTIVE — the editor can re-propose."""
    conn = get_conn()
    p = conn.execute("SELECT review_item_id FROM share_proposals WHERE id=? AND status='pending'", (prop_id,)).fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Pending proposal not found")
    conn.execute("UPDATE share_proposals SET status='rejected', resolved_at=datetime('now') WHERE id=?", (prop_id,))
    if p["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (p["review_item_id"],))
    conn.commit()
    return {"ok": True}
