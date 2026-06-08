"""Notes REST API: list, read, create/update, delete, backlinks, history."""
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser, require_capture_writer
from ..db import get_conn
from ..services import clock, diffing, wikilinks
from ..services import notes as notes_svc

router = APIRouter(prefix="/api/notes", tags=["notes"], dependencies=[CurrentUser])

# Watch/phone dictation capture lives on its own router so it can use a LOOSER auth than
# the rest of /api/notes (which is full-key only): it also accepts a per-person location
# key, so a family phone holding only its scoped setup-code key can drop a dictated note.
# Same prefix → the path stays POST /api/notes/entry, so installed apps don't change.
entry_router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteIn(BaseModel):
    """Request body for creating or updating a note."""

    title: str
    content_md: str = ""


class EntryIn(BaseModel):
    """Request body for the 'Make entry' dictation capture endpoint.

    Attributes:
        dest: Entry sub-selector destination; files the note under notes/<root>/<dest>/NN.
            Ignored when an explicit title is given.
        root: Capture tree for dest ('medical' | 'financial'); defaults to medical for
            back-compat. Clamped to a known root.
        source: Provenance label for the version badge ('user' | 'watch'). Unrecognised
            values are clamped to 'user' — capture must never 422 over a bad label.
        lat: Capture latitude, bounded to [-90, 90] so NaN/inf can't break distance math.
        lon: Capture longitude, bounded to [-180, 180].
    """

    text: str
    title: str | None = None
    # Entry sub-selector capture: file the entry under notes/<root>/<dest>/NN (a preconfigured
    # destination the PWA offers). Ignored when an explicit title is given.
    dest: str | None = None
    # Which capture tree the dest belongs to: 'medical' | 'financial'. Defaults to medical for
    # back-compat (the original Medical-mode capture sent only `dest`); clamped to a known root.
    root: str | None = None
    # Provenance for the version badge. The watch relays dictations through the phone,
    # which tags them `watch` so the note's history shows where it came from. Anything
    # unrecognised is clamped to `user` in the handler (capture must never 422 away a
    # note over a bad label).
    source: str | None = None
    # Bounded so a stray reading (incl. NaN/inf, which fail the bounds) can't be
    # stored and break downstream distance math / JSON serialisation.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class RestoreIn(BaseModel):
    """Request body for restoring a note to a previous version."""

    version_id: int
    note: str | None = None


def _note_by_slug(conn, slug: str, include_deleted: bool = False):
    """Fetch a note row by slug, raising 404 if not found.

    Args:
        conn: Active database connection.
        slug: URL slug of the note.
        include_deleted: If True, also return soft-deleted notes.

    Returns:
        The notes row for the given slug.

    Raises:
        HTTPException: 404 if no matching note exists.
    """
    sql = "SELECT * FROM notes WHERE slug = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    row = conn.execute(sql, (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row


@router.get("")
def list_notes(q: str | None = None, kind: str | None = None, limit: int = 200,
               include_hidden: bool = False):
    """List notes, optionally filtered by title substring and/or kind.

    Redirects and soft-deleted notes are excluded. Protected/system pages (any
    path segment starting with '_') are hidden unless include_hidden is True.

    Args:
        q: Optional title substring filter (LIKE match).
        kind: Optional note kind filter (e.g. 'note', 'kb').
        limit: Maximum number of results to return (default 200).
        include_hidden: If True, include protected/system pages in the results.

    Returns:
        List of note dicts with id, title, slug, kind, kb_ingest, tool_access, updated_at.
    """
    conn = get_conn()
    # Redirects are decluttered from browse: a merged-away page keeps its UNIQUE title slot
    # live so old [[links]] resolve, but it must not show up in the notes list.
    clauses = ["deleted_at IS NULL", "redirect_to IS NULL"]
    params: list = []
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    # Hide protected/system pages (any path segment starts with '_', e.g. kb/_index)
    # from the notes list by default; pass include_hidden=true to see them.
    if not include_hidden:
        clauses.append(f"NOT {notes_svc.protected_title_sql('title')}")
    params.append(limit)
    rows = conn.execute(
        "SELECT id, title, slug, kind, kb_ingest, tool_access, updated_at FROM notes "
        f"WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/located")
