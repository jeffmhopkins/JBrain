"""Local speech-to-text for audio AND video attachments (faster-whisper, no API key).

When an audio or video file is attached, a background daemon thread transcribes it with a
local Whisper model (video via its audio track, decoded by PyAV) — the same "owned, no
extra key" ethos as the local
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

from ..db import get_conn, get_meta
from . import llm

# Container/codec extensions we route to the transcriber even when the browser
# sends a vague mime (voice memos commonly arrive as audio/mp4 or octet-stream).
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
    ".aiff", ".aif", ".amr", ".wma", ".weba", ".caf", ".mka",
}
# Video containers — faster-whisper/PyAV decodes the AUDIO track out of these, so a video
# gets a transcript just like an audio file (silent videos resolve to "no speech detected").
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".ogv", ".mkv", ".avi", ".3gp", ".mpeg", ".mpg", ".mpe"}

_MAX_TRANSCRIPT_CHARS = 200_000   # parity with the PDF text cap

_model = None
_model_key: tuple[str, str] | None = None   # (model, compute_type) the cached model was loaded with
_model_lock = threading.Lock()


# --- Runtime-editable settings (DB `meta` override → env/config default) --------------------
# These are read fresh each call so a change in the Settings GUI takes effect with no restart.

def audio_model() -> str:
    from ..config import get_settings
    return get_meta("audio_model") or get_settings().audio_model


def audio_compute_type() -> str:
    from ..config import get_settings
    return get_meta("audio_compute_type") or get_settings().audio_compute_type


def video_frame_interval() -> str:
    from ..config import get_settings
    return get_meta("video_frame_interval") or get_settings().video_frame_interval


def video_frame_max() -> int:
    from ..config import get_settings
    v = get_meta("video_frame_max")
    if v is None or v == "":
        return get_settings().video_frame_max
    try:
        return int(v)
    except (TypeError, ValueError):
        return get_settings().video_frame_max


class TranscriptionUnavailable(Exception):
    """faster-whisper isn't installed, or the audio couldn't be decoded."""


def is_audio(mime: str | None, filename: str | None) -> bool:
    if (mime or "").startswith("audio/"):
        return True
    return os.path.splitext((filename or "").lower())[1] in AUDIO_EXTS


def is_video(mime: str | None, filename: str | None) -> bool:
    if (mime or "").startswith("video/"):
        return True
    return os.path.splitext((filename or "").lower())[1] in VIDEO_EXTS


def is_transcribable(mime: str | None, filename: str | None) -> bool:
    """Audio OR video — both yield a transcript (video via its audio track)."""
    return is_audio(mime, filename) or is_video(mime, filename)


def _get_model():
    """Load (and cache) the Whisper model. Reloads if the configured model/compute_type has
    changed (e.g. edited in the Settings GUI). Downloads from Hugging Face on first use."""
    global _model, _model_key
    want = (audio_model(), audio_compute_type())
    if _model is None or _model_key != want:
        with _model_lock:
            if _model is None or _model_key != want:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:  # the runtime image installs it; dev/test may not
                    raise TranscriptionUnavailable(
                        "Audio transcription needs faster-whisper "
                        "(pip install -r requirements-audio.txt)."
                    ) from exc
                _model = WhisperModel(want[0], device="cpu", compute_type=want[1])
                _model_key = want
    return _model


def _parse_frame_spec(spec: str) -> tuple[str, float]:
    """Parse VIDEO_FRAME_INTERVAL → ('percent', step%) or ('interval', seconds).
    Accepts "25%", "30s", or a bare number (seconds). Falls back to every-25%."""
    s = (spec or "").strip().lower()
    if s.endswith("%"):
        try:
            v = float(s[:-1])
        except ValueError:
            v = 0.0
        return ("percent", v if v > 0 else 25.0)
    if s.endswith("s"):
        s = s[:-1].strip()
    try:
        v = float(s)
    except ValueError:
        return ("percent", 25.0)
    return ("interval", v if v > 0 else 30.0)


def _frame_positions(duration: float, kind: str, value: float, max_frames: int) -> list[float]:
    """Sample positions as fractions of the clip [0,1]. Percent mode is duration-independent;
    interval mode needs the duration (falls back to a single frame if unknown). If the step
    would produce more than max_frames, re-space evenly across the whole clip instead."""
    if kind == "percent":
        step = max(value, 1.0) / 100.0
        fracs = [min(i * step, 1.0) for i in range(int(1.0 / step) + 1)]
    else:  # interval in seconds
        if not duration or duration <= 0:
            return [0.0]
        fracs = [min((i * value) / duration, 1.0) for i in range(int(duration // max(value, 0.001)) + 1)]
    fracs = sorted({round(f, 6) for f in fracs})
    if max_frames and len(fracs) > max_frames:
        return [0.0] if max_frames == 1 else [i / (max_frames - 1) for i in range(max_frames)]
    return fracs


def _extract_frames(raw: bytes, max_edge: int = 1568) -> list[bytes]:
    """Grab JPEG frames sampled across a video, cadence from VIDEO_FRAME_INTERVAL (% or time).
    Best-effort; returns [] if the bytes aren't a decodable video. Uses PyAV (bundled with
    faster-whisper)."""
    import io
    try:
        import av
    except ImportError:
        return []
    max_frames = video_frame_max()
    if max_frames <= 0:
        return []   # VIDEO_FRAME_MAX=0 → frame vision off entirely (transcript only, no LLM call)
    kind, value = _parse_frame_spec(video_frame_interval())
    out: list[bytes] = []
    try:
        with av.open(io.BytesIO(raw)) as c:
            if not c.streams.video:
                return []
            vs = c.streams.video[0]
            dur = (c.duration / av.time_base) if c.duration else (
                float(vs.duration * vs.time_base) if (vs.duration and vs.time_base) else 0.0)
            for f in _frame_positions(dur, kind, value, max_frames):
                try:
                    if dur > 0:
                        target = max(0.0, min(f, 0.999)) * dur
                        c.seek(int(target / vs.time_base), stream=vs, backward=True)
                    img = None
                    for frame in c.decode(vs):
                        img = frame.to_image()
                        break
                    if img is None:
                        continue
                    img.thumbnail((max_edge, max_edge))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=85)
                    out.append(buf.getvalue())
                except Exception:  # noqa: BLE001 — skip an unseekable/undecodable position
                    continue
    except Exception:  # noqa: BLE001 — not a decodable video
        return []
    return out


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
        if not is_transcribable(row["mime"], row["filename"]):
            _mark_error(conn, att_id, "Not an audio or video file.")
            return

        raw = bytes(row["content_blob"])
        # Slow part, before any write lock.
        try:
            text = _transcribe(raw)
        except TranscriptionUnavailable as exc:
            _mark_error(conn, att_id, str(exc))
            return

        # For video, also sample frames every 25% and send them through the vision model for a
        # visual summary (what's on screen, not just what's said). Needs an LLM key; best-effort
        # so a vision hiccup never loses the transcript.
        visual = ""
        if is_video(row["mime"], row["filename"]) and llm.has_credentials():
            try:
                from . import image_analysis
                visual = image_analysis.vision_summary_frames(_extract_frames(raw), row["filename"]).strip()
            except Exception:  # noqa: BLE001
                visual = ""

        searchable = "\n\n".join(p for p in (text, visual) if p)
        if visual:
            body = f"**Visual summary**\n\n{visual}\n\n**Transcript**\n\n{text or '_(no speech detected)_'}"
        else:
            body = text or "(No speech detected.)"
        # Pre-compute chunk vectors outside the write path so the embed compute doesn't
        # hold a write lock; upsert_attachment_embeddings then just (re)writes the rows.
        from . import attachments as att_svc
        from . import embeddings
        chunks = att_svc.chunk_text(searchable)

        att = conn.execute(
            "SELECT note_id, filename FROM attachments WHERE id = ?", (att_id,)
        ).fetchone()
        if not att:  # deleted mid-transcription
            return
        # Transcript (+ any visual summary) IS the searchable content: store it as content_text,
        # the FTS body, and chunk embeddings, plus the human-readable sidecar (body).
        conn.execute(
            "UPDATE attachments SET analysis_md = ?, content_text = ? WHERE id = ?",
            (body, searchable, att_id),
        )
        att_svc._sync_attachment_fts(conn, att_id, att["note_id"], att["filename"], searchable)
        embeddings.upsert_attachment_embeddings(conn, att_id, att["note_id"], chunks)
        _set_status(conn, att_id, "done")
        conn.commit()
        # The transcript changes the note's analyzable content — refresh its AI analysis so the
        # gist/facts/entities reflect what was said. Gated by the "auto-analyze new notes" toggle
        # (parity with the image-vision path) so a manual transcribe with the feature off enriches
        # the panel without spending a note-analysis call. Best-effort; no-ops without an LLM key.
        if att["note_id"] is not None:
            try:
                from . import note_analysis
                if note_analysis.auto_enabled(conn) and note_analysis.analyze(conn, att["note_id"]):
                    conn.commit()
            except Exception:  # noqa: BLE001 — analysis is a bonus; never fail the transcript on it
                pass
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
    if not is_transcribable(row["mime"], row["filename"]):
        return {"status": "error", "detail": "Not an audio or video file."}
    if row["analysis_status"] == "pending":
        return {"status": "pending"}
    if row["analysis_status"] == "done" and not force:
        return {"status": "done"}

    _set_status(conn, att_id, "pending")
    conn.commit()
    threading.Thread(target=transcribe, args=(att_id,), daemon=True).start()
    return {"status": "pending"}
