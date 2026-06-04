"""Per-article "talk" — the Wikipedia-Talk-style memory that makes KB maintenance
stateful instead of starting from scratch each pass.

Each article accrues structured entries keyed by its title (stable across rebuilds),
NOT stored in the article body:
  decision  — a choice made while writing (a merge, an exclusion, a structure call)
  conflict  — sources disagree; unresolved
  question  — an open question about the subject
  todo      — something to revisit
  directive — a standing instruction (usually authored by the owner)
  note      — anything else

The article writer records noteworthy entries; the maintenance loop (Component 3)
reads the OPEN ones to target its work and resolves/adds as it goes. The owner can add
or resolve entries from the article's panel.
"""
from __future__ import annotations

_KINDS = {"decision", "conflict", "question", "todo", "directive", "note"}
# Kinds that represent unfinished work — what maintenance should act on.
OPEN_KINDS = {"conflict", "question", "todo", "directive"}


def add(conn, article_title: str, kind: str, body: str, author: str = "ai") -> int | None:
    body = (body or "").strip()
    if not body:
        return None
    kind = kind if kind in _KINDS else "note"
    cur = conn.execute(
        "INSERT INTO article_talk (article_title, kind, body, author) VALUES (?,?,?,?)",
        (article_title, kind, body[:2000], author),
    )
    return cur.lastrowid


def record(conn, article_title: str, entries: list, author: str = "ai") -> int:
    """Add a batch of {kind, body} entries, skipping ones already present for this article
    (open OR resolved), so re-runs don't pile up the same conflict/log. Returns how many
    were added."""
    existing = {(r["kind"], r["body"]) for r in conn.execute(
        "SELECT kind, body FROM article_talk WHERE article_title=?",
        (article_title,)).fetchall()}
    n = 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "note").lower()
        kind = kind if kind in _KINDS else "note"
        body = str(e.get("body") or "").strip()
        if not body or (kind, body) in existing:
            continue
        add(conn, article_title, kind, body, author)
        existing.add((kind, body))
        n += 1
    return n


def list_for(conn, article_title: str) -> list[dict]:
    """All talk for an article — open items first, then most-recent."""
    rows = conn.execute(
        "SELECT id, kind, body, author, created_at, resolved_at, resolution FROM article_talk "
        "WHERE article_title=? ORDER BY (resolved_at IS NULL) DESC, created_at DESC",
        (article_title,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_with(conn, talk_id: int, how: str | None = None) -> None:
    """Resolve an item AND record how it was addressed (the maintenance pass uses this)."""
    conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now'), resolution=? WHERE id=? AND resolved_at IS NULL",
        ((how or "").strip()[:500] or None, talk_id))


def open_for(conn, article_title: str) -> list[dict]:
    """Unresolved entries — what the maintenance pass reads to target its work."""
    rows = conn.execute(
        "SELECT id, kind, body, author, created_at FROM article_talk "
        "WHERE article_title=? AND resolved_at IS NULL ORDER BY created_at",
        (article_title,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve(conn, talk_id: int) -> None:
    conn.execute("UPDATE article_talk SET resolved_at=datetime('now') WHERE id=? AND resolved_at IS NULL",
                 (talk_id,))
