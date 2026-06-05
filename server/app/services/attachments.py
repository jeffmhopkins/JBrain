"""Attachment write pipeline: store any file (bytes in DB) + extract searchable
text. Text/code is decoded, PDFs are text-extracted, and image EXIF/metadata is
pulled — all indexed via FTS + chunked embeddings.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os

from . import embeddings

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
MAX_CHUNKS = 200

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sh", ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".sql",
    ".ini", ".toml", ".cfg", ".conf", ".tex", ".rtf",
}
TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/javascript"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic"}


def resolve_mime(filename: str, declared: str | None) -> str:
    if declared and declared not in ("application/octet-stream", ""):
        return "text/markdown" if "markdown" in declared else declared
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


def _pdf_text(raw: bytes) -> str:
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    out = []
    for page in reader.pages[:100]:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(out)[:200_000]


def _image_meta(raw: bytes, filename: str) -> str:
    import io
    from PIL import ExifTags, Image
    img = Image.open(io.BytesIO(raw))
    lines = [f"Image: {filename}", f"Format: {img.format}",
             f"Dimensions: {img.width}x{img.height}", f"Mode: {img.mode}"]
    try:
        exif = img.getexif()
        for tid, val in exif.items():
            tag = ExifTags.TAGS.get(tid, str(tid))
            sval = str(val)
            if tag != "MakerNote" and len(sval) <= 200:
                lines.append(f"{tag}: {sval}")
        try:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            for tid, val in (gps or {}).items():
                lines.append(f"GPS {ExifTags.GPSTAGS.get(tid, str(tid))}: {val}")
        except Exception:
            pass
    except Exception:
        pass
    return "\n".join(lines)


def extract_text(raw: bytes, mime: str, filename: str) -> str:
    """Best-effort searchable text. Returns '' for unsupported/binary files."""
    ext = os.path.splitext((filename or "").lower())[1]
    try:
        if mime.startswith("text/") or ext in TEXT_EXTS or mime in TEXT_MIMES:
            return raw.decode("utf-8", "ignore")
        if mime == "application/pdf" or ext == ".pdf":
            return _pdf_text(raw)
        if mime.startswith("image/") or ext in IMAGE_EXTS:
            return _image_meta(raw, filename)
    except Exception:
        return ""
    return ""


def chunk_text(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start, n = 0, len(text)
    while start < n and len(chunks) < MAX_CHUNKS:
        end = min(start + CHUNK_CHARS, n)
        if end < n:
            brk = text.rfind("\n", start + CHUNK_CHARS - CHUNK_OVERLAP, end)
            if brk != -1 and brk > start:
                end = brk
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in chunks if c]


def _sync_attachment_fts(conn, att_id, note_id, filename, content):
    conn.execute("DELETE FROM attachments_fts WHERE attachment_id = ?", (att_id,))
    conn.execute(
        "INSERT INTO attachments_fts (attachment_id, note_id, filename, content) VALUES (?, ?, ?, ?)",
        (att_id, note_id, filename, content),
    )


def add_attachment(conn, note_id: int | None, filename: str, mime: str, raw: bytes) -> dict:
    sha = hashlib.sha256(raw).hexdigest()
    existing = conn.execute(
        "SELECT id, filename, mime, byte_size, content_text FROM attachments "
        "WHERE note_id IS ? AND sha256 = ?",
        (note_id, sha),
    ).fetchone()
    if existing:
        # Same shape as the insert path (incl. note_id/has_text) so callers can
        # act on the returned id regardless of whether it was a duplicate.
        return {
            "id": existing["id"], "note_id": note_id, "filename": existing["filename"],
            "mime": existing["mime"], "byte_size": existing["byte_size"],
            "has_text": bool(existing["content_text"]), "duplicate": True,
        }

    content_text = extract_text(raw, mime, filename)
    cur = conn.execute(
        "INSERT INTO attachments (note_id, filename, mime, content_text, content_blob, byte_size, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (note_id, filename, mime, content_text, raw, len(raw), sha),
    )
    att_id = cur.lastrowid
    _sync_attachment_fts(conn, att_id, note_id, filename, content_text)
    embeddings.upsert_attachment_embeddings(conn, att_id, note_id, chunk_text(content_text))
    return {
        "id": att_id, "note_id": note_id, "filename": filename, "mime": mime,
        "byte_size": len(raw), "has_text": bool(content_text), "duplicate": False,
    }


def delete_attachment(conn, att_id: int) -> None:
    # If this attachment had an AI image summary appended to its note, strip that
    # block (versioned) so deleting the image cleanly removes its summary.
    row = conn.execute(
        "SELECT note_id, analyzed_at FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if row and row["analyzed_at"] and row["note_id"] is not None:
        from . import image_analysis  # lazy import: avoids a service import cycle
        from . import notes as notes_svc
        note = conn.execute(
            "SELECT title, content_md FROM notes WHERE id = ? AND deleted_at IS NULL",
            (row["note_id"],),
        ).fetchone()
        if note:
            stripped = image_analysis.strip_summary_block(note["content_md"], att_id)
            if stripped != note["content_md"]:
                notes_svc.upsert_note(
                    conn, note["title"], stripped, note_id=row["note_id"],
                    source="image-analysis",
                    version_note="strip AI summary (attachment deleted)",
                )
    conn.execute("DELETE FROM attachments_fts WHERE attachment_id = ?", (att_id,))
    embeddings.delete_attachment_embeddings(conn, att_id)
    conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))


def list_for_note(conn, note_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, mime, byte_size, created_at, "
        "analysis_status, analysis_detail, analyzed_at, analysis_md FROM attachments "
        "WHERE note_id = ? ORDER BY created_at DESC",
        (note_id,),
    ).fetchall()
    return [dict(r) for r in rows]