def located_notes(since: str | None = None, until: str | None = None, limit: int = 2000):
    """Return notes that carry a capture coordinate, for the Map's note pins.

    Declared before /{slug} so 'located' is not swallowed as a slug parameter.

    Args:
        since: Optional ISO datetime lower bound on created_at.
        until: Optional ISO datetime upper bound on created_at.
        limit: Maximum number of results (default 2000, clamped to 5000).

    Returns:
        List of note dicts with slug, title, lat, lon, location_label, kind, created_at.
    """
    conn = get_conn()
    sql = ("SELECT slug, title, lat, lon, location_label, kind, created_at FROM notes "
           "WHERE deleted_at IS NULL AND lat IS NOT NULL AND lon IS NOT NULL")
    params: list = []
    s = since.strip().replace("T", " ").replace("Z", "") if since else None
    u = until.strip().replace("T", " ").replace("Z", "") if until else None
    if s:
        sql += " AND created_at >= ?"; params.append(s)
    if u:
        sql += " AND created_at <= ?"; params.append(u)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@router.get("/{slug}")
def get_note(slug: str):
    """Fetch a note by slug, including backlinks, tags, and redirect resolution.

    Args:
        slug: URL slug of the note.

    Returns:
        Full note dict with all columns plus backlinks, tags, and redirect_to_slug.

    Raises:
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    note = dict(row)
    # Redirect resolution: when this row is a redirect, resolve the (chained) FINAL target
    # to its slug so a client can forward there. redirect_to is already on the row (SELECT *).
    note["redirect_to_slug"] = None
    if row["redirect_to"]:
        from ..services import wiki_build
        final = wiki_build._resolve_redirect_chain(conn, row["redirect_to"])
        tgt = conn.execute(
            "SELECT slug FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL", (final,)
        ).fetchone()
        if tgt:
            note["redirect_to_slug"] = tgt["slug"]
    note["backlinks"] = notes_svc.backlinks(conn, row["id"])
    note["tags"] = [
        t["name"]
        for t in conn.execute(
            "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = ? ORDER BY t.name",
            (row["id"],),
        ).fetchall()
    ]
    return note


@router.get("/{slug}/preview")
def note_preview(slug: str):
    """Return a small title and excerpt for a note, used to preview [[citations]] on hover.

    Prefers the AI-generated gist; falls back to the leading prose. Returns
    {found: false} instead of 404 so the caller can degrade gracefully.

    Args:
        slug: URL slug of the note.

    Returns:
        Dict with found, and when found: title and excerpt (max 280 chars).
    """
    conn = get_conn()
    row = conn.execute("SELECT id, title, content_md FROM notes WHERE slug=? AND deleted_at IS NULL",
                       (slug,)).fetchone()
    if not row:
        return {"found": False}
    from ..services import note_analysis as na_svc, clock
    excerpt = ""
    a = na_svc.get(conn, row["id"])
    if a and a.get("gist"):
        excerpt = clock.expand_tokens(a["gist"])
    if not excerpt:
        lines = (row["content_md"] or "").split("\n")
        if lines and lines[0].lstrip().startswith("# "):   # drop a leading "# Title" heading
            lines = lines[1:]
        excerpt = clock.expand_tokens(" ".join("\n".join(lines).split()))
    excerpt = excerpt.strip()
    return {"found": True, "title": row["title"], "excerpt": excerpt[:280] + ("…" if len(excerpt) > 280 else "")}


@router.get("/{slug}/analysis")
def note_analysis(slug: str):
    """Return the read-only AI analysis sidecar for a note.

    Includes gist, salient facts, entities, and domain classification. Returns {}
    when no analysis has been computed yet. Never mutates the note.

    Args:
        slug: URL slug of the note.

    Returns:
        Analysis dict, or {} if none exists.

    Raises:
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    from ..services import note_analysis as na
    return na.get(conn, row["id"]) or {}


