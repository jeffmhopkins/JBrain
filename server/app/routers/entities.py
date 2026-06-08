"""Browse the canonical entity index (people/orgs/places/things aggregated from the
per-note AI analysis): list/filter entities, the notes that mention one, and the durable
identity controls (merge / split / alias) that survive every entity_index.rebuild()."""
from fastapi import APIRouter, Body, HTTPException

from ..auth import CurrentUser
from ..db import get_conn
from ..services import entity_decisions, entity_index, entity_rebuild

router = APIRouter(prefix="/api/entities", tags=["entities"], dependencies=[CurrentUser])


@router.get("")
def list_entities(type: str | None = None, q: str | None = None, limit: int = 500):
    """List canonical entities, most-mentioned first.

    Args:
        type: Optional type filter (person/org/place/thing).
        q: Optional name substring filter.
        limit: Maximum number of entities to return (default 500).

    Returns:
        List of entity dicts from entity_index.index.
    """
    return entity_index.index(get_conn(), type=type, q=q, limit=limit)


def _entity(conn, entity_id: int):
    """Fetch a single entity row by id, or raise 404.

    Args:
        conn: Active database connection.
        entity_id: Entity id to look up.

    Returns:
        Row with id, type, normalized_key, and canonical_name.

    Raises:
        HTTPException: 404 if the entity is not found.
    """
    row = conn.execute(
        "SELECT id, type, normalized_key, canonical_name FROM entities WHERE id=?", (entity_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return row


@router.get("/status")
def rebuild_status():
    """Return the entity index rebuild status for the Entities page loading indicator.

    A mutating operation records its decision synchronously but defers the slow
    entity_index.rebuild to a coalesced background worker; the UI watches 'generation'
    advance to know the fold has materialized. Defined before /{entity_id} so the
    literal path wins.

    Returns:
        Dict with rebuilding, status, generation, and last_error keys.
    """
    return entity_rebuild.status(get_conn())


@router.post("/merge")
def merge_entities(source_id: int = Body(...), into_id: int = Body(...)):
    """Durably merge source_id into into_id and kick a background index rebuild.

    Records the ruling synchronously, kicks a deferred (coalesced, background) index
    rebuild, and returns the survivor (the into entity) with rebuilding: true. The
    survivor id is stable across rebuilds; the source's notes fold in once the deferred
    rebuild completes (the UI polls /status).

    Args:
        source_id: Entity id to merge away.
        into_id: Entity id to merge into (the survivor).

    Returns:
        Survivor entity detail dict with 'rebuilding' flag.

    Raises:
        HTTPException: 400 if source_id equals into_id.
        HTTPException: 404 if either entity or the survivor entity is not found.
    """
    conn = get_conn()
    src = _entity(conn, source_id)
    dst = _entity(conn, into_id)
    if src["id"] == dst["id"]:
        raise HTTPException(status_code=400, detail="Cannot merge an entity into itself")
    entity_decisions.add(
        conn, "merge", type=dst["type"], norm_a=src["normalized_key"], canonical=dst["normalized_key"],
        display_a=src["canonical_name"], display_b=dst["canonical_name"],
    )
    conn.commit()                       # the DECISION is durable before we defer the rebuild
    entity_rebuild.request_rebuild(conn)
    # The survivor is keyed by (type, normalized_key); its id is stable across rebuilds, so
    # we can return its detail now (its notes reflect the fold only after the deferred pass).
    survivor = conn.execute(
        "SELECT id FROM entities WHERE type=? AND normalized_key=?",
        (dst["type"], dst["normalized_key"]),
    ).fetchone()
    out = entity_index.notes_for(conn, survivor["id"]) if survivor else None
    if out is None:
        raise HTTPException(status_code=404, detail="Survivor entity not found")
    return {**out, "rebuilding": entity_rebuild.status(conn)["rebuilding"]}


@router.post("/split")
def split_entities(a_id: int = Body(...), b_id: int = Body(...)):
    """Durably split a pair of entities, forbidding the heuristic auto-union.

    Args:
        a_id: First entity id.
        b_id: Second entity id.

    Returns:
        Dict with 'ok' and 'rebuilding' keys.

    Raises:
        HTTPException: 400 if a_id equals b_id.
        HTTPException: 404 if either entity is not found.
    """
    conn = get_conn()
    a = _entity(conn, a_id)
    b = _entity(conn, b_id)
    if a["id"] == b["id"]:
        raise HTTPException(status_code=400, detail="Cannot split an entity from itself")
    entity_decisions.add(
        conn, "split", type=a["type"], norm_a=a["normalized_key"], norm_b=b["normalized_key"],
        display_a=a["canonical_name"], display_b=b["canonical_name"],
    )
    conn.commit()                       # the DECISION is durable before we defer the rebuild
    entity_rebuild.request_rebuild(conn)
    return {"ok": True, "rebuilding": entity_rebuild.status(conn)["rebuilding"]}


@router.post("/{entity_id}/aliases")
def add_alias(entity_id: int, display: str = Body(..., embed=True)):
    """Attach an extra display-label alias to an entity, then rebuild the index.

    Args:
        entity_id: Entity to receive the alias.
        display: Human-readable alias label to attach.

    Returns:
        Updated entity detail dict with 'rebuilding' flag.

    Raises:
        HTTPException: 404 if the entity is not found.
        HTTPException: 422 if the display label is blank.
    """
    conn = get_conn()
    e = _entity(conn, entity_id)
    if not (display or "").strip():
        raise HTTPException(status_code=422, detail="alias display is required")
    entity_decisions.add(
        conn, "alias", type=e["type"], norm_a=display, norm_b=e["normalized_key"], display_a=display,
    )
    conn.commit()                       # the DECISION is durable before we defer the rebuild
    entity_rebuild.request_rebuild(conn)
    out = entity_index.notes_for(conn, entity_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {**out, "rebuilding": entity_rebuild.status(conn)["rebuilding"]}


@router.delete("/{entity_id}/aliases/{alias_norm}")
def remove_alias(entity_id: int, alias_norm: str):
    """Remove a user alias decision for this entity by normalized key, then rebuild.

    Args:
        entity_id: Entity whose alias to remove.
        alias_norm: Normalized key of the alias decision to remove.

    Returns:
        Dict with 'ok' and 'rebuilding' keys.

    Raises:
        HTTPException: 404 if the entity is not found.
    """
    conn = get_conn()
    e = _entity(conn, entity_id)
    na = entity_index.normalize(alias_norm)
    for r in conn.execute(
        "SELECT id FROM entity_decisions WHERE kind='alias' AND type=? AND norm_a=? AND norm_b=?",
        (e["type"], na, e["normalized_key"]),
    ).fetchall():
        entity_decisions.remove(conn, r["id"])
    conn.commit()                       # the DECISION is durable before we defer the rebuild
    entity_rebuild.request_rebuild(conn)
    return {"ok": True, "rebuilding": entity_rebuild.status(conn)["rebuilding"]}


@router.get("/{entity_id}/decisions")
def list_decisions(entity_id: int):
    """List identity decisions (merge/split/alias) for this entity's type.

    Args:
        entity_id: Entity whose type's decision ledger to return.

    Returns:
        List of decision dicts from entity_decisions.list_for.

    Raises:
        HTTPException: 404 if the entity is not found.
    """
    conn = get_conn()
    e = _entity(conn, entity_id)
    return entity_decisions.list_for(conn, type=e["type"])


@router.get("/resolve")
def resolve_entity(name: str):
    """Resolve a name or alias to its canonical entity.

    A nickname search collapses to one person card that links to the canonical article.
    Defined before /{entity_id} so the literal path wins.

    Args:
        name: Display name or alias string to resolve.

    Returns:
        Entity detail dict, or {'resolved': None} if not found.
    """
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
    """Return one entity with the notes that mention it and its kb article if any.

    Args:
        entity_id: Entity id to retrieve.

    Returns:
        Entity detail dict from entity_index.notes_for.

    Raises:
        HTTPException: 404 if the entity is not found.
    """
    out = entity_index.notes_for(get_conn(), entity_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return out
