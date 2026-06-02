"""PUBLIC, UNAUTHENTICATED share endpoints — the only routes with no CurrentUser.

Every handler resolves the token to exactly one note via share_svc.resolve_active_link
and exposes only that note. There is no note id/slug parameter anywhere here, so a
token can never reach another note. Keep this file small and auditable.
"""
import hmac
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import get_conn
from ..services import attachments as att_svc
from ..services import guided as guided_svc
from ..services import research as research_svc
from ..services import share as share_svc

router = APIRouter(prefix="/api/share", tags=["share"])   # NO dependencies=[CurrentUser]


def _client_ip(request: Request) -> str:
    """Real client IP — honor X-Forwarded-For so the rate limit isn't keyed on the
    reverse proxy's address (which would make it one global bucket)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


class ProposeIn(BaseModel):
    content_md: str = Field(max_length=400_000)
    name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=2000)


def _resolve_or_404(conn, request: Request, token: str):
    if share_svc.rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests; slow down.")
    link = share_svc.resolve_active_link(conn, token)
    if link is None:
        raise HTTPException(status_code=404, detail="This link isn't available.")
    return link


class ClaimIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)


def _bind_status(link, request: Request) -> str:
    """For a 'bind' link: 'ok' (this browser holds the matching cookie), 'unclaimed'
    (nobody has accepted it yet), or 'locked' (a different browser accepted it).
    Non-bind links are always 'open'."""
    if not link["bind"]:
        return "open"
    secret = link["bind_secret"]
    if not secret:
        return "unclaimed"
    cookie = request.cookies.get(f"jb_bind_{link['id']}")
    return "ok" if (cookie and hmac.compare_digest(cookie, secret)) else "locked"


def _require_access(link, request: Request) -> None:
    """Reads of content / proposals / attachments require a non-bind link or one
    already accepted by THIS browser."""
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This link is locked to the browser that accepted it.")
    if st == "unclaimed":
        raise HTTPException(status_code=403, detail="Open the link and accept it first.")


def _bind_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    response.set_cookie(f"jb_bind_{link_id}", secret, max_age=31_536_000, httponly=True,
                        samesite="lax", secure=secure, path=f"/api/share/{token}")


def _note_payload(conn, link) -> dict:
    from ..services import clock
    atts = att_svc.list_for_note(conn, link["note_id"])
    return {
        "scope": link["scope"],
        "can_edit": link["scope"] == "edit",
        "brain_name": get_settings().brain_name,
        "app_tz": clock.app_tz_name(),
        "bound_name": link["bound_name"],
        "note": {
            "title": link["title"],
            "content_md": link["content_md"],
            "kind": link["kind"],
            "updated_at": link["updated_at"],
            "attachments": [{"id": a["id"], "filename": a["filename"],
                             "mime": a["mime"], "byte_size": a["byte_size"]} for a in atts],
        },
    }


@router.get("/{token}")
def share_read(token: str, request: Request):
    """A bind link not yet accepted by this browser returns {requires_claim} (no
    content) so the page can show the consent landing; otherwise the note itself.
    Withholds backlinks, tags, geolocation, slug, and id."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    if link["kind"] == "guided":
        return _guided_landing(conn, link)   # NEVER returns note content
    if link["kind"] == "research":
        return _research_landing(conn, link)   # NEVER returns note content
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This link is locked to the browser that accepted it.")
    if st == "unclaimed":
        return {"requires_claim": True, "scope": link["scope"],
                "can_edit": link["scope"] == "edit", "brain_name": get_settings().brain_name}
    share_svc.touch(conn, link["id"]); conn.commit()
    return _note_payload(conn, link)