@router.post("/{slug}/analysis")
def refresh_note_analysis(slug: str):
    """Force-recompute a note's AI analysis sidecar, bypassing the content-hash cache.

    Also runs the title-normalization pass (a bare dated note may be renamed) and
    re-aggregates the entity index when the analysis changes. Returns the fresh
    analysis plus the note's (possibly renamed) slug and title.

    Args:
        slug: URL slug of the note to reanalyze.

    Returns:
        Fresh analysis dict with slug and title fields appended.

    Raises:
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    from ..services import note_analysis as na
    from ..services import entity_index, note_normalize
    note_normalize.title_one(conn, row["id"])          # title check first (may rename the note)
    if na.analyze(conn, row["id"], force=True):
        entity_index.rebuild(conn)                     # rebuild commits; refresh browse/search too
    conn.commit()
    cur = conn.execute("SELECT slug, title FROM notes WHERE id = ?", (row["id"],)).fetchone()
    out = na.get(conn, row["id"]) or {}
    out["slug"], out["title"] = cur["slug"], cur["title"]
    return out


class TalkIn(BaseModel):
    """Request body for adding a talk item to an article."""

    kind: str = "note"
    body: str


class TalkReplyIn(BaseModel):
    """Request body for replying to a talk item."""

    body: str


class TalkDismissIn(BaseModel):
    """Request body for dismissing a talk item."""

    reason: str = ""


def _note_title(conn, slug: str) -> str:
    """Return the title of a note by slug, raising 404 if not found.

    Args:
        conn: Active database connection.
        slug: URL slug of the note.

    Returns:
        The note's title string.

    Raises:
        HTTPException: 404 if no live note with that slug exists.
    """
    row = conn.execute("SELECT title FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return row["title"]


@router.get("/{slug}/talk")
def get_talk(slug: str):
    """Return the talk items for an article (decisions, conflicts, questions, directives).

    Talk items are the maintenance memory alongside an article.

    Args:
        slug: URL slug of the article.

    Returns:
        List of talk item dicts from article_talk.list_for.

    Raises:
        HTTPException: 404 if the article does not exist.
    """
    conn = get_conn()
    from ..services import article_talk
    return article_talk.list_for(conn, _note_title(conn, slug))


@router.post("/{slug}/talk")
def add_talk(slug: str, body: TalkIn):
    """Add an owner note, directive, or question to an article's talk.

    There is intentionally no user 'resolve' — open items are addressed through the
    Review flow or maintenance pass when the underlying issue is actually handled.

    A 'correction' kind is promoted to a dated entry note (the truth layer), and the
    talk item links to it so the next maintenance pass rewrites the article. Source
    entries are never modified.

    Args:
        slug: URL slug of the article.
        body: Talk item kind and body text.

    Returns:
        Dict with 'id' of the new talk item, and 'promoted' (note slug if a correction
        was promoted to a truth note, else None).

    Raises:
        HTTPException: 404 if the article does not exist.
    """
    conn = get_conn()
    from ..services import article_talk, corrections
    article_title = _note_title(conn, slug)
    tid = article_talk.add(conn, article_title, body.kind, body.body, author="user")
    promoted = None
    if tid is not None and body.kind == "correction":
        promoted = corrections.maybe_promote(conn, tid, article_title, body.body)
    conn.commit()
    if promoted:
        notes_svc.flush_entry_events(conn)  # fire analysis/auto-tag AFTER commit
    return {"id": tid, "promoted": promoted}


def _talk_row(conn, slug: str, talk_id: int):
    """Fetch a talk row and assert it belongs to this article.

    Prevents cross-article writes by comparing article_title on the row.

    Args:
        conn: Active database connection.
        slug: URL slug of the article the talk item must belong to.
        talk_id: Primary key of the article_talk row.

    Returns:
        The article_talk row.

    Raises:
        HTTPException: 404 if the note or talk item does not exist, or belongs to
            a different article.
    """
    article_title = _note_title(conn, slug)
    row = conn.execute(
        "SELECT id, kind, article_title FROM article_talk WHERE id=?", (talk_id,)).fetchone()
    if not row or row["article_title"] != article_title:
        raise HTTPException(status_code=404, detail="Talk item not found")
    return row


@router.post("/{slug}/talk/{talk_id}/reply")
def reply_talk(slug: str, talk_id: int, body: TalkReplyIn):
    """Reply to a talk item, contributing to the owner-AI maintenance conversation.

    The reply is folded into the next maintenance pass over this article (scheduled
    or via 'maintain-now').

    Args:
        slug: URL slug of the article.
        talk_id: Primary key of the talk item to reply to.
        body: Reply text.

    Returns:
        Dict with 'id' of the new reply.

    Raises:
        HTTPException: 400 if the reply body is empty.
        HTTPException: 404 if the article or talk item does not exist.
    """
    conn = get_conn()
    from ..services import article_talk
    _talk_row(conn, slug, talk_id)
    rid = article_talk.reply(conn, talk_id, body.body, author="user")
    conn.commit()
    if rid is None:
        raise HTTPException(status_code=400, detail="Empty reply")
    return {"id": rid}


@router.post("/{slug}/talk/{talk_id}/dismiss")
def dismiss_talk(slug: str, talk_id: int, body: TalkDismissIn):
    """Dismiss a talk item as 'dismissed by owner' (distinct from a maintenance resolution).

    Refused for 'correction' items — their promoted truth note still heals the article
    on the next pass, so hiding the row would mislead. Delete the truth note to undo a
    correction.

    Args:
        slug: URL slug of the article.
        talk_id: Primary key of the talk item to dismiss.
        body: Optional reason text.

    Returns:
        Dict with 'ok': True if dismissed, False if already resolved.

    Raises:
        HTTPException: 409 if the talk item is a correction (cannot be dismissed).
        HTTPException: 404 if the article or talk item does not exist.
    """
    conn = get_conn()
    from ..services import article_talk
    row = _talk_row(conn, slug, talk_id)
    if row["kind"] == "correction":
        raise HTTPException(
            status_code=409,
            detail="A correction can't be dismissed — it's a source-of-truth fact. "
                   "Delete the truth note it created to undo it.")
    ok = article_talk.dismiss(conn, talk_id, body.reason)
    conn.commit()
    return {"ok": ok}


@router.post("/{slug}/talk/maintain-now")
def maintain_now_talk(slug: str):
    """Run the surgical KB maintenance pass on this article immediately.

    Consumes open talk items and the owner's replies without waiting for the nightly
    batch. Runs under the KB write lock.

    Args:
        slug: URL slug of the article to maintain.

    Returns:
        Result dict from wiki_build.maintain_now.

    Raises:
        HTTPException: 404 if the article does not exist.
    """
    conn = get_conn()
    from ..services import wiki_build
    article_title = _note_title(conn, slug)
    return wiki_build.maintain_now(conn, article_title)


@router.get("/kb/dead-links")
def kb_dead_links():
    """Return KB articles that contain dangling cross-links to non-existent targets.

    Returns:
        Dict with 'count' and 'items' listing each dead link.
    """
    from ..services import wiki_build
    items = wiki_build.dead_links(get_conn())
    return {"count": len(items), "items": items}


@router.post("")
def create_or_update(body: NoteIn):
    """Create a new note or update an existing one by title.

    Fires entry_created hooks after the commit (auto-tag etc.).

    Args:
        body: Note title and markdown content.

    Returns:
        Dict with id, title, and slug of the upserted note.
    """
    conn = get_conn()
    try:
        note_id = notes_svc.upsert_note(conn, body.title, body.content_md, fire_events=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    notes_svc.flush_entry_events(conn)  # fire entry_created AFTER commit
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@router.put("/{slug}")
def update_note(slug: str, body: NoteIn):
    """Edit an existing note in place, optionally renaming it.

    Targets the note by id so a new title renames this note and its slug instead of
    creating a duplicate. Use to move notes between notes/ and kb/ roots.

    Args:
        slug: URL slug of the note to update.
        body: New title and markdown content.

    Returns:
        Dict with id, title, and slug of the updated note.

    Raises:
        HTTPException: 409 if a note with the new title already exists.
        HTTPException: 422 if the new title is empty.
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    try:
        note_id = notes_svc.upsert_note(
            conn, new_title, body.content_md, note_id=note["id"], source="user",
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="A note with that title already exists.")
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


