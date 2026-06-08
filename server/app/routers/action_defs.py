"""Action recipes REST API: list / get / create / update / delete / sync /
validate + a primitive catalog. Recipes live in the action_defs table (repo-
seeded; user edits set source='user', locked=1). Shipped (source='repo')
recipes are READ-ONLY — duplicate to a custom action to edit. The server is the
YAML parser: get/validate return a parsed step tree so the PWA can visualise a
pipeline without a client-side YAML library.
"""
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import pipeline

router = APIRouter(prefix="/api/action-defs", tags=["actions"], dependencies=[CurrentUser])


class RecipeIn(BaseModel):
    """Input body carrying a YAML recipe string."""

    recipe_yaml: str


def _parse(text: str) -> dict:
    """Parse a YAML string into a dict, raising HTTP 400 on invalid YAML or non-mapping.

    Args:
        text: Raw YAML string to parse.

    Returns:
        Parsed YAML as a dict.

    Raises:
        HTTPException: 400 if the YAML is invalid or the top-level value is not a mapping.
    """
    try:
        doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400, detail="Recipe must be a YAML mapping.")
    return doc


def _flatten(steps) -> list[str]:
    """Flatten a recipe step tree into an ordered list of action/for_each tokens.

    Args:
        steps: List of step dicts from a parsed recipe.

    Returns:
        Flat list of step names (e.g. ['for_each', 'llm', 'note_append']).
    """
    out: list[str] = []
    for s in steps or []:
        if isinstance(s, dict) and "for_each" in s:
            out.append("for_each")
            out += _flatten(s.get("steps"))
        elif isinstance(s, dict) and s.get("do"):
            out.append(s["do"])
    return out


def _ref_count(conn, type_: str) -> int:
    """Return the number of workflows currently referencing an action type.

    Args:
        conn: Active database connection.
        type_: Action type name to count references for.

    Returns:
        Integer count of referencing workflows.
    """
    return conn.execute(
        "SELECT COUNT(*) c FROM workflows WHERE action_type = ?", (type_,)
    ).fetchone()["c"]


@router.get("/primitives")
def primitives():
    """Return the catalog of built-in pipeline primitives.

    Returns:
        Primitive catalog dict from pipeline.primitive_catalog.
    """
    return pipeline.primitive_catalog()


@router.post("/validate")
def validate(body: RecipeIn):
    """Parse and validate a recipe YAML, returning warnings and the parsed recipe.

    Args:
        body: YAML string of the recipe to validate.

    Returns:
        Dict with 'warnings' list and 'recipe' parsed dict.

    Raises:
        HTTPException: 400 if the YAML is invalid.
    """
    recipe = _parse(body.recipe_yaml)
    return {"warnings": pipeline.validate_recipe(recipe), "recipe": recipe}


@router.post("/sync")
def sync():
    """Re-ingest repo action definitions from YAML files.

    Returns:
        Dict with 'synced' count of action definitions ingested.
    """
    return {"synced": pipeline.ingest_repo_action_defs(get_conn())}


@router.get("")
def list_defs():
    """List all action definitions with a summary of their step pipeline.

    Returns:
        List of dicts with type, source, locked, category, num_steps, and summary.
    """
    rows = get_conn().execute(
        "SELECT type, recipe_yaml, source, locked FROM action_defs ORDER BY type"
    ).fetchall()
    out = []
    for r in rows:
        try:
            recipe = yaml.safe_load(r["recipe_yaml"]) or {}
        except Exception:  # noqa: BLE001
            recipe = {}
        seq = _flatten(recipe.get("steps"))
        out.append({
            "type": r["type"], "source": r["source"], "locked": bool(r["locked"]),
            "category": recipe.get("category") or "Other",
            "num_steps": len(seq), "summary": " → ".join(seq),
        })
    return out


