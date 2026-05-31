"""Action recipes REST API: list / get / create / update / delete / sync /
validate + a primitive catalog. Recipes live in the action_defs table (repo-
seeded; user edits set source='user', locked=1). Shipped (source='repo')
recipes are READ-ONLY — duplicate to a custom action to edit. The server is the
YAML parser: get/validate return a parsed step tree so the PWA can visualise a
pipeline without a client-side YAML library."""
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import pipeline

router = APIRouter(prefix="/api/action-defs", tags=["actions"], dependencies=[CurrentUser])


class RecipeIn(BaseModel):
    recipe_yaml: str


def _parse(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400, detail="Recipe must be a YAML mapping.")
    return doc


def _flatten(steps) -> list[str]:
    out: list[str] = []
    for s in steps or []:
        if isinstance(s, dict) and "for_each" in s:
            out.append("for_each")
            out += _flatten(s.get("steps"))
        elif isinstance(s, dict) and s.get("do"):
            out.append(s["do"])
    return out


def _ref_count(conn, type_: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM workflows WHERE action_type = ?", (type_,)
    ).fetchone()["c"]


@router.get("/primitives")
def primitives():
    return pipeline.primitive_catalog()


@router.post("/validate")
def validate(body: RecipeIn):
    recipe = _parse(body.recipe_yaml)
    return {"warnings": pipeline.validate_recipe(recipe), "recipe": recipe}


@router.post("/sync")
def sync():
    return {"synced": pipeline.ingest_repo_action_defs(get_conn())}


@router.get("")
def list_defs():
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
            "num_steps": len(seq), "summary": " → ".join(seq),
        })
    return out


@router.get("/{type}")
def get_def(type: str):
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
