"""Parse [[wiki-links]] and reconcile the links table.

Supports [[Title]] and [[Title|display text]]. Matching to existing notes is by
title (case-insensitive); unresolved links are stored with target_note_id NULL
so a later-created note automatically gains its backlinks.
"""
from __future__ import annotations

import re

WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")


def extract_links(content_md: str) -> list[str]:
    """Return the unique, order-preserving list of [[wiki-link]] target titles in content.

    Ignores links longer than 200 chars or containing newlines.

    Args:
        content_md: Markdown note body.

    Returns:
        List of unique title strings in the order they first appear.
    """
    seen: dict[str, None] = {}
    for match in WIKILINK_RE.finditer(content_md or ""):
        title = match.group(1).strip()
        # Real note titles are single-line and bounded; ignore junk so a giant or
        # multi-line [[…]] body can't bloat the links table or be re-scanned forever.
        if title and "\n" not in title and len(title) <= 200:
            seen.setdefault(title, None)
    return list(seen.keys())


def reconcile_links(conn, source_note_id: int, content_md: str) -> None:
    """Rebuild the outgoing links table rows for a note from its current content.

    Args:
        conn: SQLite connection.
        source_note_id: Note whose outgoing links should be replaced.
        content_md: Current note body to extract links from.
    """
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
    """Attach prior unresolved [[title]] links to a newly created note.

    Args:
        conn: SQLite connection.
        note_id: ID of the newly created note.
        title: Title to match against existing dangling links (case-insensitive).
    """
    conn.execute(
        "UPDATE links SET target_note_id = ? "
        "WHERE target_note_id IS NULL AND lower(target_title) = lower(?)",
        (note_id, title),
    )


# --- Link-label hygiene ------------------------------------------------------
# A [[Target|Display]] link should show a clean, short label for the article it
# points at. Two problems get fixed here, both by DROPPING the alias so the link
# renders the article's bare name (the PWA's wikiLabel() shows the last path
# segment for kb/ titles — "Jeffrey Mark Hopkins", not "People/Jeffrey Mark Hopkins"):
#   • mismatch — the label names a DIFFERENT article than the target (e.g. a stale
#     alias left by a rename: [[kb/People/Summer E. Hopkins|Jeff Hopkins]]);
#   • verbose — the label just repeats the article's path/name (e.g.
#     [[kb/People/Jeff|People/Jeff]] or [[kb/People/Jeff|Jeff]]), clutter we tidy.
# A genuine, different shortening/nickname (e.g. |Dad, or |Jeff for "Jeff Hopkins")
# is left untouched.

# Capture target + optional display (the links table doesn't store the display).
WIKILINK_FULL_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_ROOTS = ("notes", "kb", "lists", "logs")


def wiki_label(title: str) -> str:
    """Return the bare display label for a link, mirroring the PWA's wikiLabel().

    For kb/ titles the category folders are taxonomy, so the last path segment is
    shown ('kb/People/Jeffrey Mark Hopkins' → 'Jeffrey Mark Hopkins'). Other roots
    keep their root-stripped leaf.

    Args:
        title: Full note title (may include kb/, notes/, etc. prefix).

    Returns:
        Short display label string.
    """
    t = _norm(title)
    if t.lower().startswith("kb/"):
        segs = [s for s in t.split("/") if s.strip()]
        return segs[-1] if segs else _root_leaf(t)
    return _root_leaf(t)


def _path_forms(title: str) -> set[str]:
    """Return the progressive trailing path forms of a title, lowercased.

    These are the 'verbose' label variants a writer might bake in.
    'kb/People/Jeff Hopkins' → {'jeff hopkins', 'people/jeff hopkins', 'kb/people/jeff hopkins'}.

    Args:
        title: Full note title.

    Returns:
        Set of lowercased path suffix strings.
    """
    segs = [s.strip() for s in _norm(title).split("/") if s.strip()]
    forms, suffix = set(), ""
    for seg in reversed(segs):
        suffix = seg if not suffix else f"{seg}/{suffix}"
        forms.add(suffix.lower())
    return forms


def _norm(s: str | None) -> str:
    """Return a stripped string, treating None as empty."""
    return (s or "").strip()


