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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import clock
from . import embeddings
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
    return llm.complete([{"role": "user", "content": f"{instruction}\n{joined}"}],
                        model=llm.model_for("cheap"), max_tokens=512)


def _relevant_kb(conn, entries: list, fallback_kb: list, k: int = 12) -> list[dict]:
    """The existing KB articles most relevant to this batch of entries, so
    synthesis scales as the KB grows (instead of sending every article). Per-entry
    semantic search, unioned by best distance, filtered to kb. Falls back to the
    passed-in list at cold start (no conn / no embeddings yet)."""
    if conn is None:
        return fallback_kb[:k]
    best: dict[int, float] = {}
    for e in entries:
        query = f"{e.get('title', '')}\n{(e.get('content_md') or '')[:500]}"
        try:
            hits = embeddings.semantic_search(conn, query, limit=8)
        except Exception:
            hits = []
        for r in hits:
            if r["id"] not in best or r["distance"] < best[r["id"]]:
                best[r["id"]] = r["distance"]
    top_ids = [nid for nid, _ in sorted(best.items(), key=lambda kv: kv[1])][:k]
    if not top_ids:
        return fallback_kb[:k]
    rows = conn.execute(
        f"SELECT id, title, content_md FROM notes WHERE id IN "
        f"({','.join('?' * len(top_ids))}) AND kind = 'kb' AND deleted_at IS NULL",
        top_ids,
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    kb = [by_id[i] for i in top_ids if i in by_id]
    return kb or fallback_kb[:k]


def _entry_block(e: dict) -> str:
    """Render one source entry for the prompt: heading is the EXACT title (so a
    [[cite]] resolves), then a status line + the content. Edited entries look like
    new ones (reconcile); deleted entries are flagged so the model cleans up the
    articles that cited them."""
    # Expand @t[...] live values to a DATED SNAPSHOT so the evergreen KB records a
    # timeless, sourced fact ("40 (as of 2026-06-01; born 1986-03-01)") rather than
    # a live token the synthesis LLM can't faithfully reproduce.
    content = clock.expand_tokens(e["content_md"], snapshot=True)
    if e.get("deleted"):
        date = (e.get("deleted_at") or "")[:10]
        return (f"## {e['title']}\n[REMOVED by the owner{f' on {date}' if date else ''}] — the source entry "
                f"[[{e['title']}]] was DELETED. Find articles that cite it, remove or correct the facts that "
                f"came only from it, and drop it from their Sources. Former content (for reference only):\n"
                f"{content}")
    date = (e.get("created_at") or "")[:10]
    cite = (f"Logged {date}. " if date else "") + f"Cite this entry as [[{e['title']}]]."
    return f"## {e['title']}\n{cite}\n{content}"


def _linked_kb(conn, entries: list) -> list[dict]:
    """KB articles that CITE any of these entries (via the links table) — so when an
    entry is edited or deleted, the articles that referenced it are in context to be
    updated, even if semantic search wouldn't surface them."""
    if conn is None:
        return []
    titles = [e["title"] for e in entries if e.get("title")]
    if not titles:
        return []
    rows = conn.execute(
        "SELECT DISTINCT n.id, n.title, n.content_md FROM notes n JOIN links l ON l.source_note_id = n.id "
        f"WHERE n.kind = 'kb' AND n.deleted_at IS NULL AND lower(l.target_title) IN "
        f"({','.join('?' * len(titles))})",
        [t.lower() for t in titles],
    ).fetchall()
    return [dict(r) for r in rows]


def _synthesize_actions(entries: list, existing_kb: list, instructions: str | None = None,
                        *, conn=None) -> list[dict]:
    """Ask the LLM to fold new entries into the knowledge base. Returns a list of
    {title, content_md}. Factored out so it can be stubbed in tests.

    `conn` (injected by the wiki_plan primitive) enables semantic retrieval of only
    the relevant existing KB articles; without it the passed-in existing_kb is used.
    `instructions` (from the workflow YAML config) is extra guidance appended to the
    base prompt; the JSON-output contract is always enforced."""
    if not entries:
        return []                       # nothing to synthesize — never call the LLM
    if not llm.has_credentials():
        raise RuntimeError("no LLM API key configured")

    kb = _relevant_kb(conn, entries, existing_kb)
    # Always include articles that cite the changed/deleted entries, so edits and
    # removals can be reconciled even if semantic search wouldn't surface them.
    seen = {k["title"] for k in kb}
    for k in _linked_kb(conn, entries):
        if k["title"] not in seen:
            kb.append(k); seen.add(k["title"])
    # Protected system pages (guides, the index) describe the KB — they're never
    # synthesis context and must never be overwritten as if they were articles.
    from . import wiki_guides
    kb = [k for k in kb if not wiki_guides.is_protected(k["title"])]
    entries_text = "\n\n".join(_entry_block(e) for e in entries)
    kb_text = "\n\n".join(f"### {k['title']}\n{k['content_md']}" for k in kb) or "(none yet)"
    extra = f"\nAdditional guidance: {instructions}" if instructions else ""
    template = prompts.get("actions.wiki_synthesis", _DEFAULT_WIKI_TEMPLATE)
    prompt = (template
              .replace("{instructions}", extra)
              .replace("{entries}", entries_text)
              .replace("{existing_kb}", kb_text))
    text = llm.complete([{"role": "user", "content": prompt}],
                        model=llm.model_for("synthesis"), max_tokens=8192)
    data = _parse_json_array(text)
    return [a for a in data if isinstance(a, dict) and a.get("title") and a.get("content_md")]


def _parse_json_array(text: str) -> list:
    """Extract the JSON array of articles from an LLM reply, tolerating prose
    around it AND a truncated trailing object — we recover the complete prefix
    rather than dropping the whole batch. Brackets inside string values (e.g.
    [[wiki-links]]) are ignored via string/escape tracking, so the naive
    find('[')/rfind(']') over/under-reach problems don't apply.

    Returns the parsed list, or [] if not even one complete object is recoverable
    (an empty result is treated as 'nothing durable', so the watermark won't
    advance and the batch is retried)."""
    import json as _json
    start = text.find("[")
    if start == -1:
        return []
    depth = in_str = esc = 0
    end = -1            # index past the array's closing ']', if reached
    last_obj_end = -1   # index past the last COMPLETE top-level object
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = 0
            elif c == "\\":
                esc = 1
            elif c == '"':
                in_str = 0
            continue
        if c == '"':
            in_str = 1
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 1 and c == "}":      # closed a top-level array element
                last_obj_end = i + 1
            elif depth == 0 and c == "]":    # closed the array itself
                end = i + 1
                break
    if end != -1:
        candidate = text[start:end]
    elif last_obj_end != -1:                 # truncated mid-array: salvage the prefix
        candidate = text[start:last_obj_end] + "]"
    else:
        return []
    try:
        data = _json.loads(candidate)
    except Exception:
        return []
    return data if isinstance(data, list) else []


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
        model=llm.model_for("cheap"), max_tokens=80,
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

    meta: dict[str, dict] = {}
    for t in pipeline.action_types():
        recipe = pipeline.get_action_def(t) or {}
        meta[t] = {"config": recipe.get("config", []), "category": recipe.get("category") or "Other"}
    for t, schema in _PY_ACTION_SCHEMAS.items():
        meta.setdefault(t, {"config": schema, "category": "Other"})
    return [{"type": t, "config": meta[t]["config"], "category": meta[t]["category"]} for t in sorted(meta)]


# --- Execution --------------------------------------------------------------

def _log_run(conn, workflow_id: int, status: str, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO workflow_runs (workflow_id, status, detail) VALUES (?, ?, ?)",
        (workflow_id, status, (detail or "")[:1000]),
    )


def reset_stale_runs(conn) -> None:
    """On startup, fail any run rows left 'running' by a previous process so a
    crash mid-run doesn't wedge a trigger as perpetually in-progress."""
    conn.execute(
        "UPDATE workflow_runs SET status='error', detail='interrupted (server restarted)' "
        "WHERE status='running'"
    )
    conn.execute("UPDATE workflows SET last_status='error' WHERE last_status='running'")
    conn.commit()


def start_manual_run(conn, wf_id: int) -> dict:
    """Kick off a manual run in a background thread and return immediately. Poll
    latest_run() for status. A run already in flight is returned as-is (no
    double-run)."""
    existing = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_id=? AND status='running' "
        "AND started_at > datetime('now','-15 minutes') ORDER BY id DESC LIMIT 1",
        (wf_id,),
    ).fetchone()
    if existing:
        return {"running": True, "run_id": existing["id"]}
    cur = conn.execute(
        "INSERT INTO workflow_runs (workflow_id, status, detail) VALUES (?, 'running', '')",
        (wf_id,),
    )
    run_id = cur.lastrowid
    conn.execute("UPDATE workflows SET last_run_at=datetime('now'), last_status='running' WHERE id=?", (wf_id,))
    conn.commit()
    threading.Thread(target=_execute_manual_run, args=(wf_id, run_id), daemon=True).start()
    return {"running": True, "run_id": run_id}


