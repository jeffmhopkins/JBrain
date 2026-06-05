#!/usr/bin/env python3
"""wiki_lab — drive JBrain's REAL pipeline locally with the `claude` CLI as the LLM
backend, so wiki build/maintenance can be iterated on without an API key or a server.

It loads the app in-process, points it at a throwaway SQLite DB, and monkeypatches the
LLM layer to shell out to `claude -p`. You feed it a JBrain notes export (the JSON from
Settings → "Export original notes", or any [{title, content_md, created_at}] array),
ingest it day-by-day with the original timestamps, and run any action recipe
(consolidate_daily, wiki_build, wiki_update, wiki_maintain, …) against it.

NOTHING here contains or commits data: the export you point at and the generated .db
live OUTSIDE git (the DB defaults under data/, which is .gitignored). Keep your export
out of the repo too.

Examples
--------
  # inspect a export: per-day counts of raw captures vs JBrain's own generated output
  python server/tools/wiki_lab.py --notes ~/jbrain-export.json days

  # bootstrap the KB from day one, then watch it evolve
  python server/tools/wiki_lab.py --notes ~/export.json ingest 2026-06-01
  python server/tools/wiki_lab.py run consolidate_daily '{"review": false}'
  python server/tools/wiki_lab.py run wiki_build '{}'
  python server/tools/wiki_lab.py dump --full

  # next day: incremental maintenance
  python server/tools/wiki_lab.py --notes ~/export.json ingest 2026-06-02
  python server/tools/wiki_lab.py run consolidate_daily '{"review": false}'
  python server/tools/wiki_lab.py run wiki_update '{}'
  python server/tools/wiki_lab.py run wiki_maintain '{}'

  python server/tools/wiki_lab.py reset        # wipe the DB and start over
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # …/JBrain
SERVER = REPO / "server"


# Block every tool so `claude -p` is a pure single-shot completion — it can't wander off
# into file/web tools and return prose-with-no-answer (which, on the big outline prompt,
# silently parsed to zero articles and — with kb_reset — wiped a day's KB).
_NO_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]


def _bridge(model_default: str, model_cheap: str, retries: int = 3):
    """Patch JBrain's llm layer to answer via the local `claude` CLI. cheap-tier calls
    (tagging, analysis, summaries) use the fast model; everything else the default. Retries
    on an empty/errored response so a single flaky call can't break a build."""
    from app.services import llm

    def complete(messages, *, system=None, model=None, max_tokens=1024):
        parts = [str(system)] if system else []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):                    # vision/content blocks → text only
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            parts.append(str(c))
        prompt, mdl = "\n\n".join(parts), (model_cheap if (model and "haiku" in str(model)) else model_default)
        last = "no output"
        for _ in range(retries):
            try:
                r = subprocess.run(["claude", "-p", "--model", mdl, "--disallowedTools", *_NO_TOOLS],
                                   input=prompt, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                last = "timeout"; continue
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out:
                return out
            last = (r.stderr or "empty output")[:300]
        raise RuntimeError(f"claude CLI failed after {retries} tries ({mdl}): {last}")

    llm.complete = complete
    llm.has_credentials = lambda: True


def _connect(db_path: str):
    os.environ.setdefault("JBRAIN_ACCESS_KEY", "wiki-lab-local-key-0123456789")
    os.environ.setdefault("BRAIN_NAME", "Wiki Lab")
    os.environ.setdefault("JBRAIN_DOMAIN", "localhost")
    os.environ["DB_PATH"] = db_path
    sys.path.insert(0, str(SERVER))
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db
    db.init_db()
    return db.get_conn()


# --- export classification -----------------------------------------------------
_DATED = re.compile(r"(\d{4})/(\d{2})/(\d{2})/\d+")

def _is_raw(n: dict) -> bool:
    """A genuine user capture, vs JBrain's own output that a full DB export also carries:
    the synthesized kb/ layer, and back-dated analysis/daily/profile cards (a notes/.../N
    title whose date precedes the row's created_at — i.e. generated during a later build)."""
    t = n["title"]
    if t.startswith("kb/"):
        return False
    m = _DATED.search(t)
    if m and f"{m.group(1)}-{m.group(2)}-{m.group(3)}" < n["created_at"][:10]:
        return False
    return True


def _raw_notes(notes_path: str) -> list[dict]:
    return [n for n in json.load(open(notes_path)) if _is_raw(n)]


# --- commands ------------------------------------------------------------------
def cmd_days(args):
    import collections
    data = json.load(open(args.notes))
    raw, gen = collections.Counter(), collections.Counter()
    for n in data:
        d = n["created_at"][:10]
        (raw if _is_raw(n) else gen)[d] += 1
    print(f"{'day':<12} {'raw':>5} {'generated':>10}")
    for d in sorted(set(raw) | set(gen)):
        print(f"{d:<12} {raw[d]:>5} {gen[d]:>10}")
    print(f"\n{sum(raw.values())} raw user captures · {sum(gen.values())} generated (skipped on ingest)")


def cmd_ingest(args):
    conn = _connect(args.db)
    from app.services import notes as ns
    day = args.day
    todays = [n for n in _raw_notes(args.notes) if n["created_at"][:10] == day]
    for n in sorted(todays, key=lambda x: x["created_at"]):
        title = n["title"]
        if title.startswith("Kb/"):                    # a user's hand-made card → a source note
            title = "notes/" + title[3:]
        nid = ns.upsert_note(conn, title, n["content_md"])   # prefix rules set loc/ → place, etc.
        ts = n["created_at"]
        conn.execute("UPDATE notes SET created_at=?, updated_at=? WHERE id=?", (ts, ts, nid))
        conn.execute("UPDATE note_versions SET created_at=? WHERE note_id=?", (ts, nid))
    conn.commit()
    print(f"ingested {len(todays)} raw notes for {day}")


def cmd_analyze(args):
    """Pre-compute per-note analysis CONCURRENTLY. analyze() is hash-guarded, so a
    later wiki_build/wiki_update finds nothing pending and skips its (serial) analysis
    loop — the single biggest speedup, since these calls are independent. Each thread
    uses its own thread-local connection (WAL serialises the brief writes)."""
    conn = _connect(args.db)
    _bridge(args.model_default, args.model_cheap)
    import app.db as db
    from app.services import note_analysis
    ids = note_analysis.pending_ids(conn, limit=100000, force=args.force)
    if not ids:
        print("nothing to analyze")
        return
    done, errs, lock = [0], [], threading.Lock()

    def work(nid):
        c = db.get_conn()                              # per-thread connection
        try:
            note_analysis.analyze(c, nid, force=args.force)
            c.commit()
        except Exception as e:                          # noqa: BLE001
            with lock: errs.append((nid, str(e)))
            return
        with lock:
            done[0] += 1
            print(f"   analyzed {done[0]}/{len(ids)}", end="\r", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, ids))
    print(f"\nanalyzed {done[0]}/{len(ids)} notes" + (f"; {len(errs)} failed" if errs else ""))
    for nid, e in errs[:5]:
        print(f"   note {nid}: {e}")


