"""PUBLIC, UNAUTHENTICATED share endpoints — the only routes with no CurrentUser.

Every handler resolves the token to exactly one note via share_svc.resolve_active_link
and exposes only that note. There is no note id/slug parameter anywhere here, so a
token can never reach another note. Keep this file small and auditable.
"""
import asyncio
import hmac
import sqlite3

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import get_conn
from ..services import attachments as att_svc
from ..services import chat_relay
from ..services import chat_share as chat_svc
from ..services import guided as guided_svc
from ..services import lab_share_scope
from ..services import labshare as labshare_svc
from ..services import research as research_svc
from ..services import share as share_svc

router = APIRouter(prefix="/api/share", tags=["share"])   # NO dependencies=[CurrentUser]


def _client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For.

    Without this, the rate limit would key on the reverse proxy's address and
    become one shared global bucket.

    Args:
        request: The incoming FastAPI/Starlette request.

    Returns:
        The leftmost IP from X-Forwarded-For, or request.client.host as fallback.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


class ProposeIn(BaseModel):
    """Request body for submitting an edit proposal on a shared note."""

    content_md: str = Field(max_length=400_000)
    name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=2000)


def _resolve_or_404(conn, request: Request, token: str):
    """Resolve a share token to its link row, enforcing rate limits.

    Args:
        conn: Active database connection.
        request: The incoming request (used for IP-based rate limiting).
        token: Share link token from the URL path.

    Returns:
        The share_links row for the active link.

    Raises:
        HTTPException: 429 if the client IP is rate-limited.
        HTTPException: 404 if the token resolves to no active link.
    """
    if share_svc.rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests; slow down.")
    link = share_svc.resolve_active_link(conn, token)
    if link is None:
        raise HTTPException(status_code=404, detail="This link isn't available.")
    return link


class ClaimIn(BaseModel):
    """Request body for claiming (accepting) a bind-locked share link."""

    name: str | None = Field(default=None, max_length=80)


def _bind_status(link, request: Request) -> str:
    """Return the bind state of a share link for the requesting browser.

    Args:
        link: Share link row with 'bind', 'bind_secret', and 'id' fields.
        request: The incoming request (used to read the jb_bind_ cookie).

    Returns:
        'open' for non-bind links; 'unclaimed' if no browser has accepted yet;
        'ok' if this browser holds the matching cookie; 'locked' if a different
        browser accepted it.
    """
    if not link["bind"]:
        return "open"
    secret = link["bind_secret"]
    if not secret:
        return "unclaimed"
    cookie = request.cookies.get(f"jb_bind_{link['id']}")
    return "ok" if (cookie and hmac.compare_digest(cookie, secret)) else "locked"


def _require_access(link, request: Request) -> None:
    """Assert that the requesting browser may read content from this link.

    Reads of content, proposals, and attachments require a non-bind link or one
    already accepted by this browser.

    Args:
        link: Share link row.
        request: The incoming request.

    Raises:
        HTTPException: 403 if the link is locked to a different browser.
        HTTPException: 403 if the link is unclaimed (not yet accepted).
    """
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This link is locked to the browser that accepted it.")
    if st == "unclaimed":
        raise HTTPException(status_code=403, detail="Open the link and accept it first.")


def _bind_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    """Set the bind cookie that locks this browser to the share link.

    Args:
        response: The outgoing response object.
        token: Share token (used to scope the cookie path).
        link_id: Primary key of the share link (used as the cookie name suffix).
        secret: HMAC-comparable secret to store in the cookie.
    """
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    response.set_cookie(f"jb_bind_{link_id}", secret, max_age=31_536_000, httponly=True,
                        samesite="lax", secure=secure, path=f"/api/share/{token}")


