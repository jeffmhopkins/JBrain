"""Browse the canonical entity index (people/orgs/places/things aggregated from the
per-note AI analysis): list/filter entities, the notes that mention one, and the durable
identity controls (merge / split / alias) that survive every entity_index.rebuild()."""
from fastapi import APIRouter, Body, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import entity_decisions, entity_index

router = APIRouter(prefix="/api/entities", tags=["entities"], dependencies=[CurrentUser])


@router.get("")
def list_entities(type: str | None = None, q: str | None = None, limit: int = 500):
    """Canonical entities, most-mentioned first. Optional type (person/org/place/thing)
    and name substring filters."""
    return entity_index.index(get_conn(), type=type, q=q, limit=limit)


def _entity(conn, entity_id: int):
    """(type, normalized_key, canonical_name) for an entity id, or 404."""
    row = conn.execute(
        "SELECT id, type, normalized_key, canonical_name FROM entities WHERE id=?", (entity_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return row


@router.post("/merge")
def merge_entities(source_id: int = Body(...), into_id: int = Body(...)):
    """Durably merge `source_id` into `into_id`: record the ruling, rebuild the index, and
    return the survivor (the `into` entity)."""
    conn = get_conn()
    src = _entity(conn, source_id)
    dst = _entity(conn, into_id)
    if src["id"] == dst["id"]:
        raise HTTPException(status_code=400, detail="Cannot merge an entity into itself")
    entity_decisions.add(
        conn, "merge", type=dst["type"], norm_a=src["normalized_key"], canonical=dst["normalized_key"],
        display_a=src["canonical_name"], display_b=dst["canonical_name"],
    )
    conn.commit()
    entity_index.rebuild(conn)
    # The survivor is keyed by (type, normalized_key); its id is stable across rebuilds.
    survivor = conn.execute(
        "SELECT id FROM entities WHERE type=? AND normalized_key=?",
        (dst["type"], dst["normalized_key"]),
    ).fetchone()
    out = entity_index.notes_for(conn, survivor["id"]) if survivor else None
    if out is None:
        raise HTTPException(status_code=404, detail="Survivor entity not found after merge")
    return out


@router.post("/split")
def split_entities(a_id: int = Body(...), b_id: int = Body(...)):
    """Durably split a pair: forbid the heuristic auto-union of these two, rebuild."""
    conn = get_conn()
    a = _entity(conn, a_id)
    b = _entity(conn, b_id)
    if a["id"] == b["id"]:
        raise HTTPException(status_code=400, detail="Cannot split an entity from itself")
    entity_decisions.add(
        conn, "split", type=a["type"], norm_a=a["normalized_key"], norm_b=b["normalized_key"],
        display_a=a["canonical_name"], display_b=b["canonical_name"],
    )
    conn.commit()
    entity_index.rebuild(conn)
    return {"ok": True}


@router.post("/{entity_id}/aliases")
def add_alias(entity_id: int, display: str = Body(..., embed=True)):
    """Attach an extra alias (by display label) to this entity, rebuild, return the detail."""
    conn = get_conn()
    e = _entity(conn, entity_id)
    if not (display or "").strip():
        raise HTTPException(status_code=422, detail="alias display is required")
    entity_decisions.add(
        conn, "alias", type=e["type"], norm_a=display, norm_b=e["normalized_key"], display_a=display,
    )
    conn.commit()
    entity_index.rebuild(conn)
    out = entity_index.notes_for(conn, entity_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return out


@router.delete("/{entity_id}/aliases/{alias_norm}")
def remove_alias(entity_id: int, alias_norm: str):
    """Remove a user 'alias' decision for this entity (by its normalized key), rebuild."""
    conn = get_conn()
    e = _entity(conn, entity_id)
    na = entity_index.normalize(alias_norm)
    for r in conn.execute(
        "SELECT id FROM entity_decisions WHERE kind='alias' AND type=? AND norm_a=? AND norm_b=?",
        (e["type"], na, e["normalized_key"]),
    ).fetchall():
        entity_decisions.remove(conn, r["id"])
    conn.commit()
    entity_index.rebuild(conn)
    return {"ok": True}


@router.get("/{entity_id}/decisions")
def list_decisions(entity_id: int):
    """The identity decisions touching this entity's type (merge/split/alias ledger)."""
    conn = get_conn()
    e = _entity(conn, entity_id)
    return entity_decisions.list_for(conn, type=e["type"])


@router.get("/resolve")
def resolve_entity(name: str):
    """Resolve a name OR alias to its CANONICAL entity (so a nickname search collapses to one
    person card that links to the canonical article). Returns the entity + its notes, or
    {"resolved": None}. Defined before /{entity_id} so the literal path wins."""
    conn = get_conn()
    norm = entity_index.normalize(name or "")
    if not norm:
        return {"resolved": None}
    row = conn.execute(
        "SELECT id FROM entities WHERE normalized_key=? ORDER BY note_count DESC LIMIT 1", (norm,)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT entity_id AS id FROM entity_aliases WHERE alias_norm=? LIMIT 1", (norm,)).fetchone()
    if not row:
        return {"resolved": None}
    return entity_index.notes_for(conn, row["id"])


@router.get("/{entity_id}")
def get_entity(entity_id: int):
    """One entity plus the notes that mention it (and its kb article, if any)."""
    out = entity_index.notes_for(get_conn(), entity_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return out
