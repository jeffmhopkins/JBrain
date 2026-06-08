"""Medical-mode capture settings: the owner's preconfigured destination folders.

Medical-mode entries file under notes/medical/<dest>/NN; this stores the picklist of
destinations the PWA's Medical mode offers (a JSON array in meta). Owner-only — these
are config, not content. Names are sanitized the same way the capture route routes them,
so the list and the on-disk folders always agree.
"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn, get_meta, set_meta
from ..services import lab_ingest
from ..services import lab_series
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/medical", tags=["medical"], dependencies=[CurrentUser])

_META_KEY = "medical_dests"
# Starter buckets, offered until the owner edits the list (an explicit empty list sticks).
_DEFAULTS = ["Admissions", "Labs", "Clinical Notes", "Procedures", "Medications", "Imaging"]


def _load(conn) -> list[str]:
    """Load the medical destination list from meta, falling back to defaults on error.

    Args:
        conn: Active database connection (unused; kept for signature consistency).

    Returns:
        List of sanitized destination folder names.
    """
    raw = get_meta(_META_KEY)
    if raw is None:
        return list(_DEFAULTS)
    try:
        vals = json.loads(raw)
    except Exception:  # noqa: BLE001 — a corrupt value degrades to defaults, never errors
        return list(_DEFAULTS)
    return [str(v) for v in vals if isinstance(v, str)]


class DestsIn(BaseModel):
    """Input body for replacing the medical destination picklist."""

    names: list[str] = []


@router.get("/destinations")
def list_destinations():
    """List the configured medical capture destination folders.

    Returns:
        Dict with key 'names' containing a list of destination folder name strings.
    """
    return {"names": _load(get_conn())}


@router.put("/destinations")
def set_destinations(body: DestsIn):
    """Replace the medical destination picklist.

    Each name is sanitized to a safe notes/medical sub-path; blanks and
    case-insensitive duplicates are dropped; the list is capped at 50 entries.

    Args:
        body: New list of destination names.

    Returns:
        Dict with key 'names' containing the sanitized, deduplicated list.
    """
    seen: set[str] = set()
    out: list[str] = []
    for n in body.names:
        d = notes_svc.sanitize_dest(n)
        if d and d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
        if len(out) >= 50:
            break
    conn = get_conn()
    set_meta(conn, _META_KEY, json.dumps(out))
    conn.commit()
    return {"names": out}


class OwnerIn(BaseModel):
    """Input body for setting the medical owner identity."""

    name: str = ""
    dob: str = ""           # ISO 'YYYY-MM-DD'; used to flag a wrong-patient lab upload


@router.get("/owner")
def get_owner():
    """Return the configured owner identity used to flag wrong-patient lab uploads.

    Returns:
        Dict with keys 'name' and 'dob', or an empty dict if not yet configured.
    """
    raw = get_meta("medical_owner")
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


@router.put("/owner")
def set_owner(body: OwnerIn):
    """Set the owner identity used for wrong-patient lab upload detection.

    The DOB is normalized to ISO format (YYYY-MM-DD) so comparisons are consistent
    regardless of the input format (YYYY-MM-DD or MM/DD/YYYY).

    Args:
        body: Owner name and date of birth.

    Returns:
        Dict with normalized 'name' and 'dob' values.

    Raises:
        HTTPException: 422 if a DOB is provided but cannot be parsed as a real date.
    """
    # Normalize the DOB to a validated ISO date so the wrong-patient check compares like with
    # like (a typed '07/04/1990' must not read as different from a document's '1990-07-04').
    from ..services import lab_parse
    dob_raw = body.dob.strip()
    dob = lab_parse.normalize_dob(dob_raw) or ""
    if dob_raw and not dob:
        raise HTTPException(status_code=422, detail="DOB must be a real date (YYYY-MM-DD or MM/DD/YYYY).")
    conn = get_conn()
    set_meta(conn, "medical_owner", json.dumps({"name": body.name.strip(), "dob": dob}))
    conn.commit()
    return {"name": body.name.strip(), "dob": dob}


@router.post("/notes/{slug}/extract-labs")
def extract_labs(slug: str):
    """Stage any lab-result PDFs on this note for owner review (deterministic, no LLM).

    Nothing reaches lab_results until the owner approves; posts a review card.
    Re-running re-extracts (superseding any previous staged results).

    Args:
        slug: Slug of the note whose PDF attachments should be staged.

    Returns:
        Staging result from lab_ingest.stage_note.

    Raises:
        HTTPException: 404 if no live note with the given slug exists.
    """
    conn = get_conn()
    note = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)).fetchone()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return lab_ingest.stage_note(conn, note["id"])


def _att_or_404(conn, attachment_id: int):
    """Raise 404 if the given attachment id does not exist.

    Args:
        conn: Active database connection.
        attachment_id: Attachment id to check.

    Raises:
        HTTPException: 404 if the attachment is not found.
    """
    if conn.execute("SELECT 1 FROM attachments WHERE id = ?", (attachment_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Attachment not found")


@router.get("/attachments/{attachment_id}/labs")
def attachment_labs(attachment_id: int):
    """Return the staged lab extraction for a PDF attachment (status + parsed results).

    Drives the note's Lab Import preview. Returns {status: null} when the attachment has
    no staged extraction rather than raising a 404.

    Args:
        attachment_id: ID of the attachment to inspect.

    Returns:
        Dict with 'status' and 'results' keys; status is null if not a lab PDF.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    _att_or_404(get_conn(), attachment_id)
    return lab_ingest.staged(get_conn(), attachment_id) or {"status": None, "results": []}


