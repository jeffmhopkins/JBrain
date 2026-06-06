"""Owner approval for OUTBOUND reference lookups (the medical_reference gate).

The assistant can only PROPOSE an external lookup; nothing is sent until the owner approves the
exact term here. Approval is the single moment the external fetch runs — so the owner can be sure
no PII ever leaves the system in a search query.
"""
from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import external_lookups

router = APIRouter(prefix="/api/external-lookups", tags=["external"], dependencies=[CurrentUser])


@router.post("/{lookup_id}/approve")
def approve_lookup(lookup_id: int):
    """Approve a proposed external lookup AND run it now (the only outbound moment); caches the result
    so the assistant's next call returns it. Returns {ok, found}."""
    conn = get_conn()
    row = external_lookups.decide(conn, lookup_id, approve=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Lookup not found")
    found = False
    if row["tool"] == "medical_reference":
        from ..services import medref
        found = bool(medref.health_topic(conn, row["term"]))   # owner-approved external fetch
    return {"ok": True, "found": found}


@router.post("/{lookup_id}/deny")
def deny_lookup(lookup_id: int):
    """Decline a proposed external lookup — nothing is sent, and the assistant won't re-propose it."""
    conn = get_conn()
    if external_lookups.decide(conn, lookup_id, approve=False) is None:
        raise HTTPException(status_code=404, detail="Lookup not found")
    return {"ok": True}
