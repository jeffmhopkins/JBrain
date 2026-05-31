"""Attachment write pipeline: store text/md content + FTS + chunked embeddings.

Centralised (mirrors notes.upsert_note) so any caller indexes consistently.
"""
from __future__ import annotations

import hashlib

from . import embeddings

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024  # 2 MB cap for text/md
ALLOWED_MIME = {"text/plain", "text/markdown", "text/x-markdown"}
ALLOWED_EXT = {".txt", ".md", ".markdown"}

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
MAX_CHUNKS = 200


def mime_for(filename: str, declared: str | None) -> str | None:
    """Resolve a usable mime for text/md, or None if not allowed."""
    lower = filename.lower()
    ext_ok = any(lower.endswith(e) for e in ALLOWED_EXT)
    if declared in ALLOWED_MIME:
        return "text/markdown" if "markdown" in declared else declared
    if ext_ok:
        return "text/markdown" if lower.endswith((".md", ".markdown")) else "text/plain"
    return None


def chunk_text(text: str) -> list[str]:
    """Split text into ~CHUNK_CHARS windows with overlap, capped at MAX_CHUNKS."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n and len(chunks) < MAX_CHUNKS:
        end = min(start + CHUNK_CHARS, n)
        # Prefer to break on a paragraph/newline boundary near the window end.
        if end < n:
            brk = text.rfind("\n", start + CHUNK_CHARS - CHUNK_OVERLAP, end)
            if brk != -1 and brk > start:
                end = brk
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in chunks if c]


def _sync_attachment_fts(conn, att_id: int, note_id: int | None, filename: str, content: str) -> None:
    conn.execute("DELETE FROM attachments_fts WHERE attachment_id = ?", (att_id,))
    conn.execute(
        "INSERT INTO attachments_fts (attachment_id, note_id, filename, content) "
        "VALUES (?, ?, ?, ?)",
        (att_id, note_id, filename, content),
    )


def add_attachment(conn, note_id: int | None, filename: str, mime: str, raw: bytes) -> dict:
    """Decode, store, and index a text/md attachment. Idempotent per (note, sha256)."""
    content_text = raw.decode("utf-8")  # caller guarantees utf-8 (else 415)
    sha = hashlib.sha256(raw).hexdigest()

    existing = conn.execute(
        "SELECT * FROM attachments WHERE note_id IS ? AND sha256 = ?", (note_id, sha)
    ).fetchone()
    if existing:
        return {**dict(existing), "duplicate": True}

    cur = conn.execute(
        "INSERT INTO attachments (note_id, filename, mime, content_text, byte_size, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (note_id, filename, mime, content_text, len(raw), sha),
    )
    att_id = cur.lastrowid
    _sync_attachment_fts(conn, att_id, note_id, filename, content_text)
    chunks = chunk_text(content_text)
    embeddings.upsert_attachment_embeddings(conn, att_id, note_id, chunks)
    return {
        "id": att_id, "note_id": note_id, "filename": filename, "mime": mime,
        "byte_size": len(raw), "chunk_count": len(chunks), "duplicate": False,
    }


def delete_attachment(conn, att_id: int) -> None:
    conn.execute("DELETE FROM attachments_fts WHERE attachment_id = ?", (att_id,))
    embeddings.delete_attachment_embeddings(conn, att_id)
    conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))


def list_for_note(conn, note_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, mime, byte_size, created_at FROM attachments "
        "WHERE note_id = ? ORDER BY created_at DESC",
        (note_id,),
    ).fetchall()
    return [dict(r) for r in rows]
