"""Bulk note-title normalization — two owner-run passes that tidy loose notes.

  redate_batch()  — file every non-conforming entry note under the flat dated tree
                    notes/YYYY/MM/DD/N (N = next per-day sequence). Deterministic, no LLM.
  title_batch()   — give each bare dated note a generated leaf title, so
                    notes/2026/06/04/2  ->  notes/2026/06/04/2 - cardiology invoice. LLM.

This also collapses the PWA's own notes/daily/ capture tree into the unified dated tree:
the raw captures (notes/daily/YYYY/MM/DD/N) and the day's summary rollup (kind='daily',
notes/daily/YYYY/MM/DD) are both refiled under notes/YYYY/MM/DD/N — by their title day, not
created_at, since a summary can roll up after midnight onto the next day. The summary keeps
kind='daily'. New PWA captures keep landing under notes/daily/…; this owner-run pass sweeps
them into place periodically.

Both rename via notes.upsert_note (versioned; inbound [[links]] are rewritten), skip the
kb/ layer + protected pages, and are idempotent (an already-conforming note is left alone),
so they're safe to re-run.
"""
from __future__ import annotations

import re

from . import llm, prompts, wiki_guides
from . import notes as notes_svc

# Flat dated leaf: notes/YYYY/MM/DD/N, optionally "N - title".
_DATED = re.compile(r"^notes/\d{4}/\d{2}/\d{2}/\d+( - .+)?$")
_BARE_DATED = re.compile(r"^notes/\d{4}/\d{2}/\d{2}/\d+$")
# PWA daily capture/rollup: notes/daily/YYYY/MM/DD optionally /N (raw capture).
_DAILY = re.compile(r"^notes/daily/(\d{4})/(\d{2})/(\d{2})(?:/\d+)?$")
_TITLE_MAX = 60


def _day_of(conn, title: str, created_at: str | None) -> str:
    """Return the note's capture day as 'YYYY/MM/DD'.

    For a PWA daily note (notes/daily/YYYY/MM/DD[/N]) the day comes from the title path
    because created_at can fall on the next day (a summary rolled up just after midnight).
    For any other loose note the created_at day is used (today if missing).

    Args:
        conn: SQLite connection (used to read the current date when created_at is absent).
        title: Note title (may match the PWA daily pattern).
        created_at: ISO timestamp string from the DB, or None.

    Returns:
        Date string in 'YYYY/MM/DD' format.
    """
    m = _DAILY.match(title or "")
    if m:
        return "/".join(m.groups())
    s = (created_at or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s = conn.execute("SELECT strftime('%Y-%m-%d','now') AS d").fetchone()["d"]
    return s.replace("-", "/")


def redate_batch(conn, limit: int = 2000, dry_run: bool = False) -> dict:
    """Move loose entry notes — plus the PWA daily captures and day-summary rollups — into
    notes/YYYY/MM/DD/N (by capture day, in created order). Returns the plan + count; with
    dry_run it only previews.
    """
    cands = conn.execute(
        "SELECT id, title, created_at FROM notes WHERE kind IN ('entry','daily') AND deleted_at IS NULL "
        "AND title NOT LIKE 'kb/%' "
        "ORDER BY created_at, id").fetchall()
    rows = [r for r in cands if not _DATED.match(r["title"]) and not wiki_guides.is_protected(r["title"])]
    rows = rows[:max(1, int(limit))]

    next_n: dict[str, int] = {}
    plan = []
    for r in rows:
        day = _day_of(conn, r["title"], r["created_at"])
        if day not in next_n:
            next_n[day] = notes_svc.max_dated_n(conn, day) + 1
        plan.append({"id": r["id"], "old": r["title"], "new": f"notes/{day}/{next_n[day]:02d}"})
        next_n[day] += 1

    if not dry_run:
        for p in plan:
            content = conn.execute("SELECT content_md FROM notes WHERE id=?", (p["id"],)).fetchone()["content_md"]
            notes_svc.upsert_note(conn, p["new"], content or "", note_id=p["id"],
                                  version_note="redate: filed under capture date")
        conn.commit()
    preview = "\n".join(f"{p['old']}  →  {p['new']}" for p in plan[:25])
    return {"count": len(plan), "dry_run": dry_run, "sample": plan[:20], "preview": preview}


_DEFAULT_TITLE_PROMPT = (
    "Give this note a concise 3-6 word title. Filename-friendly: words, spaces and hyphens "
    "only — no slashes, brackets, quotes or trailing punctuation. Reply with ONLY the title.")


def _sanitize_title(s: str) -> str:
    """Sanitize an LLM-generated title to a safe filename-friendly string.

    Strips slashes, brackets, quotes, pipes, newlines, and trims to _TITLE_MAX chars.

    Args:
        s: Raw title string from the LLM.

    Returns:
        Cleaned title string, possibly empty if nothing safe remains.
    """
    s = re.sub(r"[\\/\[\]\"'\n\r|]+", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip("-").strip()
    return s[:_TITLE_MAX].strip()


def _gen_title(content: str) -> str:
    """Generate a short leaf title for a note using the LLM.

    Args:
        content: Note body text (truncated to 2000 chars for the prompt).

    Returns:
        Sanitized title string, or '' on LLM failure or empty result.
    """
    prompt = prompts.get("actions.generate_note_title", _DEFAULT_TITLE_PROMPT) + "\n\nNOTE:\n" + (content or "")[:2000]
    try:
        out = llm.complete([{"role": "user", "content": prompt}], model=llm.model_for("cheap"), max_tokens=40)
    except Exception:  # noqa: BLE001
        return ""
    return _sanitize_title((out or "").splitlines()[0] if out else "")


def title_batch(conn, limit: int = 40, dry_run: bool = False) -> dict:
    """Add a generated leaf title to bare dated notes: notes/<date>/N -> notes/<date>/N - title.
    One cheap LLM call per note; bounded by limit; idempotent (titled notes are skipped).
    """
    if not llm.has_credentials():
        return {"count": 0, "skipped": "no LLM credentials"}
    cands = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE kind IN ('entry','daily') AND deleted_at IS NULL "
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
    preview = "\n".join(f"{d['old']}  →  {d['new']}" for d in done[:25])
    return {"count": len(done), "dry_run": dry_run, "sample": done[:20], "preview": preview}


def title_one(conn, note_id: int) -> str | None:
    """Title ONE note if it's a bare dated leaf (notes/YYYY/MM/DD/N -> '… - generated title').
    The per-note version of title_batch (for the note-view reanalyze button). No-op (returns
    None) for an already-titled note, a non-dated/kb note, or with no LLM. Does NOT commit.
    """
    if not llm.has_credentials():
        return None
    r = conn.execute(
        "SELECT id, title, content_md, kind FROM notes WHERE id=? AND deleted_at IS NULL", (note_id,)
    ).fetchone()
    if not r or r["kind"] not in ("entry", "daily") or not _BARE_DATED.match(r["title"]):
        return None
    gen = _gen_title(r["content_md"])
    if not gen:
        return None
    new_title = f"{r['title']} - {gen}"
    notes_svc.upsert_note(conn, new_title, r["content_md"] or "", note_id=r["id"],
                          version_note="titled: generated leaf title")
    return new_title
