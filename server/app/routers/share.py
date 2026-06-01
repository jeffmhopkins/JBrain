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


def _enforce_bind(conn, link, request: Request, response: Response = None, bind_if_new: bool = True):
    """For 'bind' links: on first open, mint a cookie that locks the link to this
    browser; thereafter require the matching cookie. The cookie is scoped to the
    token's API path so it rides along on read/propose/attachment requests."""
    if not link["bind"]:
        return
    name = f"jb_bind_{link['id']}"
    cookie = request.cookies.get(name)
    if link["bind_secret"]:                       # already bound — require the matching cookie
        if not cookie or not hmac.compare_digest(cookie, link["bind_secret"]):
            raise HTTPException(status_code=403,
                                detail="This link is locked to the device that first opened it.")
        return
    # Not yet bound:
    if not (bind_if_new and response is not None):
        # validate-only path (e.g. an attachment load) on an unbound link — refuse,
        # so attachments can't be fetched off a bind link before it's claimed.
        raise HTTPException(status_code=403, detail="Open the shared page first.")
    secret = share_svc.mint_token()
    # First-open-wins, atomically: only the request that flips NULL->secret binds.
    cur = conn.execute(
        "UPDATE share_links SET bind_secret=?, bound_at=datetime('now') WHERE id=? AND bind_secret IS NULL",
        (secret, link["id"]))
    conn.commit()
    if cur.rowcount != 1:   # another device bound first
        raise HTTPException(status_code=403,
                            detail="This link is locked to the device that first opened it.")
    domain = (get_settings().jbrain_domain or "").lower()
    secure = not (domain == "" or domain.startswith("localhost") or domain.startswith("127."))
    response.set_cookie(name, secret, max_age=31_536_000, httponly=True,
                        samesite="lax", secure=secure, path=f"/api/share/{link['token']}")


@router.get("/{token}")
def share_read(token: str, request: Request, response: Response):
    """Return ONLY this one note's public fields (+ attachment list). Withholds
    backlinks, tags, geolocation, slug, and id."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    _enforce_bind(conn, link, request, response)
    share_svc.touch(conn, link["id"]); conn.commit()
    atts = att_svc.list_for_note(conn, link["note_id"])
    return {
        "scope": link["scope"],
        "can_edit": link["scope"] == "edit",
        "brain_name": get_settings().brain_name,
        "note": {
            "title": link["title"],
            "content_md": link["content_md"],
            "kind": link["kind"],
            "updated_at": link["updated_at"],
            "attachments": [{"id": a["id"], "filename": a["filename"],
                             "mime": a["mime"], "byte_size": a["byte_size"]} for a in atts],
        },
    }


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
    _enforce_bind(conn, link, request, bind_if_new=False)   # validate only; first-bind happens on read
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
def share_propose(token: str, body: ProposeIn, request: Request, response: Response):
    """EDIT links only: store a proposed new content as a pending proposal for the
    owner to accept. Never writes the note."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    _enforce_bind(conn, link, request, response)
    try:
        r = share_svc.submit_proposal(conn, link, body.content_md, body.note, body.name, _client_ip(request))
        conn.commit()
    except sqlite3.IntegrityError:
        # Two proposals raced the one-pending-per-link index — clean retry, not a 500.
        conn.rollback()
        raise HTTPException(status_code=409, detail="Someone else just proposed a change — reload and try again.")
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, **r}
