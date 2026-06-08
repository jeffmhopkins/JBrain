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
    """Insert a single talk entry for an article; returns the new id, or None for an empty body.

    Args:
        conn: Database connection.
        article_title: Title of the KB article this entry belongs to.
        kind: Entry kind; unknown values fall back to 'note'.
        body: Entry text (truncated to 2000 chars).
        author: 'ai' or 'user'.

    Returns:
        The new article_talk.id, or None if body is empty.
    """
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
    """Build a dedup key from a talk body.

    Lowercases, collapses whitespace, and drops trailing punctuation so that
    reworded-but-identical observations ('still a stub.' vs 'Still a  stub') dedup.

    Args:
        body: Raw entry body text.

    Returns:
        Normalized key string.
    """
    return _WS.sub(" ", (body or "").lower()).strip().rstrip(".!?,;:- ")


def record(conn, article_title: str, entries: list, author: str = "ai") -> int:
    """Add a batch of {kind, body} entries with smart deduplication.

    Log entries (note/decision) dedup against ALL history so they don't pile up each
    run; actionable entries (conflict/question/todo/directive) dedup against OPEN only,
    so a genuinely re-emerged issue can resurface after an earlier resolution. Dedup is
    on a normalized body (case/whitespace/punctuation insensitive).

    Args:
        conn: Database connection.
        article_title: Title of the KB article.
        entries: List of {kind, body} dicts; invalid dicts are skipped.
        author: 'ai' or 'user'.

    Returns:
        Number of entries actually inserted.
    """
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
    """Prune stale OPEN ai-authored 'note' rows to keep at most _NOTE_CAP per article.

    These are informational logs, not actionable. Never touches actionable kinds, owner
    directives, user-authored rows, resolved history, or any note the owner has replied to
    (a reply would cascade-delete with the row — owner words are never dropped).

    Args:
        conn: Database connection.
        article_title: Title of the KB article to prune.
    """
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM article_talk WHERE article_title=? AND kind='note' AND author='ai' "
        "AND resolved_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM article_talk_reply r WHERE r.talk_id = article_talk.id) "
        "ORDER BY created_at DESC, id DESC", (article_title,)).fetchall()]
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
    """Reclassify stub-observation todos/questions into inert 'note' logs and cap them.

    Matches ai-authored, unresolved 'todo'/'question' items that merely observe the article
    is a stub / needs more source notes / should be revisited later, so they stop driving
    maintenance passes and Review cards. Never touches conflicts, owner directives, user
    items, or resolved rows.

    Args:
        conn: Database connection.
        article_title: Limit demotions to this article, or None to sweep all articles.

    Returns:
        Number of entries demoted.
    """
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


def reply(conn, talk_id: int, body: str, author: str = "user") -> int | None:
    """Add a reply to a talk item, bypassing record()'s deduplication entirely.

    Replies are deliberate utterances in the owner↔AI maintenance conversation.

    Args:
        conn: Database connection.
        talk_id: The article_talk.id to reply to.
        body: Reply text (truncated to 2000 chars).
        author: 'ai' or 'user'.

    Returns:
        The new article_talk_reply.id, or None for an empty body or unknown parent.
    """
    body = (body or "").strip()
    if not body:
        return None
    if conn.execute("SELECT 1 FROM article_talk WHERE id=?", (talk_id,)).fetchone() is None:
        return None
    author = "ai" if author == "ai" else "user"
    cur = conn.execute(
        "INSERT INTO article_talk_reply (talk_id, author, body) VALUES (?,?,?)",
        (talk_id, author, body[:2000]))
    return cur.lastrowid


def replies_for(conn, talk_ids: list[int]) -> dict[int, list[dict]]:
    """Fetch replies for a set of talk-item ids in a single query, grouped by talk_id.

    Used by both list_for (UI) and maintain_one (fold into the writer prompt).

    Args:
        conn: Database connection.
        talk_ids: List of article_talk.id values to fetch replies for.

    Returns:
        Dict mapping talk_id → list of reply dicts (oldest-first).
    """
    ids = [int(i) for i in (talk_ids or [])]
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, talk_id, author, body, created_at FROM article_talk_reply "
        f"WHERE talk_id IN ({','.join('?' * len(ids))}) ORDER BY created_at, id", ids).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["talk_id"], []).append(dict(r))
    return out


