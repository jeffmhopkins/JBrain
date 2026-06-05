"""Attachment REST API: upload (multipart), list, view, download, delete."""
import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import attachments as att_svc
from ..services import audio_transcription, image_analysis, llm

router = APIRouter(prefix="/api", tags=["attachments"], dependencies=[CurrentUser])


def _note_id_for_slug(conn, slug: str) -> int:
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row["id"]


@router.post("/notes/{slug}/attachments")
async def upload(slug: str, file: UploadFile = File(...), analyze: bool = Form(True)):
    conn = get_conn()
    note_id = _note_id_for_slug(conn, slug)

    raw = await file.read()
    if len(raw) > att_svc.MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="File too large (10 MB max).")

    mime = att_svc.resolve_mime(file.filename or "", file.content_type)
    result = att_svc.add_attachment(conn, note_id, file.filename, mime, raw)
    conn.commit()

    # Auto-analyze images by default (server-side, so it runs even if the client
    # navigates away). Callers opt out with analyze=false — e.g. the chat
    # assisted-attachment path, whose carrier note has no real content to inform it.
    if analyze and mime.startswith("image/") and llm.has_credentials():
        result["analysis"] = image_analysis.start_analysis(conn, result["id"])
    # Audio auto-transcribes regardless of the analyze flag: it's local (no API key,
    # no cost) and doesn't depend on the note body for context, so there's nothing to
    # opt out of. The transcript lands on the attachment and becomes searchable.
    elif audio_transcription.is_audio(mime, file.filename):
        result["analysis"] = audio_transcription.start_transcription(conn, result["id"])
    return result


class AnalyzeBody(BaseModel):
    force: bool = False


def _require_attachment(conn, att_id: int) -> None:
    if not conn.execute("SELECT 1 FROM attachments WHERE id = ?", (att_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Attachment not found")


@router.post("/attachments/{att_id}/analyze")
def analyze_attachment(att_id: int, body: AnalyzeBody | None = None):
    conn = get_conn()
    _require_attachment(conn, att_id)
    force = bool(body and body.force)
    row = conn.execute(
        "SELECT analysis_status FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if row["analysis_status"] == "pending" and not force:
        raise HTTPException(status_code=409, detail="Analysis already running.")
    return image_analysis.start_analysis(conn, att_id, force=force)


@router.post("/attachments/{att_id}/transcribe")
def transcribe_attachment(att_id: int, body: AnalyzeBody | None = None):
    conn = get_conn()
    _require_attachment(conn, att_id)
    force = bool(body and body.force)
    row = conn.execute(
        "SELECT analysis_status FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if row["analysis_status"] == "pending" and not force:
        raise HTTPException(status_code=409, detail="Transcription already running.")
    return audio_transcription.start_transcription(conn, att_id, force=force)


@router.get("/attachments/{att_id}/analysis-status")
def analysis_status(att_id: int):
    row = get_conn().execute(
        "SELECT analysis_status, analysis_detail, analyzed_at FROM attachments WHERE id = ?",
        (att_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return {
        "status": row["analysis_status"] or "none",
        "detail": row["analysis_detail"],
        "analyzed_at": row["analyzed_at"],
    }


@router.get("/notes/{slug}/attachments")
def list_attachments(slug: str):
    conn = get_conn()
    return att_svc.list_for_note(conn, _note_id_for_slug(conn, slug))


@router.get("/attachments/{att_id}")
def get_attachment(att_id: int):
    row = get_conn().execute(
        "SELECT id, note_id, filename, mime, content_text, byte_size, created_at "
        "FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return dict(row)


@router.get("/attachments/{att_id}/download")
def download_attachment(att_id: int):
    row = get_conn().execute(
        "SELECT filename, mime, content_text, content_blob FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    data = bytes(row["content_blob"]) if row["content_blob"] is not None else (row["content_text"] or "").encode()
    # Serve attacker-uploadable bytes safely: never let the browser render them
    # inline as active content. Force a download, neutralise script-y MIMEs, stop
    # content sniffing, and sanitise the filename (no header injection / inline).
    mime = (row["mime"] or "application/octet-stream")
    if mime in ("image/svg+xml", "text/html", "application/xhtml+xml") or "javascript" in mime:
        mime = "application/octet-stream"
    safe_name = re.sub(r'[\r\n"\\]', "_", (row["filename"] or "file"))
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
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
