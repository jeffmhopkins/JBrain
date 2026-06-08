"""Guarded read-only SQL execution (SELECT/WITH only). Shared by the SQL console
and the architect's research-mode query_sql tool.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time

# Write/DDL keywords, plus: `meta` (holds the access-key hash — never readable
# here), the `sqlite_*` schema tables (full-schema disclosure / recon), `recursive`
# (CTE DoS), the `pragma_*` table-valued functions (schema recon that bare
# `\bpragma\b` misses — the underscore is not a word boundary), the internal/secret
# tables, and dangerous SQLite functions. This text filter is the friendly first
# line; the engine-level authorizer in db.py (_ro_authorizer) is the real backstop.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback|recursive|meta|"
    r"prompt_overrides|staging_actions|workflow_runs|action_defs|review_items|"
    r"guided_specs|guided_sessions|research_specs|research_sessions|push_subscriptions|"
    r"sqlite_master|sqlite_schema|sqlite_temp_master|sqlite_temp_schema|sqlite_sequence|"
    r"load_extension|readfile|writefile|fts3_tokenizer|zipfile)\b"
    r"|\bpragma_",   # pragma_table_list / pragma_table_info(…) TVFs
    re.IGNORECASE,
)

# Strip string literals before keyword-scanning so legitimate content searches
# (LIKE '%create%', a note titled 'meta-analysis', the replace() function) aren't
# false-positives. The engine authorizer + query_only still guard the real boundary.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")

_QUERY_TIMEOUT_S = 2.0          # per-query CPU/wall cap (watchdog interrupts)
_MAX_CONCURRENT = 4             # cap parallel ad-hoc queries on the single process
_slots = threading.Semaphore(_MAX_CONCURRENT)


def run_select(conn, sql: str, limit: int = 200):
    """Execute a single read-only SELECT/WITH query with a timeout watchdog.

    Validates the query against a forbidden-keyword list (with string literals
    neutralised) and enforces a per-query CPU/wall cap via a progress-handler
    watchdog. A concurrency semaphore caps parallel ad-hoc queries.

    Args:
        conn: SQLite connection configured with the read-only authorizer.
        sql: SQL statement to execute (must start with SELECT or WITH).
        limit: Maximum rows to return (clamped to 1–1000).

    Returns:
        Tuple of (columns, rows) where columns is a list of column-name strings and
        rows is a list of lists.

    Raises:
        ValueError: If the statement is not a safe read-only query, references a
            forbidden keyword/table/function, times out, or too many concurrent
            queries are running.
    """
    sql = (sql or "").strip().rstrip(";")
    if ";" in sql:
        raise ValueError("only a single statement is allowed")
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("only SELECT/WITH queries are allowed")
    # Scan with string literals neutralised so legitimate content searches
    # (LIKE '%create%', title = 'meta-analysis') aren't falsely rejected.
    if _FORBIDDEN.search(_STRING_LITERAL.sub("''", sql)):
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