def _last_segment(title: str) -> str:
    """Return the bare leaf name — the final '/'-separated segment of a title.

    Args:
        title: Full note title (e.g. 'kb/People/Jeff').

    Returns:
        Leaf segment string (e.g. 'Jeff').
    """
    return _norm(title).split("/")[-1].strip()


def _parent(title: str) -> str:
    """Return everything above the leaf segment ('kb/People/Jeff' → 'kb/People').

    Args:
        title: Full note title.

    Returns:
        Parent path string, or '' for a top-level title.
    """
    parts = [p.strip() for p in _norm(title).split("/")]
    return "/".join(parts[:-1])


def _root_leaf(title: str) -> str:
    """Strip a single leading root folder, mirroring the PWA's leaf().

    'kb/Jeff' → 'Jeff'. Recognized roots: notes, kb, lists, logs.

    Args:
        title: Full note title.

    Returns:
        Title with exactly one leading root stripped, or the original if no root matches.
    """
    t = _norm(title)
    for r in _ROOTS:
        if t.lower().startswith(r + "/"):
            return t[len(r) + 1:]
    return t


def _tokens(s: str) -> set[str]:
    """Return the set of lowercase alphanumeric tokens in a string."""
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _iter_display_links(content_md: str):
    """Yield (raw, target, display) for every [[Target|Display]] link with an explicit alias.

    Args:
        content_md: Note body to scan.

    Yields:
        Tuple of (raw_match_string, target_title, display_text) for each aliased link.
    """
    for m in WIKILINK_FULL_RE.finditer(content_md or ""):
        target, display = _norm(m.group(1)), m.group(2)
        if display is None:
            continue
        display = _norm(display)
        if not target or not display or "\n" in target or "\n" in display:
            continue
        yield m.group(0), target, display


def scan_link_labels(conn) -> list[dict]:
    """Scan every live note for [[Target|Display]] links that should drop their alias.

    Each finding has kind 'mismatch' (label names a different article — high-confidence:
    a different note in the same folder, or a former title of the target) or 'verbose'
    (the label just repeats the article's path/name). The fix is always the bare
    [[Target]], which renders the article's clean short name. Returns [] on indeterminate
    alias surface (fail-closed: strips nothing rather than risking intentional alias loss).

    Args:
        conn: SQLite connection.

    Returns:
        List of finding dicts, each with kind, source_id, raw, target, display,
        desired_display, fixed, and reason.
    """
    notes = conn.execute(
        "SELECT id, title, slug, content_md FROM notes WHERE deleted_at IS NULL"
    ).fetchall()
    by_title: dict[str, any] = {}
    by_seg: dict[str, list] = {}
    for n in notes:
        by_title[n["title"].lower()] = n
        by_seg.setdefault(_last_segment(n["title"]).lower(), []).append(n)
    # Former titles (full + leaf) per note, from version history → spot stale rename aliases.
    former: dict[int, set[str]] = {}
    for r in conn.execute("SELECT DISTINCT note_id, title FROM note_versions"):
        s = former.setdefault(r["note_id"], set())
        s.add((r["title"] or "").lower())
        s.add(_last_segment(r["title"]).lower())

    def finding(kind, n, raw, target, tgt, display, reason, resolved=None):
        return {
            "kind": kind, "source_id": n["id"], "source_title": n["title"], "source_slug": n["slug"],
            "raw": raw, "target": target, "target_title": tgt["title"], "target_slug": tgt["slug"],
            "display": display, "desired_display": wiki_label(tgt["title"]), "fixed": f"[[{target}]]",
            "resolved_title": resolved["title"] if resolved else None,
            "resolved_slug": resolved["slug"] if resolved else None, "reason": reason,
        }

    # Registered-alias allow-list: a [[Target|Display]] whose Display is a known alias of
    # Target's entity is INTENTIONAL (e.g. [[kb/People/Jeffrey Hopkins|Jeff Hopkins]]) and must
    # NOT be "tidied" away — that would strip the nickname the linker deliberately added, every
    # night. FAIL CLOSED: if the alias surface can't be built (mid-rebuild error), strip NOTHING
    # this run (return no findings) rather than risk destroying alias links; it self-heals next
    # pass. An empty surface (a KB with no aliases) is fine — there are simply no links to protect.
    from . import entity_index
    try:
        _alias_surface = entity_index.alias_surface(conn)
    except Exception:  # noqa: BLE001 — never strip on an indeterminate alias surface
        return []
    from .wiki_build import _resolve_redirect_chain

    def _is_registered_alias(display: str, tgt) -> bool:
        info = _alias_surface.get(entity_index.normalize(display))
        if not info:
            return False
        return _resolve_redirect_chain(conn, info[0]).lower() == _resolve_redirect_chain(conn, tgt["title"]).lower()

    findings: list[dict] = []
    for n in notes:
        for raw, target, display in _iter_display_links(n["content_md"]):
            tgt = by_title.get(target.lower())
            if not tgt:
                continue  # dangling/unresolved target — out of scope
            if _is_registered_alias(display, tgt):
                continue  # intentional nickname/alias of THIS article — leave it alone
            # Verbose: the label just echoes the article's path/name → tidy to bare.
            if display.lower() in _path_forms(tgt["title"]):
                findings.append(finding("verbose", n, raw, target, tgt, display,
                                        "label repeats the article's path/name"))
                continue
            # A genuine, different shortening/expansion of the same name → leave it.
            dt, st = _tokens(display), _tokens(_last_segment(tgt["title"]))
            if dt and (dt <= st or st <= dt):
                continue
            # Mismatch: names a different same-folder article, or a former title.
            dlow = display.lower()
            if dlow in former.get(tgt["id"], set()):
                findings.append(finding("mismatch", n, raw, target, tgt, display,
                                        "stale rename alias — the label is a former title of the target"))
                continue
            cand = by_title.get(dlow)
            if cand is None:
                hits = by_seg.get(dlow, [])
                cand = hits[0] if len(hits) == 1 else None
            if (cand and cand["id"] != tgt["id"]
                    and _parent(cand["title"]).lower() == _parent(tgt["title"]).lower()):
                findings.append(finding("mismatch", n, raw, target, tgt, display,
                                        "the label names a different article in the same folder", cand))
    return findings


