"""Additive quick-task operations (Phase 2).

These are the ONLY writes the architect performs without confirmation, and they
are *structurally* additive — append a line or create-if-missing — so the worst
case is an extra line, fully reversible via Undo. There is no destructive
auto-apply tool, by design (see ROADMAP for complete/remove, which stay
confirm-gated and need fail-closed matching).
"""
from __future__ import annotations

import re

from . import clock
from . import notes as notes_svc

# A checklist line: optional indent, "- [ ]"/"- [x]", optional "(P1)" priority, text.
_ITEM_RE = re.compile(r"^(\s*)- \[( |x|X)\] (?:\(P(\d+)\) )?(.*)$")


def _format_item(indent: str, checked: bool, priority: int | None, text: str) -> str:
    """Format a single checklist line from its components.

    Args:
        indent: Leading whitespace string.
        checked: Whether the checkbox is ticked.
        priority: Optional priority number (rendered as '(P1)' token).
        text: Item text.

    Returns:
        Formatted checklist line string.
    """
    box = "[x]" if checked else "[ ]"
    ptok = f"(P{priority}) " if priority else ""
    return f"{indent}- {box} {ptok}{text}".rstrip()


def parse_items(content_md: str) -> list[dict]:
    """Parse checkbox lines from markdown into structured item dicts.

    Args:
        content_md: Markdown content to parse.

    Returns:
        List of dicts with keys index (0-based line number), indent, checked,
        priority (int or None), text, and raw.
    """
    items = []
    for i, ln in enumerate((content_md or "").split("\n")):
        m = _ITEM_RE.match(ln)
        if m:
            items.append({"index": i, "indent": m.group(1), "checked": m.group(2).lower() == "x",
                          "priority": int(m.group(3)) if m.group(3) else None,
                          "text": m.group(4), "raw": ln})
    return items


def match_item(items: list[dict], item_text: str, ordinal: int | None = None,
               expect_checked: bool | None = None) -> dict:
    """Locate a checklist item fail-closed.

    Prefers the ordinal (the index the model saw via read_list); otherwise requires
    a unique text match. Never guesses on ambiguity.

    Args:
        items: Parsed item list from parse_items.
        item_text: Exact text of the item to find.
        ordinal: Optional 0-based index from the model's read_list view.
        expect_checked: If set, the match must have this checked state.

    Returns:
        Matching item dict.

    Raises:
        LookupError: If the item is not found or multiple items match.
    """
    if ordinal is not None and 0 <= ordinal < len(items):
        c = items[ordinal]
        if c["text"] == item_text and (expect_checked is None or c["checked"] == expect_checked):
            return c
    matches = [it for it in items if it["text"] == item_text
               and (expect_checked is None or it["checked"] == expect_checked)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"item '{item_text}' not found in the list")
    raise LookupError("multiple items match that text — pass the item's index from read_list")


def _loc_kwargs(location) -> dict:
    """Extract lat/lon/location_label from a location dict for upsert_note kwargs.

    Args:
        location: Dict with lat, lon, and location_label keys, or None/falsy.

    Returns:
        Dict of keyword arguments, or {} if location is absent.
    """
    if not location:
        return {}
    return {"lat": location["lat"], "lon": location["lon"], "location_label": location["location_label"]}


def add_list_item(
    conn, list_title: str, item: str, checkbox: bool = True, priority: int | None = None, *,
    source: str = "architect", conversation_id: int | None = None, location=None,
) -> dict:
    """Append an item to a checklist note, creating the list if absent.

    Lists are their own layer: titled under the "lists/" root with kind='list',
    so they are kept apart from notes and skipped by wiki-synthesis. An optional
    priority renders as a leading "(P1)" token.

    Args:
        conn: Database connection.
        list_title: List title (rooted under 'lists/' if not already).
        item: Text of the item to add.
        checkbox: If True, render as a checkbox line; otherwise a plain bullet.
        priority: Optional priority number 1–N.
        source: Write source tag for versioning.
        conversation_id: Optional conversation ID to attach to the version.
        location: Optional location dict with lat, lon, and location_label.

    Returns:
        Dict with note_title, line, and created (True if the list was just created).
    """
    title = notes_svc.root_title(list_title, "lists")
    note = notes_svc.get_by_title(conn, title)
    created = note is None
    body = note["content_md"] if note else f"# {title.split('/')[-1]}\n"
    line = _format_item("", False, priority, item) if checkbox else f"- {item}"
    new_body = body.rstrip() + "\n" + line + "\n"
    notes_svc.upsert_note(
        conn, title, new_body, source=source, kind="list",
        conversation_id=conversation_id, version_note="added list item", **_loc_kwargs(location),
    )
    return {"note_title": title, "line": line, "created": created}