def list_for(conn, article_title: str) -> list[dict]:
    """Return all talk entries for an article with replies embedded.

    Open items sort first, then most-recent. A promoted correction carries
    source_note_slug (the truth note it spawned), or NULL if that note was deleted.
    Each item has a 'replies' key (owner↔AI conversation, oldest-first).

    Args:
        conn: Database connection.
        article_title: Title of the KB article.

    Returns:
        List of talk entry dicts, each with an embedded 'replies' list.
    """
    rows = conn.execute(
        "SELECT t.id, t.kind, t.body, t.author, t.created_at, t.resolved_at, t.resolution, "
        "t.is_correction, t.source_note_id, n.slug AS source_note_slug "
        "FROM article_talk t LEFT JOIN notes n "
        "  ON n.id = t.source_note_id AND n.deleted_at IS NULL "
        "WHERE t.article_title=? ORDER BY (t.resolved_at IS NULL) DESC, t.created_at DESC",
        (article_title,),
    ).fetchall()
    items = [dict(r) for r in rows]
    reps = replies_for(conn, [it["id"] for it in items])
    for it in items:
        it["replies"] = reps.get(it["id"], [])
    return items


def resolve_with(conn, talk_id: int, how: str | None = None) -> None:
    """Resolve an open talk item and record how it was addressed.

    Used by the maintenance pass to close items it has handled.

    Args:
        conn: Database connection.
        talk_id: The article_talk.id to resolve.
        how: Optional resolution description (truncated to 500 chars).
    """
    conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now'), resolution=? WHERE id=? AND resolved_at IS NULL",
        ((how or "").strip()[:500] or None, talk_id))


def dismiss(conn, talk_id: int, reason: str | None = None) -> bool:
    """Owner terminal close — same DB state as a maintenance resolve but labelled 'dismissed by owner'.

    The label distinguishes owner judgement from work the maintenance loop actually did.
    Guarded on resolved_at IS NULL (a harmless no-op against a concurrent maintenance
    resolve). The caller must refuse dismissal of 'correction' items — their truth note
    still heals the article on the next pass, so hiding the row would mislead.

    Args:
        conn: Database connection.
        talk_id: The article_talk.id to dismiss.
        reason: Optional reason appended to the 'dismissed by owner' label.

    Returns:
        True if a row was closed, False if it was already resolved.
    """
    reason = (reason or "").strip()
    label = "dismissed by owner" + (f": {reason}" if reason else "")
    cur = conn.execute(
        "UPDATE article_talk SET resolved_at=datetime('now'), resolution=? "
        "WHERE id=? AND resolved_at IS NULL", (label[:500], talk_id))
    return cur.rowcount > 0


def open_for(conn, article_title: str) -> list[dict]:
    """Return unresolved talk entries for an article, ordered by creation time.

    This is what the maintenance pass reads to target its work. The
    is_correction/source_note_id fields let the pass treat an owner correction as
    authoritative and feed its promoted note in as a source.

    Args:
        conn: Database connection.
        article_title: Title of the KB article.

    Returns:
        List of open talk entry dicts.
    """
    rows = conn.execute(
        "SELECT id, kind, body, author, created_at, is_correction, source_note_id "
        "FROM article_talk WHERE article_title=? AND resolved_at IS NULL ORDER BY created_at",
        (article_title,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve(conn, talk_id: int) -> None:
    """Resolve an open talk item without recording how it was addressed.

    Args:
        conn: Database connection.
        talk_id: The article_talk.id to resolve.
    """
    conn.execute("UPDATE article_talk SET resolved_at=datetime('now') WHERE id=? AND resolved_at IS NULL",
                 (talk_id,))
