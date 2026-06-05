"""Staged lab ingestion: a lab PDF is EXTRACTED to a per-attachment sidecar for preview, and
its values reach the authoritative `lab_results` table only when the owner APPROVES — mirroring
the image-analysis flow (extract → review on the note → approve / re-analyze / revoke).

Deterministic and LLM-free. Two safeguards survive into the staged data:
  * FAITHFULNESS — a value is staged only if its exact text appears in the attachment's
    extracted text (a hard backstop against ever storing a number the document doesn't show).
  * DEDUP — each approved row carries a deterministic identity_key (analyte | date | value |
    unit | source-file sha); approving replaces the attachment's rows, so it's idempotent.

Status (attachments.lab_status): NULL (not a lab PDF) | extracted | approved | error.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from . import lab_parse
from .lab_parse import is_faithful as _is_faithful   # hoisted to lab_parse (shared by lab_vision)

log = logging.getLogger("jbrain")

_INSERT = (
    "INSERT OR IGNORE INTO lab_results (note_id, attachment_id, encounter_id, test_name, analyte_key, "
    "value_text, value_num, unit, ref_low, ref_high, ref_text, collected_at, collected_time, "
    "identity_key, source) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _identity_key(r: dict, source_sha: str) -> str:
    """Stable per-result dedup hash (analyte|date|value|unit|source-sha)."""
    raw = "|".join([r.get("analyte_key") or "", r.get("collected_at") or "",
                    r.get("value_text") or "", r.get("unit") or "", source_sha])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_pdf(att) -> bool:
    return "pdf" in (att["mime"] or "").lower() or (att["filename"] or "").lower().endswith(".pdf")


def _identity_state(conn, identity: dict) -> str:
    """Three-state patient check against the configured owner (P1):
      * 'mismatch'   — owner DOB set AND the document's DOB differs → wrong patient, warn loudly.
      * 'unverified' — owner DOB set but the document has no comparable DOB → cannot confirm, say
                       so (never let 'not contradicted' read as 'verified same patient').
      * ''           — no owner configured (feature off), or owner set and DOBs agree.
    DOB is the only field compared (names like 'DOE,JANE' vs 'Jane Doe' are too fuzzy
    to assert on); both sides are normalized to ISO so mere format drift never fabricates a
    mismatch."""
    try:
        from ..db import get_meta
        owner = json.loads(get_meta("medical_owner") or "{}")
    except Exception:  # noqa: BLE001
        owner = {}
    odob = lab_parse.normalize_dob((owner or {}).get("dob") or "")
    if not odob:
        return ""                                      # feature off — no owner identity configured
    ddob = lab_parse.normalize_dob((identity or {}).get("dob") or "")
    if not ddob:
        return "unverified"                            # owner known, document DOB missing/unreadable
    return "mismatch" if ddob != odob else ""


def _is_image(att) -> bool:
    return (att["mime"] or "").lower().startswith("image/") or bool(
        re.search(r"\.(png|jpe?g|gif|webp|heic|tiff?|bmp)$", (att["filename"] or "").lower()))


def _extract(att) -> dict | None:
    """Pick the extractor by modality and return a lab_parse-shaped dict (or None for a non-lab
    attachment). A text PDF goes through the deterministic parser; a photo/screenshot, or a PDF
    with no extractable text (scanned/image-only), is rendered and routed to the OCR-gated
    vision path (Phase 1). Either way the result feeds the SAME staging lifecycle below."""
    blob = bytes(att["content_blob"])
    if _is_pdf(att):
        parsed = lab_parse.parse_lab_pdf(blob)
        if parsed["doc_type"] != "unknown" and parsed["results"]:
            return parsed
        # Distinguish a SCANNED lab (near-empty text layer → route to vision) from a text PDF
        # that simply isn't a lab (lots of text, no lab structure → not our business, stay
        # silent). Only a genuine scan should ever reach the visible 'image_unparsed' state.
        if len((parsed.get("visible_text") or "").strip()) >= 40:
            return parsed                              # has real text but no labs -> 'unknown'
        from . import lab_vision
        imgs = lab_vision.render_pdf_to_images(blob)
        if imgs:
            return lab_vision.parse_lab_image(imgs)
        # A scan we can't render (no pypdfium2): surface a VISIBLE 'unreadable' state (hard
        # rule #3), not the bare 'unknown' the reviewer would read as 'no labs in this file'.
        return {"doc_type": "image_unparsed", "confidence": 0.0, "results": [], "pages": parsed.get("pages", 0),
                "skips": [{"analyte": "", "date": None, "value": "",
                           "reason": "scanned PDF — no PDF renderer (pypdfium2) available to read it"}]}
    if _is_image(att):
        from . import lab_vision
        return lab_vision.parse_lab_image([blob])
    return None


def stage_attachment(conn, attachment_id: int) -> dict:
    """Parse an attachment (PDF or image) and STAGE its results for review (no write to
    lab_results). Re-staging supersedes any prior approval (its approved rows are cleared,
    status -> extracted). Returns {status, doc_type, n, analytes, skipped}."""
    none_result = {"status": None, "doc_type": "unknown", "n": 0, "analytes": 0, "skipped": 0}
    att = conn.execute(
        "SELECT id, note_id, filename, mime, sha256, content_blob, content_text FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att or att["content_blob"] is None or not (_is_pdf(att) or _is_image(att)):
        return none_result
    try:
        parsed = _extract(att)
    except Exception as exc:  # noqa: BLE001 — a parse failure must never 500 an upload
        log.info("lab_ingest: parse failed for attachment %s (%s)", attachment_id, exc)
        conn.execute("UPDATE attachments SET lab_status='error', lab_extracted_at=datetime('now') WHERE id=?",
                     (att["id"],))
        return {"status": "error", "doc_type": "error", "n": 0, "analytes": 0, "skipped": 0}
    if parsed is None or parsed["doc_type"] == "unknown":
        return none_result
    # An image/scan we recognised as a lab but couldn't read gets a VISIBLE status (never a
    # silent NULL the user mistakes for "no labs found") so the note can offer Re-analyze.
    if parsed["doc_type"] == "image_unparsed":
        payload = {"doc_type": "image_unparsed", "results": [], "skipped": len(parsed.get("skips", [])),
                   "skips": parsed.get("skips", []), "analytes": [], "low_confidence": 0}
        conn.execute("UPDATE attachments SET lab_status='image_unparsed', lab_json=?, "
                     "lab_extracted_at=datetime('now') WHERE id=?", (json.dumps(payload), att["id"]))
        return {"status": "image_unparsed", "doc_type": "image_unparsed", "n": 0, "analytes": 0,
                "skipped": payload["skipped"]}

    # Faithfulness corpus for the DETERMINISTIC text-PDF path: the VISIBLE words the parser saw
    # (P2 — defeats hidden / white-on-white injected text). Image-derived results (lab_vision)
    # are NOT re-checked here: parse_lab_image already did their faithfulness against the page OCR
    # (corroborated -> medium, unconfirmed -> kept+flagged low). Re-filtering vision values
    # against a SCAN's own text layer — often just a header (name/date) with none of the tabular
    # values — would wrongly drop every correct value ("not found verbatim in document").
    text = "" if parsed["doc_type"] == "lab_image" else (parsed.get("visible_text") or att["content_text"] or "")
    results = []
    skips = list(parsed.get("skips", []))              # extractor-level drops (e.g. OCR cross-read)
    for r in parsed["results"]:
        # Itemized, reasoned drops (F6) — not a bare count. A dropped value (failed faithfulness
        # or no date) is shown to the reviewer so a real-but-rejected result is never silent.
        if text and r["value_text"] and not _is_faithful(r["value_text"], text):
            skips.append({"analyte": r["analyte_key"], "date": r.get("collected_at"),
                          "value": r["value_text"], "reason": "not found verbatim in document"})
            continue
        if not r.get("collected_at"):                  # F7: a dateless row is invisible to every
            skips.append({"analyte": r["analyte_key"], "date": None,                # read path —
                          "value": r["value_text"], "reason": "no collection date"})  # don't store it
            continue
        results.append(r)
    identity = parsed.get("identity") or {}
    payload = {"doc_type": parsed["doc_type"], "results": results, "skipped": len(skips),
               "skips": skips, "analytes": sorted({r["analyte_key"] for r in results}),
               "low_confidence": sum(1 for r in results if r.get("confidence") == "low"),
               "identity": identity,                   # P1: show whose results these are at review
               "identity_state": _identity_state(conn, identity)}
    # Re-staging invalidates any prior approval for this attachment.
    conn.execute("DELETE FROM lab_results WHERE attachment_id = ?", (att["id"],))
    conn.execute("UPDATE attachments SET lab_status='extracted', lab_json=?, "
                 "lab_extracted_at=datetime('now') WHERE id=?", (json.dumps(payload), att["id"]))
    return {"status": "extracted", "doc_type": parsed["doc_type"], "n": len(results),
            "analytes": len(payload["analytes"]), "skipped": len(skips)}


def staged(conn, attachment_id: int) -> dict | None:
    """The lab state of an attachment: its staged extraction (decoded) AND how many rows it has
    in lab_results. Returns a dict for ANY existing attachment — `status` is None when it was
    never staged for review (e.g. imported under the old auto-apply path), and `imported`
    surfaces those legacy rows so the note can offer Re-analyze / Remove."""
    att = conn.execute(
        "SELECT id, note_id, lab_status, lab_json, lab_extracted_at FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att:
        return None
    imported = conn.execute(
        "SELECT COUNT(*) AS c FROM lab_results WHERE attachment_id = ?", (attachment_id,)).fetchone()["c"]
    try:
        payload = json.loads(att["lab_json"]) if att["lab_json"] else {"results": []}
    except Exception:  # noqa: BLE001
        payload = {"results": []}
    return {"attachment_id": att["id"], "note_id": att["note_id"], "status": att["lab_status"],
            "imported": imported, "extracted_at": att["lab_extracted_at"], "doc_type": payload.get("doc_type"),
            "results": payload.get("results", []), "skipped": payload.get("skipped", 0),
            "skips": payload.get("skips", []), "low_confidence": payload.get("low_confidence", 0),
            "identity": payload.get("identity", {}), "identity_state": payload.get("identity_state", "")}


def approve_attachment(conn, attachment_id: int) -> dict:
    """Write the staged results into lab_results (replacing this attachment's rows) and mark it
    approved. Idempotent."""
    att = conn.execute(
        "SELECT id, note_id, sha256, lab_status, lab_json FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att or att["lab_status"] not in ("extracted", "approved") or not att["lab_json"]:
        return {"approved": 0}
    payload = json.loads(att["lab_json"]) or {}
    results = payload.get("results", [])
    source = payload.get("doc_type")                   # P3: how these rows were extracted
    conn.execute("DELETE FROM lab_results WHERE attachment_id = ?", (att["id"],))
    inserted = 0
    for r in results:
        # INSERT OR IGNORE: a duplicate identity_key means this exact result is ALREADY in the
        # trends (e.g. re-importing the same export, or a byte-identical file) — skip it rather
        # than 500 on the unique index. Approve stays idempotent.
        cur = conn.execute(_INSERT, (
            att["note_id"], att["id"], None, r["test_name"], r["analyte_key"], r["value_text"],
            r["value_num"], r["unit"], r["ref_low"], r["ref_high"], r["ref_text"],
            r["collected_at"], r.get("collected_time"), _identity_key(r, att["sha256"]), source))
        inserted += cur.rowcount
    conn.execute("UPDATE attachments SET lab_status='approved' WHERE id=?", (att["id"],))
    conn.commit()
    return {"approved": inserted, "duplicates": len(results) - inserted}


def revoke_attachment(conn, attachment_id: int) -> dict:
    """Remove this attachment's rows from lab_results, but keep the staged extraction (so it can
    be re-approved). Status -> extracted."""
    removed = conn.execute("DELETE FROM lab_results WHERE attachment_id = ?", (attachment_id,)).rowcount
    conn.execute("UPDATE attachments SET lab_status='extracted' WHERE id=? AND lab_status='approved'",
                 (attachment_id,))
    conn.commit()
    return {"removed": removed}


def stage_note(conn, note_id: int, *, post_review: bool = True) -> dict:
    """Stage every PDF attachment on a note for review (no auto-apply). Posts a Review card when
    anything was extracted, linking the note so the owner can approve it."""
    from . import reviews as reviews_svc
    atts = conn.execute("SELECT id FROM attachments WHERE note_id = ?", (note_id,)).fetchall()
    total = {"doc_type": "unknown", "staged": 0, "analytes": 0, "skipped": 0}
    for a in atts:
        s = stage_attachment(conn, a["id"])
        total["staged"] += s["n"]
        total["skipped"] += s["skipped"]
        if s["status"] == "extracted":
            total["doc_type"] = s["doc_type"]
            total["analytes"] = max(total["analytes"], s["analytes"])
    conn.commit()
    if total["staged"] and post_review:
        note = conn.execute("SELECT title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
        leaf = (note["title"] or "").split("/")[-1] if note else "note"
        reviews_svc.create_review_item(
            conn, None, title=f"{total['staged']} lab results to review",
            message=f"Extracted {total['analytes']} analytes from {leaf} — open it to preview and approve.",
            link_slug=note["slug"] if note else None)
        conn.commit()
    return total
