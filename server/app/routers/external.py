"""Owner approval for OUTBOUND reference lookups (the medical_reference gate).

The assistant can only PROPOSE an external lookup; nothing is sent until the owner approves the
exact term here. Approval is the single moment the external fetch runs — so the owner can be sure
no PII ever leaves the system in a search query.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import external_lookups

router = APIRouter(prefix="/api/external-lookups", tags=["external"], dependencies=[CurrentUser])


class ApproveIn(BaseModel):
    """Input body for approving an external lookup, with an optional term edit."""

    term: str | None = None   # the owner may EDIT the term before it's sent (e.g. trim PHI to a topic)


@router.post("/{lookup_id}/approve")
def approve_lookup(lookup_id: int, body: ApproveIn | None = None):
    """Approve a proposed external lookup and execute it immediately.

    This is the only moment an outbound network request is made. The owner may edit
    the search term (e.g. to remove PHI) before approval. The result is cached so the
    assistant's next call returns it without a second fetch.

    Args:
        lookup_id: ID of the pending external lookup to approve.
        body: Optional edited term to use instead of the original proposed term.

    Returns:
        Dict with 'ok', 'found' (bool), and the effective 'term' sent.

    Raises:
        HTTPException: 404 if the lookup is not found.
    """
    conn = get_conn()
    row = external_lookups.decide(conn, lookup_id, approve=True, term=(body.term if body else None))
    if row is None:
        raise HTTPException(status_code=404, detail="Lookup not found")
    found = False
    if row["tool"] in ("medical_reference", "drug_reference"):
        from ..services import medref
        # The owner-approved external fetch (edited term) — caches the result so the assistant's next
        # call returns it. A drug resolves via RxNorm→MedlinePlus Connect; a topic via health-topics.
        found = bool(medref.drug_topic(conn, row["term"]) if row["tool"] == "drug_reference"
                     else medref.health_topic(conn, row["term"]))
    return {"ok": True, "found": found, "term": row["term"]}


@router.post("/{lookup_id}/deny")
def deny_lookup(lookup_id: int):
    """Decline a proposed external lookup; nothing is sent and the assistant won't re-propose it.

    Args:
        lookup_id: ID of the pending external lookup to deny.

    Returns:
        Dict with key 'ok' set to True.

    Raises:
        HTTPException: 404 if the lookup is not found.
    """
    conn = get_conn()
    if external_lookups.decide(conn, lookup_id, approve=False) is None:
        raise HTTPException(status_code=404, detail="Lookup not found")
    return {"ok": True}
