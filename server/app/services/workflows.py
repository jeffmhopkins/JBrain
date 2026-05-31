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
from pathlib import Path

import yaml

import re

from ..config import get_settings
from ..db import get_meta, set_meta
from . import notes as notes_svc
from . import reviews as reviews_svc

_DATED_LINE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\* (.*)$")

_TODAY = lambda: __import__("datetime").date.today().isoformat()


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


# --- Actions ----------------------------------------------------------------
# All actions share the signature (conn, cfg, workflow_id, context).

def _slug_for(conn, title: str | None) -> str | None:
    if not title:
        return None
    n = notes_svc.get_by_title(conn, title)
    return n["slug"] if n else None


def _maybe_review(conn, cfg: dict, workflow_id, default_title: str, link_slug: str | None) -> str:
    """If the action config has a `review` block, post a Review item linking to
    the written entry. Optional — not every workflow uses it."""
    rev = cfg.get("review")
    if not rev:
        return ""
    title = (rev.get("title") or default_title).replace("{date}", _TODAY())
    message = (rev.get("message") or "").replace("{date}", _TODAY())
    reviews_svc.create_review_item(conn, workflow_id, title, message, link_slug)
    return " (+review)"


def _action_append_to_note(conn, cfg: dict, workflow_id, context=None) -> str:
    title = cfg["title"]
    text = (cfg.get("text") or "").replace("{date}", _TODAY())
    note = notes_svc.get_by_title(conn, title)
    body = note["content_md"] if note else f"# {title}\n"
    notes_svc.upsert_note(
        conn, title, body.rstrip() + "\n" + text + "\n",
        source="workflow", version_note="workflow append",
    )
    return f"appended to '{title}'" + _maybe_review(conn, cfg, workflow_id, f"Review: {title}", _slug_for(conn, title))


def _action_claude_synthesize(conn, cfg: dict, workflow_id, context=None) -> str:
    """Gather context, run a Claude prompt, write the result. (Foundation for the
    day-log summariser.) Requires an Anthropic key."""
    settings = get_settings()
    if not settings.has_anthropic:
        raise RuntimeError("no Anthropic API key configured")

    from anthropic import Anthropic
    from . import embeddings

    target = cfg["target_title"]
    prompt = cfg.get("prompt", "Summarise the following:")
    gathered = ""
    if cfg.get("source_title"):
        row = notes_svc.get_by_title(conn, cfg["source_title"])
        gathered = row["content_md"] if row else ""
    elif cfg.get("context_query"):
        for r in embeddings.semantic_search(conn, cfg["context_query"], 8):
            n = notes_svc.get_by_title(conn, r["title"])
            if n:
                gathered += f"\n\n## {n['title']}\n{n['content_md']}"

    client = Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": f"{prompt}\n\n<content>\n{gathered}\n</content>"}],
    )
    result = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    note = notes_svc.get_by_title(conn, target)
    if cfg.get("mode") == "append" and note:
        result = note["content_md"].rstrip() + "\n\n" + result
    notes_svc.upsert_note(conn, target, result, source="workflow", version_note="workflow synthesis")
    return f"synthesised into '{target}'" + _maybe_review(conn, cfg, workflow_id, f"Review: {target}", _slug_for(conn, target))


def _action_create_review_item(conn, cfg: dict, workflow_id, context=None) -> str:
    link_slug = _slug_for(conn, cfg.get("link_title"))
    title = (cfg["title"]).replace("{date}", _TODAY())
    message = (cfg.get("message") or "").replace("{date}", _TODAY())
    reviews_svc.create_review_item(conn, workflow_id, title, message, link_slug)
    return "created review item"


def _summarise_entries(entries: list[str]) -> str:
    """Summarise a day's log entries. Uses Claude when configured, else a plain
    recap so the workflow still works without an API key."""
    joined = "\n".join(f"- {e}" for e in entries)
    settings = get_settings()
    if not settings.has_anthropic:
        return "Entries:\n" + joined
    from anthropic import Anthropic
    client = Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=512,
        messages=[{"role": "user", "content":
                   "Summarise this day's log entries into a tight paragraph or a "
                   f"few bullets:\n{joined}"}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _action_summarize_day_log(conn, cfg: dict, workflow_id, context=None) -> str:
    """On a new day, summarise each completed prior day of a log into a summary
    note (once each, tracked by a per-log watermark in meta)."""
    log_title = cfg["log_title"]
    note = notes_svc.get_by_title(conn, log_title)
    if not note:
        return f"no log note '{log_title}' yet"

    by_date: dict[str, list[str]] = {}
    for line in note["content_md"].splitlines():
        m = _DATED_LINE.match(line.strip())
        if m:
            by_date.setdefault(m.group(1), []).append(m.group(2))
    if not by_date:
        return "no dated entries"

    dates = sorted(by_date)
    current_day = dates[-1]  # the day currently being logged into
    wm_key = f"daylog_summarized:{log_title.lower()}"
    last = get_meta(wm_key)
    todo = [d for d in dates if d < current_day and (last is None or d > last)]
    if not todo:
        return "nothing new to summarise"

    summary_title = cfg.get("summary_title") or f"{log_title} — Daily Summaries"
    for d in todo:
        text = _summarise_entries(by_date[d])
        snote = notes_svc.get_by_title(conn, summary_title)
        body = snote["content_md"] if snote else f"# {summary_title}\n"
        notes_svc.upsert_note(
            conn, summary_title, body.rstrip() + f"\n\n## {d}\n\n{text}\n",
            source="workflow", version_note=f"day summary {d}",
        )
    set_meta(conn, wm_key, todo[-1])

    if cfg.get("review") is not False:
        rev = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
        title = (rev.get("title") or f"Daily review — {todo[-1]}")
        reviews_svc.create_review_item(
            conn, workflow_id, title,
            f"Summarised {len(todo)} day(s) of '{log_title}'.", _slug_for(conn, summary_title),
        )
    return f"summarised {', '.join(todo)}"


_ACTIONS = {
    "append_to_note": _action_append_to_note,
    "claude_synthesize": _action_claude_synthesize,
    "create_review_item": _action_create_review_item,
    "summarize_day_log": _action_summarize_day_log,
}


# --- Execution --------------------------------------------------------------

def _log_run(conn, workflow_id: int, status: str, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO workflow_runs (workflow_id, status, detail) VALUES (?, ?, ?)",
        (workflow_id, status, (detail or "")[:1000]),
    )


def run_workflow(conn, wf, context: dict | None = None) -> tuple[str, str]:
    cfg = json.loads(wf["action_config"] or "{}")
    action = _ACTIONS.get(wf["action_type"])
    try:
        if action is None:
            status, detail = "error", f"unknown action '{wf['action_type']}'"
        else:
            detail = action(conn, cfg, wf["id"], context)
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


def run_due_scheduled(conn) -> int:
    """Run interval-scheduled workflows that are due. Returns count run."""
    rows = conn.execute(
        "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = 'schedule'"
    ).fetchall()
    ran = 0
    for wf in rows:
        tc = json.loads(wf["trigger_config"] or "{}")
        interval = int(tc.get("interval_seconds", 0) or 0)
        if interval <= 0:
            continue
        if wf["last_run_at"] is None:
            due = True
        else:
            secs = conn.execute(
                "SELECT (julianday('now') - julianday(?)) * 86400 AS s", (wf["last_run_at"],)
            ).fetchone()["s"]
            due = secs is not None and secs >= interval
        if due:
            run_workflow(conn, wf)
            ran += 1
    return ran