@router.post("/{token}/claim")
def share_claim(token: str, body: ClaimIn, request: Request, response: Response):
    """Accept a bind link: lock it to THIS browser (cookie), store the name for
    edit links, and return the note. Idempotent for the already-bound browser."""
    conn = get_conn()
    # Claim is the binding action; reject cross-site forged POSTs (drive-by claim).
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    link = _resolve_or_404(conn, request, token)
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This link is locked to the browser that accepted it.")
    if st == "unclaimed":
        secret = share_svc.mint_token()
        cur = conn.execute(
            "UPDATE share_links SET bind_secret=?, bound_at=datetime('now'), bound_name=? "
            "WHERE id=? AND bind_secret IS NULL",
            (secret, (body.name or "").strip()[:80] or None, link["id"]))
        conn.commit()
        if cur.rowcount != 1:    # another browser accepted in the race
            raise HTTPException(status_code=403, detail="This link was just accepted on another browser.")
        _bind_cookie(response, token, link["id"], secret)
        link = share_svc.resolve_active_link(conn, token)   # re-read so bound_name is included
    share_svc.touch(conn, link["id"]); conn.commit()
    return _note_payload(conn, link)


# --- Guided AI intake (kind='guided') ---------------------------------------
# A recipient is interviewed by an isolated AI (guided_svc) that has NO brain
# access. The owner-injected consent + disclaimer are non-editable.

def _guided_landing(conn, link) -> dict:
    spec = guided_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return {
        "kind": "guided",
        "brain_name": get_settings().brain_name,
        "owner": get_settings().brain_name,
        "intro": spec["intro"],
        "goal": spec["goal"],
        # Server-injected, non-editable consent + disclaimer:
        "consent": (f"You’re chatting with an AI assistant set up by {get_settings().brain_name} "
                    "to gather some information. Your conversation is shared with them and "
                    "reviewed before anything is saved. This is not professional advice."),
    }


def _guided_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    response.set_cookie(f"jb_guided_{link_id}", secret, max_age=7 * 24 * 3600, httponly=True,
                        samesite="lax", secure=secure, path=f"/api/share/{token}")


def _resolve_guided(conn, request, token):
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "guided":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = guided_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active" or not llm_ready():
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return link, spec


def llm_ready() -> bool:
    from ..services import llm
    return llm.has_credentials()


class GuidedStartIn(BaseModel):
    name: str | None = None


class GuidedTurnIn(BaseModel):
    message: str = Field("", max_length=8000)


@router.post("/{token}/guided/start")
def guided_start(token: str, body: GuidedStartIn, request: Request, response: Response):
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_guided(conn, request, token)
    # Resume an existing session for this browser if present.
    existing = guided_svc.find_session(conn, link["id"], request.cookies.get(f"jb_guided_{link['id']}"))
    if existing and existing["status"] in ("active", "drafting"):
        import json as _json
        transcript = _json.loads(existing["transcript_json"] or "[]")
        return {"resumed": True, "name": existing["name"], "transcript": transcript,
                "document": existing["document_md"],
                "phase": "review" if existing["status"] == "drafting" else "asking"}
    sid, secret = guided_svc.start_session(conn, link, spec, body.name, _client_ip(request),
                                           request.cookies.get(f"jb_guided_{link['id']}"))
    conn.commit()
    session = conn.execute("SELECT * FROM guided_sessions WHERE id = ?", (sid,)).fetchone()
    out = guided_svc.first_message(conn, link, spec, session)
    _guided_cookie(response, token, link["id"], secret)
    return out


@router.post("/{token}/guided/turn")
def guided_turn(token: str, body: GuidedTurnIn, request: Request):
    # The turn endpoint reads the session cookie and bills an LLM call — reject
    # cross-site forged POSTs (drive-by session abuse), like start/claim do.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_guided(conn, request, token)
    session = guided_svc.find_session(conn, link["id"], request.cookies.get(f"jb_guided_{link['id']}"))
    if session is None or session["status"] not in ("active", "drafting"):
        raise HTTPException(status_code=409, detail="Your session has ended — reload to start over.")
    return guided_svc.advance(conn, link, spec, session, body.message)


@router.post("/{token}/guided/submit")
def guided_submit(token: str, request: Request):
    conn = get_conn()
    link, spec = _resolve_guided(conn, request, token)
    session = guided_svc.find_session(conn, link["id"], request.cookies.get(f"jb_guided_{link['id']}"))
    if session is None:
        raise HTTPException(status_code=409, detail="Your session has ended — reload to start over.")
    return guided_svc.submit(conn, link, spec, session)


# --- Research links (kind='research') ---------------------------------------
# A recipient asks questions answered by a scope-bounded AI (research_svc). The AI
# reads ONLY the spec's approved note allowlist; it never returns note content here.

