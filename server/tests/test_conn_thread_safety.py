"""Enforce the SQLite connection-ownership invariant so it can't silently regress.

Every recurring "Edit with AI / chat / research stops responding" hang traced to ONE root
cause: the shared event-loop connection (get_conn() is thread-local, and the event loop is a
single thread, so every concurrent async turn gets the SAME connection) was handed to a worker
thread via asyncio.to_thread. Two turns then drove one sqlite3.Connection at once and wedged it
on the connection mutex — an unkillable hang the surrounding try/except can't catch.

These tests turn that invariant from honor-code into something enforced:

1. ``test_connection_is_thread_affine`` — production connections are opened
   ``check_same_thread=True``, so a cross-thread use RAISES (loudly, at the offending call)
   instead of deadlocking. This fails if anyone flips the flag back.
2. ``test_no_async_offload_shares_the_event_loop_connection`` — an AST guard over app/ + scripts/
   that fails if any ``asyncio.to_thread`` / ``run_in_executor`` / ``submit_write`` site lets a
   worker touch the event-loop connection, in EITHER shape: a bare ``conn``/``tconn`` argument, OR
   a closure that *captures* an outer ``conn``/``tconn`` (the shape that slipped past the original
   regex guard and caused the attachments hang). Offloaded DB work must open its own connection
   inside the worker via ``get_conn()`` (the ``_persist_user_turn`` pattern).
"""
import ast
import os
import sqlite3
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_CONN_NAMES = {"conn", "tconn"}


def test_connection_is_thread_affine(tmp_path):
    """A production connection refuses use from a thread other than its creator.

    This is the runtime guard: the cross-thread share that caused the hangs now raises a
    ``sqlite3.ProgrammingError`` at the first DB call instead of wedging the connection mutex.
    """
    os.environ["DB_PATH"] = str(tmp_path / "affinity.db")
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db

    conn = db._connect()
    captured: dict = {}

    def _use_from_another_thread():
        """Touch the connection from a foreign thread; record whatever it raises."""
        try:
            conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 — we WANT to capture the guard's raise
            captured["exc"] = exc

    try:
        t = threading.Thread(target=_use_from_another_thread)
        t.start()
        t.join(5)
        assert isinstance(captured.get("exc"), sqlite3.ProgrammingError), (
            "connections must be thread-affine (check_same_thread=True) so a cross-thread use "
            "raises immediately instead of silently wedging the connection mutex"
        )
    finally:
        conn.close()


