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

from . import lab_parse

log = logging.getLogger("jbrain")

_INSERT = (
    "INSERT INTO lab_results (note_id, attachment_id, encounter_id, test_name, analyte_key, "
    "value_text, value_num, unit, ref_low, ref_high, ref_text, collected_at, identity_key) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _identity_key(r: dict, source_sha: str) -> str:
    """Stable per-result dedup hash (analyte|date|value|unit|source-sha)."""
    raw = "|".join([r.get("analyte_key") or "", r.get("collected_at") or "",
                    r.get("value_text") or "", r.get("unit") or "", source_sha])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_pdf(att) -> bool:
    return "pdf" in (att["mime"] or "").lower() or (att["filename"] or "").lower().endswith(".pdf")


def stage_attachment(conn, attachment_id: int) -> dict:
    """Parse a PDF attachment and STAGE its results for review (no write to lab_results). Re-
    staging supersedes any prior approval (its approved rows are cleared, status -> extracted).
    Returns {status, doc_type, n, analytes, skipped}."""
    att = conn.execute(
        "SELECT id, note_id, filename, mime, sha256, content_blob, content_text FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att or att["content_blob"] is None or not _is_pdf(att):
        return {"status": None, "doc_type": "unknown", "n": 0, "analytes": 0, "skipped": 0}
    try:
        parsed = lab_parse.parse_lab_pdf(bytes(att["content_blob"]))
    except Exception as exc:  # noqa: BLE001 — a parse failure must never 500 an upload
        log.info("lab_ingest: parse failed for attachment %s (%s)", attachment_id, exc)
        conn.execute("UPDATE attachments SET lab_status='error', lab_extracted_at=datetime('now') WHERE id=?",
                     (att["id"],))
        return {"status": "error", "doc_type": "error", "n": 0, "analytes": 0, "skipped": 0}
    if parsed["doc_type"] == "unknown" or not parsed["results"]:
        return {"status": None, "doc_type": "unknown", "n": 0, "analytes": 0, "skipped": 0}

    text = att["content_text"] or ""
    results, skipped = [], 0
    for r in parsed["results"]:
        if text and r["value_text"] and r["value_text"] not in text:   # faithfulness
            skipped += 1
            continue
        results.append(r)
    payload = {"doc_type": parsed["doc_type"], "results": results, "skipped": skipped,
               "analytes": sorted({r["analyte_key"] for r in results})}
    # Re-staging invalidates any prior approval for this attachment.
    conn.execute("DELETE FROM lab_results WHERE attachment_id = ?", (att["id"],))
    conn.execute("UPDATE attachments SET lab_status='extracted', lab_json=?, "
                 "lab_extracted_at=datetime('now') WHERE id=?", (json.dumps(payload), att["id"]))
    return {"status": "extracted", "doc_type": parsed["doc_type"], "n": len(results),
            "analytes": len(payload["analytes"]), "skipped": skipped}


def staged(conn, attachment_id: int) -> dict | None:
    """The staged extraction for an attachment (decoded), or None if it has no lab status."""
    att = conn.execute(
        "SELECT id, note_id, lab_status, lab_json, lab_extracted_at FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att or not att["lab_status"]:
        return None
    try:
        payload = json.loads(att["lab_json"]) if att["lab_json"] else {"results": []}
    except Exception:  # noqa: BLE001
        payload = {"results": []}
    return {"attachment_id": att["id"], "note_id": att["note_id"], "status": att["lab_status"],
            "extracted_at": att["lab_extracted_at"], "doc_type": payload.get("doc_type"),
            "results": payload.get("results", []), "skipped": payload.get("skipped", 0)}


def approve_attachment(conn, attachment_id: int) -> dict:
    """Write the staged results into lab_results (replacing this attachment's rows) and mark it
    approved. Idempotent."""
    att = conn.execute(
        "SELECT id, note_id, sha256, lab_status, lab_json FROM attachments WHERE id = ?",
        (attachment_id,)).fetchone()
    if not att or att["lab_status"] not in ("extracted", "approved") or not att["lab_json"]:
        return {"approved": 0}
    results = (json.loads(att["lab_json"]) or {}).get("results", [])
    conn.execute("DELETE FROM lab_results WHERE attachment_id = ?", (att["id"],))
    for r in results:
        conn.execute(_INSERT, (
            att["note_id"], att["id"], None, r["test_name"], r["analyte_key"], r["value_text"],
            r["value_num"], r["unit"], r["ref_low"], r["ref_high"], r["ref_text"],
            r["collected_at"], _identity_key(r, att["sha256"])))
    conn.execute("UPDATE attachments SET lab_status='approved' WHERE id=?", (att["id"],))
    conn.commit()
    return {"approved": len(results)}


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
