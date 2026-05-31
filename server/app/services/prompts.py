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
    for c in (
        os.environ.get("JBRAIN_PROMPTS_FILE"),
        Path(__file__).resolve().parents[3] / "prompts.yaml",  # repo root
        Path("/app/prompts.yaml"),                             # container
    ):
        if c and Path(c).is_file():
            return Path(c)
    return None


def _load() -> dict:
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
    node = _load()
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node if isinstance(node, str) else default


def _override(key: str) -> str | None:
    """DB override, if any. Best-effort (returns None if the table isn't ready)."""
    try:
        from ..db import get_conn
        row = get_conn().execute(
            "SELECT value FROM prompt_overrides WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def _node(dotted_key: str):
    """Raw node from the YAML (any type). Used for non-string config (lists/ints)."""
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
    """Effective prompt string: DB override (new key → legacy key) → prompts.yaml
    → code default."""
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
    node = _node(dotted_key)
    return node if isinstance(node, list) else (default or [])


def get_int(dotted_key: str, default: int) -> int:
    node = _node(dotted_key)
    try:
        return int(node)
    except (TypeError, ValueError):
        return default


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, str):
            out[key] = v
    return out


def list_all(conn) -> list[dict]:
    """Merged view for the editor: key, file default, override (if any), effective."""
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
    conn.execute(
        "INSERT INTO prompt_overrides (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


def clear_override(conn, key: str) -> None:
    conn.execute("DELETE FROM prompt_overrides WHERE key = ?", (key,))
