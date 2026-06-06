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

import re

_KINDS = {"decision", "conflict", "question", "todo", "directive", "note", "restructure",
          "correction"}
# Kinds that represent unfinished work — what maintenance should act on. `restructure`
# (split/merge/fold hints) is deliberately NOT here: a per-article maintain pass can't do
# it, so it's logged for a later structural pass / Reorganize and never re-worked (no nag).
# `correction` IS here: an owner source-of-truth correction must drive the next pass (its
# promoted note is fed in as a source), then get resolved like a directive.
OPEN_KINDS = {"conflict", "question", "todo", "directive", "correction"}


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


# Log kinds are immutable records (don't re-add ever); actionable kinds may legitimately
# RE-EMERGE after being resolved (a conflict that comes back), so they dedup against OPEN only.
_LOG_KINDS = {"note", "decision"}

_WS = re.compile(r"\s+")
_NOTE_CAP = 6   # keep at most this many OPEN, ai-authored 'note' rows per article (newest win)


def _norm(body: str) -> str:
    """Dedup key for a body: lowercased, whitespace-collapsed, trailing punctuation dropped —
    so a reworded-but-identical observation ('still a stub.' vs 'Still a  stub') dedups."""
    return _WS.sub(" ", (body or "").lower()).strip().rstrip(".!?,;:- ")


def record(conn, article_title: str, entries: list, author: str = "ai") -> int:
    """Add a batch of {kind, body} entries. Log entries (note/decision) dedup against ALL
    history so they don't pile up each run; actionable entries (conflict/question/todo/
    directive) dedup against OPEN only, so a genuinely re-emerged issue can resurface after
    an earlier resolution. Dedup is on a NORMALIZED body (case/whitespace/punctuation
    insensitive). Returns how many were added."""
    rows = conn.execute("SELECT kind, body, resolved_at FROM article_talk WHERE article_title=?",
                        (article_title,)).fetchall()
    all_keys = {(r["kind"], _norm(r["body"])) for r in rows}
    open_keys = {(r["kind"], _norm(r["body"])) for r in rows if r["resolved_at"] is None}
    n = 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "note").lower()
        kind = kind if kind in _KINDS else "note"
        body = str(e.get("body") or "").strip()
        if not body:
            continue
        key = (kind, _norm(body))
        seen = all_keys if kind in _LOG_KINDS else open_keys
        if key in seen:
            continue
        add(conn, article_title, kind, body, author)
        all_keys.add(key); open_keys.add(key)
        n += 1
    if n:
        _cap_notes(conn, article_title)
    return n


def _cap_notes(conn, article_title: str) -> None:
    """Bound clutter: keep only the newest _NOTE_CAP OPEN, ai-authored 'note' rows per article
    (they're informational logs, not actionable). NEVER touches actionable kinds, owner
    directives, user-authored rows, or resolved history."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM article_talk WHERE article_title=? AND kind='note' AND author='ai' "
        "AND resolved_at IS NULL ORDER BY created_at DESC, id DESC", (article_title,)).fetchall()]
    extra = ids[_NOTE_CAP:]
    if extra:
        conn.execute(f"DELETE FROM article_talk WHERE id IN ({','.join('?' * len(extra))})", extra)


# Phrases that mark a NON-ACTIONABLE "this is a stub / needs more source notes / revisit
# later" observation. These belong in the inert log, never as actionable items that nag the
# maintenance pass and post Review cards — a stub is the correct result, nothing to act on.
_STUB_LIKE = ["%stub%", "%more source%", "%additional source%", "%more notes become available%",
              "%richer sourc%", "%revisit when%", "External reference needed%",
              "%no information about%", "%remain undocumented%", "%background remain%"]


def demote_stub_notes(conn, article_title: str | None = None) -> int:
    """Reclassify ai-authored, unresolved 'todo'/'question' items that merely observe the
    article is a stub / needs more source notes / should be revisited later into inert
    'note' logs (then cap), so they stop driving maintenance + Review cards. NEVER touches
    conflicts, owner directives, user items, or resolved rows. Returns how many were demoted."""
    like = " OR ".join(["body LIKE ?"] * len(_STUB_LIKE))
    where = f"kind IN ('todo','question') AND author='ai' AND resolved_at IS NULL AND ({like})"
    args = list(_STUB_LIKE)
    if article_title:
        where += " AND article_title=?"
        args.append(article_title)
    n = conn.execute(f"UPDATE article_talk SET kind='note' WHERE {where}", args).rowcount
    if n:
        for r in conn.execute("SELECT DISTINCT article_title FROM article_talk "
                              "WHERE kind='note' AND author='ai' AND resolved_at IS NULL").fetchall():
            _cap_notes(conn, r["article_title"])
    return n


def list_for(conn, article_title: str) -> list[dict]:
    """All talk for an article — open items first, then most-recent. A promoted correction
    carries source_note_slug (the truth note it spawned), or NULL if that note was deleted."""
    rows = conn.execute(
        "SELECT t.id, t.kind, t.body, t.author, t.created_at, t.resolved_at, t.resolution, "
        "t.is_correction, t.source_note_id, n.slug AS source_note_slug "
        "FROM article_talk t LEFT JOIN notes n "
        "  ON n.id = t.source_note_id AND n.deleted_at IS NULL "
        "WHERE t.article_title=? ORDER BY (t.resolved_at IS NULL) DESC, t.created_at DESC",
        (article_title,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_with(conn, talk_id: int, how: str | None = None) -> None:
    """Resolve an item AND record how it was addressed (the maintenance pass uses this)."""
    conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now'), resolution=? WHERE id=? AND resolved_at IS NULL",
        ((how or "").strip()[:500] or None, talk_id))


def open_for(conn, article_title: str) -> list[dict]:
    """Unresolved entries — what the maintenance pass reads to target its work.
    `is_correction`/`source_note_id` let the pass treat an owner correction as
    authoritative and feed its promoted note in as a source."""
    rows = conn.execute(
        "SELECT id, kind, body, author, created_at, is_correction, source_note_id "
        "FROM article_talk WHERE article_title=? AND resolved_at IS NULL ORDER BY created_at",
        (article_title,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve(conn, talk_id: int) -> None:
    conn.execute("UPDATE article_talk SET resolved_at=datetime('now') WHERE id=? AND resolved_at IS NULL",
                 (talk_id,))
