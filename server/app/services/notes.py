"""Note write pipeline: persist content + version + FTS + embedding + links.

Centralised so the REST API and the architect's staging-apply step behave
identically.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading

from . import embeddings, wikilinks

# Keep at most this many version rows per note (newest wins). Markdown versions
# are tiny, so this is generous; it just bounds runaway growth on churny notes.
MAX_VERSIONS_PER_NOTE = 50

# Per-thread state: the re-entrancy guard (so an entry-event workflow that writes
# can't recursively re-fire) and the deferred entry_created queue. Thread-local
# (not a module global) because sync request handlers run on a shared threadpool,
# each with its own DB connection — a global flag would leak across requests.
_state = threading.local()


def set_tags(conn, note_id: int, tags: list[str]) -> list[str]:
    """Replace a note's tags (lower-cased, de-duped). Populates tags/note_tags."""
    norm: list[str] = []
    for t in tags:
        t = (t or "").strip().lower()
        if t and t not in norm:
            norm.append(t)
    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    for name in norm:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)", (note_id, tid)
        )
    return norm


def _fire_entry_created(conn, note_id: int, title: str, *, commit: bool = False) -> None:
    if getattr(_state, "suppress", False):
        return
    _state.suppress = True
    try:
        from . import workflows as wf_svc  # lazy import avoids a cycle
        wf_svc.fire_event(conn, "entry_created", {"note_title": title, "note_id": note_id}, commit=commit)
    except Exception:  # noqa: BLE001 — a workflow must never break note creation
        pass
    finally:
        _state.suppress = False


def flush_entry_events(conn) -> None:
    """Fire entry_created for notes created with fire_events=False, AFTER the
    caller has committed. Run post-commit so a slow (LLM-backed) workflow doesn't
    hold the note's write transaction open and block other writers."""
    pending = getattr(_state, "pending", None) or []
    _state.pending = []
    for note_id, title in pending:
        _fire_entry_created(conn, note_id, title, commit=True)


def _rename_inbound_links(conn, old_title: str, new_title: str, renamed_id: int) -> None:
    """When a note is renamed, rewrite [[old title]] -> [[new title]] (keeping any
    |display alias) in every OTHER note that links to it, so inline links keep
    resolving. Backlinks (tracked by note id) already survive; this fixes the
    link *text/href* in referencing notes."""
    pat = re.compile(r"\[\[\s*" + re.escape(old_title) + r"\s*(\|[^\]]*)?\]\]", re.IGNORECASE)
    sources = conn.execute(
        "SELECT DISTINCT source_note_id FROM links WHERE lower(target_title) = lower(?)",
        (old_title,),
    ).fetchall()
    for s in sources:
        sid = s["source_note_id"]
        if sid == renamed_id:
            continue
        row = conn.execute(
            "SELECT title, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (sid,)
        ).fetchone()
        if not row:
            continue
        new_content = pat.sub(lambda m: f"[[{new_title}{m.group(1) or ''}]]", row["content_md"])
        if new_content != row["content_md"]:
            upsert_note(conn, row["title"], new_content, note_id=sid, source="rename",
                        version_note=f"link rename: {old_title} → {new_title}", fire_events=False)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    # Non-ASCII titles (emoji/CJK/RTL) collapse to empty — derive a stable,
    # distinct slug from the title so they don't all become "note".
    return s or ("note-" + hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:8])


# The wiki is organised under two top-level folders: raw captures live under
# "notes/", the synthesized encyclopedia under "kb/". Keeping them in separate
# title roots means an entry and its KB article never collide (and the "/"-path
# tree groups them automatically).
def root_title(title: str, root: str) -> str:
    """Place `title` under the given root ('notes' or 'kb') without doubling or
    cross-prefixing. 'Jeff'->'notes/Jeff'; 'kb/Jeff' under 'kb' stays 'kb/Jeff';
    'notes/Jeff' under 'kb' becomes 'kb/Jeff'. Deeper sub-paths are preserved."""
    t = (title or "").strip().strip("/")
    low = t.lower()
    for r in ("notes/", "kb/", "lists/", "logs/"):
        if low.startswith(r):
            t = t[len(r):]
            break
    return f"{root}/{t}" if t else root


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


def _unique_title(conn, base: str, exclude_id: int | None = None) -> str:
    """A title not already used by a live note (case-insensitive), suffixing
    ' (2)', ' (3)', … on collision. Used to avoid clobbering an existing note."""
    base = base.strip()[:120] or "Untitled"
    title, i = base, 2
    while conn.execute(
        "SELECT 1 FROM notes WHERE lower(title)=lower(?) AND id IS NOT ? AND deleted_at IS NULL",
        (title, exclude_id),
    ).fetchone():
        title = f"{base} ({i})"
        i += 1
    return title


def next_daily_title(conn, day) -> str:
    """The next dated-capture title: notes/daily/YYYY/MM/DD/<n>.

    `n` is MAX(existing trailing integer for that day) + 1. It counts ALL rows,
    including soft-deleted ones, because `notes.title` is UNIQUE across every row
    — reissuing a deleted note's number would hit that constraint. So numbering is
    gap-tolerant (a deleted /3 is never reused). Single-writer SQLite means no
    retry loop is needed; `day` is a date in the caller's local timezone."""
    prefix = f"notes/daily/{day:%Y/%m/%d}"
    rows = conn.execute(
        "SELECT title FROM notes WHERE title LIKE ?",
        (prefix + "/%",),
    ).fetchall()
    n = 0
    for r in rows:
        tail = r["title"][len(prefix) + 1:]
        if tail.isdigit():
            n = max(n, int(tail))
    return f"{prefix}/{n + 1}"


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