@router.get("/attachments/{attachment_id}/labs/series")
def attachment_labs_series(attachment_id: int, analyte: str, unit: str | None = None):
    """Return a trend series built from one analyte's staged (unapproved) results.

    Used to render the preview plot before the owner approves an import.

    Args:
        attachment_id: ID of the attachment whose staged results to use.
        analyte: Analyte name to trend (e.g. 'Glucose').
        unit: Optional unit filter; if omitted the first matching unit is used.

    Returns:
        Series dict compatible with the lab chart (points, bands, domain).
    """
    s = lab_ingest.staged(get_conn(), attachment_id)
    return lab_series.series_from_results((s or {}).get("results", []), analyte, unit)


@router.post("/attachments/{attachment_id}/labs/approve")
def approve_labs(attachment_id: int):
    """Approve the staged lab extraction for an attachment, committing results to lab_results.

    Args:
        attachment_id: ID of the attachment to approve.

    Returns:
        Approval result from lab_ingest.approve_attachment.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    _att_or_404(get_conn(), attachment_id)
    return lab_ingest.approve_attachment(get_conn(), attachment_id)


@router.post("/attachments/{attachment_id}/labs/revoke")
def revoke_labs(attachment_id: int):
    """Revoke a previously approved lab extraction, removing results from lab_results.

    Args:
        attachment_id: ID of the attachment whose approved results to revoke.

    Returns:
        Revocation result from lab_ingest.revoke_attachment.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    _att_or_404(get_conn(), attachment_id)
    return lab_ingest.revoke_attachment(get_conn(), attachment_id)


@router.post("/attachments/{attachment_id}/labs/reanalyze")
def reanalyze_labs(attachment_id: int):
    """Re-parse the PDF and re-stage its labs, superseding any prior approval.

    Equivalent to re-running image analysis from the note.

    Args:
        attachment_id: ID of the attachment to reanalyze.

    Returns:
        New staging result from lab_ingest.stage_attachment.

    Raises:
        HTTPException: 404 if the attachment does not exist.
    """
    _att_or_404(get_conn(), attachment_id)
    conn = get_conn()
    out = lab_ingest.stage_attachment(conn, attachment_id)
    conn.commit()
    return out


@router.get("/labs/pending")
def labs_pending():
    """List attachments with extracted but unapproved labs, for the review-imports pointer.

    Returns:
        Dict with key 'pending' containing a list of attachment dicts (id, note_id,
        note_slug, note_title), ordered by extraction time descending.
    """
    rows = get_conn().execute(
        "SELECT a.id AS attachment_id, a.note_id, n.slug AS note_slug, n.title AS note_title "
        "FROM attachments a JOIN notes n ON n.id = a.note_id "
        "WHERE a.lab_status = 'extracted' AND n.deleted_at IS NULL ORDER BY a.lab_extracted_at DESC").fetchall()
    return {"pending": [dict(r) for r in rows]}


@router.get("/labs/analytes")
def labs_analytes():
    """Return the lab-chart picker feed: one entry per analyte with a value.

    Returns:
        Dict with key 'analytes' containing a list of analyte summary dicts (name,
        latest value, status).
    """
    return {"analytes": lab_series.list_analytes(get_conn())}


@router.get("/labs/series")
def labs_series(analyte: str, unit: str | None = None):
    """Return one analyte's approved trend for the lab chart.

    Includes data points (including censored values), stepped reference-band segments,
    the date domain and value range, and overlapping encounters — for the SVG chart.

    Args:
        analyte: Analyte name to trend (e.g. 'Glucose').
        unit: Optional unit filter; if omitted the first matching unit is used.

    Returns:
        Series dict with points, bands, domain, and encounter overlaps.
    """
    return lab_series.series(get_conn(), analyte, unit)