@router.get("/{type}")
def get_def(type: str):
    """Return a single action definition with parsed recipe, warnings, and workflow ref count.

    Args:
        type: Action type identifier.

    Returns:
        Dict with type, source, locked, recipe_yaml, recipe, warnings, and ref_count.

    Raises:
        HTTPException: 404 if the action type does not exist.
    """
    row = get_conn().execute(
        "SELECT type, recipe_yaml, source, locked FROM action_defs WHERE type = ?", (type,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    recipe = yaml.safe_load(row["recipe_yaml"]) or {}
    return {
        "type": row["type"], "source": row["source"], "locked": bool(row["locked"]),
        "recipe_yaml": row["recipe_yaml"], "recipe": recipe,
        "warnings": pipeline.validate_recipe(recipe),
        "ref_count": _ref_count(get_conn(), type),
    }


@router.post("")
def create_def(body: RecipeIn):
    """Create a new user-owned action definition from a YAML recipe.

    Args:
        body: YAML string for the new recipe (must declare a 'type' field).

    Returns:
        Dict with 'ok', 'type', and 'warnings'.

    Raises:
        HTTPException: 400 if the YAML is invalid or missing a 'type' field.
        HTTPException: 409 if the type already exists or is a reserved alias.
    """
    recipe = _parse(body.recipe_yaml)
    t = recipe.get("type")
    if not t:
        raise HTTPException(status_code=400, detail="Recipe must declare a 'type'.")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM action_defs WHERE type = ?", (t,)).fetchone():
        raise HTTPException(status_code=409, detail=f"Action type '{t}' already exists.")
    pipeline._repo_defs()  # ensure aliases loaded
    if t in pipeline._ALIASES:
        raise HTTPException(status_code=409, detail=f"'{t}' is a reserved alias.")
    conn.execute(
        "INSERT INTO action_defs (type, recipe_yaml, source, locked) VALUES (?, ?, 'user', 1)",
        (t, body.recipe_yaml),
    )
    conn.commit()
    return {"ok": True, "type": t, "warnings": pipeline.validate_recipe(recipe)}


@router.put("/{type}")
def update_def(type: str, body: RecipeIn):
    """Replace a user-owned action definition's YAML recipe.

    Shipped (source='repo') actions are read-only and cannot be updated here.

    Args:
        type: Action type identifier to update.
        body: New YAML recipe (must declare the same 'type').

    Returns:
        Dict with 'ok' and 'warnings'.

    Raises:
        HTTPException: 400 if the YAML is invalid or attempts a rename.
        HTTPException: 403 if the action is a shipped (repo-sourced) definition.
        HTTPException: 404 if the action type does not exist.
    """
    conn = get_conn()
    row = conn.execute("SELECT source FROM action_defs WHERE type = ?", (type,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["source"] == "repo":
        raise HTTPException(status_code=403,
                            detail="Shipped actions are read-only — duplicate it to edit.")
    recipe = _parse(body.recipe_yaml)
    if recipe.get("type") != type:
        raise HTTPException(status_code=400,
                            detail="Renaming an action isn't supported; create a new one.")
    conn.execute(
        "UPDATE action_defs SET recipe_yaml = ?, locked = 1, updated_at = datetime('now') WHERE type = ?",
        (body.recipe_yaml, type),
    )
    conn.commit()
    return {"ok": True, "warnings": pipeline.validate_recipe(recipe)}


@router.delete("/{type}")
def delete_def(type: str):
    """Delete a user-owned action definition.

    Shipped (source='repo') actions cannot be deleted. The type must not be
    referenced by any workflow.

    Args:
        type: Action type identifier to delete.

    Returns:
        Dict with key 'ok' set to True.

    Raises:
        HTTPException: 403 if the action is a shipped (repo-sourced) definition.
        HTTPException: 404 if the action type does not exist.
        HTTPException: 409 if one or more workflows still reference this action type.
    """
    conn = get_conn()
    row = conn.execute("SELECT source FROM action_defs WHERE type = ?", (type,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["source"] == "repo":
        raise HTTPException(status_code=403, detail="Shipped actions can't be deleted.")
    n = _ref_count(conn, type)
    if n:
        raise HTTPException(status_code=409,
                            detail=f"{n} workflow(s) still use this action — repoint them first.")
    conn.execute("DELETE FROM action_defs WHERE type = ?", (type,))
    conn.commit()
    return {"ok": True}
