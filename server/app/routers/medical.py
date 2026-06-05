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
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/medical", tags=["medical"], dependencies=[CurrentUser])

_META_KEY = "medical_dests"
# Starter buckets, offered until the owner edits the list (an explicit empty list sticks).
_DEFAULTS = ["Admissions", "Labs", "Clinical Notes", "Procedures", "Medications", "Imaging"]


def _load(conn) -> list[str]:
    raw = get_meta(_META_KEY)
    if raw is None:
        return list(_DEFAULTS)
    try:
        vals = json.loads(raw)
    except Exception:  # noqa: BLE001 — a corrupt value degrades to defaults, never errors
        return list(_DEFAULTS)
    return [str(v) for v in vals if isinstance(v, str)]


class DestsIn(BaseModel):
    names: list[str] = []


@router.get("/destinations")
def list_destinations():
    return {"names": _load(get_conn())}


@router.put("/destinations")
def set_destinations(body: DestsIn):
    """Replace the destination picklist. Each name is sanitized to a safe notes/medical
    sub-path; blanks and case-insensitive duplicates are dropped; capped at 50."""
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


@router.post("/notes/{slug}/extract-labs")
def extract_labs(slug: str):
    """Parse any lab-result PDF(s) attached to this note and upsert the values into
    lab_results (deterministic, no LLM; dedup + faithfulness-guarded). Auto-applies and
    posts a Review card. Idempotent — re-running upserts in place."""
    conn = get_conn()
    note = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)).fetchone()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return lab_ingest.ingest_note(conn, note["id"])
