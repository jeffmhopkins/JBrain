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
    """Replace a note's tags (lower-cased, de-duped), populating tags and note_tags.

    Args:
        conn: SQLite connection.
        note_id: Note to update.
        tags: New tag list; duplicates and blank entries are silently dropped.

    Returns:
        Normalised, de-duplicated tag list that was actually stored.
    """
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
    """Fire the entry_created workflow event, guarded against re-entrancy.

    Args:
        conn: SQLite connection.
        note_id: Newly created note's ID.
        title: Note title passed to the event context.
        commit: Whether to commit after the event fires (False when inside a txn).
    """
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
    """Rewrite [[old title]] → [[new title]] in every note that links to the renamed note.

    Backlinks (tracked by note id) already survive renaming; this fixes the link text/href
    in referencing notes. A |display alias is preserved EXCEPT when it merely echoed the
    old name (any path form of old_title): that alias would otherwise become a stale label
    (e.g. renaming People/Jeff → People/Summer would leave [[…Summer…|Jeff]]). Such an echo
    is dropped — the bare link renders the new article's clean short name (wikiLabel).

    Args:
        conn: SQLite connection.
        old_title: Previous title of the renamed note.
        new_title: New title of the renamed note.
        renamed_id: Note ID of the renamed note (its own content is skipped).
    """
    from .wikilinks import _path_forms
    old_forms = _path_forms(old_title)   # {'jeff', 'people/jeff', 'kb/people/jeff'}

    def _rewrite(m: "re.Match") -> str:
        """Rewrite one [[old_title|alias]] match, dropping a stale echo alias."""
        alias = m.group(1) or ""           # includes leading "|", or ""
        label = alias[1:].strip() if alias else ""
        if label and label.lower() in old_forms:
            alias = ""                      # stale echo of the old name → drop it
        return f"[[{new_title}{alias}]]"

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
        new_content = pat.sub(_rewrite, row["content_md"])
        if new_content != row["content_md"]:
            upsert_note(conn, row["title"], new_content, note_id=sid, source="rename",
                        version_note=f"link rename: {old_title} → {new_title}", fire_events=False)


def slugify(title: str) -> str:
    """Convert a note title to a URL-safe slug.

    Non-ASCII titles (emoji, CJK, RTL) that collapse to empty get a stable, distinct
    slug derived from the title hash so they don't all become "note".

    Args:
        title: Raw note title.

    Returns:
        Lowercase, hyphen-separated slug string.
    """
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    # Non-ASCII titles (emoji/CJK/RTL) collapse to empty — derive a stable,
    # distinct slug from the title so they don't all become "note".
    return s or ("note-" + hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:8])


# The wiki is organised under two top-level folders: raw captures live under
# "notes/", the synthesized encyclopedia under "kb/". Keeping them in separate
# title roots means an entry and its KB article never collide (and the "/"-path
# tree groups them automatically).
def protected_title_sql(col: str = "title") -> str:
    """Return a SQL predicate matching 'protected' system notes.

    Matches any note whose path has a segment starting with '_' (e.g. kb/_index,
    _scratch, kb/People/_Guide). The SQL twin of wiki_guides.is_protected, used to
    hide these from the notes list and graph (they stay directly reachable by slug).
    The '_' is backslash-escaped so LIKE treats it as a literal underscore.

    Args:
        col: Column name to test; must be a trusted, hard-coded identifier.

    Returns:
        SQL fragment string (no leading AND).
    """
    return rf"({col} LIKE '\_%' ESCAPE '\' OR {col} LIKE '%/\_%' ESCAPE '\')"


def root_title(title: str, root: str) -> str:
    """Place a title under the given root folder without doubling or cross-prefixing.

    'Jeff' → 'notes/Jeff'; 'kb/Jeff' under 'kb' stays 'kb/Jeff';
    'notes/Jeff' under 'kb' becomes 'kb/Jeff'. Deeper sub-paths are preserved.

    Args:
        title: Note title, possibly already prefixed.
        root: Target root folder name (e.g. 'notes' or 'kb').

    Returns:
        Title string with exactly one root prefix applied.
    """
    t = (title or "").strip().strip("/")
    low = t.lower()
    for r in ("notes/", "kb/", "lists/", "logs/", "loc/"):
        if low.startswith(r):
            t = t[len(r):]
            break
    return f"{root}/{t}" if t else root


