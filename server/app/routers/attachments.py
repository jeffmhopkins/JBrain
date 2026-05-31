"""Attachment REST API: upload (multipart), list, view, download, delete."""
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from ..auth import CurrentUser
from ..db import get_conn
from ..services import attachments as att_svc

router = APIRouter(prefix="/api", tags=["attachments"], dependencies=[CurrentUser])


def _note_id_for_slug(conn, slug: str) -> int:
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row["id"]


@router.post("/notes/{slug}/attachments")
async def upload(slug: str, file: UploadFile = File(...)):
    conn = get_conn()
    note_id = _note_id_for_slug(conn, slug)

    mime = att_svc.mime_for(file.filename or "", file.content_type)
    if mime is None:
        raise HTTPException(status_code=415, detail="Only .txt and .md/.markdown files are supported.")

    raw = await file.read()
    if len(raw) > att_svc.MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="File too large (2 MB max).")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="File must be UTF-8 text.")

    result = att_svc.add_attachment(conn, note_id, file.filename, mime, raw)
    conn.commit()
    return result


@router.get("/notes/{slug}/attachments")
def list_attachments(slug: str):
    conn = get_conn()
    return att_svc.list_for_note(conn, _note_id_for_slug(conn, slug))


@router.get("/attachments/{att_id}")
def get_attachment(att_id: int):
    row = get_conn().execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return dict(row)


@router.get("/attachments/{att_id}/download")
def download_attachment(att_id: int):
    row = get_conn().execute(
        "SELECT filename, mime, content_text FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(
        content=row["content_text"],
        media_type=f"{row['mime']}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
    )


@router.delete("/attachments/{att_id}")
def delete_attachment(att_id: int):
    conn = get_conn()
    row = conn.execute("SELECT id FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    att_svc.delete_attachment(conn, att_id)
    conn.commit()
    return {"ok": True}
