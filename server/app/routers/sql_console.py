"""Read-only ad-hoc SQL console (the explicit 'SQL access' feature).

Only a single SELECT/WITH statement is permitted. For full SQL access use the
sqlite3 CLI documented in the README.
"""
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn

router = APIRouter(prefix="/api/sql", tags=["sql"], dependencies=[CurrentUser])

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class QueryIn(BaseModel):
    sql: str
    limit: int = 200


@router.post("")
def run_query(body: QueryIn):
    sql = body.sql.strip().rstrip(";")
    if ";" in sql:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed.")
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only SELECT/WITH queries are allowed.")
    if _FORBIDDEN.search(sql):
        raise HTTPException(status_code=400, detail="Query contains a write/DDL keyword.")

    limit = max(1, min(body.limit, 1000))
    try:
        cur = get_conn().execute(f"SELECT * FROM ({sql}) LIMIT {limit}")
        rows = cur.fetchall()
        columns = [c[0] for c in cur.description] if cur.description else []
    except Exception as exc:  # surface SQLite errors to the console UI
        raise HTTPException(status_code=400, detail=str(exc))
    return {"columns": columns, "rows": [list(r) for r in rows]}