def _load_list(conn, list_title: str):
    """Locate a list note by title, trying the as-given form then the lists/ root.

    Handles lists that predate the lists/ convention or were named without the prefix.

    Args:
        conn: Database connection.
        list_title: List title to resolve.

    Returns:
        Tuple of (actual_title, note_row_or_None). actual_title is the resolved
        title so writes target the real note rather than a re-rooted guess.
    """
    raw = (list_title or "").strip()
    note = notes_svc.get_by_title(conn, raw) or notes_svc.get_by_title(conn, notes_svc.root_title(raw, "lists"))
    title = note["title"] if note else notes_svc.root_title(raw, "lists")
    return title, note


def _write_list(conn, title, lines, *, source, version_note, conversation_id=None, location=None):
    """Write a list note from its lines, joining with newlines.

    Args:
        conn: Database connection.
        title: Note title.
        lines: List of markdown line strings.
        source: Write source tag.
        version_note: Version description for the history entry.
        conversation_id: Optional conversation ID.
        location: Optional location dict.
    """
    notes_svc.upsert_note(conn, title, "\n".join(lines), source=source, kind="list",
                          conversation_id=conversation_id, version_note=version_note, **_loc_kwargs(location))


def _edit_one(conn, list_title, item, ordinal, make_line, *, version_note,
              source="architect", conversation_id=None, location=None) -> dict:
    """Locate one item fail-closed and replace its line with make_line(item).

    Args:
        conn: Database connection.
        list_title: List note title.
        item: Exact item text to locate.
        ordinal: Optional 0-based index hint.
        make_line: Callable accepting the item dict and returning the new line string.
        version_note: Version description for the history entry.
        source: Write source tag.
        conversation_id: Optional conversation ID.
        location: Optional location dict.

    Returns:
        Dict with note_title, old_line, new_line, and index.

    Raises:
        LookupError: If the list or item is not found.
    """
    title, note = _load_list(conn, list_title)
    if note is None:
        raise LookupError(f"no list titled '{title}'")
    items = parse_items(note["content_md"])
    it = match_item(items, item, ordinal)
    old_line = it["raw"]
    new_line = make_line(it)
    lines = note["content_md"].split("\n")
    lines[it["index"]] = new_line
    _write_list(conn, title, lines, source=source, version_note=version_note,
                conversation_id=conversation_id, location=location)
    return {"note_title": title, "old_line": old_line, "new_line": new_line, "index": it["index"]}


def set_item_checked(conn, list_title, item, checked, ordinal=None, **kw) -> dict:
    """Set the checked state of a checklist item.

    Args:
        conn: Database connection.
        list_title: List note title.
        item: Exact item text.
        checked: New checked state.
        ordinal: Optional 0-based index hint.
        **kw: Forwarded to _edit_one (source, conversation_id, location).

    Returns:
        Dict with note_title, old_line, new_line, and index.
    """
    return _edit_one(conn, list_title, item, ordinal,
                     lambda it: _format_item(it["indent"], checked, it["priority"], it["text"]),
                     version_note="checked item", **kw)


def set_item_priority(conn, list_title, item, priority, ordinal=None, **kw) -> dict:
    """Set the priority of a checklist item.

    Args:
        conn: Database connection.
        list_title: List note title.
        item: Exact item text.
        priority: New priority number, or None to clear.
        ordinal: Optional 0-based index hint.
        **kw: Forwarded to _edit_one (source, conversation_id, location).

    Returns:
        Dict with note_title, old_line, new_line, and index.
    """
    return _edit_one(conn, list_title, item, ordinal,
                     lambda it: _format_item(it["indent"], it["checked"], priority, it["text"]),
                     version_note="set priority", **kw)


def edit_item(conn, list_title, item, new_text, ordinal=None, **kw) -> dict:
    """Replace the text of a checklist item while preserving its checked state and priority.

    Args:
        conn: Database connection.
        list_title: List note title.
        item: Exact current item text.
        new_text: Replacement text.
        ordinal: Optional 0-based index hint.
        **kw: Forwarded to _edit_one (source, conversation_id, location).

    Returns:
        Dict with note_title, old_line, new_line, and index.
    """
    return _edit_one(conn, list_title, item, ordinal,
                     lambda it: _format_item(it["indent"], it["checked"], it["priority"], new_text),
                     version_note="edited item", **kw)


def remove_item(conn, list_title, item, ordinal=None, *, source="architect",
                conversation_id=None, location=None) -> dict:
    """Remove a checklist item by exact text, fail-closed.

    Args:
        conn: Database connection.
        list_title: List note title.
        item: Exact item text to remove.
        ordinal: Optional 0-based index hint.
        source: Write source tag.
        conversation_id: Optional conversation ID.
        location: Optional location dict.

    Returns:
        Dict with note_title, removed_line, and index.

    Raises:
        LookupError: If the list or item is not found.
    """
    title, note = _load_list(conn, list_title)
    if note is None:
        raise LookupError(f"no list titled '{title}'")
    items = parse_items(note["content_md"])
    it = match_item(items, item, ordinal)
    lines = note["content_md"].split("\n")
    removed = lines.pop(it["index"])
    _write_list(conn, title, lines, source=source, version_note="removed item",
                conversation_id=conversation_id, location=location)
    return {"note_title": title, "removed_line": removed, "index": it["index"]}


