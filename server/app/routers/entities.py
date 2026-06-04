"""Browse the canonical entity index (people/orgs/places/things aggregated from the
per-note AI analysis): list/filter entities, and the notes that mention one."""
from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import entity_index

router = APIRouter(prefix="/api/entities", tags=["entities"], dependencies=[CurrentUser])


@router.get("")
def list_entities(type: str | None = None, q: str | None = None, limit: int = 500):
    """Canonical entities, most-mentioned first. Optional type (person/org/place/thing)
    and name substring filters."""
    return entity_index.index(get_conn(), type=type, q=q, limit=limit)


@router.get("/{entity_id}")
def get_entity(entity_id: int):
    """One entity plus the notes that mention it (and its kb article, if any)."""
    out = entity_index.notes_for(get_conn(), entity_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return out
