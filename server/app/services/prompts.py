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


def get(dotted_key: str, default: str = "") -> str:
    node = _load()
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node if isinstance(node, str) else default