class TagsIn(BaseModel):
    """Request body for replacing a note's tags."""

    tags: list[str] = []


@router.put("/{slug}/tags")
def set_note_tags(slug: str, body: TagsIn):
    """Replace a note's tags directly (owner UI path, bypasses staging).

    The AI path stages a tag change for approval; the owner editing in the UI writes
    directly.

    Args:
        slug: URL slug of the note.
        body: Complete new tag list.

    Returns:
        Dict with 'tags' — the updated tag list.

    Raises:
        HTTPException: 404 if the note does not exist.
    """
    conn = get_conn()
    note = _note_by_slug(conn, slug)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")
    tags = notes_svc.set_tags(conn, note["id"], body.tags)
    conn.commit()
    return {"tags": tags}


class FlagsIn(BaseModel):
    """Request body for updating a note's governance flags (PATCH semantics).

    Omit a field to leave it unchanged so individual checkboxes toggle independently.
    Only the three coherent states are permitted: Full (1/1), Research-only (0/1),
    Private (0/0).
    """

    # PATCH semantics: omit a field to leave it unchanged (so a single checkbox toggles
    # independently). Coherent states only — see set_note_flags.
    kb_ingest: bool | None = None
    tool_access: bool | None = None


@router.put("/{slug}/flags")
def set_note_flags(slug: str, body: FlagsIn):
    """Update the per-note governance flags directly (owner UI path).

    kb_ingest: feed this note to KB synthesis. tool_access: surface it in the
    assistant's search and research tools. Only three coherent states are allowed:
    Full (1/1), Research-only (0/1), Private (0/0). kb_ingest=1 with tool_access=0
    is rejected — the KB would otherwise cite a source the assistant cannot read.

    Args:
        slug: URL slug of the note.
        body: kb_ingest and/or tool_access flags (omit to leave unchanged).

    Returns:
        Dict with 'kb_ingest' and 'tool_access' as booleans.

    Raises:
        HTTPException: 404 if the note does not exist.
        HTTPException: 422 if the requested state is incoherent (kb_ingest=1, tool_access=0).
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id, kb_ingest, tool_access FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such note")
    kb = row["kb_ingest"] if body.kb_ingest is None else int(body.kb_ingest)
    tool = row["tool_access"] if body.tool_access is None else int(body.tool_access)
    if kb == 1 and tool == 0:
        raise HTTPException(
            status_code=422,
            detail="A note kept in the Knowledge Base must stay readable by the assistant.")
    conn.execute("UPDATE notes SET kb_ingest = ?, tool_access = ? WHERE id = ?",
                 (kb, tool, row["id"]))
    # Re-enabling KB ingestion (0→1) clears the per-entry 'already evaluated / promoted'
    # markers so the next synthesis pass reconsiders this entry instead of skipping it.
    if row["kb_ingest"] == 0 and kb == 1:
        conn.execute("DELETE FROM meta WHERE key IN (?, ?)",
                     (f"wiki_synth:evaluated:{row['id']}", f"chatter_promoted:{row['id']}"))
    conn.commit()
    return {"kb_ingest": bool(kb), "tool_access": bool(tool)}


@entry_router.post("/entry")
def create_entry(body: EntryIn, writer=Depends(require_capture_writer)):
    """'Make entry' mode: store text directly as a NEW note (unique title), no LLM.
    Fires the entry_created hooks (auto-tag, etc.).

    `writer` is None for the full access key (PWA / owner) or a person row when a
    family phone authenticates with its per-person location key — in which case the
    dictation is attributed to that person so you can tell whose watch it came from.
    """
    conn = get_conn()
    text = body.text.strip()
    explicit = (body.title or "").strip()
    if not text and not explicit:
        raise HTTPException(status_code=422, detail="Entry text cannot be empty")
    # Attribute a family member's dictation (per-person key, not the owner/default) so
    # the note shows whose watch spoke it. The owner's own captures stay pristine.
    if writer is not None and not writer["is_default"] and text:
        text = f"({writer['name']}) {text}"
    # Provenance: `watch` for relayed wrist dictations, else a plain human `user`
    # entry. Clamp anything unknown so the version badge stays a known value and the
    # entry_created enrichment still fires (see upsert_note's human-source gate).
    source = (body.source or "user").strip().lower()
    if source not in ("user", "watch"):
        source = "user"
    dest = (body.dest or "").strip()
    capture_root = (body.root or notes_svc._MED_ROOT).strip().lower()
    if capture_root not in notes_svc.CAPTURE_ROOTS:
        capture_root = notes_svc._MED_ROOT
    if explicit:
        # An explicit title (the assisted-attachment path) keeps its own name —
        # "assisted notes can go somewhere else". The first line is NOT a title.
        title = notes_svc._unique_title(conn, notes_svc.root_title(explicit, "notes"))
    elif dest:
        # Entry sub-selector capture: file under the chosen destination, notes/<root>/<dest>/NN,
        # so medical/financial captures land in their own browsable folder (not the daily tree).
        title = notes_svc._unique_title(conn, notes_svc.next_capture_title(conn, capture_root, dest))
    else:
        # Pure Entry capture: no title. File chronologically under the standard flat
        # dated tree as notes/YYYY/MM/DD/NN; the whole text is the body. Day boundary
        # is the app timezone (same TZ the scheduler uses for midnight).
        title = notes_svc.next_dated_title(conn, clock.today_local())
    try:
        note_id = notes_svc.upsert_note(
            conn, title, text, source=source, lat=body.lat, lon=body.lon, fire_events=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()  # don't leave a half-written note on the pooled connection
        raise
    # Fire entry_created AFTER commit so an (optional, LLM-backed) auto-tag
    # workflow doesn't hold the note's write lock or freeze the "no-LLM" Send.
    notes_svc.flush_entry_events(conn)
    row = conn.execute("SELECT id, title, slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@router.delete("/{slug}")
def delete_note(slug: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    try:
        notes_svc.soft_delete(conn, row["id"])
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — surface the REAL reason, don't return an opaque 500
        conn.rollback()
        logging.getLogger("jbrain").exception("delete_note failed for %s", slug)
        msg = str(exc) or exc.__class__.__name__
        # A transient write-lock (e.g. background note-analysis is mid-write) is retryable —
        # say so instead of a bare 500 the UI shows as "Internal Server Error".
        if "locked" in msg.lower() or "busy" in msg.lower():
            raise HTTPException(status_code=503, detail="The database was busy (something is being processed in the background). Try deleting again in a moment.")
        raise HTTPException(status_code=500, detail=f"Couldn't delete this note: {msg}")
    return {"ok": True}


@router.get("/{slug}/versions")
def versions(slug: str):
    """Timeline of authored states, newest first. The newest is the current one."""
    conn = get_conn()
    row = _note_by_slug(conn, slug, include_deleted=True)
    rows = conn.execute(
        "SELECT id, title, source, conversation_id, note, created_at, "
        "length(content_md) AS size FROM note_versions "
        "WHERE note_id = ? ORDER BY created_at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    out = [dict(r) for r in rows]
    for i, v in enumerate(out):
        v["version_id"] = v.pop("id")
        v["is_current"] = i == 0
    return out


@router.get("/{slug}/versions/{version_id}")
def get_version(slug: str, version_id: int):
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    v = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
        (version_id, note["id"]),
    ).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")
    return dict(v)


@router.get("/{slug}/diff/{from_id}/{to_id}")
def diff_versions(slug: str, from_id: int, to_id: int):
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)

    def _ver(vid: int):
        v = conn.execute(
            "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
            (vid, note["id"]),
        ).fetchone()
        if not v:
            raise HTTPException(status_code=404, detail=f"Version {vid} not found")
        return v

    a, b = _ver(from_id), _ver(to_id)
    return {
        "from": {"version_id": a["id"], "created_at": a["created_at"], "title": a["title"]},
        "to": {"version_id": b["id"], "created_at": b["created_at"], "title": b["title"]},
        "title_changed": a["title"] != b["title"],
        # Raw before/after content powers the rendered-markdown diff in the client;
        # `hunks` is kept for any plain-text consumer / backward compatibility.
        "before": a["content_md"],
        "after": b["content_md"],
        "hunks": diffing.line_diff(a["content_md"], b["content_md"]),
    }


@router.get("/links/audit")
def links_audit():
    """List [[Target|Display]] links whose shortened label names a different article than
    the target (high-confidence only) — the interactive Wiki link-label audit."""
    return {"findings": wikilinks.audit_display_mismatches(get_conn())}


class LinkFixIn(BaseModel):
    note_id: int
    target: str
    display: str


@router.post("/links/audit/fix")
def links_audit_fix(body: LinkFixIn):
    """Correct one flagged link (re-derives the right label from the live target)."""
    conn = get_conn()
    changed = wikilinks.fix_note_link(conn, body.note_id, body.target, body.display)
    conn.commit()
    return {"fixed": bool(changed)}


@router.post("/links/audit/fix-all")
def links_audit_fix_all():
    """Correct every currently-flagged link. Returns how many were changed."""
    conn = get_conn()
    fixed = 0
    for f in wikilinks.audit_display_mismatches(conn):
        if wikilinks.fix_note_link(conn, f["source_id"], f["target"], f["display"]):
            fixed += 1
    conn.commit()
    return {"fixed": fixed}


@router.post("/{slug}/restore")
def restore(slug: str, body: RestoreIn):
    """Restore an old version. Snapshots current first (history is never lost)."""
    conn = get_conn()
    note = _note_by_slug(conn, slug, include_deleted=True)
    v = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
        (body.version_id, note["id"]),
    ).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")

    try:
        notes_svc.upsert_note(
            conn,
            v["title"],
            v["content_md"],
            note_id=note["id"],
            source="restore",
            version_note=body.note or f"restored from version {body.version_id}",
        )
        # Restoring resurrects a soft-deleted note (upsert re-indexed it).
        conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    out = conn.execute(
        "SELECT id, title, slug FROM notes WHERE id = ?", (note["id"],)
    ).fetchone()
    return dict(out)
