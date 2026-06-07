"""Financial-capture settings: the owner's preconfigured destination folders.

The Entry sub-selector's Financial sub-type files entries under notes/financial/<dest>/NN;
this stores the picklist of destinations the PWA offers (a JSON array in meta). Owner-only —
config, not content. Names are sanitized the same way the capture route routes them (with the
'financial' root), so the list and the on-disk folders always agree. Folder-only filing: no
parsing or staging (unlike the medical lab pipeline).
"""
import json

from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn, get_meta, set_meta
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/financial", tags=["financial"], dependencies=[CurrentUser])

_META_KEY = "financial_dests"
_ROOT = "financial"
# Starter buckets, offered until the owner edits the list (an explicit empty list sticks).
_DEFAULTS = ["Statements", "Receipts", "Invoices", "Taxes", "Accounts"]


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
    """Replace the destination picklist. Each name is sanitized to a safe notes/financial
    sub-path; blanks and case-insensitive duplicates are dropped; capped at 50."""
    seen: set[str] = set()
    out: list[str] = []
    for n in body.names:
        d = notes_svc.sanitize_dest(n, _ROOT)
        if d and d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
        if len(out) >= 50:
            break
    conn = get_conn()
    set_meta(conn, _META_KEY, json.dumps(out))
    conn.commit()
    return {"names": out}