def _note_payload(conn, link) -> dict:
    """Build the public note payload for a resolved share link.

    Withholds backlinks, tags, geolocation, slug, and id. Includes attachment metadata.

    Args:
        conn: Active database connection.
        link: Resolved share link row with note fields joined in.

    Returns:
        Dict with scope, can_edit, brain_name, app_tz, bound_name, and a note sub-dict.
    """
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
    """Resolve a share token and return the appropriate content or consent landing.

    A bind link not yet accepted by this browser returns {requires_claim: true} with
    no content, so the page can show the consent landing. Accepted bind links and
    non-bind links return the note payload. Withholds backlinks, tags, geolocation,
    slug, and id. Guided, research, labs, and chat tokens dispatch to their own
    landing helpers and never return note content.

    Args:
        token: Share link token from the URL path.
        request: The incoming request (bind cookie and rate-limit checks).

    Returns:
        Note payload, link-kind landing dict, or {requires_claim: true}.

    Raises:
        HTTPException: 429 if the client is rate-limited.
        HTTPException: 403 if the link is locked to a different browser.
        HTTPException: 404 if the token is not active.
    """
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    if link["kind"] == "guided":
        return _guided_landing(conn, link)   # NEVER returns note content
    if link["kind"] == "research":
        return _research_landing(conn, link)   # NEVER returns note content
    if link["kind"] == "labs":
        return _labs_landing(conn, link)       # consent only; charts/series need a started session
    if link["kind"] == "chat":
        return _chat_landing(conn, link, request)   # E2EE channel; key material rides the URL fragment
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
    """Accept a bind link: lock it to this browser via cookie and return the note.

    Stores the recipient's name for edit links. Idempotent for the browser that
    already holds the bind cookie. Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: Optional recipient name (stored on edit links).
        request: The incoming request (sec-fetch-site and bind cookie checks).
        response: Used to set the bind cookie.

    Returns:
        The note payload.

    Raises:
        HTTPException: 403 if the request is cross-site, the link is locked to another
            browser, or a race caused another browser to claim it first.
        HTTPException: 429 if the client is rate-limited.
        HTTPException: 404 if the token is not active.
    """
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
    """Build the public guided-link landing payload (consent + goal; no note content).

    Args:
        conn: Active database connection.
        link: Resolved share link row.

    Returns:
        Dict with kind, brain_name, owner, intro, goal, llm_ready, and consent text.

    Raises:
        HTTPException: 404 if the spec is missing or not yet activated.
    """
    spec = guided_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return {
        "kind": "guided",
        "brain_name": get_settings().brain_name,
        "owner": get_settings().brain_name,
        "intro": spec["intro"],
        "goal": spec["goal"],
        # So the public recipient page can show "temporarily unavailable" instead of a
        # chat that 404s at start/turn (same has_credentials() predicate as the dot).
        "llm_ready": llm_ready(),
        # Server-injected, non-editable consent + disclaimer:
        "consent": (f"You’re chatting with an AI assistant set up by {get_settings().brain_name} "
                    "to gather some information. Your conversation is shared with them and "
                    "reviewed before anything is saved. This is not professional advice."),
    }


def _guided_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    """Set the per-session guided cookie that ties this browser to its guided session.

    Args:
        response: The outgoing response object.
        token: Share token (used to scope the cookie path).
        link_id: Primary key of the guided share link.
        secret: Session secret for resumption.
    """
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    response.set_cookie(f"jb_guided_{link_id}", secret, max_age=7 * 24 * 3600, httponly=True,
                        samesite="lax", secure=secure, path=f"/api/share/{token}")


