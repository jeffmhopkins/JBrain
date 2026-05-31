"""Note write pipeline: persist content + version + FTS + embedding + links.

Centralised so the REST API and the architect's staging-apply step behave
identically.
"""
from __future__ import annotations

import re
import sqlite3

from . import embeddings, wikilinks


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "note"


def _unique_slug(conn, title: str, exclude_id: int | None = None) -> str:
    base = slugify(title)
    slug = base
    i = 2
    while True:
        row = conn.execute(
            "SELECT id FROM notes WHERE slug = ? AND id IS NOT ?",
            (slug, exclude_id),
        ).fetchone()
        if row is None:
            return slug
        slug = f"{base}-{i}"
        i += 1


def _sync_fts(conn, note_id: int, title: str, content_md: str) -> None:
    conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
    conn.execute(
        "INSERT INTO notes_fts (note_id, title, content) VALUES (?, ?, ?)",
        (note_id, title, content_md),
    )


def get_by_title(conn, title: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL",
        (title,),
    ).fetchone()


def upsert_note(conn, title: str, content_md: str) -> int:
    """Create a note, or update it if the title already exists. Returns note id."""
    title = title.strip()
    existing = get_by_title(conn, title)

    if existing:
        note_id = existing["id"]
        # Snapshot previous version before overwriting.
        conn.execute(
            "INSERT INTO note_versions (note_id, title, content_md) VALUES (?, ?, ?)",
            (note_id, existing["title"], existing["content_md"]),
        )
        conn.execute(
            "UPDATE notes SET content_md = ?, updated_at = datetime('now') WHERE id = ?",
            (content_md, note_id),
        )
    else:
        slug = _unique_slug(conn, title)
        cur = conn.execute(
            "INSERT INTO notes (title, slug, content_md) VALUES (?, ?, ?)",
            (title, slug, content_md),
        )
        note_id = cur.lastrowid
        wikilinks.resolve_dangling_links(conn, note_id, title)

    _sync_fts(conn, note_id, title, content_md)
    wikilinks.reconcile_links(conn, note_id, content_md)
    embeddings.upsert_note_embedding(conn, note_id, title, content_md)
    return note_id


def soft_delete(conn, note_id: int) -> None:
    conn.execute(
        "UPDATE notes SET deleted_at = datetime('now') WHERE id = ?", (note_id,)
    )
    conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM links WHERE source_note_id = ?", (note_id,))
    conn.execute("UPDATE links SET target_note_id = NULL WHERE target_note_id = ?", (note_id,))
    embeddings.delete_note_embedding(conn, note_id)


def backlinks(conn, note_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT n.id, n.title, n.slug
        FROM links l JOIN notes n ON n.id = l.source_note_id
        WHERE l.target_note_id = ? AND n.deleted_at IS NULL
        ORDER BY n.title
        """,
        (note_id,),
    ).fetchall()
    return [dict(r) for r in rows]