# Live per-run step progress, for the "watch" modal. In-memory (NOT the DB) because
# the run's connection holds the pipeline's uncommitted writes — committing progress
# mid-run would persist partial work. Keyed by run_id; last few runs retained.
_RUN_PROGRESS: dict[int, dict] = {}
_RUN_PROGRESS_LOCK = threading.Lock()
_RUN_PROGRESS_MAX = 30


def _progress_init(run_id: int) -> None:
    with _RUN_PROGRESS_LOCK:
        _RUN_PROGRESS[run_id] = {"events": [], "status": "running", "detail": ""}
        for old in sorted(_RUN_PROGRESS)[:-_RUN_PROGRESS_MAX]:   # evict the oldest
            _RUN_PROGRESS.pop(old, None)


def _progress_step(run_id: int, name: str) -> None:
    with _RUN_PROGRESS_LOCK:
        p = _RUN_PROGRESS.get(run_id)
        if p is not None:
            p["events"].append({"name": name, "at": datetime.now(timezone.utc).isoformat()})
            if len(p["events"]) > 1000:
                p["events"] = p["events"][-1000:]


def _progress_finish(run_id: int, status: str, detail: str) -> None:
    with _RUN_PROGRESS_LOCK:
        p = _RUN_PROGRESS.get(run_id)
        if p is not None:
            p["status"], p["detail"] = status, detail or ""


