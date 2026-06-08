"""Attachment REST API: upload (multipart), list, view, download, delete."""
import asyncio
import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import attachments as att_svc
from ..services import audio_transcription, image_analysis, llm, media_tokens

router = APIRouter(prefix="/api", tags=["attachments"], dependencies=[CurrentUser])
# Token-gated (NOT bearer-gated) routes media elements can hit directly. The signed,
# short-lived token authorizes a single attachment, so no access key rides in the URL.
public_router = APIRouter(prefix="/api", tags=["attachments"])


def _note_id_for_slug(conn, slug: str) -> int:
    """Resolve a note slug to its primary key, or raise 404 if not found.

    Args:
        conn: Active DB connection.
        slug: URL slug of the note.

    Returns:
        Integer primary key of the note.

    Raises:
        HTTPException: 404 if no live note with that slug exists.
    """
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row["id"]


@router.post("/notes/{slug}/attachments")
async def upload(slug: str, file: UploadFile = File(...), analyze: bool = Form(True)):
    """Upload a file attachment to a note and optionally auto-analyze it.

    Stores the file, extracts text if applicable, and (when auto-analyze is
    enabled) kicks off image vision analysis, audio/video transcription, or
    document-based note re-analysis in a background thread.

    Args:
        slug: URL slug of the note to attach the file to.
        file: Multipart file upload.
        analyze: Set to False to suppress AI analysis (e.g. chat-assisted
            attachment paths). Defaults to True.

    Returns:
        Attachment metadata dict from the storage service, possibly with an
        ``analysis`` key when enrichment was started.

    Raises:
        HTTPException: 404 if the note does not exist.
        HTTPException: 413 if the file exceeds the 100 MB limit.
    """
    conn = get_conn()
    note_id = _note_id_for_slug(conn, slug)

    raw = await file.read()
    if len(raw) > att_svc.MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="File too large (100 MB max).")

    filename = file.filename
    mime = att_svc.resolve_mime(filename or "", file.content_type)

    def _store_and_enrich() -> dict:
        # Off the event loop: add_attachment text-extracts + embeds (multi-second), and the
        # document path below runs a full note-analysis LLM call — doing either inline on the
        # single event loop would block every other request. The connection is the event-loop
        # thread's; we await this offload before resuming, so it's the only thread touching conn.
        result = att_svc.add_attachment(conn, note_id, filename, mime, raw)
        conn.commit()

        # The "auto-analyze new notes" toggle is the master switch for ALL on-upload
        # enrichment: when it's OFF, nothing auto-runs (no image vision, no audio/video
        # transcription, no note re-analysis) — the owner can still trigger any of it by hand
        # via the per-attachment Analyze/Transcribe buttons. When ON, attachments enrich
        # server-side (so it runs even if the client navigates away) and fold into the note's
        # AI analysis as their text becomes ready.
        from ..services import note_analysis as na
        if not na.auto_enabled(conn):
            return result
        # Auto-analyze images. Callers opt out with analyze=false — e.g. the chat
        # assisted-attachment path, whose carrier note has no real content to inform it.
        if analyze and mime.startswith("image/") and llm.has_credentials():
            result["analysis"] = image_analysis.start_analysis(conn, result["id"])
        # Audio & video auto-transcribe regardless of the analyze flag: it doesn't depend on the
        # note body for context, so there's nothing to opt out of. Video is transcribed via its
        # audio track (local faster-whisper). The transcript lands on the attachment.
        elif audio_transcription.is_transcribable(mime, filename):
            result["analysis"] = audio_transcription.start_transcription(conn, result["id"])
        # Documents (PDF / text / office) extract their text synchronously in add_attachment —
        # there's no async completion to fold them into the note's AI analysis later (unlike
        # images/audio/video). So refresh the note's analysis now that this attachment's text is
        # indexed, so a doc uploaded right after a note still flows into its gist/facts.
        # Best-effort, hash-guarded, no-ops without an LLM key.
        elif analyze and note_id is not None and result.get("has_text"):
            if na.analyze(conn, note_id):
                conn.commit()
        return result

    return await asyncio.to_thread(_store_and_enrich)


class AnalyzeBody(BaseModel):
    """Input schema for manual analyze/transcribe endpoints."""

    force: bool = False