def cmd_run(args):
    conn = _connect(args.db)
    _bridge(args.model_default, args.model_cheap)
    from app.services import pipeline
    recipe = pipeline.get_action_def(args.action)
    if not recipe:
        sys.exit(f"unknown action '{args.action}'. Available: {', '.join(sorted(pipeline.action_types()))}")
    cfg = json.loads(args.config) if args.config else {}
    # workflow_id=None: there's no workflows row in the lab, and review_items.workflow_id
    # is a nullable FK — passing a dummy id (e.g. 0) trips a FOREIGN KEY constraint when
    # a recipe posts a review card.
    msg = pipeline.run_pipeline(conn, recipe, cfg, None, None, on_step=lambda s: print("   ·", s, flush=True))
    print(f"[{args.action}] -> {msg}")


def cmd_dump(args):
    conn = _connect(args.db)
    rows = conn.execute("SELECT title, content_md FROM notes WHERE kind='kb' "
                        "AND deleted_at IS NULL ORDER BY title").fetchall()
    print(f"===== KB: {len(rows)} articles =====")
    for r in rows:
        body = r["content_md"]
        print(f"\n### {r['title']}  ({len(body)} chars)")
        print(body if args.full else "\n".join(body.splitlines()[:6]))


def cmd_entities(args):
    conn = _connect(args.db)
    for r in conn.execute("SELECT canonical_name, type, note_count FROM entities "
                          "ORDER BY note_count DESC, canonical_name LIMIT 60"):
        print(f"  {r['note_count']:>2}  {r['type']:<10} {r['canonical_name']}")


def cmd_reset(args):
    for suffix in ("", "-wal", "-shm"):
        p = Path(args.db + suffix)
        if p.exists():
            p.unlink()
    print(f"removed {args.db}")


def main():
    ap = argparse.ArgumentParser(description="Drive JBrain's pipeline locally via the claude CLI.")
    ap.add_argument("--db", default=os.environ.get("WIKI_LAB_DB", str(REPO / "data" / "wiki_lab.db")),
                    help="throwaway SQLite DB (default: data/wiki_lab.db, gitignored)")
    ap.add_argument("--notes", default=os.environ.get("WIKI_LAB_NOTES"),
                    help="path to a JBrain notes export JSON (keep it outside the repo)")
    ap.add_argument("--model-default", default=os.environ.get("WIKI_LAB_MODEL", "sonnet"))
    ap.add_argument("--model-cheap", default=os.environ.get("WIKI_LAB_MODEL_CHEAP", "haiku"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("days").set_defaults(func=cmd_days)
    p = sub.add_parser("ingest"); p.add_argument("day"); p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("analyze", help="pre-compute note analysis concurrently (speeds up build/update)")
    p.add_argument("--workers", type=int, default=6); p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("run"); p.add_argument("action"); p.add_argument("config", nargs="?"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("dump"); p.add_argument("--full", action="store_true"); p.set_defaults(func=cmd_dump)
    sub.add_parser("entities").set_defaults(func=cmd_entities)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    args = ap.parse_args()
    if args.cmd in ("days", "ingest") and not args.notes:
        sys.exit("--notes PATH (or $WIKI_LAB_NOTES) is required for this command")
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