def _resolve_guided(conn, request, token):
    """Resolve a token to an active guided link and spec, asserting LLM availability.

    Args:
        conn: Active database connection.
        request: The incoming request (rate-limit check).
        token: Share link token from the URL path.

    Returns:
        Tuple of (link row, guided_specs row).

    Raises:
        HTTPException: 404 if the token is not a guided link, the spec is missing,
            the spec is not active, or the LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "guided":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = guided_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active" or not llm_ready():
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return link, spec


def llm_ready() -> bool:
    """Return True if the LLM service has credentials configured.

    Returns:
        True if credentials are present and the LLM is usable; False otherwise.
    """
    from ..services import llm
    return llm.has_credentials()


class GuidedStartIn(BaseModel):
    """Request body for starting a guided session."""

    name: str | None = None


class GuidedTurnIn(BaseModel):
    """Request body for advancing a guided session by one turn."""

    message: str = Field("", max_length=8000)


@router.post("/{token}/guided/start")
def guided_start(token: str, body: GuidedStartIn, request: Request, response: Response):
    """Start or resume a guided AI-interview session on a shared guided link.

    Resumes an existing active or drafting session for this browser when one is found.
    Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: Optional recipient name.
        request: The incoming request (sec-fetch-site, guided session cookie).
        response: Used to set the guided session cookie.

    Returns:
        First-message payload from guided_svc, or a resumed-session dict.

    Raises:
        HTTPException: 403 if the request is cross-site.
        HTTPException: 404 if the link is not an active guided link or LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
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
    """Advance a guided session by one recipient message, triggering an LLM response.

    Rejects cross-site forged POSTs to prevent drive-by session abuse. The session
    cookie is required; a missing or ended session returns 409.

    Args:
        token: Share link token from the URL path.
        body: Recipient's message text (max 8000 chars).
        request: The incoming request (sec-fetch-site and session cookie).

    Returns:
        Next-turn payload from guided_svc.advance.

    Raises:
        HTTPException: 403 if the request is cross-site.
        HTTPException: 409 if the session has ended or is not found.
        HTTPException: 404 if the link is not an active guided link or LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
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
    """Submit a completed guided session for owner review.

    Args:
        token: Share link token from the URL path.
        request: The incoming request (session cookie).

    Returns:
        Submit result from guided_svc.submit.

    Raises:
        HTTPException: 409 if the session has ended or is not found.
        HTTPException: 404 if the link is not an active guided link or LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
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
    """Build the public research-link landing payload (consent + intro; no note content).

    Args:
        conn: Active database connection.
        link: Resolved share link row.

    Returns:
        Dict with kind, brain_name, owner, intro, llm_ready, has_labs, and consent text.

    Raises:
        HTTPException: 404 if the spec is missing or not yet activated.
    """
    spec = research_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return {
        "kind": "research",
        "brain_name": get_settings().brain_name,
        "owner": get_settings().brain_name,
        "intro": spec["intro"],
        # See _guided_landing: lets the recipient page gate on AI availability up front.
        "llm_ready": llm_ready(),
        # When labs are attached the assistant can pull up specific shared results on demand; the
        # recipient page uses this only for the affordance — no charts/analytes are dumped up front.
        "has_labs": bool(research_svc.lab_allowed(spec)),
        "consent": (f"You’re chatting with an AI assistant set up by {get_settings().brain_name} that "
                    "can answer questions from a specific set of records they’ve shared. It only "
                    "reads what they approved, and conversations are logged for them. Not professional advice."),
    }


def _research_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    """Set the per-session research cookie that ties this browser to its research session.

    Uses samesite='strict' — tighter than guided's 'lax' — so the cookie never rides
    a cross-site navigation.

    Args:
        response: The outgoing response object.
        token: Share token (used to scope the cookie path).
        link_id: Primary key of the research share link.
        secret: Session secret for resumption.
    """
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    # samesite='strict': the cookie never rides a cross-site navigation (tighter than guided's 'lax').
    response.set_cookie(f"jb_research_{link_id}", secret, max_age=7 * 24 * 3600, httponly=True,
                        samesite="strict", secure=secure, path=f"/api/share/{token}")


