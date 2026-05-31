"""Declarative ("YAML-defined") workflow actions.

An action TYPE can be defined as data in `actions/*.yaml` instead of as a Python
function in workflows._ACTIONS. A definition is a list of `steps`, each invoking
one PRIMITIVE (a small, fixed, code-backed operation — see _PRIMITIVES) with
templated inputs. This keeps *effects* in a vetted primitive library while
letting *composition* live in YAML.

Templating uses Jinja2's SandboxedEnvironment (value/condition expressions only —
no side effects, no arbitrary attribute access). The engine supports linear
steps, `when:` guards, `for_each:` sub-pipelines, and `stop_when_empty:` early
returns. Dispatch (workflows.run_workflow) prefers a YAML definition over the
Python fallback, so both can coexist during migration.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined
from jinja2 import meta as _jinja_meta
from jinja2.sandbox import SandboxedEnvironment

from . import embeddings
from . import llm
from . import notes as notes_svc
from . import reviews as reviews_svc
from ..db import get_conn, get_meta, set_meta


def _today() -> str:
    return datetime.date.today().isoformat()


# --- Templating -------------------------------------------------------------
# A single sandboxed environment. ChainableUndefined lets `a.b.c` chain over
# missing keys without raising; expressions never perform I/O.
_env = SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False)
_env.filters["clip"] = lambda s, n: (s or "")[:n]


def _eval(expr: str, scope: dict):
    """Evaluate an expression to its native Python value (Undefined → None).
    Accepts a bare expression or one wrapped in `{{ }}` (for when/for_each)."""
    expr = expr.strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()
    return _env.compile_expression(expr, undefined_to_none=True)(**scope)


def _render_value(val, scope):
    """Render a `with:` value: a lone `{{ expr }}` yields the native value; a
    string with embedded `{{ }}` is interpolated; dicts/lists recurse. The legacy
    `{date}` token is substituted in any resulting string for backward-compat."""
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{{") and s.endswith("}}") and s.count("{{") == 1:
            out = _eval(s[2:-2], scope)
            return out.replace("{date}", _today()) if isinstance(out, str) else out
        if "{{" in val:
            return _env.from_string(val).render(**scope).replace("{date}", _today())
        return val.replace("{date}", _today())
    if isinstance(val, dict):
        return {k: _render_value(v, scope) for k, v in val.items()}
    if isinstance(val, list):
        return [_render_value(v, scope) for v in val]
    return val


def _truthy(v) -> bool:
    try:
        return bool(v)
    except Exception:
        return False


# --- Primitives -------------------------------------------------------------
# Each primitive is (ctx, **kwargs) -> value. They are the ONLY effecting code;
# YAML can compose them but never bypass them.

class _Ctx:
    def __init__(self, conn, workflow_id, trigger):
        self.conn = conn
        self.workflow_id = workflow_id
        self.trigger = trigger or {}


def _p_read_note(ctx, title=None, id=None):
    if id is not None:
        row = ctx.conn.execute(
            "SELECT id, title, slug, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (id,)
        ).fetchone()
    elif title:
        row = notes_svc.get_by_title(ctx.conn, title)
    else:
        return None
    return dict(row) if row else None


def _p_write_note(ctx, title, content_md=None, text=None, mode="replace", kind=None, version_note=None):
    body_in = content_md if content_md is not None else (text or "")
    if mode == "append":
        note = notes_svc.get_by_title(ctx.conn, title)
        base = note["content_md"] if note else f"# {title}\n"
        content = base.rstrip() + "\n" + body_in + "\n"
        vn = version_note or "workflow append"
    else:
        content = body_in
        vn = version_note or "workflow synthesis"
    nid = notes_svc.upsert_note(ctx.conn, title, content, source="workflow", version_note=vn, kind=kind)
    note = notes_svc.get_by_title(ctx.conn, title)
    return {"id": nid, "title": title, "slug": note["slug"] if note else None}


def _p_create_review(ctx, title, message="", link_title=None):
    link_slug = None
    if link_title:
        n = notes_svc.get_by_title(ctx.conn, link_title)
        link_slug = n["slug"] if n else None
    rid = reviews_svc.create_review_item(ctx.conn, ctx.workflow_id, title, message or "", link_slug)
    return {"id": rid, "link_slug": link_slug}


def _p_semantic_search(ctx, query, limit=8):
    return embeddings.semantic_search(ctx.conn, query, int(limit))


def _p_query_notes(ctx, kind=None, since_id=0, limit=1000):
    sql = "SELECT id, title, slug, content_md FROM notes WHERE deleted_at IS NULL"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if since_id:
        sql += " AND id > ?"
        params.append(int(since_id))
    sql += " ORDER BY id LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in ctx.conn.execute(sql, params).fetchall()]


def _p_get_meta(ctx, key, default=None):
    return get_meta(key, default)


def _p_set_meta(ctx, key, value):
    set_meta(ctx.conn, key, str(value))
    return None


def _p_set_tags(ctx, note_id, tags):
    return notes_svc.set_tags(ctx.conn, int(note_id), list(tags or []))


# Code-backed helpers exposed as named primitives. These wrap the irregular
# logic (LLM prompt building, parsing, watermark arithmetic) that doesn't belong
# in YAML — and delegate to the same workflows.* functions tests already stub.

def _p_suggest_tags(ctx, title, content, prompt=None):
    from . import workflows as wf
    return wf._suggest_tags(title or "", content or "", prompt)


def _p_summarise_entries(ctx, entries, prompt=None):
    from . import workflows as wf
    return wf._summarise_entries(list(entries or []), prompt)


def _p_wiki_plan(ctx, entries, existing_kb, instructions=None):
    from . import workflows as wf
    return wf._synthesize_actions(list(entries or []), list(existing_kb or []), instructions)


def _p_gather_context(ctx, source_title=None, context_query=None):
    """Build context text from a named note or a semantic search (synthesize)."""
    if source_title:
        row = notes_svc.get_by_title(ctx.conn, source_title)
        return row["content_md"] if row else ""
    if context_query:
        out = ""
        for r in embeddings.semantic_search(ctx.conn, context_query, 8):
            n = notes_svc.get_by_title(ctx.conn, r["title"])
            if n:
                out += f"\n\n## {n['title']}\n{n['content_md']}"
        return out
    return ""


def _p_llm(ctx, prompt, content="", max_tokens=1024, on_no_key="raise"):
    """Run an LLM prompt over optional context. `on_no_key` controls behaviour
    when no provider key is configured: raise | fallback (return content) | skip ('')."""
    if not llm.has_credentials():
        if on_no_key == "raise":
            raise RuntimeError("no LLM API key configured")
        if on_no_key == "fallback":
            return content or ""
        return ""
    user = f"{prompt}\n\n<content>\n{content}\n</content>" if content else prompt
    return llm.complete([{"role": "user", "content": user}], max_tokens=int(max_tokens))


def _p_daylog_pending(ctx, log_title):
    """Parse a log note's dated lines and the per-log watermark; return the days
    still to summarise (encapsulates summarize_day_log's irregular front-end)."""
    from . import workflows as wf
    empty = {"days": [], "watermark_key": None, "last_date": None}
    note = notes_svc.get_by_title(ctx.conn, log_title)
    if not note:
        return empty
    by_date: dict[str, list[str]] = {}
    for line in note["content_md"].splitlines():
        m = wf._DATED_LINE.match(line.strip())
        if m:
            by_date.setdefault(m.group(1), []).append(m.group(2))
    if not by_date:
        return empty
    dates = sorted(by_date)
    current = dates[-1]  # the day still being logged into — leave it alone
    wm_key = f"daylog_summarized:{log_title.lower()}"
    last = get_meta(wm_key)
    todo = [d for d in dates if d < current and (last is None or d > last)]
    return {
        "days": [{"date": d, "entries": by_date[d]} for d in todo],
        "watermark_key": wm_key,
        "last_date": (todo[-1] if todo else None),
    }


_PRIMITIVES = {
    "read_note": _p_read_note,
    "write_note": _p_write_note,
    "create_review": _p_create_review,
    "semantic_search": _p_semantic_search,
    "query_notes": _p_query_notes,
    "get_meta": _p_get_meta,
    "set_meta": _p_set_meta,
    "set_tags": _p_set_tags,
    "suggest_tags": _p_suggest_tags,
    "summarise_entries": _p_summarise_entries,
    "wiki_plan": _p_wiki_plan,
    "daylog_pending": _p_daylog_pending,
    "gather_context": _p_gather_context,
    "llm": _p_llm,
}


# Machine-readable contract for each primitive — drives the editor's step palette
# and the recipe validator. Hand-maintained; a test pins its keys + input names to
# _PRIMITIVES (and the function signatures) so it can't drift.
_PRIMITIVE_META: dict[str, dict] = {
    "read_note": {"summary": "Read a note by title or id.",
                  "inputs": [{"name": "title", "type": "str"}, {"name": "id", "type": "int"}],
                  "output": "object"},
    "write_note": {"summary": "Create or update a note (versioned).",
                   "inputs": [{"name": "title", "type": "str", "required": True},
                              {"name": "content_md", "type": "str"}, {"name": "text", "type": "str"},
                              {"name": "mode", "type": "enum", "choices": ["replace", "append"]},
                              {"name": "kind", "type": "str"}, {"name": "version_note", "type": "str"}],
                   "output": "object"},
    "create_review": {"summary": "Post a card to the Review inbox.",
                      "inputs": [{"name": "title", "type": "str", "required": True},
                                 {"name": "message", "type": "str"}, {"name": "link_title", "type": "str"}],
                      "output": "object"},
    "semantic_search": {"summary": "Vector search over notes.",
                        "inputs": [{"name": "query", "type": "str", "required": True},
                                   {"name": "limit", "type": "int"}], "output": "list"},
    "query_notes": {"summary": "List notes by kind / since id.",
                    "inputs": [{"name": "kind", "type": "str"}, {"name": "since_id", "type": "int"},
                               {"name": "limit", "type": "int"}], "output": "list"},
    "get_meta": {"summary": "Read a stored key (e.g. a watermark).",
                 "inputs": [{"name": "key", "type": "str", "required": True}, {"name": "default", "type": "str"}],
                 "output": "scalar"},
    "set_meta": {"summary": "Write a stored key.",
                 "inputs": [{"name": "key", "type": "str", "required": True}, {"name": "value", "type": "str", "required": True}],
                 "output": "none"},
    "set_tags": {"summary": "Set a note's tags.",
                 "inputs": [{"name": "note_id", "type": "int", "required": True}, {"name": "tags", "type": "list"}],
                 "output": "list"},
    "suggest_tags": {"summary": "Ask the LLM for tags for a note.",
                     "inputs": [{"name": "title", "type": "str", "required": True},
                                {"name": "content", "type": "str", "required": True}, {"name": "prompt", "type": "str"}],
                     "output": "list"},
    "summarise_entries": {"summary": "Summarise a list of log lines (LLM, with no-key fallback).",
                          "inputs": [{"name": "entries", "type": "list", "required": True}, {"name": "prompt", "type": "str"}],
                          "output": "scalar"},
    "wiki_plan": {"summary": "Ask the LLM to fold entries into KB notes; returns [{op,title,content_md}].",
                  "inputs": [{"name": "entries", "type": "list", "required": True},
                             {"name": "existing_kb", "type": "list", "required": True}, {"name": "instructions", "type": "str"}],
                  "output": "list"},
    "daylog_pending": {"summary": "Days of a log still to summarise (+ watermark).",
                       "inputs": [{"name": "log_title", "type": "str", "required": True}], "output": "object"},
    "gather_context": {"summary": "Build context text from a note or a semantic search.",
                       "inputs": [{"name": "source_title", "type": "str"}, {"name": "context_query", "type": "str"}],
                       "output": "scalar"},
    "llm": {"summary": "Run an LLM prompt over optional context.",
            "inputs": [{"name": "prompt", "type": "str", "required": True}, {"name": "content", "type": "str"},
                       {"name": "max_tokens", "type": "int"},
                       {"name": "on_no_key", "type": "enum", "choices": ["raise", "fallback", "skip"]}],
            "output": "scalar"},
}


def primitive_catalog() -> list[dict]:
    """The step palette: each primitive + its declared inputs + output shape."""
    return [{"name": n, **_PRIMITIVE_META.get(n, {"inputs": [], "output": "scalar"})}
            for n in sorted(_PRIMITIVES)]


# --- Interpreter ------------------------------------------------------------

class _Stop(Exception):
    """Carries an early-return message out of nested steps."""
    def __init__(self, message: str):
        self.message = message


def _run_steps(ctx, steps, scope, trace):
    for step in steps:
        when = step.get("when")
        if when is not None and not _truthy(_eval(when, scope)):
            continue

        stop_when = step.get("stop_when")
        if stop_when is not None and _truthy(_eval(stop_when, scope)):
            raise _Stop(str(step.get("stop_message", "stopped")))

        if step.get("do") is None and "for_each" not in step:
            continue  # control-only step (when/stop_when)

        if "for_each" in step:
            coll = _eval(step["for_each"], scope) or []
            for item in coll:
                child = dict(scope)
                child["item"] = item
                _run_steps(ctx, step.get("steps", []), child, trace)
            if step.get("id"):
                scope[step["id"]] = {"count": len(coll)}
            continue

        prim = _PRIMITIVES.get(step.get("do"))
        if prim is None:
            raise RuntimeError(f"unknown primitive '{step.get('do')}'")
        args = {k: _render_value(v, scope) for k, v in (step.get("with") or {}).items()}
        out = prim(ctx, **args)
        if step.get("id"):
            scope[step["id"]] = out
        trace.append(step["do"])

        swe = step.get("stop_when_empty")
        if swe is not None and not _truthy(out):
            raise _Stop(str(swe))


def run_pipeline(conn, recipe: dict, cfg: dict, workflow_id, context: dict | None) -> str:
    ctx = _Ctx(conn, workflow_id, context)
    scope = {
        "config": cfg or {},
        "trigger": context or {},
        "today": _today(),
        "prompts": _PromptsProxy(),
    }
    trace: list[str] = []
    try:
        _run_steps(ctx, recipe.get("steps", []), scope, trace)
    except _Stop as s:
        return s.message
    return f"ran {len(trace)} step(s): " + ", ".join(trace) if trace else "ok"


class _PromptsProxy:
    """Read-only `prompts.<section>.<key>` access inside templates (DB-override
    aware), so recipes can reference the shared prompt store."""
    def __init__(self, prefix: str = ""):
        self._prefix = prefix

    def __getattr__(self, name):
        from . import prompts as _prompts
        key = f"{self._prefix}{name}"
        val = _prompts.get(key)
        return val if val else _PromptsProxy(key + ".")

    def __str__(self):
        from . import prompts as _prompts
        return _prompts.get(self._prefix.rstrip(".")) or ""


# --- Definition loading & validation ---------------------------------------

# Repo files are the seed; the action_defs DB table is the editable source of
# truth (DB-first). _REPO_DEFS / _ALIASES are the file layer (read-only, used for
# seeding, fallback, and alias resolution). _cache is a per-thread parsed-recipe
# cache keyed by (type -> (updated_at, recipe)) so we don't re-parse YAML each run
# but still see committed edits (we read updated_at from the DB every time).
_REPO_DEFS: dict | None = None
_ALIASES: dict[str, str] = {}  # legacy action_type -> canonical type
_cache = threading.local()


def _actions_dir() -> Path | None:
    for c in (
        os.environ.get("JBRAIN_ACTIONS_DIR"),
        Path(__file__).resolve().parents[3] / "actions",  # repo root
        Path("/app/actions"),                              # container
    ):
        if c and Path(c).is_dir():
            return Path(c)
    return None


def _load_repo() -> dict:
    """Parse actions/*.yaml into {type: recipe} and rebuild the alias map."""
    global _REPO_DEFS, _ALIASES
    defs: dict = {}
    aliases: dict[str, str] = {}
    d = _actions_dir()
    if d:
        for path in sorted(d.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text())
            except Exception:
                continue
            if doc and doc.get("type") and isinstance(doc.get("steps"), list):
                defs[doc["type"]] = doc
                for a in (doc.get("aliases") or []):
                    aliases[a] = doc["type"]
    _REPO_DEFS = defs
    _ALIASES = aliases
    return defs


def _repo_defs() -> dict:
    if _REPO_DEFS is None:
        _load_repo()
    return _REPO_DEFS


# Public names kept for tests/validation that operate on the repo files.
def load_action_defs() -> dict:
    return _load_repo()


def reload_action_defs() -> dict:
    return _load_repo()


def ingest_repo_action_defs(conn) -> int:
    """Seed/update the action_defs table from actions/*.yaml. Mirrors the
    workflows ingest: insert new types, refresh unlocked ones whose repo file
    changed, leave user-locked rows untouched. Stores the raw YAML verbatim."""
    d = _actions_dir()
    if not d:
        return 0
    count = 0
    for path in sorted(d.glob("*.yaml")):
        try:
            text = path.read_text()
            doc = yaml.safe_load(text)
        except Exception:
            continue
        if not (doc and doc.get("type") and isinstance(doc.get("steps"), list)):
            continue
        t = doc["type"]
        h = hashlib.sha256(text.encode()).hexdigest()
        existing = conn.execute(
            "SELECT locked, origin_hash FROM action_defs WHERE type = ?", (t,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO action_defs (type, recipe_yaml, source, locked, origin_hash) "
                "VALUES (?, ?, 'repo', 0, ?)", (t, text, h),
            )
            count += 1
        elif not existing["locked"] and existing["origin_hash"] != h:
            conn.execute(
                "UPDATE action_defs SET recipe_yaml = ?, origin_hash = ?, "
                "updated_at = datetime('now') WHERE type = ?", (text, h, t),
            )
            count += 1
    conn.commit()
    return count


def get_action_def(action_type: str) -> dict | None:
    """Resolve a recipe (DB-first, alias-aware). Falls back to the repo file only
    before the table is seeded. Uses a per-thread (type, updated_at) cache to
    avoid re-parsing YAML while still reflecting committed edits."""
    _repo_defs()  # ensure the alias map is loaded
    canonical = _ALIASES.get(action_type, action_type)
    try:
        row = get_conn().execute(
            "SELECT recipe_yaml, updated_at FROM action_defs WHERE type = ?", (canonical,)
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return _repo_defs().get(canonical)
    cache = getattr(_cache, "d", None)
    if cache is None:
        cache = {}
        _cache.d = cache
    hit = cache.get(canonical)
    if hit and hit[0] == row["updated_at"]:
        return hit[1]
    try:
        recipe = yaml.safe_load(row["recipe_yaml"]) or {}
    except Exception:
        recipe = _repo_defs().get(canonical)
    cache[canonical] = (row["updated_at"], recipe)
    return recipe


def action_types() -> list[str]:
    """Every action type: DB rows ∪ repo files (DB is authoritative once seeded)."""
    types = set(_repo_defs().keys())
    try:
        for r in get_conn().execute("SELECT type FROM action_defs"):
            types.add(r["type"])
    except Exception:
        pass
    return sorted(types)


_SCOPE_BASE = {"config", "trigger", "today", "prompts"}


def _expr_names(text) -> set:
    """Top-level variable names referenced in a value/expression (bare or {{ }})."""
    s = str(text).strip()
    if "{{" not in s and "{%" not in s:
        s = "{{ " + s + " }}"  # bare expr (when / for_each / stop_when)
    try:
        return set(_jinja_meta.find_undeclared_variables(_env.parse(s)))
    except Exception:
        return set()


def _value_names(val) -> set:
    if isinstance(val, str):
        return _expr_names(val) if "{{" in val else set()
    if isinstance(val, dict):
        return set().union(*(_value_names(v) for v in val.values())) if val else set()
    if isinstance(val, list):
        return set().union(*(_value_names(v) for v in val)) if val else set()
    return set()


def validate_recipe(recipe) -> list[str]:
    """Soft lint of a recipe: unknown primitive, unknown input, malformed
    for_each, and references to variables not yet in scope. Warnings only — the
    Jinja runtime tolerates missing names (ChainableUndefined → None)."""
    if not isinstance(recipe, dict):
        return ["recipe must be a mapping"]
    warnings: list[str] = []
    if not recipe.get("type"):
        warnings.append("missing 'type'")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        return warnings + ["'steps' must be a list"]

    def walk(steps, scope: set):
        scope = set(scope)
        for step in steps:
            if not isinstance(step, dict):
                warnings.append("each step must be a mapping")
                continue
            refs: set = set()
            for k in ("when", "for_each", "stop_when"):
                if step.get(k) is not None:
                    refs |= _expr_names(step[k])
            refs |= _value_names(step.get("with") or {})
            for name in sorted(refs - scope):
                warnings.append(f"references unknown variable '{name}'")
            if "for_each" in step:
                if not isinstance(step.get("steps"), list):
                    warnings.append("for_each step missing nested 'steps'")
                else:
                    walk(step["steps"], scope | {"item"})
            else:
                do = step.get("do")
                if do is not None:
                    if do not in _PRIMITIVES:
                        warnings.append(f"unknown primitive '{do}'")
                    else:
                        valid = {i["name"] for i in _PRIMITIVE_META.get(do, {}).get("inputs", [])}
                        for k in (step.get("with") or {}):
                            if valid and k not in valid:
                                warnings.append(f"{do}: unknown input '{k}'")
            if step.get("id"):
                scope.add(step["id"])

    walk(steps, set(_SCOPE_BASE))
    return warnings


def validate_action_defs() -> list[str]:
    """Boot lint over the shipped repo recipes (warnings printed at startup)."""
    out: list[str] = []
    for name, recipe in _load_repo().items():
        out += [f"{name}: {w}" for w in validate_recipe(recipe)]
    return out