def run_progress(run_id: int) -> dict | None:
    """Snapshot of a run's live step progress, or None if not tracked (e.g. after a
    restart, or a scheduled — non-manual — run)."""
    with _RUN_PROGRESS_LOCK:
        p = _RUN_PROGRESS.get(run_id)
        return {"events": list(p["events"]), "status": p["status"], "detail": p["detail"]} if p else None


def _execute_manual_run(wf_id: int, run_id: int) -> None:
    """Background worker: run the pipeline on its OWN connection, then update the
    pre-created run row + the workflow's last_status."""
    from ..db import get_conn
    from . import pipeline
    conn = get_conn()  # this thread's own sqlite connection
    _progress_init(run_id)
    status, detail = "error", "workflow not found"
    wf = conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if wf is not None:
        cfg = json.loads(wf["action_config"] or "{}")
        recipe = pipeline.get_action_def(wf["action_type"])
        try:
            if recipe is None:
                status, detail = "error", f"unknown action '{wf['action_type']}'"
            else:
                detail = pipeline.run_pipeline(conn, recipe, cfg, wf["id"], None,
                                               on_step=lambda n: _progress_step(run_id, n))
                status = "ok"
                conn.commit()          # persist the pipeline's writes
        except Exception as exc:        # noqa: BLE001 — record any failure
            conn.rollback()             # discard partial pipeline writes
            status, detail = "error", str(exc)
    _progress_finish(run_id, status, detail)
    try:
        conn.execute("UPDATE workflows SET last_run_at=datetime('now'), last_status=? WHERE id=?", (status, wf_id))
        conn.execute("UPDATE workflow_runs SET status=?, detail=? WHERE id=?", (status, (detail or "")[:1000], run_id))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()


def run_workflow(conn, wf, context: dict | None = None, commit: bool = True) -> tuple[str, str]:
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
    if commit:  # synchronous in-transaction callers (entry_created hook) commit themselves
        conn.commit()
    return status, detail


def fire_event(conn, event: str, context: dict | None = None, commit: bool = True) -> None:
    """Run enabled event-workflows whose trigger matches `event` (+ optional match).

    `commit=False` when fired from inside another write transaction so the
    workflow's writes are flushed by the caller's commit (atomicity)."""
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
        run_workflow(conn, wf, context, commit=commit)


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
        tz = clock.app_tz()
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


# --- Location triggers ------------------------------------------------------
# Evaluated by the SCHEDULER (not at ingest): ingest only refreshes location_state
# (geotrail.update_location_state). Here we read that state and decide which
# location:* event workflows should fire, dedup via location_fired, and run each
# matched workflow DIRECTLY (run_workflow) — never fire_event, which would broadcast
# to every workflow sharing the event name regardless of which place it watches.