def _unique_slug(conn, title: str, exclude_id: int | None = None) -> str:
    """Return a slug for title that is not already used by any other note.

    Appends '-2', '-3', … until an unused slug is found.

    Args:
        conn: SQLite connection.
        title: Note title to slugify.
        exclude_id: Note ID that may already own the base slug (e.g. on rename).

    Returns:
        Unique slug string.
    """
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
    """Return a title not already in use by any live note (case-insensitive).

    Appends ' (2)', ' (3)', … on collision to avoid clobbering an existing note.

    Args:
        conn: SQLite connection.
        base: Desired title string.
        exclude_id: Note ID to exclude from the collision check (e.g. the note itself).

    Returns:
        Unique title string.
    """
    base = base.strip()[:120] or "Untitled"
    title, i = base, 2
    while conn.execute(
        "SELECT 1 FROM notes WHERE lower(title)=lower(?) AND id IS NOT ? AND deleted_at IS NULL",
        (title, exclude_id),
    ).fetchone():
        title = f"{base} ({i})"
        i += 1
    return title


def max_dated_n(conn, day_path: str) -> int:
    """Highest leading number already used under notes/<day_path>/ (day_path is the
    slashed date, e.g. '2026/06/04').

    Reads the leading integer of each leaf, so it counts both bare ('NN') and titled
    ('NN - cardiology invoice') notes, and counts ALL rows including soft-deleted ones
    — `notes.title` is UNIQUE across every row, so reissuing a deleted note's number
    would hit that constraint. Numbering is therefore gap-tolerant (a deleted /3 is
    never reused). 0 when the day is empty."""
    prefix = f"notes/{day_path}/"
    n = 0
    for r in conn.execute("SELECT title FROM notes WHERE title LIKE ?", (prefix + "%",)).fetchall():
        head = r["title"][len(prefix):].split(" - ", 1)[0]
        if head.isdigit():
            n = max(n, int(head))
    return n


def next_dated_title(conn, day) -> str:
    """The next entry-capture title in the flat dated tree: notes/YYYY/MM/DD/NN.

    This is the standard location every 'Make entry' capture lands in. NN is the day's
    highest existing leading number + 1, zero-padded to ≥2 digits so a day's notes sort
    right (.../01 … /10). Single-writer SQLite means no retry loop is needed; `day` is a
    date in the caller's local timezone."""
    day_path = f"{day:%Y/%m/%d}"
    return f"notes/{day_path}/{max_dated_n(conn, day_path) + 1:02d}"


_MED_ROOT = "medical"
# Capture roots the Entry sub-selector can file into (notes/<root>/<dest>/NN). An enum —
# never free text spliced into a path; the entry router clamps anything else to medical.
CAPTURE_ROOTS = ("medical", "financial")


def sanitize_dest(dest: str, root: str = _MED_ROOT) -> str:
    """Sanitize a user-supplied destination name into a safe folder path.

    Collapses whitespace, drops empty/'.'/'..' segments (no path traversal), caps each
    segment and the whole path, and strips any leading capture root (so a financial dest
    typed as 'medical/X' can't land cross-tree). Nested names ('Cardiology/Visits') are
    preserved.

    Args:
        dest: Raw user-supplied destination path string.
        root: Root name kept for call-site clarity; all capture roots are stripped
            regardless of which root is passed.

    Returns:
        Sanitized relative folder path, or '' if nothing safe remains.
    """
    _ = root  # kept for call-site clarity; all capture roots are stripped regardless
    raw = (dest or "").strip().strip("/")
    segs: list[str] = []
    for s in raw.split("/"):
        s = " ".join(s.split())
        if not s or s in (".", ".."):
            continue
        segs.append(s[:60])
    while segs and segs[0].lower() in ("notes", *CAPTURE_ROOTS):
        segs.pop(0)
    return "/".join(segs)[:160]


