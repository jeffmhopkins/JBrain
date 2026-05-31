"""Workflow engine: trigger + action automations.

Workflows are seeded from repo YAML (server/.. /workflows/*.yaml) into the DB on
boot, and are editable in the PWA. A PWA edit sets `locked=1` so re-ingesting the
repo definition won't clobber the user's version.

Triggers: 'event' (fired from app code, e.g. log_appended) and 'schedule'
(interval-based, polled by a background loop). Actions run through the same
write funnel as everything else (notes_svc.upsert_note) so they're versioned and
attributed source='workflow'. All workflow runs are logged for audit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import llm
from . import notes as notes_svc
from . import prompts

_DATED_LINE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\* (.*)$")


# --- Repo YAML ingestion ----------------------------------------------------

def _workflows_dir() -> Path | None:
    candidates = [
        os.environ.get("JBRAIN_WORKFLOWS_DIR"),
        Path(__file__).resolve().parents[3] / "workflows",  # repo root (local/dev)
        Path("/app/workflows"),                              # container
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def _normalise(doc: dict) -> dict:
    trig = doc.get("trigger", {}) or {}
    act = doc.get("action", {}) or {}
    trigger_type = trig.get("type", "event")
    trigger_config = {k: v for k, v in trig.items() if k != "type"}
    return {
        "key": doc["key"],
        "name": doc.get("name", doc["key"]),
        "trigger_type": trigger_type,
        "trigger_config": json.dumps(trigger_config, sort_keys=True),
        "action_type": act.get("type", ""),
        "action_config": json.dumps(act.get("config", {}), sort_keys=True),
        "enabled": 1 if doc.get("enabled", True) else 0,
    }


def _hash(defn: dict) -> str:
    return hashlib.sha256(
        json.dumps(defn, sort_keys=True).encode()
    ).hexdigest()


def ingest_repo_workflows(conn) -> int:
    """Seed/update repo workflows by key. User-locked rows are left untouched."""
    directory = _workflows_dir()
    if not directory:
        return 0
    count = 0
    for path in sorted(directory.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text())
            if not doc or "key" not in doc:
                continue
            defn = _normalise(doc)
        except Exception:
            continue
        h = _hash(defn)
        existing = conn.execute(
            "SELECT id, locked, origin_hash FROM workflows WHERE key = ?", (defn["key"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO workflows (key, name, trigger_type, trigger_config, "
                "action_type, action_config, enabled, source, origin_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'repo', ?)",
                (defn["key"], defn["name"], defn["trigger_type"], defn["trigger_config"],
                 defn["action_type"], defn["action_config"], defn["enabled"], h),
            )
            count += 1
        elif not existing["locked"] and existing["origin_hash"] != h:
            conn.execute(
                "UPDATE workflows SET name=?, trigger_type=?, trigger_config=?, "
                "action_type=?, action_config=?, enabled=?, origin_hash=?, "
                "updated_at=datetime('now') WHERE id=?",
                (defn["name"], defn["trigger_type"], defn["trigger_config"],
                 defn["action_type"], defn["action_config"], defn["enabled"], h, existing["id"]),
            )
            count += 1
    conn.commit()
    return count


# --- LLM helpers (back the pipeline primitives) -----------------------------
# Actions themselves are defined declaratively in actions/*.yaml and executed by
# the pipeline interpreter. The helpers below back the summarise_entries /
# wiki_plan / suggest_tags primitives; they're factored out so tests can stub
# them. (See server/app/services/pipeline.py.)

DEFAULT_DAYLOG_PROMPT = "Summarise this day's log entries into a tight paragraph or a few bullets:"
_DEFAULT_WIKI_TEMPLATE = (
    "You maintain a personal KNOWLEDGE BASE synthesized from raw entries. Fold the "
    "NEW ENTRIES into topic notes (update existing, else create). Cite sources as "
    "[[Entry Title]].{instructions}\n\nNEW ENTRIES:\n{entries}\n\nEXISTING KB NOTES:\n"
    "{existing_kb}\n\nReturn ONLY a JSON array: "
    '[{"op":"create"|"update","title":"Topic","content_md":"markdown with [[links]]"}].'
)


def _summarise_entries(entries: list[str], prompt: str | None = None) -> str:
    """Summarise a day's log entries. Uses the LLM when configured, else a plain
    recap so the workflow still works without a key. The prompt is overridable
    from the workflow YAML (config.prompt)."""
    joined = "\n".join(f"- {e}" for e in entries)
    if not llm.has_credentials():
        return "Entries:\n" + joined
    instruction = prompt or prompts.get("actions.daylog_summary", DEFAULT_DAYLOG_PROMPT)
    return llm.complete([{"role": "user", "content": f"{instruction}\n{joined}"}], max_tokens=512)


def _synthesize_actions(entries: list, existing_kb: list, instructions: str | None = None) -> list[dict]:
    """Ask the LLM to fold new entries into the knowledge base. Returns a list of
    {op, title, content_md}. Factored out so it can be stubbed in tests.

    `instructions` (from the workflow YAML config) is extra guidance appended to
    the base prompt; the JSON-output contract is always enforced."""
    if not llm.has_credentials():
        raise RuntimeError("no LLM API key configured")
    import json as _json

    entries_text = "\n\n".join(f"## {e['title']}\n{e['content_md']}" for e in entries)
    kb_text = "\n\n".join(f"### {k['title']}\n{k['content_md']}" for k in existing_kb) or "(none yet)"
    extra = f"\nAdditional guidance: {instructions}" if instructions else ""
    template = prompts.get("actions.wiki_synthesis", _DEFAULT_WIKI_TEMPLATE)
    prompt = (template
              .replace("{instructions}", extra)
              .replace("{entries}", entries_text)
              .replace("{existing_kb}", kb_text))
    text = llm.complete([{"role": "user", "content": prompt}], max_tokens=4096)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = _json.loads(text[start:end + 1])
    except Exception:
        return []
    return [a for a in data if isinstance(a, dict) and a.get("title") and a.get("content_md")]


DEFAULT_TAG_PROMPT = (
    "Suggest 3-6 short, lowercase topic tags (comma-separated, no '#') that "
    "classify this note. Reply with ONLY the comma-separated tags."
)


def _suggest_tags(title: str, content: str, prompt: str | None = None) -> list[str]:
    """Ask the LLM for tags. Returns [] if no key (graceful no-op)."""
    if not llm.has_credentials():
        return []
    instruction = prompt or prompts.get("actions.generate_tags", DEFAULT_TAG_PROMPT)
    text = llm.complete(
        [{"role": "user", "content": f"{instruction}\n\nTitle: {title}\n{content[:2000]}"}],
        max_tokens=80,
    )
    return [t.strip().lower().lstrip("#") for t in text.replace("\n", ",").split(",") if t.strip()][:6]


# Config-form schemas for any actions implemented in Python. All shipped actions
# are now YAML-defined (they declare their own `config:` in actions/*.yaml); this
# stays as the extension point for future Python-only actions.
_PY_ACTION_SCHEMAS: dict[str, list] = {}


def action_catalog() -> list[dict]:
    """Every runnable action type + its config-form schema (drives the PWA
    picker). YAML definitions plus any Python-only actions in _PY_ACTION_SCHEMAS."""
    from . import pipeline

    schemas: dict[str, list] = {}
    for t in pipeline.action_types():
        recipe = pipeline.get_action_def(t) or {}
        schemas[t] = recipe.get("config", [])
    for t, schema in _PY_ACTION_SCHEMAS.items():
        schemas.setdefault(t, schema)
    return [{"type": t, "config": schemas[t]} for t in sorted(schemas)]


# --- Execution --------------------------------------------------------------

def _log_run(conn, workflow_id: int, status: str, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO workflow_runs (workflow_id, status, detail) VALUES (?, ?, ?)",
        (workflow_id, status, (detail or "")[:1000]),
    )


def run_workflow(conn, wf, context: dict | None = None) -> tuple[str, str]:
    from . import pipeline

    cfg = json.loads(wf["action_config"] or "{}")
    recipe = pipeline.get_action_def(wf["action_type"])
    try:
        if recipe is None:
            status, detail = "error", f"unknown action '{wf['action_type']}'"
        else:
            detail = pipeline.run_pipeline(conn, recipe, cfg, wf["id"], context)
            status = "ok"
    except Exception as exc:  # noqa: BLE001 — record any failure, never crash a trigger
        status, detail = "error", str(exc)

    conn.execute(
        "UPDATE workflows SET last_run_at=datetime('now'), last_status=? WHERE id=?",
        (status, wf["id"]),
    )
    _log_run(conn, wf["id"], status, detail)
    conn.commit()
    return status, detail


def fire_event(conn, event: str, context: dict | None = None) -> None:
    """Run enabled event-workflows whose trigger matches `event` (+ optional match)."""
    rows = conn.execute(
        "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = 'event'"
    ).fetchall()
    for wf in rows:
        tc = json.loads(wf["trigger_config"] or "{}")
        if tc.get("event") != event:
            continue
        match = tc.get("match") or {}
        if match and context and not all(context.get(k) == v for k, v in match.items()):
            continue
        run_workflow(conn, wf, context)


def _parse_utc(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def schedule_due(last_run_at: str | None, tc: dict, now: datetime | None = None) -> bool:
    """Is a scheduled workflow due? Supports cron ("0 7 * * *", in the server's
    TZ) and interval_seconds. Cron never fires immediately on first enable."""
    now = now or datetime.now(timezone.utc)
    last = _parse_utc(last_run_at)
    cron = tc.get("cron")
    if cron:
        try:
            from croniter import croniter
        except Exception:
            return False
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(os.environ.get("TZ") or "UTC")
        except Exception:
            tz = timezone.utc
        now_local = now.astimezone(tz)
        base = (last or now).astimezone(tz)
        try:
            nxt = croniter(cron, base).get_next(datetime)
            return nxt <= now_local
        except Exception:
            return False
    interval = int(tc.get("interval_seconds", 0) or 0)
    if interval <= 0:
        return False
    if last is None:
        return True
    return (now - last).total_seconds() >= interval


def run_due_scheduled(conn) -> int:
    """Run scheduled workflows (cron or interval) that are due. Returns count run."""
    rows = conn.execute(
        "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = 'schedule'"
    ).fetchall()
    ran = 0
    for wf in rows:
        if schedule_due(wf["last_run_at"], json.loads(wf["trigger_config"] or "{}")):
            run_workflow(conn, wf)
            ran += 1
    return ran
