"""Attachment write pipeline: store any file (bytes in DB) + extract searchable
text. Text/code is decoded, PDFs are text-extracted, and image EXIF/metadata is
pulled — all indexed via FTS + chunked embeddings.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re

from . import embeddings

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB (audio/video can be large)

# A chunk is capped at 1200 chars so that even token-DENSE content (code, transcripts,
# tables ≈ 2.5 chars/token) stays under bge-small's hard 512-token input limit — past
# which the embedder SILENTLY truncates the tail, blinding the vector half of search to
# it. For ordinary prose (≈ 4 chars/token) 1200 chars ≈ 300 tokens, squarely inside the
# empirically optimal 256–400-token retrieval band. The cap is a deterministic guarantee
# (no tokenizer dependency); it doubles as the size target.
CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP = 200
MAX_CHUNKS = 300

# Markdown structure: cut sections at ATX headers, never split inside a fenced code
# block, and prefix every chunk with its header breadcrumb ("H1 > H2 > H3") so a short
# section embeds WITH the context that disambiguates it — at zero LLM cost.
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SEPARATORS = ("\n\n", "\n", ". ", " ")  # preferred break points, best→worst

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sh", ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".sql",
    ".ini", ".toml", ".cfg", ".conf", ".tex", ".rtf",
}
TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/javascript"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic"}


def resolve_mime(filename: str, declared: str | None) -> str:
    """Resolve the effective MIME type for a file, preferring the declared type.

    Args:
        filename: Original filename, used for extension-based guessing.
        declared: MIME type declared by the upload client (may be None or generic).

    Returns:
        The resolved MIME type string.
    """
    if declared and declared not in ("application/octet-stream", ""):
        return "text/markdown" if "markdown" in declared else declared
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


def _pdf_text(raw: bytes) -> str:
    """Extract plain text from a PDF byte string (first 100 pages, 200 000 char cap).

    Args:
        raw: Raw PDF file bytes.

    Returns:
        Extracted text, or an empty string if extraction fails.
    """
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
    """Extract image metadata and EXIF tags as a plain-text block for FTS indexing.

    Args:
        raw: Raw image file bytes.
        filename: Original filename, used as the label in the output.

    Returns:
        Multi-line string of image/EXIF fields, including GPS if present.
    """
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
    """Extract best-effort searchable text from any supported file type.

    Dispatches to the appropriate extractor: UTF-8 decode for text/code, pypdf for
    PDFs, and EXIF/metadata harvesting for images. Returns '' for unsupported or
    unreadable binary files.

    Args:
        raw: Raw file bytes.
        mime: Resolved MIME type (from resolve_mime).
        filename: Original filename used for extension-based dispatch and labelling.

    Returns:
        Extracted text, or '' for unsupported/binary files.
    """
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


def _sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (breadcrumb, body) sections at ATX headers.

    Fenced code blocks are kept intact (a ``#`` inside a fence is not treated as a
    header). The breadcrumb is the header path above the body (e.g. "Setup > Linux");
    the header line itself is lifted into the breadcrumb rather than left in the body.
    Text with no headers yields a single ("", whole-text) section.

    Args:
        text: Raw markdown text to split.

    Returns:
        List of (breadcrumb, body) tuples, one per logical section.
    """
    sections: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []        # (level, title) stack
    buf: list[str] = []
    in_fence, fence_tok = False, ""

    def flush() -> None:
        """Flush the current buffer as a section entry."""
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(t for _, t in path), body))
        buf.clear()

    for ln in text.split("\n"):
        fence = _FENCE_RE.match(ln)
        if fence:
            tok = fence.group(1)
            if not in_fence:
                in_fence, fence_tok = True, tok
            elif tok == fence_tok:
                in_fence = False
            buf.append(ln)
            continue
        m = None if in_fence else _HEADER_RE.match(ln)
        if m:
            flush()                          # close the open section before descending
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m.group(2).strip()))
        else:
            buf.append(ln)
    flush()
    return sections


def _best_break(body: str, start: int, end: int, budget: int) -> int:
    """Find the best position to end a chunk window.

    Returns the latest preferred separator (paragraph, sentence, word) found in the
    final third of the proposed window so chunks stay reasonably full. Falls back to a
    hard cut at ``end`` when no separator is found.

    Args:
        body: Full section body being windowed.
        start: Start index of the current window.
        end: Proposed hard-cut end index.
        budget: Maximum chars per chunk (used to compute the "final third" threshold).

    Returns:
        The character index at which to end the current chunk.
    """
    lo = start + (budget * 2) // 3
    for sep in _SEPARATORS:
        brk = body.rfind(sep, lo, end)
        if brk != -1 and brk > start:
            return brk + len(sep)
    return end