def _resolve_research(conn, request, token):
    """Resolve a token to an active research link and spec, asserting LLM availability.

    Args:
        conn: Active database connection.
        request: The incoming request (rate-limit check).
        token: Share link token from the URL path.

    Returns:
        Tuple of (link row, research_specs row).

    Raises:
        HTTPException: 404 if the token is not a research link, the spec is missing,
            the spec is not active, or the LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "research":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = research_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active" or not llm_ready():
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return link, spec


class ResearchStartIn(BaseModel):
    """Request body for starting a research Q&A session."""

    name: str | None = None


class ResearchTurnIn(BaseModel):
    """Request body for advancing a research session by one turn."""

    message: str = Field("", max_length=8000)


@router.post("/{token}/research/start")
def research_start(token: str, body: ResearchStartIn, request: Request, response: Response):
    """Start or resume a scoped research Q&A session on a shared research link.

    Resumes an existing active session for this browser when one is found.
    Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: Optional recipient name.
        request: The incoming request (sec-fetch-site, research session cookie).
        response: Used to set the research session cookie.

    Returns:
        Dict with name and transcript (empty list for a new session, or the
        existing transcript if resumed).

    Raises:
        HTTPException: 403 if the request is cross-site.
        HTTPException: 404 if the link is not an active research link or LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
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
    """Advance a research session by one recipient message, triggering a scoped AI response.

    Rejects cross-site forged POSTs. The AI reads only the spec's approved note
    allowlist; it never returns raw note content here.

    Args:
        token: Share link token from the URL path.
        body: Recipient's question text (max 8000 chars).
        request: The incoming request (sec-fetch-site and session cookie).

    Returns:
        Answer payload from research_svc.answer.

    Raises:
        HTTPException: 403 if the request is cross-site.
        HTTPException: 409 if the session has ended or is not found.
        HTTPException: 404 if the link is not an active research link or LLM is unavailable.
        HTTPException: 429 if the client is rate-limited.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_research(conn, request, token)
    session = research_svc.find_session(conn, link["id"], request.cookies.get(f"jb_research_{link['id']}"))
    if session is None or session["status"] != "active":
        raise HTTPException(status_code=409, detail="Your session has ended — reload to start over.")
    return research_svc.answer(conn, link, spec, session, body.message)


@router.get("/{token}/research/labs/series")
def research_labs_series(token: str, analyte: str, request: Request):
    """Return the scoped time-series for one analyte attached to a research link.

    The analyte must appear in the link's labs allow-list (independently re-checked via
    lab_share_scope, so a forged or jailbroken chart spec still 404s). Results are
    identity-stripped, clamped to the owner's date window, and never cached. Requires an
    active research session on this browser (samesite=strict jb_research cookie).

    Args:
        token: Share link token from the URL path.
        analyte: Analyte key to fetch series data for.
        request: The incoming request (session cookie and rate-limit check).

    Returns:
        JSON series data (no-store, no-cache headers).

    Raises:
        HTTPException: 403 if no active session exists for this browser.
        HTTPException: 404 if the link, spec, or analyte is not available.
        HTTPException: 429 if the client is rate-limited.
    """
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "research":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = research_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    session = research_svc.find_session(conn, link["id"], request.cookies.get(f"jb_research_{link['id']}"))
    if session is None or session["status"] != "active":
        raise HTTPException(status_code=403, detail="Start the session first.")
    s = lab_share_scope.series_scoped(conn, analyte, allowed=research_svc.lab_allowed(spec),
                                      dfrom=spec["lab_window_from"], dto=spec["lab_window_to"])
    if s is None:                                          # out-of-scope analyte — defense in depth
        raise HTTPException(status_code=404, detail="Not available.")
    return JSONResponse(s, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


# --- Lab-share links (kind='labs') ------------------------------------------
# A recipient views a SCOPED set of lab trend charts (+ optional scoped AI chat). All lab data
# comes through lab_share_scope (allow-list + identity-stripped); series responses are no-store.

def _labs_landing(conn, link) -> dict:
    """Build the public lab-share landing payload (consent + intro; no analyte values).

    Standalone labs shares are data-only; allow_chat is reported False unconditionally
    so legacy chat-enabled links degrade to charts/table only.

    Args:
        conn: Active database connection.
        link: Resolved share link row.

    Returns:
        Dict with kind, brain_name, owner, intro, allow_chat (always False), and consent text.

    Raises:
        HTTPException: 404 if the spec is missing or not yet activated.
    """
    spec = labshare_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return {
        "kind": "labs",
        "brain_name": get_settings().brain_name,
        "owner": get_settings().brain_name,
        "intro": spec["intro"],
        # Standalone labs shares are DATA-ONLY now — the scoped AI lives in assisted (research)
        # links. Reported False unconditionally so legacy chat-enabled labs links degrade to
        # charts/table only (no broken composer); /labs/turn is also hard-disabled below.
        "allow_chat": False,
        "consent": (f"You’re viewing a selection of lab results {get_settings().brain_name} chose to share. "
                    "It’s a fixed selection, not their full records, and may not be current. "
                    "This is not medical advice or a diagnosis. Views are logged for them."),
    }


def _labs_cookie(response: Response, token: str, link_id: int, secret: str) -> None:
    """Set the per-session labs cookie that ties this browser to its lab-share session.

    Uses samesite='strict' for PHI hardening — the cookie never rides a cross-site
    navigation.

    Args:
        response: The outgoing response object.
        token: Share token (used to scope the cookie path).
        link_id: Primary key of the labs share link.
        secret: Session secret for resumption.
    """
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    # samesite='strict': the session cookie never rides a cross-site navigation (PHI hardening).
    response.set_cookie(f"jb_labs_{link_id}", secret, max_age=7 * 24 * 3600, httponly=True,
                        samesite="strict", secure=secure, path=f"/api/share/{token}")


def _resolve_labs(conn, request, token):
    """Resolve a token to an active labs link and spec.

    Args:
        conn: Active database connection.
        request: The incoming request (rate-limit check).
        token: Share link token from the URL path.

    Returns:
        Tuple of (link row, labshare_specs row).

    Raises:
        HTTPException: 404 if the token is not a labs link or the spec is missing or inactive.
        HTTPException: 429 if the client is rate-limited.
    """
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "labs":
        raise HTTPException(status_code=404, detail="Not found.")
    spec = labshare_svc.get_spec(conn, link["id"])
    if spec is None or spec["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn’t available.")
    return link, spec


def _labs_session(conn, link, request):
    """Look up the recipient's lab-share session from the jb_labs_ cookie.

    Args:
        conn: Active database connection.
        link: Resolved labs share link row.
        request: The incoming request (labs session cookie).

    Returns:
        The labshare_sessions row, or None if no session matches the cookie.
    """
    return labshare_svc.session_for(conn, link["id"], request.cookies.get(f"jb_labs_{link['id']}") or "")


class LabsStartIn(BaseModel):
    """Request body for starting a lab-share session."""

    name: str | None = None


class LabsTurnIn(BaseModel):
    """Request body for a chat turn on a lab-share link (hard-disabled; present for schema parity)."""

    message: str = Field("", max_length=8000)


@router.post("/{token}/labs/start")
def labs_start(token: str, body: LabsStartIn, request: Request, response: Response):
    """Start or resume a lab-share session, returning the allowed analyte list.

    Bind links claim the first browser that calls this endpoint. Rejects cross-site
    forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: Optional recipient name.
        request: The incoming request (sec-fetch-site, bind and labs session cookies).
        response: Used to set the bind and labs session cookies.

    Returns:
        Dict with name, allow_chat (always False), analytes, and window metadata.
        Includes transcript for a resumed session.

    Raises:
        HTTPException: 403 if the request is cross-site or the link is locked to
            another browser.
        HTTPException: 404 if the link is not an active labs link.
        HTTPException: 429 if the client is rate-limited.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, spec = _resolve_labs(conn, request, token)
    # A bind link locks to the FIRST browser that accepts it (PHI: a forwarded link can't open
    # a fresh view from another device).
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This link is locked to the browser that accepted it.")
    existing = _labs_session(conn, link, request)
    analytes = [{"analyte": a["analyte"], "test_name": a["test_name"], "unit": a["unit"]}
                for a in lab_share_scope.list_analytes_scoped(conn, labshare_svc.allowed_analytes(spec))]
    out = {"name": (existing["name"] if existing else (body.name or "").strip()[:80]) or None,
           "allow_chat": False, "analytes": analytes,         # data-only now (chat moved to assisted links)
           "window": {"from": spec["window_from"], "to": spec["window_to"]}}
    if existing and existing["status"] == "active":
        import json as _json
        out["transcript"] = _json.loads(existing["transcript_json"] or "[]")
        return out
    # The bind-claim, session INSERT, and link touch all run inside start_session's single
    # write unit (on the writer connection) — the route holds no write across them, so the
    # submit_write deadlock guard never trips. Cookies are set from the returned secrets.
    claim_bind = bool(link["bind"] and st == "unclaimed")  # claim it to this browser
    sid, sess_secret, bind_secret = labshare_svc.start_session(
        conn, link["id"], name=body.name, client_ip=_client_ip(request), claim_bind=claim_bind)
    if bind_secret is not None:
        _bind_cookie(response, token, link["id"], bind_secret)
    _labs_cookie(response, token, link["id"], sess_secret)
    out["transcript"] = []
    return out


