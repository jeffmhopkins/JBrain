"""Local speech-to-text for audio attachments (faster-whisper, no API key).

When an audio file is attached, a background daemon thread transcribes it with a
local Whisper model — the same "owned, no extra key" ethos as the local
embeddings. The transcript is stored as a READ-ONLY SIDECAR on the attachment row
(reusing attachments.analysis_md, the same slot the image vision summary uses — an
attachment is either an image or audio, never both) and ALSO written to
content_text + the FTS index + chunk embeddings, so the spoken words become
first-class searchable content the AI can read through the existing
search_attachments / read_attachment path.

It runs off the request thread (transcription is multi-second to minutes) and does
not touch the note body, so it never churns note versions or blocks a note edit.
Stale 'pending' rows are reset on boot by image_analysis.reset_stale (same column).
"""
from __future__ import annotations

import io
import os
import threading

from ..db import get_conn

# Container/codec extensions we route to the transcriber even when the browser
# sends a vague mime (voice memos commonly arrive as audio/mp4 or octet-stream).
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
    ".aiff", ".aif", ".amr", ".wma", ".weba", ".3gp", ".caf", ".mka",
}

_MAX_TRANSCRIPT_CHARS = 200_000   # parity with the PDF text cap

_model = None
_model_lock = threading.Lock()


class TranscriptionUnavailable(Exception):
    """faster-whisper isn't installed, or the audio couldn't be decoded."""


def is_audio(mime: str | None, filename: str | None) -> bool:
    if (mime or "").startswith("audio/"):
        return True
    return os.path.splitext((filename or "").lower())[1] in AUDIO_EXTS


def _get_model():
    """Load the Whisper model once and cache it for the process lifetime (mirrors
    the embeddings model). Downloads from Hugging Face on first use."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:  # the runtime image installs it; dev/test may not
                    raise TranscriptionUnavailable(
                        "Audio transcription needs faster-whisper "
                        "(pip install -r requirements-audio.txt)."
                    ) from exc
                from ..config import get_settings
                s = get_settings()
                _model = WhisperModel(s.audio_model, device="cpu", compute_type=s.audio_compute_type)
    return _model


def _transcribe(raw: bytes) -> str:
    model = _get_model()
    try:
        # vad_filter drops long silences (cheaper, fewer hallucinated fillers);
        # language is auto-detected. faster-whisper decodes the container via PyAV.
        segments, _info = model.transcribe(io.BytesIO(raw), vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except TranscriptionUnavailable:
        raise
    except Exception as exc:  # decode/runtime failure → surfaced as an attachment error
        raise TranscriptionUnavailable(f"Could not transcribe audio: {exc}") from exc
    return text[:_MAX_TRANSCRIPT_CHARS]


# --- Status + worker (writes the same analysis_* columns image analysis uses) ---

def _set_status(conn, att_id: int, status: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE attachments SET analysis_status = ?, analysis_detail = ?, "
        "analyzed_at = CASE WHEN ? IN ('done','error') THEN strftime('%Y-%m-%d %H:%M:%f','now') "
        "ELSE analyzed_at END WHERE id = ?",
        (status, detail, status, att_id),
    )


def _mark_error(conn, att_id: int, detail: str) -> None:
    try:
        _set_status(conn, att_id, "error", detail[:500])
        conn.commit()
    except Exception:
        pass


def transcribe(att_id: int) -> None:
    """Background worker (own thread → own thread-local connection)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT filename, mime, content_blob FROM attachments WHERE id = ?", (att_id,)
        ).fetchone()
        if not row or row["content_blob"] is None:
            _mark_error(conn, att_id, "Attachment not found.")
            return
        if not is_audio(row["mime"], row["filename"]):
            _mark_error(conn, att_id, "Not an audio file.")
            return

        # Slow part, before any write lock.
        try:
            text = _transcribe(bytes(row["content_blob"]))
        except TranscriptionUnavailable as exc:
            _mark_error(conn, att_id, str(exc))
            return

        body = text or "(No speech detected.)"
        # Pre-compute chunk vectors outside the write path so the embed compute doesn't
        # hold a write lock; upsert_attachment_embeddings then just (re)writes the rows.
        from . import attachments as att_svc
        from . import embeddings
        chunks = att_svc.chunk_text(text)

        att = conn.execute(
            "SELECT note_id, filename FROM attachments WHERE id = ?", (att_id,)
        ).fetchone()
        if not att:  # deleted mid-transcription
            return
        # Transcript IS the searchable content for audio: store it as content_text,
        # the FTS body, and chunk embeddings, plus the human-readable sidecar.
        conn.execute(
            "UPDATE attachments SET analysis_md = ?, content_text = ? WHERE id = ?",
            (body, text, att_id),
        )
        att_svc._sync_attachment_fts(conn, att_id, att["note_id"], att["filename"], text)
        embeddings.upsert_attachment_embeddings(conn, att_id, att["note_id"], chunks)
        _set_status(conn, att_id, "done")
        conn.commit()
    except Exception as exc:  # never let the worker thread die silently
        try:
            conn.rollback()
        except Exception:
            pass
        _mark_error(conn, att_id, str(exc)[:500])


def start_transcription(conn, att_id: int, *, force: bool = False) -> dict:
    """Request thread: guard double-runs, mark pending, spawn the worker. Needs no
    LLM credentials — the model is local. Returns {"status": ...}."""
    row = conn.execute(
        "SELECT filename, mime, analysis_status FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        return {"status": "error", "detail": "Attachment not found."}
    if not is_audio(row["mime"], row["filename"]):
        return {"status": "error", "detail": "Not an audio file."}
    if row["analysis_status"] == "pending":
        return {"status": "pending"}
    if row["analysis_status"] == "done" and not force:
        return {"status": "done"}

    _set_status(conn, att_id, "pending")
    conn.commit()
    threading.Thread(target=transcribe, args=(att_id,), daemon=True).start()
    return {"status": "pending"}
