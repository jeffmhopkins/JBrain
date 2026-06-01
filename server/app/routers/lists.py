"""Create empty lists (kind='list') from the Lists card."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/lists", tags=["lists"], dependencies=[CurrentUser])


class ListIn(BaseModel):
    title: str


@router.post("")
def create_list(body: ListIn):
    conn = get_conn()
    base = body.title.strip()
    if not base:
        raise HTTPException(status_code=422, detail="List name cannot be empty")
    title = notes_svc.root_title(base, "lists")
    note = notes_svc.get_by_title(conn, title)
    if note is None:
        notes_svc.upsert_note(conn, title, f"# {title.split('/')[-1]}\n",
                              kind="list", source="user", version_note="new list")
        conn.commit()
        note = notes_svc.get_by_title(conn, title)
    return {"slug": note["slug"], "title": note["title"]}