def _prune_versions(conn, note_id: int) -> None:
    conn.execute(
        "DELETE FROM note_versions WHERE note_id = ? AND id NOT IN ("
        "  SELECT id FROM note_versions WHERE note_id = ? "
        "  ORDER BY created_at DESC, id DESC LIMIT ?)",
        (note_id, note_id, MAX_VERSIONS_PER_NOTE),
    )


def conversation_location(conn, conversation_id: int | None):
    """Latest geolocation the user attached to a message in this conversation."""
    if conversation_id is None:
        return None
    return conn.execute(
        "SELECT lat, lon, location_label FROM messages "
        "WHERE conversation_id = ? AND role = 'user' AND lat IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()


def upsert_note(
    conn,
    title: str,
    content_md: str,
    *,
    note_id: int | None = None,
    source: str = "user",
    conversation_id: int | None = None,
    version_note: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    location_label: str | None = None,
    kind: str | None = None,
    create_only: bool = False,
    fire_events: bool = True,
) -> int:
    """Create or update a note and append a version row for the new state.

    Every write (create, update, restore) records a `note_versions` row tagged
    with `source` (who authored this content), so the newest version always
    equals the live note. Pass `note_id` to target a specific note (used by
    restore, which may also change the title).

    A title-keyed write must never silently clobber an unrelated note. When the
    title matches an existing note we still UPDATE it, EXCEPT:
      - `create_only` — the caller wants a brand-new note (e.g. a staged CREATE);
      - cross-kind — an explicit `kind` that differs from the matched note (e.g.
        a wiki-synthesis `kind='kb'` write must not overwrite a user's entry).
    In those cases we fall back to a uniquely-titled new note instead.

    `fire_events=False` defers the entry_created hook: the caller fires it via
    flush_entry_events() AFTER committing (so a slow workflow doesn't hold the
    note's write transaction open).
    """
    title = title.strip()
    id_targeted = note_id is not None
    if note_id is not None:
        existing = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    else:
        existing = get_by_title(conn, title)
        if existing and (create_only or (kind is not None and existing["kind"] != kind)):
            title = _unique_title(conn, title)
            existing = None

    has_location = lat is not None or lon is not None or location_label is not None

    renamed_from = None
    if existing:
        note_id = existing["id"]
        slug = existing["slug"]
        if existing["title"].lower() != title.lower():
            renamed_from = existing["title"]   # rewrite inbound [[links]] after the write
            slug = _unique_slug(conn, title, exclude_id=note_id)
        conn.execute(
            "UPDATE notes SET title = ?, slug = ?, content_md = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
            (title, slug, content_md, note_id),
        )
        if has_location:  # only overwrite location when new coords are supplied
            conn.execute(
                "UPDATE notes SET lat = ?, lon = ?, location_label = ? WHERE id = ?",
                (lat, lon, location_label, note_id),
            )
        # Only change kind on a title-keyed write (where the cross-kind guard
        # above already ensured kinds match). Never flip kind on an id-targeted
        # write — that would let a caller convert e.g. a user entry into a kb note.
        if kind is not None and not id_targeted:
            conn.execute("UPDATE notes SET kind = ? WHERE id = ?", (kind, note_id))
        created = False
    else:
        created = True
        slug = _unique_slug(conn, title)
        cur = conn.execute(
            "INSERT INTO notes (title, slug, content_md, kind, lat, lon, location_label, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))",
            (title, slug, content_md, kind or "entry", lat, lon, location_label),
        )
        note_id = cur.lastrowid
        wikilinks.resolve_dangling_links(conn, note_id, title)

    conn.execute(
        "INSERT INTO note_versions (note_id, title, content_md, source, conversation_id, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (note_id, title, content_md, source, conversation_id, version_note),
    )
    _prune_versions(conn, note_id)

    _sync_fts(conn, note_id, title, content_md)
    wikilinks.reconcile_links(conn, note_id, content_md)
    embeddings.upsert_note_embedding(conn, note_id, title, content_md)

    # Renamed? Rewrite [[old title]] references in other notes so their inline
    # links don't dangle at the old slug.
    if renamed_from:
        _rename_inbound_links(conn, renamed_from, title, note_id)

    # "On every new entry" hook — fires for human/architect-created entry notes
    # only (not kb/workflow/restore writes), so workflows can enrich them.
    effective_kind = kind if kind is not None else (existing["kind"] if existing else "entry")
    if created and effective_kind == "entry" and source in ("user", "architect"):
        if fire_events:
            _fire_entry_created(conn, note_id, title)
        else:  # defer to the caller's flush_entry_events() after it commits
            pending = getattr(_state, "pending", None) or []
            pending.append((note_id, title))
            _state.pending = pending
    return note_id


def soft_delete(conn, note_id: int) -> None:
    conn.execute(
        "UPDATE notes SET deleted_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?", (note_id,)
    )
    conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM links WHERE source_note_id = ?", (note_id,))
    conn.execute("UPDATE links SET target_note_id = NULL WHERE target_note_id = ?", (note_id,))
    embeddings.delete_note_embedding(conn, note_id)

    # Drop the note's attachment search artifacts so they don't surface while the
    # note is deleted, but KEEP the attachment rows so a restore can re-index.
    for att in conn.execute(
        "SELECT id FROM attachments WHERE note_id = ?", (note_id,)
    ).fetchall():
        embeddings.delete_attachment_embeddings(conn, att["id"])
    conn.execute("DELETE FROM attachments_fts WHERE note_id = ?", (note_id,))


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
