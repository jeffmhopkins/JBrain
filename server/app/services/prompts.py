"""Central prompt store, loaded from prompts.yaml (hot-reloaded on file change).

All tunable prompts live in one YAML so they're easy to update without code
changes. `get("architect.research")` etc. returns the string, or a fallback.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_cache: dict = {"mtime": 0.0, "data": {}}


def _file() -> Path | None:
    """Locate the prompts.yaml file from env override, repo root, or container path.

    Returns:
        Path to the first existing prompts.yaml, or None if not found.
    """
    for c in (
        os.environ.get("JBRAIN_PROMPTS_FILE"),
        Path(__file__).resolve().parents[3] / "prompts.yaml",  # repo root
        Path("/app/prompts.yaml"),                             # container
    ):
        if c and Path(c).is_file():
            return Path(c)
    return None


def _load() -> dict:
    """Load (or return the cached) prompts.yaml data, hot-reloading on mtime change.

    Returns:
        Parsed YAML dict, or {} if the file is missing or unreadable.
    """
    f = _file()
    if not f:
        return {}
    mtime = f.stat().st_mtime
    if mtime != _cache["mtime"]:
        try:
            _cache["data"] = yaml.safe_load(f.read_text()) or {}
        except Exception:
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def _file_value(dotted_key: str, default: str = "") -> str:
    """Return the string value at a dotted key path from prompts.yaml.

    Args:
        dotted_key: Dot-separated path into the YAML tree (e.g. 'architect.research').
        default: Value returned when the key is absent or not a string.

    Returns:
        String value, or default.
    """
    node = _load()
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node if isinstance(node, str) else default


def _override(key: str) -> str | None:
    """Return the DB override value for a prompt key, or None if absent.

    Best-effort: returns None if the prompt_overrides table is not yet ready.

    Args:
        key: Prompt key to look up.

    Returns:
        Override string, or None.
    """
    try:
        from ..db import get_conn
        row = get_conn().execute(
            "SELECT value FROM prompt_overrides WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def _node(dotted_key: str):
    """Return the raw YAML node at a dotted key path (any type).

    Used for non-string config values such as lists or ints.

    Args:
        dotted_key: Dot-separated path into the YAML tree.

    Returns:
        The raw node (any type), or None if the path is absent.
    """
    node = _load()
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


# Renamed prompt keys: new canonical -> legacy. An override saved under the old
# key still applies after the rename (existing customisations are preserved).
_KEY_ALIASES = {"actions.synthesize": "actions.claude_synthesize"}


def get(dotted_key: str, default: str = "") -> str:
    """Return the effective prompt string for a key.

    Resolution order: DB override (new key then legacy alias) -> prompts.yaml -> default.

    Args:
        dotted_key: Prompt key to resolve.
        default: Fallback value when no source provides a value.

    Returns:
        Effective prompt string.
    """
    ov = _override(dotted_key)
    if ov is not None:
        return ov
    legacy = _KEY_ALIASES.get(dotted_key)
    if legacy is not None:
        ov = _override(legacy)          # honour a customisation saved under the old key
        if ov is not None:
            return ov
    return _file_value(dotted_key, default)


def get_list(dotted_key: str, default: list | None = None) -> list:
    """Return the list value at a dotted key path from prompts.yaml.

    Args:
        dotted_key: Dot-separated path into the YAML tree.
        default: Fallback when the key is absent or not a list.

    Returns:
        List value, or default (empty list if default is None).
    """
    node = _node(dotted_key)
    return node if isinstance(node, list) else (default or [])


def get_int(dotted_key: str, default: int) -> int:
    """Return the integer value at a dotted key path from prompts.yaml.

    Args:
        dotted_key: Dot-separated path into the YAML tree.
        default: Fallback when the key is absent or not coercible to int.

    Returns:
        Integer value, or default.
    """
    node = _node(dotted_key)
    try:
        return int(node)
    except (TypeError, ValueError):
        return default


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten a nested dict to dotted-key -> string-value pairs (non-string leaves omitted).

    Args:
        d: Dict to flatten (may be nested).
        prefix: Accumulated key prefix for recursive calls.

    Returns:
        Flat dict mapping dotted key strings to string values.
    """
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, str):
            out[key] = v
    return out


def list_all(conn) -> list[dict]:
    """Return a merged view of all prompt keys for the editor UI.

    Args:
        conn: SQLite connection used to read prompt_overrides.

    Returns:
        List of dicts with keys: key, default (from file), override (or None),
        effective (the value actually used).
    """
    defaults = _flatten(_load())
    overrides = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM prompt_overrides")}
    keys = sorted(set(defaults) | set(overrides))
    return [
        {
            "key": k,
            "default": defaults.get(k, ""),
            "override": overrides.get(k),
            "effective": overrides.get(k, defaults.get(k, "")),
        }
        for k in keys
    ]


def set_override(conn, key: str, value: str) -> None:
    """Upsert a DB override for a prompt key.

    Args:
        conn: SQLite connection (caller owns commit).
        key: Prompt key to override.
        value: Override string value.
    """
    conn.execute(
        "INSERT INTO prompt_overrides (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


def clear_override(conn, key: str) -> None:
    """Remove the DB override for a prompt key, reverting to the file default.

    Args:
        conn: SQLite connection (caller owns commit).
        key: Prompt key whose override should be cleared.
    """
    conn.execute("DELETE FROM prompt_overrides WHERE key = ?", (key,))
