"""Guarded read-only SQL execution (SELECT/WITH only). Shared by the SQL console
and the architect's research-mode query_sql tool."""
from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)


def run_select(conn, sql: str, limit: int = 200):
    """Execute a single SELECT/WITH query. Returns (columns, rows).

    Raises ValueError if the statement isn't a safe read-only query.
    """
    sql = (sql or "").strip().rstrip(";")
    if ";" in sql:
        raise ValueError("only a single statement is allowed")
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(sql):
        raise ValueError("query contains a write/DDL keyword")
    limit = max(1, min(int(limit), 1000))
    cur = conn.execute(f"SELECT * FROM ({sql}) LIMIT {limit}")
    rows = cur.fetchall()
    columns = [c[0] for c in cur.description] if cur.description else []
    return columns, [list(r) for r in rows]
