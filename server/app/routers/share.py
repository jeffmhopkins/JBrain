"""PUBLIC, UNAUTHENTICATED share endpoints — the only routes with no CurrentUser.

Every handler resolves the token to exactly one note via share_svc.resolve_active_link
and exposes only that note. There is no note id/slug parameter anywhere here, so a
token can never reach another note. Keep this file small and auditable.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import get_conn
from ..services import attachments as att_svc
from ..services import share as share_svc

router = APIRouter(prefix="/api/share", tags=["share"])   # NO dependencies=[CurrentUser]


class ProposeIn(BaseModel):
    content_md: str = Field(max_length=400_000)
    note: str | None = Field(default=None, max_length=2000)


def _resolve_or_404(conn, request: Request, token: str):
    if share_svc.rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(status_code=429, detail="Too many requests; slow down.")
    link = share_svc.resolve_active_link(conn, token)
    if link is None:
        raise HTTPException(status_code=404, detail="This link isn't available.")
    return link


@router.get("/{token}")
def share_read(token: str, request: Request):
    """Return ONLY this one note's public fields (+ attachment list). Withholds
    backlinks, tags, geolocation, slug, and id."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
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


@router.get("/{token}/attachments/{att_id}")
def share_attachment(token: str, att_id: int, request: Request):
    """Serve an attachment — only if it belongs to THIS token's note."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    row = conn.execute(
        "SELECT filename, mime, content_text, content_blob FROM attachments WHERE id=? AND note_id=?",
        (att_id, link["note_id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    data = bytes(row["content_blob"]) if row["content_blob"] is not None else (row["content_text"] or "").encode()
    return Response(content=data, media_type=row["mime"] or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{row["filename"]}"'})


@router.post("/{token}/propose")
def share_propose(token: str, body: ProposeIn, request: Request):
    """EDIT links only: store a proposed new content as a pending proposal for the
    owner to accept. Never writes the note."""
    conn = get_conn()
    link = _resolve_or_404(conn, request, token)
    try:
        r = share_svc.submit_proposal(conn, link, body.content_md, body.note,
                                       request.client.host if request.client else None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, **r}
