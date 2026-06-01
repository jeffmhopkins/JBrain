"""Guarded read-only SQL execution (SELECT/WITH only). Shared by the SQL console
and the architect's research-mode query_sql tool."""
from __future__ import annotations

import re
import sqlite3
import threading
import time

# Write/DDL keywords, plus: `meta` (holds the access-key hash — never readable
# here), the `sqlite_*` schema tables (full-schema disclosure / recon), `recursive`
# (CTE DoS), and dangerous SQLite functions.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback|recursive|meta|"
    r"sqlite_master|sqlite_schema|sqlite_temp_master|sqlite_temp_schema|sqlite_sequence|"
    r"load_extension|readfile|writefile|fts3_tokenizer|zipfile)\b",
    re.IGNORECASE,
)

_QUERY_TIMEOUT_S = 2.0          # per-query CPU/wall cap (watchdog interrupts)
_MAX_CONCURRENT = 4             # cap parallel ad-hoc queries on the single process
_slots = threading.Semaphore(_MAX_CONCURRENT)


def run_select(conn, sql: str, limit: int = 200):
    """Execute a single SELECT/WITH query. Returns (columns, rows).

    Raises ValueError if the statement isn't a safe read-only query or runs too
    long (a watchdog interrupts heavy queries to protect the single API process).
    """
    sql = (sql or "").strip().rstrip(";")
    if ";" in sql:
        raise ValueError("only a single statement is allowed")
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(sql):
        raise ValueError("query references a forbidden keyword, table, or function")
    limit = max(1, min(int(limit), 1000))

    if not _slots.acquire(blocking=False):
        raise ValueError("too many concurrent queries; please retry in a moment")
    deadline = time.monotonic() + _QUERY_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100_000)
    try:
        cur = conn.execute(f"SELECT * FROM ({sql}) LIMIT {limit}")
        rows = cur.fetchall()
        columns = [c[0] for c in cur.description] if cur.description else []
        return columns, [list(r) for r in rows]
    except sqlite3.OperationalError as exc:
        raise ValueError(f"query stopped: {exc}")
    finally:
        conn.set_progress_handler(None, 100_000)
        _slots.release()