def _bound_names(func_node: ast.AST) -> set[str]:
    """Return names bound (assigned/param/with-as/for-target) anywhere inside a function/lambda.

    A name bound locally is NOT a capture of an outer connection — e.g. ``conn = get_conn()``
    inside the worker is the SANCTIONED pattern.

    Args:
        func_node: A FunctionDef/AsyncFunctionDef/Lambda AST node.

    Returns:
        Set of locally-bound identifier names.
    """
    bound: set[str] = set()
    args = getattr(func_node, "args", None)
    if args is not None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            bound.add(a.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    for n in ast.walk(func_node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name):
            bound.add(n.target.id)
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
            bound.add(n.target.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if isinstance(item.optional_vars, ast.Name):
                    bound.add(item.optional_vars.id)
    return bound


def _captures_bare_conn(func_node: ast.AST) -> bool:
    """True if the function/lambda LOADS a bare conn/tconn it never binds locally (a capture).

    Args:
        func_node: A FunctionDef/AsyncFunctionDef/Lambda AST node.

    Returns:
        True if an outer ``conn``/``tconn`` is read inside without being bound locally.
    """
    bound = _bound_names(func_node)
    for n in ast.walk(func_node):
        if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in _CONN_NAMES and n.id not in bound):
            return True
    return False


def _offending_offloads(path: Path) -> list[str]:
    """Return offload sites in one file that let a worker touch the event-loop connection.

    Flags both shapes: a bare conn/tconn passed as an argument, and a worker closure that
    captures an outer conn/tconn.

    Args:
        path: Python source file to scan.

    Returns:
        List of ``"<file>:<line>"`` strings for each offending offload call.
    """
    tree = ast.parse(path.read_text())
    # Parent map + per-scope def index, so a worker referenced by name resolves to the def that
    # is actually visible at the call site (innermost enclosing scope first, like Python) — never
    # an unrelated same-named def elsewhere in the module (which would be a spurious flag).
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_scope(n):
        """Return the nearest enclosing FunctionDef/AsyncFunctionDef/Module of a node."""
        p = parents.get(n)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                return p
            p = parents.get(p)
        return None

    defs_by_scope: dict[int, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs_by_scope.setdefault(id(enclosing_scope(node)), []).append(node)

    def resolve_def(name: str, call: ast.AST):
        """Resolve a worker name to its def, searching enclosing scopes innermost-out."""
        scope = enclosing_scope(call)
        while scope is not None:
            for d in defs_by_scope.get(id(scope), []):
                if d.name == name:
                    return d
            if isinstance(scope, ast.Module):
                break
            scope = enclosing_scope(scope)
        return None

    def is_bare_conn(n) -> bool:
        """True if a node is a bare ``conn``/``tconn`` Name."""
        return isinstance(n, ast.Name) and n.id in _CONN_NAMES

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            api = node.func.attr            # asyncio.to_thread / loop.run_in_executor / ...
        elif isinstance(node.func, ast.Name):
            api = node.func.id              # bare submit_write(...)
        else:
            continue
        if api == "to_thread":
            worker, extra = (node.args[0] if node.args else None), node.args[1:]
        elif api == "run_in_executor":
            worker, extra = (node.args[1] if len(node.args) >= 2 else None), node.args[2:]
        elif api == "submit_write":
            # submit_write(fn): fn is a zero-arg closure that must open its OWN (the writer's)
            # connection via get_conn() inside, never capture the event-loop conn. There are no
            # extra args through which a bare conn could be passed, so only the capture shape applies.
            worker, extra = (node.args[0] if node.args else None), []
        else:
            continue
        # Shape 1: a bare conn/tconn handed to the worker — as a positional OR keyword argument
        # (asyncio.to_thread forwards **kwargs to the worker), or wrapped in functools.partial.
        bare = any(is_bare_conn(a) for a in extra) or any(is_bare_conn(kw.value) for kw in node.keywords)
        if isinstance(worker, ast.Call):   # functools.partial(fn, conn, ...)
            bare = bare or any(is_bare_conn(a) for a in worker.args) \
                or any(is_bare_conn(kw.value) for kw in worker.keywords)
        # Shape 2: the worker is a lambda / local def that CAPTURES an outer conn/tconn.
        captures = False
        if isinstance(worker, ast.Lambda):
            captures = _captures_bare_conn(worker)
        elif isinstance(worker, ast.Name):
            d = resolve_def(worker.id, node)
            captures = bool(d) and _captures_bare_conn(d)
        if bare or captures:
            try:
                rel = path.relative_to(APP_DIR.parent)
            except ValueError:
                rel = path
            offenders.append(f"{rel}:{node.lineno}")
    return offenders


def test_no_async_offload_shares_the_event_loop_connection():
    """No offload site may let a worker thread touch the shared event-loop connection.

    Offloaded DB work must open its OWN connection inside the worker (get_conn() is
    thread-local) rather than pass or capture the event-loop connection. Catches both the
    bare-argument shape and the closure-capture shape that produced the gather/research/upload
    hangs.
    """
    offenders: list[str] = []
    roots = [APP_DIR] + ([SCRIPTS_DIR] if SCRIPTS_DIR.is_dir() else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            offenders += _offending_offloads(path)
    assert not offenders, (
        "these offload sites let a worker touch the shared event-loop connection — open a "
        "worker-local connection inside the closure instead (see _persist_user_turn):\n  "
        + "\n  ".join(offenders)
    )
