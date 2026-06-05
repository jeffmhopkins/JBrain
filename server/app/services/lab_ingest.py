"""Route parsed lab-result rows from a note's PDF attachment into the `lab_results` table.

Deterministic and LLM-free: it runs lab_parse over the attachment bytes and writes one row
per (analyte, date) value. Three safeguards:

  * FAITHFULNESS — a value is written only if its exact text appears in the attachment's
    extracted text (a hard backstop against ever storing a number the document doesn't show).
  * DEDUP — each row carries a deterministic identity_key (analyte | date | value | unit |
    source-file sha) with a partial-unique index, so re-uploading or re-running upserts in
    place instead of duplicating (trend re-exports overlap heavily).
  * AUTO-APPLY — a confident deterministic parse writes directly (the owner asked for clean
    labs to auto-apply); a Review card summarises what landed, linking the source note.
"""
from __future__ import annotations

import hashlib
import logging

from . import lab_parse

log = logging.getLogger("jbrain")

_INSERT = (
    "INSERT INTO lab_results (note_id, attachment_id, encounter_id, test_name, analyte_key, "
    "value_text, value_num, unit, ref_low, ref_high, ref_text, collected_at, identity_key) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_UPDATE = (
    "UPDATE lab_results SET value_num=?, unit=?, ref_low=?, ref_high=?, ref_text=?, "
    "test_name=? WHERE id=?"
)


def _identity_key(r: dict, source_sha: str) -> str:
    """Stable per-result dedup hash. Includes the source file's sha so the same result drawn
    from two DIFFERENT documents stays two rows (provenance differs), while the same document
    re-parsed collapses."""
    raw = "|".join([
        r.get("analyte_key") or "", r.get("collected_at") or "",
        r.get("value_text") or "", r.get("unit") or "", source_sha,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ingest_attachment(conn, attachment_id: int, *, encounter_id: int | None = None) -> dict:
    """Parse one PDF attachment and upsert its lab rows. Returns a summary dict; doc_type
    'unknown' (not a lab PDF) is a no-op."""
    att = conn.execute(
        "SELECT id, note_id, filename, mime, sha256, content_blob, content_text "
        "FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if not att or att["content_blob"] is None:
        return {"doc_type": "unknown", "inserted": 0, "updated": 0, "skipped": 0}
    name = (att["filename"] or "").lower()
    if "pdf" not in (att["mime"] or "").lower() and not name.endswith(".pdf"):
        return {"doc_type": "unknown", "inserted": 0, "updated": 0, "skipped": 0}
    try:
        parsed = lab_parse.parse_lab_pdf(bytes(att["content_blob"]))
    except Exception as exc:  # noqa: BLE001 — a parse failure must never 500 an upload
        log.info("lab_ingest: parse failed for attachment %s (%s)", attachment_id, exc)
        return {"doc_type": "error", "inserted": 0, "updated": 0, "skipped": 0}
    if parsed["doc_type"] == "unknown" or not parsed["results"]:
        return {"doc_type": "unknown", "inserted": 0, "updated": 0, "skipped": 0}

    text = att["content_text"] or ""
    inserted = updated = skipped = 0
    for r in parsed["results"]:
        # Faithfulness: never store a value the document doesn't literally contain.
        if text and r["value_text"] and r["value_text"] not in text:
            skipped += 1
            continue
        ik = _identity_key(r, att["sha256"])
        existing = conn.execute("SELECT id FROM lab_results WHERE identity_key = ?", (ik,)).fetchone()
        if existing:
            conn.execute(_UPDATE, (r["value_num"], r["unit"], r["ref_low"], r["ref_high"],
                                   r["ref_text"], r["test_name"], existing["id"]))
            updated += 1
        else:
            conn.execute(_INSERT, (
                att["note_id"], att["id"], encounter_id, r["test_name"], r["analyte_key"],
                r["value_text"], r["value_num"], r["unit"], r["ref_low"], r["ref_high"],
                r["ref_text"], r["collected_at"], ik))
            inserted += 1
    return {"doc_type": parsed["doc_type"], "inserted": inserted, "updated": updated,
            "skipped": skipped, "analytes": len({r["analyte_key"] for r in parsed["results"]})}


def ingest_note(conn, note_id: int, *, post_review: bool = True) -> dict:
    """Ingest every PDF attachment on a note. Auto-applies (writes rows directly) and, when
    anything landed, posts a Review card linking the note. Returns the combined summary."""
    from . import reviews as reviews_svc
    atts = conn.execute(
        "SELECT id FROM attachments WHERE note_id = ?", (note_id,)).fetchall()
    total = {"doc_type": "unknown", "inserted": 0, "updated": 0, "skipped": 0, "analytes": 0}
    for a in atts:
        s = ingest_attachment(conn, a["id"])
        for k in ("inserted", "updated", "skipped"):
            total[k] += s[k]
        if s["doc_type"] not in ("unknown", "error"):
            total["doc_type"] = s["doc_type"]
            total["analytes"] = max(total["analytes"], s.get("analytes", 0))
    if total["inserted"] or total["updated"]:
        conn.commit()
        if post_review:
            note = conn.execute("SELECT title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
            leaf = (note["title"] or "").split("/")[-1] if note else "note"
            reviews_svc.create_review_item(
                conn, None, title=f"Imported {total['inserted']} lab results",
                message=(f"{total['analytes']} analytes from {leaf} "
                         f"({total['inserted']} new, {total['updated']} updated"
                         + (f", {total['skipped']} skipped" if total["skipped"] else "") + ")."),
                link_slug=note["slug"] if note else None)
            conn.commit()
    return total