def audit_display_mismatches(conn) -> list[dict]:
    """Return high-confidence label mismatches only (a label naming a different article).

    These are what the on-view Wiki panel flags. Verbose-alias tidying is left to the
    maintenance sweep.

    Args:
        conn: SQLite connection.

    Returns:
        Subset of scan_link_labels() findings where kind == 'mismatch'.
    """
    return [f for f in scan_link_labels(conn) if f["kind"] == "mismatch"]


def fix_note_link(conn, note_id: int, target: str, display: str) -> bool:
    """Drop the alias on one [[Target|Display]] link so it renders the target's clean short name.

    The change is versioned and undoable via the note's version history.

    Args:
        conn: SQLite connection.
        note_id: Note containing the link to fix.
        target: Exact target title of the link to rewrite.
        display: Current display alias to remove.

    Returns:
        True if the link was found and rewritten, False otherwise.
    """
    from . import notes as notes_svc  # lazy — notes imports this module

    row = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (note_id,)
    ).fetchone()
    if not row:
        return False
    if not conn.execute(
        "SELECT 1 FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL", (target,)
    ).fetchone():
        return False  # don't rewrite a link whose target no longer resolves

    changed = False

    def repl(m):
        nonlocal changed
        if (_norm(m.group(1)).lower() == target.lower()
                and m.group(2) is not None and _norm(m.group(2)) == display):
            changed = True
            return f"[[{target}]]"
        return m.group(0)

    new_content = WIKILINK_FULL_RE.sub(repl, row["content_md"])
    if not changed or new_content == row["content_md"]:
        return False
    notes_svc.upsert_note(conn, row["title"], new_content, note_id=note_id,
                          source="link-audit", version_note="link label tidy", fire_events=False)
    return True


def normalize_all_link_labels(conn, limit: int = 5000) -> dict:
    """Drop redundant and mismatched aliases across the whole wiki (deterministic, no LLM).

    Used by the nightly sweep and the post-write KB maintenance/build passes.

    Args:
        conn: SQLite connection.
        limit: Maximum number of findings to fix in this pass.

    Returns:
        Dict with 'fixed', 'mismatches', and 'verbose' counts.
    """
    mism = verb = 0
    for f in scan_link_labels(conn)[: int(limit)]:
        if fix_note_link(conn, f["source_id"], f["target"], f["display"]):
            if f["kind"] == "mismatch":
                mism += 1
            else:
                verb += 1
    return {"fixed": mism + verb, "mismatches": mism, "verbose": verb}