def _research_landing(conn, link) -> dict:
    spec = research_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return {
        "kind": "research",
        "brain_name": get_settings().brain_name,
        "owner": get_settings().brain_name,
        "intro": spec["intro"],
        "consent": (f"You’re chatting with an AI assistant set up by {get_settings().brain_name} that "
                    "can answer questions from a specific set of records they’ve shared. It only "
                    "reads what they approved, and conversations are logged for them. Not professional advice."),
    }


def _research_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    # samesite='strict': the cookie never rides a cross-site navigation (tighter than guided's 'lax').
    response.set_cookie(f"jb_research_{link_id}", secret, max_age=7 * 24 * 3600, httponly=True,
                        samesite="strict", secure=secure, path=f"/api/share/{token}")


def _resolve_research(conn, request, token):
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "research":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = research_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active" or not llm_ready():
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return link, spec


class ResearchStartIn(BaseModel):
    name: str | None = None


class ResearchTurnIn(BaseModel):
    message: str = Field("", max_length=8000)


@router.post("/{token}/research/start")
def research_start(token: str, body: ResearchStartIn, request: Request, response: Response):
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_research(conn, request, token)
    existing = research_svc.find_session(conn, link["id"], request.cookies.get(f"jb_research_{link['id']}"))
    if existing and existing["status"] == "active":
        import json as _json
        return {"resumed": True, "name": existing["name"],
                "transcript": _json.loads(existing["transcript_json"] or "[]")}
    sid, secret = research_svc.start_session(conn, link, spec, body.name, _client_ip(request),
                                             request.cookies.get(f"jb_research_{link['id']}"))
    conn.commit()
    _research_cookie(response, token, link["id"], secret)
    return {"name": (body.name or "").strip()[:80] or None, "transcript": []}


@router.post("/{token}/research/turn")
def research_turn(token: str, body: ResearchTurnIn, request: Request):
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_research(conn, request, token)
    session = research_svc.find_session(conn, link["id"], request.cookies.get(f"jb_research_{link['id']}"))
    if session is None or session["status"] != "active":
        raise HTTPException(status_code=409, detail="Your session has ended — reload to start over.")
    return research_svc.answer(conn, link, spec, session, body.message)


# Only these render inline on the public page; everything else (esp. SVG/HTML,
# which can carry script) is forced to download as an opaque blob so it can never
# execute on this origin.
_SAFE_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.get("/{token}/attachments/{att_id}")
def share_attachment(token: str, att_id: int, request: Request):
    """Serve an attachment — only if it belongs to THIS token's note. Hardened:
    nosniff + restrictive CSP, and only image types render inline (others download)."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    _require_access(link, request)   # bind links must be accepted by this browser first
    row = conn.execute(
        "SELECT filename, mime, content_text, content_blob FROM attachments WHERE id=? AND note_id=?",
        (att_id, link["note_id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    data = bytes(row["content_blob"]) if row["content_blob"] is not None else (row["content_text"] or "").encode()
    inline = (row["mime"] or "") in _SAFE_INLINE
    fn = (row["filename"] or "file").replace('"', "")
    return Response(
        content=data,
        media_type=row["mime"] if inline else "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{fn}"',
        },
    )


@router.post("/{token}/propose")
def share_propose(token: str, body: ProposeIn, request: Request):
    """EDIT links only: store a proposed new content as a pending proposal for the
    owner to accept. Never writes the note."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    _require_access(link, request)
    name = link["bound_name"] or body.name   # reuse the name given when the link was accepted
    try:
        r = share_svc.submit_proposal(conn, link, body.content_md, body.note, name, _client_ip(request))
        conn.commit()
    except sqlite3.IntegrityError:
        # Two proposals raced the one-pending-per-link index — clean retry, not a 500.
        conn.rollback()
        raise HTTPException(status_code=409, detail="Someone else just proposed a change — reload and try again.")
    except Exception:
        conn.rollback()
        raise
    # Post-commit, fire-and-forget: notify the owner (banner + badge, even if the
    # app is closed). Generic body — push banners show on the lock screen.
    from ..services import push
    push.notify_review_created("JBrain", "New edit proposal to review")
    return {"ok": True, **r}