def _window_split(breadcrumb: str, body: str) -> list[str]:
    """Pack one section's body into CHUNK_MAX_CHARS windows with overlap.

    Breaks on paragraph/sentence/word boundaries where possible. Each emitted chunk
    is prefixed with the breadcrumb so a short section embeds with disambiguating
    context. Overlap is CHUNK_OVERLAP characters carried over from the previous window.

    Args:
        breadcrumb: Header path for this section (e.g. "Setup > Linux"), may be empty.
        body: Section body text to split.

    Returns:
        List of chunk strings, each prefixed with the breadcrumb.
    """
    prefix = f"{breadcrumb}\n\n" if breadcrumb else ""
    budget = max(CHUNK_MAX_CHARS - len(prefix), 200)    # leave room for the breadcrumb
    out: list[str] = []
    start, n = 0, len(body)
    while start < n:
        end = min(start + budget, n)
        if end < n:
            end = _best_break(body, start, end, budget)
        piece = body[start:end].strip()
        if piece:
            out.append(prefix + piece)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping, embed-ready chunks.

    Markdown-aware: sections are cut at ATX headers (each chunk carries its header
    breadcrumb so a short section embeds with disambiguating context) and fenced code
    blocks are kept intact. Within a section, text is packed into CHUNK_MAX_CHARS
    windows on paragraph/sentence boundaries with CHUNK_OVERLAP carry-over; the char
    ceiling keeps every chunk under the embedder's 512-token truncation limit. The
    returned list is flat and contiguous — the chunk_index a consumer stores is just a
    position in it.

    Args:
        text: Raw text (markdown or plain) to chunk.

    Returns:
        List of chunk strings, capped at MAX_CHUNKS entries.
    """
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    for breadcrumb, body in _sections(text):
        for piece in _window_split(breadcrumb, body):
            chunks.append(piece)
            if len(chunks) >= MAX_CHUNKS:
                return chunks
    return chunks


def _sync_attachment_fts(conn, att_id, note_id, filename, content):
    """Rebuild the FTS row for an attachment (delete-then-insert).

    Args:
        conn: SQLite connection.
        att_id: Attachment primary key.
        note_id: Owning note id (may be None for orphan attachments).
        filename: Original filename stored in the FTS row.
        content: Extracted text content to index.
    """
    conn.execute("DELETE FROM attachments_fts WHERE attachment_id = ?", (att_id,))
    conn.execute(
        "INSERT INTO attachments_fts (attachment_id, note_id, filename, content) VALUES (?, ?, ?, ?)",
        (att_id, note_id, filename, content),
    )


def add_attachment(conn, note_id: int | None, filename: str, mime: str, raw: bytes) -> dict:
    """Store a file attachment and index its content, deduplicating by SHA-256.

    Extracts searchable text, writes the FTS row, and upserts chunked embeddings.
    Returns a metadata dict with the same shape whether this was a new insert or a
    deduplicated existing record; the ``duplicate`` key distinguishes the two cases.

    Args:
        conn: SQLite connection (caller owns commit).
        note_id: Owning note id, or None for an orphan/global attachment.
        filename: Original client filename.
        mime: Resolved MIME type (from resolve_mime).
        raw: Raw file bytes.

    Returns:
        Dict with keys: id, note_id, filename, mime, byte_size, has_text, duplicate.
    """
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


def _att_label(mime: str | None, filename: str | None) -> str:
    """Human label for an attachment's text in an LLM context block (so the model knows
    what each block is, and transcripts/PDF text aren't mislabeled as image summaries)."""
    from . import audio_transcription  # lazy: avoids an import cycle
    if (mime or "").startswith("image/"):
        return "AI image summary"
    if audio_transcription.is_video(mime, filename):
        return "video transcript"
    if audio_transcription.is_audio(mime, filename):
        return "audio transcript"
    return "attachment text"


def context_block_for_note(conn, note_id: int | None, cap: int = 2500, per_att_cap: int = 1500) -> str:
    """A bounded, labelled text digest of a note's attachments for feeding an analyzer/LLM.
    Per attachment, prefer the rich enrichment sidecar (image vision summary or audio/video
    transcript in analysis_md); otherwise fall back to extracted content_text (PDF, text,
    office). Returns "" if the note has no attachment text. This is what makes the note-analysis
    sidecar (and the KB synthesis it feeds) aware of PDFs, transcripts, and text files."""
    if note_id is None:
        return ""
    rows = conn.execute(
        "SELECT filename, mime, analysis_md, content_text FROM attachments WHERE note_id = ? ORDER BY id",
        (note_id,),
    ).fetchall()
    parts: list[str] = []
    for r in rows:
        body = (r["analysis_md"] or "").strip() or (r["content_text"] or "").strip()
        if not body:
            continue
        parts.append(f"[{_att_label(r['mime'], r['filename'])} — {r['filename']}]\n{body[:per_att_cap]}")
    text = "\n\n".join(parts)
    return text[:cap] if (cap and len(text) > cap) else text


def list_for_note(conn, note_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, mime, byte_size, created_at, "
        "analysis_status, analysis_detail, analyzed_at, analysis_md FROM attachments "
        "WHERE note_id = ? ORDER BY created_at DESC",
        (note_id,),
    ).fetchall()
    return [dict(r) for r in rows]
