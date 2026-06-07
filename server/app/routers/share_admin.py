"""Owner-side (AUTHENTICATED) share-link management: mint, list, revoke, and
accept/reject the edit proposals that arrive via public EDIT links."""
import asyncio
import hashlib
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..db import get_conn
from ..services import chat_relay
from ..services import chat_share as chat_svc
from ..services import labshare as labshare_svc
from ..services import notes as notes_svc
from ..services import research as research_svc
from ..services import share as share_svc
from .staging import _apply_action

router = APIRouter(prefix="/api/shares", tags=["shares"], dependencies=[CurrentUser])

_KEEPALIVE_SECONDS = 15.0


def _set_link_expiry(conn, link_id: int, ttl_days: int) -> None:
    """Set/clear a share link's expiry from the host now (0 = never). Shared by the
    view/edit, guided, and research detail editors so expiry behaves identically."""
    exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
    conn.execute("UPDATE share_links SET expires_at = %s WHERE id = ?"
                 % ("datetime('now', ?)" if exp else "NULL"),
                 ((exp, link_id) if exp else (link_id,)))


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
        "FROM share_links sl JOIN guided_specs gs ON gs.share_link_id=sl.id LEFT JOIN notes n ON n.id=sl.note_id "
        "WHERE sl.kind='guided' AND sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()
    guided_pending = conn.execute(
        "SELECT s.id, s.name, s.document_md, s.transcript_json, s.created_at, s.completed_at, gs.goal, "
        "       n.title AS note_title, n.slug AS note_slug "
        "FROM guided_sessions s JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "JOIN share_links sl ON sl.id=s.share_link_id LEFT JOIN notes n ON n.id=sl.note_id "
        "JOIN review_items ri ON ri.id=s.review_item_id "
        "WHERE s.status='submitted' AND ri.status='pending' ORDER BY s.completed_at DESC"
    ).fetchall()
    # Sessions auto-ended for abuse or distress, awaiting the owner's acknowledgement.
    guided_ended = conn.execute(
        "SELECT s.id, s.name, s.end_reason, s.transcript_json, s.completed_at, gs.goal, "
        "       sl.id AS link_id, sl.status AS link_status, n.title AS note_title, n.slug AS note_slug "
        "FROM guided_sessions s JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "JOIN share_links sl ON sl.id=s.share_link_id LEFT JOIN notes n ON n.id=sl.note_id "
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
        "JOIN share_links sl ON sl.id=s.share_link_id LEFT JOIN notes n ON n.id=sl.note_id "
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
        "       rs.bind, rs.single_use, rs.approved_ids_json, rs.lab_analytes_json, rs.reply_count, "
        "       rs.max_total_replies, "
        "       (SELECT COUNT(*) FROM research_sessions s WHERE s.share_link_id=sl.id) AS sessions "
        "FROM share_links sl JOIN research_specs rs ON rs.share_link_id=sl.id "
        "WHERE sl.kind='research' AND sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()

    def _rl(r):
        d = dict(r)
        d["approved_count"] = len(json.loads(d.pop("approved_ids_json") or "[]"))
        d["lab_count"] = len(json.loads(d.pop("lab_analytes_json") or "[]"))
        d["url"] = share_svc.share_url(d["token"])
        return d

    # Lab-share links (the PHI surface) — so the owner can list, copy, audit, and REVOKE them.
    lab_links = conn.execute(
        "SELECT sl.id, sl.token, sl.label, sl.created_at, sl.expires_at, sl.bind, sl.bound_at, "
        "       ls.allow_chat, ls.analytes_json, ls.reply_count, ls.max_total_replies, "
        "       (SELECT COUNT(*) FROM labshare_sessions s WHERE s.share_link_id=sl.id) AS sessions "
        "FROM share_links sl JOIN labshare_specs ls ON ls.share_link_id=sl.id "
        "WHERE sl.kind='labs' AND sl.status='active' ORDER BY sl.created_at DESC"
    ).fetchall()

    def _ll(r):
        d = dict(r)
        d["analyte_count"] = len(json.loads(d.pop("analytes_json") or "[]"))
        d["url"] = share_svc.share_url(d["token"])
        return d

    return {
        "chat_links": chat_svc.list_channels(conn),
        "lab_links": [_ll(r) for r in lab_links],
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


@router.get("/guided/{link_id}/sessions")
def guided_sessions(link_id: int):
    """Owner-only: every recipient session for a guided link — INCLUDING ones still in
    progress (active/drafting) that have no review item yet, so partial or abandoned
    conversations are visible, not just submitted/ended ones. The transcript itself is
    fetched per session below (kept out of this list payload)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, status, end_reason, turn_count, created_at, completed_at, "
        "       (document_md IS NOT NULL AND document_md <> '') AS has_document, "
        "       (transcript_json IS NOT NULL AND LENGTH(transcript_json) > 2) AS has_transcript "
        "FROM guided_sessions WHERE share_link_id = ? ORDER BY created_at DESC LIMIT 100",
        (link_id,),
    ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@router.get("/guided/{link_id}/sessions/{sid}")
def guided_session_transcript(link_id: int, sid: int):
    """Owner-only: read a recipient's full guided transcript (+ any drafted document)
    for this link — works for in-progress, submitted, ended, or abandoned sessions.
    Like the pending/ended views, this is for owner review only: it's never written to
    a note or embedded, so it stays out of brain search."""
    conn = get_conn()
    row = conn.execute(
        "SELECT name, status, end_reason, document_md, transcript_json "
        "FROM guided_sessions WHERE id = ? AND share_link_id = ?", (sid, link_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"name": row["name"], "status": row["status"], "end_reason": row["end_reason"],
            "document_md": row["document_md"], "transcript": json.loads(row["transcript_json"] or "[]")}


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
    # Retain the transcript as an archived record — reopening re-enables the link; a new
    # visit starts a fresh session. (Use "Delete conversation" to purge deliberately.)
    conn.commit()
    return {"ok": True}


@router.post("/guided/sessions/{sid}/acknowledge")
def guided_acknowledge(sid: int):
    """Dismiss an auto-ended (abuse/distress) session without re-opening the link.
    The transcript is kept as a record (delete it explicitly if you want it gone)."""
    conn = get_conn()
    s = conn.execute("SELECT review_item_id FROM guided_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    conn.commit()
    return {"ok": True}


@router.delete("/guided/sessions/{sid}")
def guided_delete_session(sid: int):
    """Permanently delete one guided conversation + its drafted artifact (owner choice).
    Transcripts are kept by default now, so this is the deliberate way to purge one."""
    conn = get_conn()
    if conn.execute("SELECT 1 FROM guided_sessions WHERE id=?", (sid,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    conn.execute("UPDATE guided_sessions SET transcript_json='[]', document_md=NULL WHERE id=?", (sid,))
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


class GuidedDetailsIn(BaseModel):
    goal: str = ""
    intro: str = ""
    sub_prompt: str = ""
    ttl_days: int = 0


@router.post("/guided/{link_id}/details")
def guided_set_details(link_id: int, body: GuidedDetailsIn):
    """Edit a guided link's interview brief + expiry post-creation (parity with
    research). The sensitive-content guard runs here too, so an edit can't slip in
    a request for passwords/IDs."""
    conn = get_conn()
    from ..services import guided as guided_svc
    bad = guided_svc.sensitive_reason(f"{body.goal}\n{body.intro}\n{body.sub_prompt}")
    if bad:
        raise HTTPException(status_code=400, detail=f"Can't ask for sensitive info (matched “{bad}”).")
    if not body.sub_prompt.strip():
        raise HTTPException(status_code=422, detail="The interview instructions can't be empty.")
    guided_svc.set_details(conn, link_id, goal=body.goal, intro=body.intro, sub_prompt=body.sub_prompt)
    _set_link_expiry(conn, link_id, body.ttl_days)
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
        "SELECT s.*, sl.note_id, gs.goal, gs.dest_title FROM guided_sessions s "
        "JOIN share_links sl ON sl.id=s.share_link_id "
        "JOIN guided_specs gs ON gs.share_link_id=s.share_link_id "
        "WHERE s.id=? AND s.status='submitted'", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="No submitted guided response found.")
    who = s["name"] or "a recipient"
    vn = f"guided intake from {who}"
    # Provenance: record WHO contributed so the analyzer treats them as a person entity
    # (a recipe "from mom" links mom into People) and the source is never lost.
    doc = s["document_md"] or ""
    if s["name"] and "_Contributed by" not in doc:
        doc = doc.rstrip() + f"\n\n---\n_Contributed by {s['name']} via guided intake._\n"
    if s["note_id"]:
        # Legacy link that pre-created its note: update it in place.
        note = conn.execute("SELECT title FROM notes WHERE id=? AND deleted_at IS NULL", (s["note_id"],)).fetchone()
        if note is None:
            raise HTTPException(status_code=409, detail="The destination note no longer exists.")
        notes_svc.upsert_note(conn, note["title"], doc, note_id=s["note_id"],
                              source="shared", version_note=vn)
        note_id = s["note_id"]
    else:
        # No page existed until now — create it from the spec's destination title.
        title = notes_svc.root_title(s["dest_title"] or f"Intake — {s['goal']}", "notes")
        note_id = notes_svc.upsert_note(conn, title, doc, create_only=True,
                                        source="shared", version_note=vn)
        conn.execute("UPDATE share_links SET note_id=? WHERE id=?", (note_id, s["share_link_id"]))
    note = conn.execute("SELECT slug FROM notes WHERE id=?", (note_id,)).fetchone()
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    # Approved: the conversation + draft are KEPT as a record. The saved note (note_slug)
    # is the canonical artifact going forward; the session keeps the draft-as-submitted.
    conn.commit()
    return {"ok": True, "note_slug": note["slug"]}


@router.post("/guided/sessions/{sid}/reject")
def guided_reject(sid: int):
    """Discard a guided response (nothing is written to a note). The conversation + the
    drafted document are KEPT as a record — use 'Delete conversation' to purge them."""
    conn = get_conn()
    s = conn.execute("SELECT review_item_id FROM guided_sessions s WHERE id=? AND status='submitted'", (sid,)).fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="No submitted guided response found.")
    conn.execute("UPDATE guided_sessions SET status='abandoned' WHERE id=?", (sid,))
    if s["review_item_id"]:
        conn.execute("UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                     (s["review_item_id"],))
    conn.commit()
    return {"ok": True}


# --- Research links -----------------------------------------------------------

class LabShareIn(BaseModel):
    analytes: list[str] = []
    window_from: str | None = None
    window_to: str | None = None
    allow_chat: bool = True
    intro: str = ""
    label: str | None = None
    # Standardized share options (shared with note/guided/research). PHI defaults differ: bind on,
    # a finite TTL is enforced (a medical link is never a permanent bearer credential).
    bind: bool = True
    single_use: bool = False
    ttl_days: int = 14
    max_turns: int = 30
    max_total_replies: int = 120


@router.post("/labs")
def create_lab_share(body: LabShareIn):
    """Mint + ACTIVATE a lab-share link in one step. The analyte allow-list is the boundary;
    refusing an empty list is enforced in labshare.activate (default-deny)."""
    conn = get_conn()
    analytes = [a for a in (body.analytes or []) if a]
    if not analytes:
        raise HTTPException(status_code=400, detail="Select at least one result to share.")
    token, link_id = labshare_svc.create(
        conn, analytes=analytes, window_from=body.window_from, window_to=body.window_to,
        allow_chat=False, intro=(body.intro or "").strip()[:1000],   # data-only: chat moved to assisted links
        label=(body.label or "").strip()[:80] or None, ttl_days=max(1, body.ttl_days),
        bind=body.bind, single_use=body.single_use,
        max_turns=body.max_turns, max_total_replies=body.max_total_replies)
    if not labshare_svc.activate(conn, link_id):
        raise HTTPException(status_code=400, detail="Couldn't activate (need at least one result).")
    conn.commit()
    return {"token": token, "link_id": link_id, "url": share_svc.share_url(token)}


@router.get("/labs/{link_id}/audit")
def lab_share_audit(link_id: int):
    """Owner audit: per recipient session, which analytes were shown and when."""
    return {"sessions": labshare_svc.audit(get_conn(), link_id)}


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
    link = conn.execute("SELECT expires_at FROM share_links WHERE id=?", (link_id,)).fetchone()
    # "Bound" for research = bind is on AND a device already has an active session.
    bound = bool(spec["bind"]) and conn.execute(
        "SELECT 1 FROM research_sessions WHERE share_link_id=? AND status='active' LIMIT 1",
        (link_id,)).fetchone() is not None
    return {
        "spec": {k: spec[k] for k in ("status", "persona_voice", "topics", "intro", "bind", "single_use",
                                       "max_turns", "max_total_replies", "reply_count")},
        "expires_at": link["expires_at"] if link else None,
        "bound": bound,
        "scope": json.loads(spec["scope_json"] or "{}"),
        "candidates": research_svc.list_candidates(conn, link_id),
        "approved": research_svc.list_approved(conn, link_id),
        # Attached labs scope (owner-only metadata: analyte keys + window, never values).
        "labs": {"analytes": sorted(research_svc.lab_allowed(spec)),
                 "window_from": spec["lab_window_from"] if "lab_window_from" in spec.keys() else None,
                 "window_to": spec["lab_window_to"] if "lab_window_to" in spec.keys() else None},
        "sessions": [{**dict(s), "retrieved": len(json.loads(s["retrieved_ids_json"] or "[]"))} for s in sessions],
    }


@router.get("/research/{link_id}/sessions/{sid}")
def research_session_transcript(link_id: int, sid: int):
    """Owner-only: read a recipient's Q&A transcript for this link (parity with
    guided). It's the owner's own notes being queried, so the exchange is visible."""
    conn = get_conn()
    row = conn.execute("SELECT name, transcript_json FROM research_sessions WHERE id=? AND share_link_id=?",
                       (sid, link_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"name": row["name"], "transcript": json.loads(row["transcript_json"] or "[]")}


@router.delete("/research/{link_id}/sessions/{sid}")
def research_delete_session(link_id: int, sid: int):
    """Permanently delete one research Q&A conversation (owner choice)."""
    conn = get_conn()
    if conn.execute("SELECT 1 FROM research_sessions WHERE id=? AND share_link_id=?", (sid, link_id)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    conn.execute("UPDATE research_sessions SET transcript_json='[]' WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


class ResearchScopeIn(BaseModel):
    prefixes: list[str] = []
    kinds: list[str] = []


@router.post("/research/{link_id}/scope")
def research_set_scope(link_id: int, body: ResearchScopeIn):
    conn = get_conn()
    research_svc.set_scope(conn, link_id, _clean_scope(body.prefixes, body.kinds))
    conn.commit()
    return {"ok": True, "candidates": research_svc.list_candidates(conn, link_id)}


class ResearchLabsIn(BaseModel):
    analytes: list[str] = []
    window_from: str | None = None
    window_to: str | None = None


@router.post("/research/{link_id}/labs")
def research_set_labs(link_id: int, body: ResearchLabsIn):
    """Attach/adjust the scoped LABS allow-list + date window on an assisted (research) link.
    An empty list detaches labs. Default-deny: nothing is exposed until the link is activated,
    and the same research_specs.status gate governs it. Narrowing takes effect on the next read."""
    conn = get_conn()
    if research_svc.get_spec(conn, link_id) is None:
        raise HTTPException(status_code=404, detail="Not found.")
    research_svc.set_lab_scope(conn, link_id, analytes=body.analytes,
                               window_from=body.window_from, window_to=body.window_to)
    conn.commit()
    return {"ok": True, "labs": sorted({str(a).strip() for a in body.analytes if str(a).strip()})}


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
    _set_link_expiry(conn, link_id, body.ttl_days)   # reset the clock on each save (0 = never)
    conn.commit()
    return {"ok": True}


@router.post("/research/{link_id}/reset-bind")
def research_reset_bind(link_id: int):
    """Forget the device a lock-to-browser research link bound to, so it can be
    re-opened on a different device. Research binds via the first ACTIVE session's
    secret (not share_links), so abandon active sessions — mirrors guided reset-bind."""
    conn = get_conn()
    conn.execute("UPDATE research_sessions SET status='ended' "
                 "WHERE share_link_id=? AND status='active'", (link_id,))
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
    if not research_svc.scope.approved_ids(spec) and not research_svc.lab_allowed(spec):
        raise HTTPException(status_code=400, detail="Approve at least one note or lab result before activating.")
    research_svc.activate_spec(conn, link_id)
    conn.commit()
    return {"ok": True}


# --- Encrypted chat (kind='chat') — owner side --------------------------------
# The channel key is sealed client-side; the owner's device unseals owner_wrap with a key
# derived from the access key. The server only relays + persists opaque ciphertext.

class ChatCreateIn(BaseModel):
    owner_wrap: str = Field(max_length=4000)     # channel key sealed under the access-key KEK {salt,iv,ct}
    guest_wrap: str = Field(max_length=4000)     # channel key sealed under the link-fragment secret [+ OTP]
    persist: bool = True
    otp_required: bool = False
    label: str | None = Field(default=None, max_length=80)
    ttl_days: int | None = None
    single_use: bool = False


@router.post("/chat")
def chat_create(body: ChatCreateIn):
    """Mint an encrypted-chat link + its channel. Returns the token (the client appends the
    secret fragment locally) so the raw key never transits the server."""
    conn = get_conn()
    token, link_id = chat_svc.create_channel(
        conn, owner_wrap=body.owner_wrap, guest_wrap=body.guest_wrap,
        persist=body.persist, otp_required=body.otp_required,
        label=(body.label or "").strip()[:80] or None, ttl_days=body.ttl_days, single_use=body.single_use)
    conn.commit()
    return {"token": token, "link_id": link_id, "url": share_svc.share_url(token)}


@router.get("/chat/{link_id}")
def chat_detail(link_id: int):
    return chat_svc.owner_detail(get_conn(), link_id)


@router.get("/chat/{link_id}/stream")
async def chat_owner_stream(link_id: int, after: int = 0):
    """Owner SSE: replay the (persisted) backlog after `after`, then live messages + presence."""
    conn = get_conn()
    ch = chat_svc.get_channel(conn, link_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    persist = bool(ch["persist"])

    async def gen():
        hub, sub = await chat_relay.subscribe(link_id, "owner")
        chat_svc.handle_presence(conn, link_id)
        try:
            if persist:
                for ev in chat_svc.backlog(conn, link_id, after):
                    yield chat_svc.sse(ev)
            owner_p, guest_p = chat_relay.present(link_id)
            yield chat_svc.sse({"type": "presence", "owner": owner_p, "guest": guest_p})
            if ch["status"] == "closed":
                yield chat_svc.sse({"type": "closed"})
                return
            while True:
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield chat_svc.sse(ev)
                if ev.get("type") == "closed":
                    break
        finally:
            chat_relay.unsubscribe(hub, sub)
            chat_svc.handle_presence(conn, link_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ChatSendIn(BaseModel):
    iv: str = Field(max_length=64)
    ct: str = Field(max_length=700_000)


@router.post("/chat/{link_id}/send")
def chat_owner_send(link_id: int, body: ChatSendIn):
    ev = chat_svc.append_message(get_conn(), link_id, "owner", body.iv, body.ct)
    return {"ok": True, "seq": ev["seq"]}


@router.post("/chat/{link_id}/file")
def chat_owner_file(link_id: int, iv: str = Form(...), file: UploadFile = File(...)):
    fid = chat_svc.store_file(get_conn(), link_id, iv[:64], file.file.read())
    return {"ok": True, "file_id": fid}


@router.get("/chat/{link_id}/file/{file_id}")
def chat_owner_file_download(link_id: int, file_id: int):
    row = chat_svc.get_file(get_conn(), link_id, file_id)
    return Response(content=bytes(row["blob"]), media_type="application/octet-stream",
                    headers={"X-Chat-IV": row["iv"], "Cache-Control": "no-store"})


@router.post("/chat/{link_id}/close")
def chat_close(link_id: int):
    chat_svc.close_channel(get_conn(), link_id)
    return {"ok": True}


class ChatSaveIn(BaseModel):
    transcript_md: str = Field(max_length=2_000_000)
    title: str | None = Field(default=None, max_length=120)
    guest_name: str | None = Field(default=None, max_length=80)
    # Each attachment is {name, mime, data} where data is base64 of the DECRYPTED bytes.
    attachments: list[dict] = []


@router.post("/chat/{link_id}/save")
def chat_save(link_id: int, body: ChatSaveIn):
    """Commit the owner's client-decrypted transcript into the brain (auto-save on close).
    Idempotent per channel."""
    conn = get_conn()
    out = chat_svc.save_to_brain(conn, link_id, transcript_md=body.transcript_md, title=body.title,
                                 attachments=body.attachments, guest_name=body.guest_name)
    return {"ok": True, **out}


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


class ExpiryIn(BaseModel):
    ttl_days: int = 0


@router.post("/{link_id}/expiry")
def set_expiry(link_id: int, body: ExpiryIn):
    """Edit a view/edit link's expiry post-creation (0 = never) — parity with the
    AI links, which can already edit expiry via their details panels."""
    conn = get_conn()
    _set_link_expiry(conn, link_id, body.ttl_days)
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
