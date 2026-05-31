"""Prompt editor API: list effective prompts, override them, reset to default."""
from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import prompts

router = APIRouter(prefix="/api/prompts", tags=["prompts"], dependencies=[CurrentUser])


class PromptIn(BaseModel):
    value: str


@router.get("")
def list_prompts():
    return prompts.list_all(get_conn())


@router.put("/{key:path}")
def set_prompt(key: str, body: PromptIn):
    conn = get_conn()
    prompts.set_override(conn, key, body.value)
    conn.commit()
    return {"ok": True}


@router.delete("/{key:path}")
def reset_prompt(key: str):
    conn = get_conn()
    prompts.clear_override(conn, key)
    conn.commit()
    return {"ok": True}
