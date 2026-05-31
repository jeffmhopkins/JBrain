"""Read-only ad-hoc SQL console (the explicit 'SQL access' feature).

Only a single SELECT/WITH statement is permitted. For full SQL access use the
sqlite3 CLI documented in the README.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_conn
from ..services import sqlsafe

router = APIRouter(prefix="/api/sql", tags=["sql"], dependencies=[CurrentUser])


class QueryIn(BaseModel):
    sql: str
    limit: int = 200


@router.post("")
def run_query(body: QueryIn):
    try:
        columns, rows = sqlsafe.run_select(get_conn(), body.sql, body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc).capitalize() + ".")
    except Exception as exc:  # surface SQLite errors to the console UI
        raise HTTPException(status_code=400, detail=str(exc))
    return {"columns": columns, "rows": rows}