def next_capture_title(conn, root: str, dest: str) -> str:
    """Return the next capture slot under an Entry sub-selector root.

    Format: notes/<root>/<dest>/NN (dest sanitized; falls back to General when empty).
    root is clamped to a known capture root. Reuses the dated-tree per-folder numbering,
    so a destination's captures sort .../01, .../02, … and never collide with a deleted one.

    Args:
        conn: SQLite connection.
        root: Capture root name; clamped to CAPTURE_ROOTS.
        dest: User-supplied sub-folder path, sanitized before use.

    Returns:
        Full note title string for the next capture slot.
    """
    r = root if root in CAPTURE_ROOTS else _MED_ROOT
    d = sanitize_dest(dest, r) or "General"
    folder = f"{r}/{d}"
    return f"notes/{folder}/{max_dated_n(conn, folder) + 1:02d}"


def next_medical_title(conn, dest: str) -> str:
    """Return the next medical capture title (notes/medical/<dest>/NN).

    Back-compat wrapper around next_capture_title for the medical root.

    Args:
        conn: SQLite connection.
        dest: Sub-folder destination within notes/medical/.

    Returns:
        Full note title string for the next medical capture slot.
    """
    return next_capture_title(conn, _MED_ROOT, dest)


def _sync_fts(conn, note_id: int, title: str, content_md: str) -> None:
    """Rebuild the FTS index row for a note, skipping redirect-only notes.

    Args:
        conn: SQLite connection.
        note_id: Note to index.
        title: Current note title.
        content_md: Current note body.
    """
    conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
    # A redirect row must NOT be a keyword hit: its old title still occupies the UNIQUE
    # title slot, but a search for that title should surface the canonical article, not the
    # one-line redirect marker. We drop the stale FTS row (above) and skip re-indexing.
    row = conn.execute("SELECT redirect_to FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row and row["redirect_to"]:
        return
    conn.execute(
        "INSERT INTO notes_fts (note_id, title, content) VALUES (?, ?, ?)",
        (note_id, title, content_md),
    )


def get_by_title(conn, title: str) -> sqlite3.Row | None:
    """Return the live note row for a title (case-insensitive), or None.

    Args:
        conn: SQLite connection.
        title: Note title to look up.

    Returns:
        sqlite3.Row for the note, or None if not found or soft-deleted.
    """
    return conn.execute(
        "SELECT * FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL",
        (title,),
    ).fetchone()


def _prune_versions(conn, note_id: int) -> None:
    """Trim note_versions to the MAX_VERSIONS_PER_NOTE most recent rows.

    Args:
        conn: SQLite connection.
        note_id: Note whose version history should be pruned.
    """
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
    low_title = title.lower()
    is_loc = title == "loc" or low_title.startswith("loc/")
    is_kb = title == "kb" or low_title.startswith("kb/")
    explicit_kind = kind
    # The root prefix is authoritative for kind: loc/ → a PLACE note backing a
    # geofence; kb/ → a synthesized KB article. Both are kept OUT of KB synthesis
    # as *sources* (it only pulls entry/daily) while staying searchable. This lets
    # the user (or assistant) promote a note to KB just by titling it kb/<name>.
    if kind is None and is_loc:
        kind = "place"
    elif kind is None and is_kb:
        kind = "kb"
    id_targeted = note_id is not None
    old_slug = None
    if note_id is not None:
        existing = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    else:
        existing = get_by_title(conn, title)
        if existing and (create_only or (kind is not None and existing["kind"] != kind)):
            title = _unique_title(conn, title)
            existing = None
        elif existing is None and not create_only:
            # Revive a soft-deleted note of the same title rather than INSERT a new row
            # (whose generated slug would collide with the dead one's). Makes rebuilds —
            # which soft-delete then recreate the same article titles — idempotent, and
            # keeps each article's version history continuous across rebuilds.
            dead = conn.execute(
                "SELECT * FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (title,),
            ).fetchone()
            if dead and (kind is None or dead["kind"] == kind):
                existing = dead

    has_location = lat is not None or lon is not None or location_label is not None

    renamed_from = None
    if existing:
        note_id = existing["id"]
        slug = existing["slug"]
        old_slug = existing["slug"]   # pre-rename slug (to re-pair a linked place)
        if existing["title"].lower() != title.lower():
            renamed_from = existing["title"]   # rewrite inbound [[links]] after the write
            slug = _unique_slug(conn, title, exclude_id=note_id)
        conn.execute(
            # A real content write supersedes any redirect on this title (clear redirect_to),
            # so a row can never be a redirect AND a live article at once (FTS/embeddings/browse
            # would otherwise disagree). create_redirect uses a direct UPDATE, not this path.
            "UPDATE notes SET title = ?, slug = ?, content_md = ?, deleted_at = NULL, "
            "redirect_to = NULL, updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
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
        elif id_targeted and explicit_kind is None:
            # The root prefix is authoritative for kind even on a rename: a note moved
            # INTO loc/ or kb/ takes that kind (so it stops leaking into synthesis), and
            # one moved back OUT of either reverts to a plain entry. Lists are untouched.
            if is_loc and existing["kind"] != "place":
                conn.execute("UPDATE notes SET kind = 'place' WHERE id = ?", (note_id,))
            elif is_kb and existing["kind"] != "kb":
                conn.execute("UPDATE notes SET kind = 'kb' WHERE id = ?", (note_id,))
            elif not is_loc and not is_kb and existing["kind"] in ("place", "kb"):
                conn.execute("UPDATE notes SET kind = 'entry' WHERE id = ?", (note_id,))
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
        # Keep a place geofence paired with its loc/ note when the NOTE is renamed in
        # the wiki (the place→note direction is handled by the places router). Renamed
        # out of loc/ → it's no longer a place note, so unlink it from any place.
        if is_loc and title.startswith("loc/"):
            conn.execute("UPDATE places SET note_slug = ?, name = ? WHERE note_slug = ?",
                         (slug, title[4:][:80], old_slug))
        elif not is_loc:
            conn.execute("UPDATE places SET note_slug = NULL WHERE note_slug = ?", (old_slug,))

    # "On every new entry" hook — fires for human-authored entry notes only (typed
    # `user`, dictated from the `watch`, or `architect`-created), not kb/workflow/
    # restore writes, so workflows can enrich them (auto-title, auto-tag, …).
    effective_kind = kind if kind is not None else (existing["kind"] if existing else "entry")
    if created and effective_kind == "entry" and source in ("user", "architect", "watch"):
        if fire_events:
            _fire_entry_created(conn, note_id, title)
        else:  # defer to the caller's flush_entry_events() after it commits
            pending = getattr(_state, "pending", None) or []
            pending.append((note_id, title))
            _state.pending = pending
    return note_id


def soft_delete(conn, note_id: int) -> None:
    """Soft-delete a note: stamp deleted_at, clear FTS/links/embeddings/attachment indexes.

    Attachment rows are kept so a restore can re-index them.

    Args:
        conn: SQLite connection.
        note_id: Note to soft-delete.
    """
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


def restore(conn, note_id: int) -> None:
    """Resurrect a soft-deleted note AND rebuild everything soft_delete tore down:
    the keyword index, its OUTGOING links (from its content's [[wiki-links]]), the
    INBOUND links other notes had to it (nulled on delete), and its embedding.
    Without this a restored/undeleted note comes back as a graph orphan and is
    missing from search."""
    row = conn.execute("SELECT title, content_md FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return
    conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))
    _sync_fts(conn, note_id, row["title"], row["content_md"])
    wikilinks.reconcile_links(conn, note_id, row["content_md"])      # outgoing
    wikilinks.resolve_dangling_links(conn, note_id, row["title"])    # inbound (others that cited it)
    try:
        embeddings.upsert_note_embedding(conn, note_id, row["title"], row["content_md"])
    except Exception:
        pass


def backlinks(conn, note_id: int) -> list[dict]:
    """Return all live notes that link to the given note.

    Args:
        conn: SQLite connection.
        note_id: Target note ID to look up backlinks for.

    Returns:
        List of dicts with 'id', 'title', and 'slug', sorted by title.
    """
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