def _require_attachment(conn, att_id: int) -> None:
    """Assert an attachment exists, raising 404 otherwise.

    Args:
        conn: Active DB connection.
        att_id: Primary key of the attachment to check.

    Raises:
        HTTPException: 404 if no attachment with that ID exists.
    """
    if not conn.execute("SELECT 1 FROM attachments WHERE id = ?", (att_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Attachment not found")


@router.post("/attachments/{att_id}/analyze")
def analyze_attachment(att_id: int, body: AnalyzeBody | None = None):
    """Manually trigger AI vision analysis for an image attachment.

    Args:
        att_id: Primary key of the attachment.
        body: Optional ``{"force": bool}`` — set True to re-run even if analysis
            is already pending or complete.

    Returns:
        Analysis job metadata dict from the image analysis service.

    Raises:
        HTTPException: 404 if the attachment does not exist.
        HTTPException: 409 if analysis is already running and ``force`` is False.
    """
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
    """Manually trigger audio/video transcription for an attachment.

    Args:
        att_id: Primary key of the attachment.
        body: Optional ``{"force": bool}`` — set True to re-run even if
            transcription is already pending or complete.

    Returns:
        Transcription job metadata dict from the audio transcription service.

    Raises:
        HTTPException: 404 if the attachment does not exist.
        HTTPException: 409 if transcription is already running and ``force`` is False.
    """
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
    """Return the current analysis or transcription status for an attachment.

    Args:
        att_id: Primary key of the attachment.

    Returns:
        JSON with ``status`` (str), ``detail`` (str or null), and
        ``analyzed_at`` (timestamp or null).

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
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
    """List all attachments for a note.

    Args:
        slug: URL slug of the note.

    Returns:
        List of attachment metadata dicts for the note.

    Raises:
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    return att_svc.list_for_note(conn, _note_id_for_slug(conn, slug))


@router.get("/attachments/{att_id}")
def get_attachment(att_id: int):
    """Return metadata for a single attachment (no binary content).

    Args:
        att_id: Primary key of the attachment.

    Returns:
        Dict with ``id``, ``note_id``, ``filename``, ``mime``, ``content_text``,
        ``byte_size``, and ``created_at``.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    row = get_conn().execute(
        "SELECT id, note_id, filename, mime, content_text, byte_size, created_at "
        "FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return dict(row)


@router.get("/attachments/{att_id}/media-url")
def media_url(att_id: int):
    """Mint a short-lived signed URL for direct browser media loading.

    The signed token authorises a single attachment so the browser's
    ``<img>``/``<audio>``/``<video>`` can load it without an Authorization
    header or a ``blob:`` URL (no ``media-src`` CSP rule needed).

    Args:
        att_id: Primary key of the attachment.

    Returns:
        JSON ``{"url": str}`` pointing to the stream endpoint with a token.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    conn = get_conn()
    _require_attachment(conn, att_id)
    token = media_tokens.make_token(conn, att_id)
    return {"url": f"/api/attachments/{att_id}/stream?token={token}"}


@public_router.get("/attachments/{att_id}/stream")
def stream_attachment(att_id: int, request: Request, token: str = ""):
    """Stream attachment bytes inline, supporting HTTP range requests.

    Gated by a short-lived signed token (not the access key, which must never
    appear in a media URL). Only images, audio, and video render inline; any
    script-like or unknown MIME is forced to a download to prevent same-origin
    XSS.

    Args:
        att_id: Primary key of the attachment to stream.
        request: FastAPI request (used to inspect the ``Range`` header).
        token: Signed media token from the ``/media-url`` endpoint.

    Returns:
        206 partial content for ranged requests; 200 for full-file requests;
        or a forced-download response for non-inlineable types.

    Raises:
        HTTPException: 403 if the token is invalid or expired.
        HTTPException: 404 if the attachment or its binary content is missing.
    """
    conn = get_conn()
    if not media_tokens.verify_token(conn, att_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired media link.")
    row = conn.execute(
        "SELECT filename, mime, content_blob FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row or row["content_blob"] is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    data = bytes(row["content_blob"])
    total = len(data)
    mime = row["mime"] or "application/octet-stream"
    base = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600"}

    inlineable = mime.startswith(("image/", "audio/", "video/")) and "javascript" not in mime and mime != "image/svg+xml"
    if not inlineable:
        safe_name = re.sub(r'[\r\n"\\]', "_", (row["filename"] or "file"))
        return Response(content=data, media_type="application/octet-stream",
                        headers={**base, "Content-Disposition": f'attachment; filename="{safe_name}"'})

    rng = request.headers.get("range", "")
    if rng.startswith("bytes="):
        spec = rng[6:].split(",")[0].strip()
        start_s, _, end_s = spec.partition("-")
        try:
            if not start_s:                       # suffix range: last N bytes
                n = int(end_s)
                start, end = max(0, total - n), total - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
            if start < 0 or start >= total or end < start:
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{total}"})
        end = min(end, total - 1)
        chunk = data[start:end + 1]
        return Response(content=chunk, status_code=206, media_type=mime,
                        headers={**base, "Content-Disposition": "inline",
                                 "Content-Range": f"bytes {start}-{end}/{total}",
                                 "Content-Length": str(len(chunk))})
    return Response(content=data, media_type=mime,
                    headers={**base, "Content-Disposition": "inline", "Content-Length": str(total)})


@router.get("/attachments/{att_id}/download")
def download_attachment(att_id: int):
    """Force-download an attachment safely, neutralising dangerous MIME types.

    SVG, HTML, and JavaScript MIMEs are remapped to
    ``application/octet-stream``; the filename is sanitised to prevent header
    injection; content sniffing is disabled.

    Args:
        att_id: Primary key of the attachment.

    Returns:
        Forced-download response with sanitised headers.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
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
    """Permanently delete an attachment and its stored data.

    Args:
        att_id: Primary key of the attachment to delete.

    Returns:
        JSON ``{"ok": true}``.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    conn = get_conn()
    row = conn.execute("SELECT id FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    att_svc.delete_attachment(conn, att_id)
    conn.commit()
    return {"ok": True}