@router.get("/{token}/labs/series")
def labs_series(token: str, analyte: str, request: Request):
    """Return the scoped time-series for one analyte on a lab-share link.

    The analyte must appear in the allow-list (independently re-checked, so a forged
    chart spec still 404s). Results are identity-stripped and never cached on the
    recipient device. Requires an active session on this browser.

    Args:
        token: Share link token from the URL path.
        analyte: Analyte key to fetch series data for.
        request: The incoming request (bind and labs session cookies).

    Returns:
        JSON series data (no-store, no-cache headers).

    Raises:
        HTTPException: 403 if the bind check fails or no active session exists.
        HTTPException: 404 if the link, spec, or analyte is not available.
        HTTPException: 429 if the client is rate-limited.
    """
    conn = get_conn()
    link, spec = _resolve_labs(conn, request, token)
    _require_access(link, request)
    session = _labs_session(conn, link, request)
    if session is None or session["status"] != "active":
        raise HTTPException(status_code=403, detail="Start the session first.")
    s = lab_share_scope.series_scoped(conn, analyte, allowed=labshare_svc.allowed_analytes(spec),
                                      dfrom=spec["window_from"], dto=spec["window_to"])
    if s is None:                                          # out-of-scope analyte — defense in depth
        raise HTTPException(status_code=404, detail="Not available.")
    return JSONResponse(s, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@router.post("/{token}/labs/turn")
def labs_turn(token: str, body: LabsTurnIn, request: Request):
    """Reject AI chat turns on standalone lab-share links (hard-disabled).

    Standalone labs shares are data-only (charts/table). The scoped AI lives in assisted
    (research) links. Hard-disabled so even a legacy allow_chat=1 link never bills an AI turn.

    Args:
        token: Share link token from the URL path.
        body: Unused message body.
        request: The incoming request.

    Raises:
        HTTPException: 403 always — chat is not enabled for this link kind.
    """
    # Standalone labs shares are DATA-ONLY (charts/table). The scoped AI moved to assisted
    # (research) links, where it gets real tools over the attached labs. Hard-disabled here so
    # even a legacy allow_chat=1 link can never bill an AI turn.
    raise HTTPException(status_code=403, detail="Chat isn’t enabled for this link.")


# Only these render inline on the public page; everything else (esp. SVG/HTML,
# which can carry script) is forced to download as an opaque blob so it can never
# execute on this origin.
_SAFE_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.get("/{token}/attachments/{att_id}")
def share_attachment(token: str, att_id: int, request: Request):
    """Serve an attachment that belongs to the token's note.

    Security-hardened: X-Content-Type-Options: nosniff, restrictive CSP, and only safe
    image types (PNG/JPEG/GIF/WebP) render inline — all others are forced to download
    as opaque blobs so they cannot execute on this origin.

    Args:
        token: Share link token from the URL path.
        att_id: Primary key of the attachment.
        request: The incoming request (bind cookie check).

    Returns:
        The attachment bytes with appropriate Content-Disposition and security headers.

    Raises:
        HTTPException: 403 if the bind check fails.
        HTTPException: 404 if the attachment does not belong to this token's note.
        HTTPException: 429 if the client is rate-limited.
    """
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
    """Submit a proposed content change for the owner to review (edit links only).

    Stores the proposed content as a pending proposal. Never writes the note directly.
    A 409 is returned if another proposal is already pending on this link.

    Args:
        token: Share link token from the URL path.
        body: Proposed markdown content, optional proposer name, and optional note.
        request: The incoming request (bind cookie and rate-limit checks).

    Returns:
        Dict with 'ok': True and additional fields from share_svc.submit_proposal.

    Raises:
        HTTPException: 403 if the bind check fails.
        HTTPException: 409 if a concurrent proposal was just submitted.
        HTTPException: 429 if the client is rate-limited.
        HTTPException: 404 if the token is not active.
    """
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


# --- Encrypted chat (kind='chat') -------------------------------------------
# A real-time, end-to-end-encrypted 1:1 channel with the owner. Every message/file body
# is opaque ciphertext to the server (the key rides the URL fragment, never sent here).
# The link is browser-bound (1:1): the first recipient to JOIN claims it.

_KEEPALIVE_SECONDS = 15.0


def _chat_landing(conn, link, request: Request) -> dict:
    """Build the public chat-link landing payload.

    Returns {requires_claim: true} for an unclaimed 1:1 channel; returns the guest_wrap
    and session material for the already-bound browser.

    Args:
        conn: Active database connection.
        link: Resolved share link row.
        request: The incoming request (bind cookie).

    Returns:
        Dict with kind, brain_name, owner_name, otp_required, persist, and either
        requires_claim or guest_wrap/guest_name.

    Raises:
        HTTPException: 403 if the channel is locked to a different browser.
        HTTPException: 404 if the channel does not exist or is not active.
    """
    ch = chat_svc.get_channel(conn, link["id"])
    if ch is None or ch["status"] != "active":
        raise HTTPException(status_code=404, detail="This link isn't available.")
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This chat is already open in another browser.")
    base = {"kind": "chat", "brain_name": get_settings().brain_name,
            "owner_name": ch["owner_name"] or get_settings().brain_name,
            "otp_required": bool(ch["otp_required"]), "persist": bool(ch["persist"])}
    if st == "unclaimed":
        return {**base, "requires_claim": True}
    # Already bound to THIS browser → hand back the material to resume the session.
    return {**base, "requires_claim": False, "guest_wrap": ch["guest_wrap"], "guest_name": ch["guest_name"]}


def _resolve_chat(conn, request: Request, token: str):
    """Resolve a token to an active chat link and channel.

    Args:
        conn: Active database connection.
        request: The incoming request (rate-limit check).
        token: Share link token from the URL path.

    Returns:
        Tuple of (link row, chat_channels row).

    Raises:
        HTTPException: 404 if the token is not a chat link or the channel is missing.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    link = _resolve_or_404(conn, request, token)
    if link["kind"] != "chat":
        raise HTTPException(status_code=404, detail="Not found.")
    ch = chat_svc.get_channel(conn, link["id"])
    if ch is None or ch["status"] != "active":
        raise HTTPException(status_code=409, detail="This chat has ended.")
    return link, ch


class ChatJoinIn(BaseModel):
    """Request body for claiming a 1:1 encrypted-chat link."""

    name: str | None = Field(default=None, max_length=80)


@router.post("/{token}/chat/join")
def chat_join(token: str, body: ChatJoinIn, request: Request, response: Response):
    """Claim a 1:1 encrypted-chat link to this browser and return the wrapped channel key.

    The recipient derives the channel key from the URL fragment [+ OTP] client-side.
    The raw key never reaches the server. Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: Optional recipient display name.
        request: The incoming request (sec-fetch-site and bind cookie).
        response: Used to set the bind cookie.

    Returns:
        Dict with guest_wrap, persist flag, guest_name, and status 'active'.

    Raises:
        HTTPException: 403 if the request is cross-site, or the channel is already
            open on another browser.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, ch = _resolve_chat(conn, request, token)
    st = _bind_status(link, request)
    if st == "locked":
        raise HTTPException(status_code=403, detail="This chat is already open in another browser.")
    name = (body.name or "").strip()[:80] or None
    if st == "unclaimed":
        secret = share_svc.mint_token()
        cur = conn.execute(
            "UPDATE share_links SET bind_secret=?, bound_at=datetime('now'), bound_name=? "
            "WHERE id=? AND bind_secret IS NULL", (secret, name, link["id"]))
        if cur.rowcount != 1:                         # another browser joined in the race
            conn.rollback()
            raise HTTPException(status_code=403, detail="This chat was just opened on another browser.")
        conn.execute("UPDATE chat_channels SET guest_name=? WHERE share_link_id=?", (name, link["id"]))
        conn.commit()
        _bind_cookie(response, token, link["id"], secret)
    elif name and not ch["guest_name"]:
        conn.execute("UPDATE chat_channels SET guest_name=? WHERE share_link_id=?", (name, link["id"]))
        conn.commit()
    return {"guest_wrap": ch["guest_wrap"], "persist": bool(ch["persist"]),
            "guest_name": name or ch["guest_name"], "status": "active"}


@router.get("/{token}/chat/stream")
async def chat_stream(token: str, request: Request, after: int = 0):
    """Stream recipient-side SSE events: backlog replay, live messages, and presence updates.

    Cookie-bound to the browser that joined the channel.

    Args:
        token: Share link token from the URL path.
        after: Sequence number to resume from; replays persisted messages with seq > after.
        request: The incoming request (bind cookie and disconnect check).

    Returns:
        StreamingResponse (text/event-stream) with presence, message, and closed events.

    Raises:
        HTTPException: 403 if the bind check fails.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    conn = get_conn()
    link, ch = _resolve_chat(conn, request, token)
    _require_access(link, request)
    link_id = link["id"]
    persist = bool(ch["persist"])

    async def gen():
        """Stream the guest's encrypted-chat relay events as Server-Sent Events."""
        hub, sub = await chat_relay.subscribe(link_id, "guest")
        chat_svc.handle_presence(conn, link_id)
        try:
            if persist:
                for ev in chat_svc.backlog(conn, link_id, after):
                    yield chat_svc.sse(ev)
            owner_p, guest_p = chat_relay.present(link_id)
            yield chat_svc.sse({"type": "presence", "owner": owner_p, "guest": guest_p})
            while True:
                if await request.is_disconnected():
                    break
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
    """Request body for sending one encrypted chat message."""

    iv: str = Field(max_length=64)
    ct: str = Field(max_length=700_000)


@router.post("/{token}/chat/send")
def chat_send(token: str, body: ChatSendIn, request: Request):
    """Send one encrypted message as the recipient on a chat channel.

    Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        body: AES-GCM iv and ciphertext of the message.
        request: The incoming request (sec-fetch-site and bind cookie).

    Returns:
        Dict with 'ok': True and the assigned sequence number.

    Raises:
        HTTPException: 403 if the request is cross-site or the bind check fails.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, _ = _resolve_chat(conn, request, token)
    _require_access(link, request)
    ev = chat_svc.append_message(conn, link["id"], "guest", body.iv, body.ct)
    return {"ok": True, "seq": ev["seq"]}


@router.post("/{token}/chat/file")
def chat_file_upload(token: str, request: Request, iv: str = Form(...), file: UploadFile = File(...)):
    """Upload one encrypted file blob as the recipient on a chat channel.

    The sender references the returned file_id inside an encrypted message, so the
    filename and MIME type never reach the server. Rejects cross-site forged POSTs.

    Args:
        token: Share link token from the URL path.
        iv: AES-GCM initialisation vector (form field, max 64 chars).
        file: Encrypted file blob.
        request: The incoming request (sec-fetch-site and bind cookie).

    Returns:
        Dict with 'ok': True and the assigned file_id.

    Raises:
        HTTPException: 403 if the request is cross-site or the bind check fails.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed.")
    conn = get_conn()
    link, _ = _resolve_chat(conn, request, token)
    _require_access(link, request)
    blob = file.file.read()
    fid = chat_svc.store_file(conn, link["id"], iv[:64], blob)
    return {"ok": True, "file_id": fid}


@router.get("/{token}/chat/file/{file_id}")
def chat_file_download(token: str, file_id: int, request: Request):
    """Download an encrypted file blob as the recipient on a chat channel.

    Args:
        token: Share link token from the URL path.
        file_id: Primary key of the stored file.
        request: The incoming request (bind cookie check).

    Returns:
        Opaque octet-stream with X-Chat-IV header, nosniff, sandbox CSP, and no-store.

    Raises:
        HTTPException: 403 if the bind check fails.
        HTTPException: 409 if the channel has ended.
        HTTPException: 429 if the client is rate-limited.
    """
    conn = get_conn()
    link, _ = _resolve_chat(conn, request, token)
    _require_access(link, request)
    row = chat_svc.get_file(conn, link["id"], file_id)
    return Response(
        content=bytes(row["blob"]), media_type="application/octet-stream",
        headers={"X-Chat-IV": row["iv"], "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})
