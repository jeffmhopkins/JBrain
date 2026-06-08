"""Prompt editor API: list effective prompts, override them, reset to default."""
from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import prompts

router = APIRouter(prefix="/api/prompts", tags=["prompts"], dependencies=[CurrentUser])


class PromptIn(BaseModel):
    """Input body for setting a prompt override value."""

    value: str


@router.get("")
def list_prompts():
    """List all effective prompts, showing defaults and any owner overrides.

    Returns:
        List of prompt dicts from prompts.list_all.
    """
    return prompts.list_all(get_conn())


@router.put("/{key:path}")
def set_prompt(key: str, body: PromptIn):
    """Override a prompt by key with a custom value.

    Args:
        key: Dot-separated prompt key path (e.g. 'assistant.system').
        body: New prompt value string.

    Returns:
        Dict with key 'ok' set to True.
    """
    conn = get_conn()
    prompts.set_override(conn, key, body.value)
    conn.commit()
    return {"ok": True}


@router.delete("/{key:path}")
def reset_prompt(key: str):
    """Reset a prompt override, reverting it to its built-in default.

    Args:
        key: Dot-separated prompt key path to reset.

    Returns:
        Dict with key 'ok' set to True.
    """
    conn = get_conn()
    prompts.clear_override(conn, key)
    conn.commit()
    return {"ok": True}