def _loc_place_decision(conn, kind: str, tc: dict, now: datetime):
    """For a place-bound location trigger, return (marker, context) when it should
    fire now, else (None, None). `marker` makes the firing idempotent per visit/
    departure (location_fired stores the last marker per workflow+kind)."""
    name = (tc.get("place") or "").strip()
    if not name:
        return None, None
    p = conn.execute(
        "SELECT id, name, lat, lon, radius_m FROM places WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)
    ).fetchone()
    if not p:
        return None, None
    st = conn.execute(
        "SELECT inside, since, last_inside_at FROM location_state WHERE place_id = ?", (p["id"],)
    ).fetchone()
    if not st:
        return None, None
    threshold = float(tc.get("minutes", 60) or 60)
    ctx = {"place": p["name"], "lat": p["lat"], "lon": p["lon"], "kind": kind, "minutes": threshold}

    if kind == "arrived":
        if st["inside"] and st["since"]:
            ctx["since"] = st["since"]
            return st["since"], ctx
    elif kind == "left":
        if not st["inside"] and st["since"] and st["last_inside_at"]:
            ctx["left_at"] = st["since"]
            return st["since"], ctx
    elif kind == "dwell":
        start = _parse_utc(st["since"])
        if st["inside"] and start:
            elapsed = (now - start).total_seconds() / 60.0
            if elapsed >= threshold:
                ctx["minutes"] = round(elapsed, 1)
                return st["since"], ctx
    elif kind == "away":
        last = _parse_utc(st["last_inside_at"])
        if not st["inside"] and last:
            elapsed = (now - last).total_seconds() / 60.0
            if elapsed >= threshold:
                ctx["minutes"] = round(elapsed, 1)
                return st["last_inside_at"], ctx
    return None, None


def _loc_new_place(conn, tc: dict, now: datetime):
    """An unlabeled stay (not near any saved place / coord-note) held long enough,
    in the last ~2 days. Marker = coarse coords so each distinct spot fires once."""
    from . import geotrail
    min_min = float(tc.get("minutes", 30) or 30)
    since = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    unlabeled = [s for s in geotrail.stay_points(conn, since=since, min_min=min_min) if not s["label"]]
    if not unlabeled:
        return None, None
    s = unlabeled[-1]   # most recent
    ctx = {"place": "an unlabeled spot", "lat": s["lat"], "lon": s["lon"],
           "kind": "new_place", "minutes": s["minutes"]}
    return f"{round(s['lat'], 3)},{round(s['lon'], 3)}", ctx


def evaluate_location_triggers(conn) -> int:
    """Fire due location:* event workflows. Called from the scheduler loop on its
    own connection (LLM-capable actions run here, where latency is fine)."""
    rows = conn.execute(
        "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = 'event'"
    ).fetchall()
    loc = []
    for wf in rows:
        tc = json.loads(wf["trigger_config"] or "{}")
        ev = tc.get("event") or ""
        if ev.startswith("location:"):
            loc.append((wf, tc, ev.split(":", 1)[1]))
    if not loc:
        return 0
    now = datetime.now(timezone.utc)
    ran = 0
    for wf, tc, kind in loc:
        try:
            marker, ctx = (_loc_new_place(conn, tc, now) if kind == "new_place"
                           else _loc_place_decision(conn, kind, tc, now))
        except Exception:  # noqa: BLE001 — a bad trigger config must not wedge the loop
            marker, ctx = None, None
        if not marker:
            continue
        prev = conn.execute(
            "SELECT marker FROM location_fired WHERE workflow_id = ? AND kind = ?", (wf["id"], kind)
        ).fetchone()
        if prev and prev["marker"] == marker:
            continue   # already fired for this visit/departure/spot
        status, _ = run_workflow(conn, wf, ctx, commit=False)
        # Only record the firing if the action succeeded — a transient failure
        # (e.g. push offline) should be retried next tick, not silently swallowed.
        if status == "ok":
            conn.execute(
                "INSERT INTO location_fired (workflow_id, kind, marker) VALUES (?, ?, ?) "
                "ON CONFLICT(workflow_id, kind) DO UPDATE SET marker = excluded.marker, "
                "fired_at = datetime('now')",
                (wf["id"], kind, marker),
            )
            ran += 1
        conn.commit()
    return ran