def add_sublist(conn, parent_list, child_name, items=None, *, source="architect",
                conversation_id=None, location=None) -> dict:
    """Create a child list under lists/<Parent>/<child> and link it from the parent.

    The link is added to the parent as a '[[lists/...]]' checklist line.

    Args:
        conn: Database connection.
        parent_list: Parent list title.
        child_name: Name of the child list to create.
        items: Optional initial item texts to populate the child list.
        source: Write source tag.
        conversation_id: Optional conversation ID.
        location: Optional location dict.

    Returns:
        Dict with parent_title, child_title, and parent_line.
    """
    parent = notes_svc.root_title(parent_list, "lists")
    child = notes_svc.root_title(f"{parent}/{child_name}", "lists")
    for it in (items or []):
        add_list_item(conn, child, it, source=source, conversation_id=conversation_id, location=location)
    if notes_svc.get_by_title(conn, child) is None:  # ensure an empty child exists
        notes_svc.upsert_note(conn, child, f"# {child.split('/')[-1]}\n", source=source,
                              kind="list", version_note="new sub-list")
    r = add_list_item(conn, parent, f"[[{child}]]", source=source,
                      conversation_id=conversation_id, location=location)
    return {"parent_title": parent, "child_title": child, "parent_line": r["line"]}


def append_log(
    conn, target: str, text: str, date: str | None = None, *,
    source: str = "architect", conversation_id: int | None = None, location=None,
) -> dict:
    """Append a dated bullet to a log/journal note, creating it if absent.

    Uses the local day (not UTC) so a late-evening log does not roll to tomorrow.

    Args:
        conn: Database connection.
        target: Note title of the log.
        text: Bullet text to append.
        date: ISO date string; defaults to today in the app timezone.
        source: Write source tag.
        conversation_id: Optional conversation ID.
        location: Optional location dict.

    Returns:
        Dict with note_title, block (the appended line), and created.
    """
    date = date or clock.today_iso()   # local day, not UTC (a late-evening log must not roll to tomorrow)
    note = notes_svc.get_by_title(conn, target)
    created = note is None
    body = note["content_md"] if note else f"# {target}\n"
    block = f"- **{date}** {text}"
    new_body = body.rstrip() + "\n" + block + "\n"
    notes_svc.upsert_note(
        conn, target, new_body, source=source,
        conversation_id=conversation_id, version_note="log entry", **_loc_kwargs(location),
    )
    return {"note_title": target, "block": block, "created": created}


# --- Undo helpers (used by the staging /undo endpoint) ----------------------

def remove_line_from_note(conn, title: str, line: str, *, source: str = "user") -> bool:
    """Remove the first exact occurrence of a line from a note.

    Args:
        conn: Database connection.
        title: Note title.
        line: Exact line string to remove.
        source: Write source tag.

    Returns:
        True if the line was found and removed; False if the note or line was not found.
    """
    note = notes_svc.get_by_title(conn, title)
    if not note:
        return False
    lines = note["content_md"].split("\n")
    try:
        lines.remove(line)
    except ValueError:
        return False
    notes_svc.upsert_note(conn, title, "\n".join(lines), source=source, version_note="undo")
    return True


def replace_line_in_note(conn, title: str, from_line: str, to_line: str, *, source: str = "user") -> bool:
    """Replace the first exact occurrence of from_line with to_line in a note. Fail-closed.

    Args:
        conn: Database connection.
        title: Note title.
        from_line: Exact line to replace.
        to_line: Replacement line.
        source: Write source tag.

    Returns:
        True if the replacement was made; False if the note or line was not found.
    """
    note = notes_svc.get_by_title(conn, title)
    if not note:
        return False
    lines = note["content_md"].split("\n")
    try:
        i = lines.index(from_line)
    except ValueError:
        return False
    lines[i] = to_line
    notes_svc.upsert_note(conn, title, "\n".join(lines), source=source, version_note="undo")
    return True


def insert_line_in_note(conn, title: str, index: int, line: str, *, source: str = "user") -> bool:
    """Re-insert a line at the given index (used to undo a remove).

    Args:
        conn: Database connection.
        title: Note title.
        index: 0-based position to insert the line at (clamped to valid range).
        line: Line string to insert.
        source: Write source tag.

    Returns:
        True if the insertion was made; False if the note was not found.
    """
    note = notes_svc.get_by_title(conn, title)
    if not note:
        return False
    lines = note["content_md"].split("\n")
    lines.insert(max(0, min(index, len(lines))), line)
    notes_svc.upsert_note(conn, title, "\n".join(lines), source=source, version_note="undo")
    return True
