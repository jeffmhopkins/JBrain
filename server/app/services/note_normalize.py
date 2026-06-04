"""Bulk note-title normalization — two owner-run passes that tidy loose notes:

  redate_batch()  — file every non-conforming entry note under the flat dated tree
                    notes/YYYY/MM/DD/N (N = next per-day sequence). Deterministic, no LLM.
  title_batch()   — give each bare dated note a generated leaf title, so
                    notes/2026/06/04/2  ->  notes/2026/06/04/2 - cardiology invoice. LLM.

Both rename via notes.upsert_note (versioned; inbound [[links]] are rewritten), skip the
kb/ layer + protected pages + the PWA's own notes/daily/ capture tree, and are idempotent
(an already-conforming note is left alone), so they're safe to re-run.
"""
from __future__ import annotations

import re

from . import llm, prompts, wiki_guides
from . import notes as notes_svc

# Flat dated leaf: notes/YYYY/MM/DD/N, optionally "N - title".
_DATED = re.compile(r"^notes/\d{4}/\d{2}/\d{2}/\d+( - .+)?$")
_BARE_DATED = re.compile(r"^notes/\d{4}/\d{2}/\d{2}/\d+$")
_TITLE_MAX = 60


def _day_of(conn, created_at: str | None) -> str:
    """The note's capture day as YYYY/MM/DD (from created_at; today if missing)."""
    s = (created_at or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s = conn.execute("SELECT strftime('%Y-%m-%d','now') AS d").fetchone()["d"]
    return s.replace("-", "/")


def _max_flat_n(conn, day: str) -> int:
    """Highest existing N under notes/<day>/ — counting soft-deleted rows too, since
    notes.title is UNIQUE, so we never reissue a number."""
    prefix = f"notes/{day}/"
    n = 0
    for r in conn.execute("SELECT title FROM notes WHERE title LIKE ?", (prefix + "%",)).fetchall():
        head = r["title"][len(prefix):].split(" - ", 1)[0]
        if head.isdigit():
            n = max(n, int(head))
    return n


def redate_batch(conn, limit: int = 2000, dry_run: bool = False) -> dict:
    """Move loose entry notes into notes/YYYY/MM/DD/N (by created_at, in order). Returns the
    plan + count; with dry_run it only previews."""
    cands = conn.execute(
        "SELECT id, title, created_at FROM notes WHERE kind='entry' AND deleted_at IS NULL "
        "AND title NOT LIKE 'notes/daily/%' AND title NOT LIKE 'kb/%' "
        "ORDER BY created_at, id").fetchall()
    rows = [r for r in cands if not _DATED.match(r["title"]) and not wiki_guides.is_protected(r["title"])]
    rows = rows[:max(1, int(limit))]

    next_n: dict[str, int] = {}
    plan = []
    for r in rows:
        day = _day_of(conn, r["created_at"])
        if day not in next_n:
            next_n[day] = _max_flat_n(conn, day) + 1
        plan.append({"id": r["id"], "old": r["title"], "new": f"notes/{day}/{next_n[day]:02d}"})
        next_n[day] += 1

    if not dry_run:
        for p in plan:
            content = conn.execute("SELECT content_md FROM notes WHERE id=?", (p["id"],)).fetchone()["content_md"]
            notes_svc.upsert_note(conn, p["new"], content or "", note_id=p["id"],
                                  version_note="redate: filed under capture date")
        conn.commit()
    return {"count": len(plan), "dry_run": dry_run, "sample": plan[:20]}


_DEFAULT_TITLE_PROMPT = (
    "Give this note a concise 3-6 word title. Filename-friendly: words, spaces and hyphens "
    "only — no slashes, brackets, quotes or trailing punctuation. Reply with ONLY the title.")


def _sanitize_title(s: str) -> str:
    s = re.sub(r"[\\/\[\]\"'\n\r|]+", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip("-").strip()
    return s[:_TITLE_MAX].strip()


def _gen_title(content: str) -> str:
    prompt = prompts.get("actions.generate_note_title", _DEFAULT_TITLE_PROMPT) + "\n\nNOTE:\n" + (content or "")[:2000]
    try:
        out = llm.complete([{"role": "user", "content": prompt}], model=llm.model_for("cheap"), max_tokens=40)
    except Exception:  # noqa: BLE001
        return ""
    return _sanitize_title((out or "").splitlines()[0] if out else "")


def title_batch(conn, limit: int = 40, dry_run: bool = False) -> dict:
    """Add a generated leaf title to bare dated notes: notes/<date>/N -> notes/<date>/N - title.
    One cheap LLM call per note; bounded by limit; idempotent (titled notes are skipped)."""
    if not llm.has_credentials():
        return {"count": 0, "skipped": "no LLM credentials"}
    cands = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE kind='entry' AND deleted_at IS NULL "
        "AND title LIKE 'notes/%/%/%/%' ORDER BY created_at, id").fetchall()
    rows = [r for r in cands if _BARE_DATED.match(r["title"])][:max(1, int(limit))]
    done = []
    for r in rows:
        gen = _gen_title(r["content_md"])
        if not gen:
            continue
        new_title = f"{r['title']} - {gen}"
        if not dry_run:
            notes_svc.upsert_note(conn, new_title, r["content_md"] or "", note_id=r["id"],
                                  version_note="titled: generated leaf title")
        done.append({"id": r["id"], "old": r["title"], "new": new_title})
    if not dry_run:
        conn.commit()
    return {"count": len(done), "dry_run": dry_run, "sample": done[:20]}
