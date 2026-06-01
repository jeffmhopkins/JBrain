"""Additive quick-task operations (Phase 2).

These are the ONLY writes the architect performs without confirmation, and they
are *structurally* additive — append a line, create-if-missing, or capture to
the inbox — so the worst case is an extra line, fully reversible via Undo. There
is no destructive auto-apply tool, by design (see ROADMAP for complete/remove,
which stay confirm-gated and need fail-closed matching).
"""
from __future__ import annotations

from datetime import datetime

from . import notes as notes_svc


def _loc_kwargs(location) -> dict:
    if not location:
        return {}
    return {"lat": location["lat"], "lon": location["lon"], "location_label": location["location_label"]}


def add_list_item(
    conn, list_title: str, item: str, checkbox: bool = True, *,
    source: str = "architect", conversation_id: int | None = None, location=None,
) -> dict:
    """Append an item to a checklist note, creating the list if absent. Lists are
    their own layer: titled under the "lists/" root with kind='list', so they're
    kept apart from notes and skipped by wiki-synthesis."""
    title = notes_svc.root_title(list_title, "lists")
    note = notes_svc.get_by_title(conn, title)
    created = note is None
    body = note["content_md"] if note else f"# {title.split('/')[-1]}\n"
    line = f"- [ ] {item}" if checkbox else f"- {item}"
    new_body = body.rstrip() + "\n" + line + "\n"
    notes_svc.upsert_note(
        conn, title, new_body, source=source, kind="list",
        conversation_id=conversation_id, version_note="added list item", **_loc_kwargs(location),
    )
    return {"note_title": title, "line": line, "created": created}


def append_log(
    conn, target: str, text: str, date: str | None = None, *,
    source: str = "architect", conversation_id: int | None = None, location=None,
) -> dict:
    """Append a dated bullet to a log/journal note, creating it if absent."""
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
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


def capture_inbox(conn, content: str, source_label: str = "architect-capture") -> int:
    cur = conn.execute(
        "INSERT INTO inbox (source, content) VALUES (?, ?)", (source_label, content.strip())
    )
    return cur.lastrowid


def mark_inbox_processed(conn, ids: list[int]) -> None:
    conn.executemany("UPDATE inbox SET processed = 1 WHERE id = ?", [(i,) for i in ids])


# --- Undo helpers (used by the staging /undo endpoint) ----------------------

def remove_line_from_note(conn, title: str, line: str, *, source: str = "user") -> bool:
    """Remove the first exact occurrence of `line` from a note. Returns success."""
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
