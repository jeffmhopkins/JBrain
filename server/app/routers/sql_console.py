"""Read-only ad-hoc SQL console (the explicit 'SQL access' feature).

Only a single SELECT/WITH statement is permitted. For full SQL access use the
sqlite3 CLI documented in the README.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_query_conn
from ..services import sqlsafe

router = APIRouter(prefix="/api/sql", tags=["sql"], dependencies=[CurrentUser])


class QueryIn(BaseModel):
    """Input body for the read-only SQL console query."""

    sql: str
    limit: int = 200


@router.post("")
def run_query(body: QueryIn):
    """Execute a read-only SELECT or WITH statement and return the results.

    Only a single SELECT or WITH statement is permitted; any other SQL (INSERT,
    UPDATE, DELETE, DDL, multiple statements) is rejected. Row output is capped at
    the requested limit (default 200).

    Args:
        body: SQL statement string and optional row limit.

    Returns:
        Dict with 'columns' (list of column name strings) and 'rows' (list of value lists).

    Raises:
        HTTPException: 400 if the SQL is disallowed, invalid, or raises a SQLite error.
    """
    try:
        columns, rows = sqlsafe.run_select(get_query_conn(), body.sql, body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc).capitalize() + ".")
    except Exception as exc:  # surface SQLite errors to the console UI
        raise HTTPException(status_code=400, detail=str(exc))
    return {"columns": columns, "rows": rows}
