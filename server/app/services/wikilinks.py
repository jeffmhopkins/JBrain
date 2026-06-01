"""Parse [[wiki-links]] and reconcile the links table.

Supports [[Title]] and [[Title|display text]]. Matching to existing notes is by
title (case-insensitive); unresolved links are stored with target_note_id NULL
so a later-created note automatically gains its backlinks.
"""
from __future__ import annotations

import re

WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")


def extract_links(content_md: str) -> list[str]:
    """Return the unique, order-preserving list of linked note titles."""
    seen: dict[str, None] = {}
    for match in WIKILINK_RE.finditer(content_md or ""):
        title = match.group(1).strip()
        # Real note titles are single-line and bounded; ignore junk so a giant or
        # multi-line [[…]] body can't bloat the links table or be re-scanned forever.
        if title and "\n" not in title and len(title) <= 200:
            seen.setdefault(title, None)
    return list(seen.keys())


def reconcile_links(conn, source_note_id: int, content_md: str) -> None:
    """Rebuild the outgoing links for a note from its current content."""
    conn.execute("DELETE FROM links WHERE source_note_id = ?", (source_note_id,))
    for title in extract_links(content_md):
        target = conn.execute(
            "SELECT id FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL",
            (title,),
        ).fetchone()
        conn.execute(
            "INSERT INTO links (source_note_id, target_note_id, target_title) "
            "VALUES (?, ?, ?)",
            (source_note_id, target["id"] if target else None, title),
        )


def resolve_dangling_links(conn, note_id: int, title: str) -> None:
    """When a note is created, attach any prior unresolved links to its title."""
    conn.execute(
        "UPDATE links SET target_note_id = ? "
        "WHERE target_note_id IS NULL AND lower(target_title) = lower(?)",
        (note_id, title),
    )
