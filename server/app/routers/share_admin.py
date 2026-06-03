"""Owner-side (AUTHENTICATED) share-link management: mint, list, revoke, and
accept/reject the edit proposals that arrive via public EDIT links."""
import hashlib
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc
from ..services import research as research_svc
from ..services import share as share_svc
from .staging import _apply_action

router = APIRouter(prefix="/api/shares", tags=["shares"], dependencies=[CurrentUser])


class MintIn(BaseModel):
    title: str
    scope: str = "view"
    label: str | None = None
    ttl_days: int | None = None      # optional expiry; None = no expiry
    bind: bool = False               # lock to the first browser that opens it


@router.post("")
def mint(body: MintIn):
    if body.scope not in ("view", "edit"):
        raise HTTPException(status_code=400, detail="scope must be 'view' or 'edit'")
    conn = get_conn()
    note = notes_svc.get_by_title(conn, body.title.strip())
    if note is None:
        raise HTTPException(status_code=404, detail=f"No note titled '{body.title}'")
    token = share_svc.create_link(conn, note["id"], body.scope, body.label, body.ttl_days, body.bind)
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
        "       sl.bind, sl.bound_at, "
        "       n.title AS note_title, n.slug AS note_slug, "
        "       (SELECT COUNT(*) FROM share_proposals p WHERE p.share_link_id = sl.id AND p.status='pending') AS pending "
        "FROM share_links sl JOIN notes n ON n.id = sl.note_id "
        "WHERE sl.status='active' AND sl.kind='note' ORDER BY sl.created_at DESC"
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
    # Guided AI intake links (draft + active) and the responses awaiting approval #2.
    guided_links = conn.execute(
        "SELECT sl.id, sl.token, sl.created_at, sl.expires_at, gs.goal, gs.intro, gs.sub_prompt, "
        "       gs.status AS spec_status, gs.bind, gs.single_use, n.title AS note_title, n.slug AS note_slug, "
        "       (SELECT COUNT(*) FROM guided_sessions s WHERE s.share_link_id=sl.id AND s.status='submitted') AS submitted, "
        "       (SELECT COUNT(*) FROM guided_sessions s WHERE s.share_link_id=sl.id AND s.status IN ('active','drafting','submitted')) AS started "
        "FROM share_links sl JOIN guided_specs gs ON gs.share_link_id=sl.id JOIN notes n ON n.id=sl.note_id "
        "WHERE sl.kind='guided' AND sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()
    guided_pending = conn.execute(
        "SELECT s.id, s.name, s.document_md, s.transcript_json, s.created_at, s.completed_at, gs.goal, "
        "       n.title AS note_title, n.slug AS note_slug "
        "FROM guided_sessions s JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "JOIN share_links sl ON sl.id=s.share_link_id JOIN notes n ON n.id=sl.note_id "
        "JOIN review_items ri ON ri.id=s.review_item_id "
        "WHERE s.status='submitted' AND ri.status='pending' ORDER BY s.completed_at DESC"
    ).fetchall()
    # Sessions auto-ended for abuse or distress, awaiting the owner's acknowledgement.
    guided_ended = conn.execute(
        "SELECT s.id, s.name, s.end_reason, s.transcript_json, s.completed_at, gs.goal, "
        "       sl.id AS link_id, sl.status AS link_status, n.title AS note_title, n.slug AS note_slug "
        "FROM guided_sessions s JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "JOIN share_links sl ON sl.id=s.share_link_id JOIN notes n ON n.id=sl.note_id "
        "JOIN review_items ri ON ri.id=s.review_item_id "
        "WHERE s.end_reason IS NOT NULL AND ri.status='pending' ORDER BY s.completed_at DESC"
    ).fetchall()
    def _gp(r):
        d = dict(r)
        # The raw chat is for OWNER REVIEW ONLY — surfaced here, never written to a
        # note or embedded, so it stays out of brain search. Deleted on accept/reject.
        d["transcript"] = json.loads(d.pop("transcript_json") or "[]")
        return d
    # Resolved guided sessions (approved / discarded / ended) — the history record.
    # Excludes anything still pending above (those have a pending review item).
    guided_history = conn.execute(
        "SELECT s.id, s.name, s.status, s.end_reason, s.completed_at, gs.goal, "
        "       n.title AS note_title, n.slug AS note_slug "
        "FROM guided_sessions s JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "JOIN share_links sl ON sl.id=s.share_link_id JOIN notes n ON n.id=sl.note_id "
        "LEFT JOIN review_items ri ON ri.id=s.review_item_id "
        "WHERE s.completed_at IS NOT NULL AND (ri.id IS NULL OR ri.status != 'pending') "
        "ORDER BY s.completed_at DESC LIMIT 50"
    ).fetchall()
    def _disp(r):
        er = r["end_reason"] or ""
        disp = ("ended" if er.startswith("abuse") else "distress" if er == "distress"
                else "approved" if r["status"] == "submitted"
                else "discarded" if r["status"] == "abandoned" else r["status"])
        return {"id": r["id"], "name": r["name"], "goal": r["goal"], "disposition": disp,
                "note_title": r["note_title"], "note_slug": r["note_slug"], "completed_at": r["completed_at"]}
    # Research Q&A links (draft + active) with their exposure + usage counters.
    research_links = conn.execute(
        "SELECT sl.id, sl.token, sl.label, sl.created_at, sl.expires_at, rs.status AS spec_status, "
        "       rs.bind, rs.single_use, rs.approved_ids_json, rs.reply_count, rs.max_total_replies, "
        "       (SELECT COUNT(*) FROM research_sessions s WHERE s.share_link_id=sl.id) AS sessions "
        "FROM share_links sl JOIN research_specs rs ON rs.share_link_id=sl.id "
        "WHERE sl.kind='research' AND sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()

    def _rl(r):
        d = dict(r)
        d["approved_count"] = len(json.loads(d.pop("approved_ids_json") or "[]"))
        d["url"] = share_svc.share_url(d["token"])
        return d

    return {
        "research_links": [_rl(r) for r in research_links],
        "links": [{**dict(r), "url": share_svc.share_url(r["token"])} for r in links],
        "proposals": [{**dict(r),
                       "stale": hashlib.sha256((r["current_content"] or "").encode()).hexdigest() != r["basis_hash"]}
                      for r in proposals],
        "history": [dict(r) for r in history],
        "guided_history": [_disp(r) for r in guided_history],
        "guided_links": [{**dict(r), "url": share_svc.share_url(r["token"])} for r in guided_links],
        "guided_pending": [_gp(r) for r in guided_pending],
        "guided_ended": [_gp(r) for r in guided_ended],
    }


@router.post("/guided/sessions/{sid}/reopen")
def guided_reopen(sid: int):
    """Recover from an abuse lock: un-revoke the link and clear the ended session."""
    conn = get_conn()
    s = conn.execute("SELECT share_link_id, review_item_id FROM guided_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    share_svc.reactivate_link(conn, s["share_link_id"])
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    conn.execute("UPDATE guided_sessions SET transcript_json='[]' WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


@router.post("/guided/sessions/{sid}/acknowledge")
def guided_acknowledge(sid: int):
    """Dismiss an auto-ended (abuse/distress) session without re-opening the link."""
    conn = get_conn()
    s = conn.execute("SELECT review_item_id FROM guided_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    conn.execute("UPDATE guided_sessions SET transcript_json='[]' WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


class GuidedOptionsIn(BaseModel):
    bind: bool = False
    single_use: bool = False


@router.post("/guided/{link_id}/options")
def guided_options(link_id: int, body: GuidedOptionsIn):
    """Toggle the lock-to-device / run-once options for a guided link."""
    conn = get_conn()
    from ..services import guided as guided_svc
    guided_svc.set_options(conn, link_id, bind=body.bind, single_use=body.single_use)
    conn.commit()
    return {"ok": True}


@router.post("/guided/{link_id}/reset-bind")
def guided_reset_bind(link_id: int):
    """Forget the device a locked guided link bound to, so it can start fresh."""
    conn = get_conn()
    from ..services import guided as guided_svc
    guided_svc.reset_bind(conn, link_id)
    conn.commit()
    return {"ok": True}


@router.post("/guided/{link_id}/activate")
def guided_activate(link_id: int):
    """Approval #1: make a draft guided link live for recipients."""
    conn = get_conn()
    from ..services import guided as guided_svc
    guided_svc.activate_spec(conn, link_id)
    conn.commit()
    return {"ok": True}


@router.post("/guided/sessions/{sid}/accept")
def guided_accept(sid: int):
    """Approval #2: write the AI-drafted document into the destination note."""
    conn = get_conn()
    s = conn.execute(
        "SELECT s.*, sl.note_id FROM guided_sessions s JOIN share_links sl ON sl.id=s.share_link_id "
        "WHERE s.id=? AND s.status='submitted'", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="No submitted guided response found.")
    note = conn.execute("SELECT title, slug FROM notes WHERE id=? AND deleted_at IS NULL", (s["note_id"],)).fetchone()
    if note is None:
        raise HTTPException(status_code=409, detail="The destination note no longer exists.")
    notes_svc.upsert_note(conn, note["title"], s["document_md"] or "", note_id=s["note_id"],
                          source="shared", version_note=f"guided intake from {s['name'] or 'a recipient'}")
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    # Approved → the raw conversation has served its review purpose; delete it.
    conn.execute("UPDATE guided_sessions SET transcript_json='[]' WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True, "note_slug": note["slug"]}


@router.post("/guided/sessions/{sid}/reject")
def guided_reject(sid: int):
    """Discard a guided response (nothing is written)."""
    conn = get_conn()
    s = conn.execute("SELECT review_item_id FROM guided_sessions s WHERE id=? AND status='submitted'", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="No submitted guided response found.")
    # Discarded → drop the document and the raw conversation entirely.
    conn.execute("UPDATE guided_sessions SET status='abandoned', document_md=NULL, transcript_json='[]' WHERE id=?", (sid,))
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    conn.commit()
    return {"ok": True}


# --- Research links -----------------------------------------------------------

class MintResearchIn(BaseModel):
    label: str | None = None
    prefixes: list[str] = []
    kinds: list[str] = []
    ttl_days: int | None = None
    bind: bool = False
    single_use: bool = False
    persona_voice: str = ""
    intro: str = ""
    max_turns: int = 30
    max_total_replies: int = 200


def _clean_scope(prefixes, kinds) -> dict:
    return {"prefixes": [p.strip().strip("/") for p in (prefixes or []) if p and p.strip().strip("/")],
            "kinds": [k for k in (kinds or []) if k]}


@router.post("/research/mint")
def research_mint(body: MintResearchIn):
    """Mint a DRAFT research link: an anchor/audit note + the scope spec. Nothing is
    exposed yet — the owner approves candidate notes, then activates."""
    conn = get_conn()
    scope = _clean_scope(body.prefixes, body.kinds)
    if not scope["prefixes"]:
        raise HTTPException(status_code=400, detail="Pick at least one folder to scope the link.")
    label = (body.label or scope["prefixes"][0]).strip()[:80]
    title = notes_svc.root_title(f"Research — {label}", "notes")
    note_id = notes_svc.upsert_note(
        conn, title, f"# {title.split('/')[-1]}\n\n_Anchor for a scoped Q&A research link._\n",
        source="user", version_note="research link anchor", fire_events=False)
    token, link_id = share_svc.create_research_link(conn, note_id, label=label, ttl_days=body.ttl_days, bind=body.bind)
    research_svc.create_spec(conn, link_id, scope_json=scope, persona_voice=body.persona_voice,
                             intro=body.intro, bind=body.bind, single_use=body.single_use,
                             max_turns=body.max_turns, max_total_replies=body.max_total_replies)
    conn.commit()
    return {"link_id": link_id, "token": token, "url": share_svc.share_url(token),
            "candidates": research_svc.list_candidates(conn, link_id)}


@router.get("/research/{link_id}")
def research_detail(link_id: int):
    conn = get_conn()
    spec = research_svc.get_spec(conn, link_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Not found.")
    sessions = conn.execute(
        "SELECT id, name, turn_count, denied_count, retrieved_ids_json, created_at, last_at, status "
        "FROM research_sessions WHERE share_link_id=? ORDER BY created_at DESC LIMIT 50", (link_id,)).fetchall()
    link = conn.execute("SELECT expires_at, bound_at FROM share_links WHERE id=?", (link_id,)).fetchone()
    return {
        "spec": {k: spec[k] for k in ("status", "persona_voice", "topics", "intro", "bind", "single_use",
                                       "max_turns", "max_total_replies", "reply_count")},
        "expires_at": link["expires_at"] if link else None,
        "bound_at": link["bound_at"] if link else None,
        "scope": json.loads(spec["scope_json"] or "{}"),
        "candidates": research_svc.list_candidates(conn, link_id),
        "approved": research_svc.list_approved(conn, link_id),
        "sessions": [{**dict(s), "retrieved": len(json.loads(s["retrieved_ids_json"] or "[]"))} for s in sessions],
    }


class ResearchScopeIn(BaseModel):
    prefixes: list[str] = []
    kinds: list[str] = []


@router.post("/research/{link_id}/scope")
def research_set_scope(link_id: int, body: ResearchScopeIn):
    conn = get_conn()
    research_svc.set_scope(conn, link_id, _clean_scope(body.prefixes, body.kinds))
    conn.commit()
    return {"ok": True, "candidates": research_svc.list_candidates(conn, link_id)}


class ResearchDetailsIn(BaseModel):
    persona_voice: str = ""
    topics: str = ""
    intro: str = ""
    bind: bool = False
    single_use: bool = False
    ttl_days: int = 0                 # 0 = never expires; reset on each save
    max_turns: int = 30
    max_total_replies: int = 200


@router.post("/research/{link_id}/details")
def research_set_details(link_id: int, body: ResearchDetailsIn):
    conn = get_conn()
    research_svc.set_details(conn, link_id, persona_voice=body.persona_voice, topics=body.topics,
                             intro=body.intro, bind=body.bind, single_use=body.single_use,
                             max_turns=body.max_turns, max_total_replies=body.max_total_replies)
    # Expiry lives on the share_link; reset the clock on each save (0 = never).
    exp = f"+{int(body.ttl_days)} days" if (body.ttl_days and int(body.ttl_days) > 0) else None
    conn.execute("UPDATE share_links SET expires_at = %s WHERE id = ?"
                 % ("datetime('now', ?)" if exp else "NULL"),
                 ((exp, link_id) if exp else (link_id,)))
    conn.commit()
    return {"ok": True}


@router.post("/research/{link_id}/reset-bind")
def research_reset_bind(link_id: int):
    """Forget the device a lock-to-browser research link bound to, so it can be
    re-opened on a different device."""
    conn = get_conn()
    share_svc.reset_bind(conn, link_id)
    conn.commit()
    return {"ok": True}


class IdsIn(BaseModel):
    ids: list[int] = []


@router.post("/research/{link_id}/approve")
def research_approve(link_id: int, body: IdsIn):
    conn = get_conn()
    research_svc.approve(conn, link_id, body.ids)
    conn.commit()
    return {"ok": True, "candidates": research_svc.list_candidates(conn, link_id),
            "approved": research_svc.list_approved(conn, link_id)}


@router.post("/research/{link_id}/dismiss")
def research_dismiss(link_id: int, body: IdsIn):
    conn = get_conn()
    research_svc.dismiss(conn, link_id, body.ids)
    conn.commit()
    return {"ok": True, "candidates": research_svc.list_candidates(conn, link_id)}


@router.post("/research/{link_id}/remove")
def research_remove(link_id: int, body: IdsIn):
    conn = get_conn()
    research_svc.remove_approved(conn, link_id, body.ids)
    conn.commit()
    return {"ok": True, "approved": research_svc.list_approved(conn, link_id)}


@router.post("/research/{link_id}/activate")
def research_activate(link_id: int):
    """Make a draft research link live. Refuses if nothing has been approved yet
    (an active link with an empty allowlist would expose nothing but still bill)."""
    conn = get_conn()
    spec = research_svc.get_spec(conn, link_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Not found.")
    if not research_svc.scope.approved_ids(spec):
        raise HTTPException(status_code=400, detail="Approve at least one note before activating.")
    research_svc.activate_spec(conn, link_id)
    conn.commit()
    return {"ok": True}


@router.post("/{link_id}/revoke")
def revoke(link_id: int):
    conn = get_conn()
    share_svc.revoke_link(conn, link_id)
    conn.commit()
    return {"ok": True}


@router.post("/{link_id}/reset-bind")
def reset_bind(link_id: int):
    """Forget the bound browser so a 'bind' link can be opened fresh (e.g. it
    locked to the wrong in-app browser)."""
    conn = get_conn()
    share_svc.reset_bind(conn, link_id)
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
