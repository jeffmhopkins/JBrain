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


# --- Display-label audit -----------------------------------------------------
# A [[Target|Display]] link is meant to show a shortened label for the SAME
# article (e.g. [[kb/People/Jeff Hopkins|Jeff Hopkins]]). A class of bug — most
# often minted when a note is renamed/merged and the inbound-link rewrite keeps
# the old |Display verbatim — leaves the label naming a DIFFERENT article than
# the target (e.g. [[kb/People/Summer E. Hopkins|Jeff Hopkins]]). This audit
# finds those high-confidence mismatches and corrects the label.

# Capture target + optional display (the links table doesn't store the display).
WIKILINK_FULL_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_ROOTS = ("notes", "kb", "lists", "logs")


def _norm(s: str | None) -> str:
    return (s or "").strip()


def _last_segment(title: str) -> str:
    """The bare leaf name — the final '/'-separated segment ('kb/People/Jeff' → 'Jeff')."""
    return _norm(title).split("/")[-1].strip()


def _parent(title: str) -> str:
    """Everything above the leaf ('kb/People/Jeff' → 'kb/People')."""
    parts = [p.strip() for p in _norm(title).split("/")]
    return "/".join(parts[:-1])


def _root_leaf(title: str) -> str:
    """Mirror the PWA's leaf(): strip a single leading root folder ('kb/Jeff' → 'Jeff')."""
    t = _norm(title)
    for r in _ROOTS:
        if t.lower().startswith(r + "/"):
            return t[len(r) + 1:]
    return t


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _iter_display_links(content_md: str):
    """Yield (raw, target, display) for every [[Target|Display]] with an explicit display."""
    for m in WIKILINK_FULL_RE.finditer(content_md or ""):
        target, display = _norm(m.group(1)), m.group(2)
        if display is None:
            continue
        display = _norm(display)
        if not target or not display or "\n" in target or "\n" in display:
            continue
        yield m.group(0), target, display


def _desired_label(target: str, target_title: str) -> str:
    """The corrected link: keep an explicit bare-name label unless dropping the alias
    would already render it (target has no folder depth past its root)."""
    seg = _last_segment(target_title)
    return f"[[{target}]]" if _root_leaf(target_title).lower() == seg.lower() else f"[[{target}|{seg}]]"


def audit_display_mismatches(conn) -> list[dict]:
    """Scan every live note for [[Target|Display]] links whose label names a different
    article than the target. High-confidence only: the label resolves to a different
    note in the SAME parent folder, or equals a former title of the target."""
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

    findings: list[dict] = []
    for n in notes:
        for raw, target, display in _iter_display_links(n["content_md"]):
            tgt = by_title.get(target.lower())
            if not tgt:
                continue  # dangling/unresolved target — out of scope
            tseg, tleaf = _last_segment(tgt["title"]), _root_leaf(tgt["title"])
            dlow = display.lower()
            # Canonical label, or a legit shortening/expansion of the SAME name → fine.
            if dlow in (tgt["title"].lower(), tleaf.lower(), tseg.lower()):
                continue
            dt, st = _tokens(display), _tokens(tseg)
            if dt and (dt <= st or st <= dt):
                continue

            reason, resolved = None, None
            if dlow in former.get(tgt["id"], set()):
                reason = "stale rename alias — the label is a former title of the target"
            else:
                cand = by_title.get(dlow)
                if cand is None:
                    seg_hits = by_seg.get(dlow, [])
                    cand = seg_hits[0] if len(seg_hits) == 1 else None
                if (cand and cand["id"] != tgt["id"]
                        and _parent(cand["title"]).lower() == _parent(tgt["title"]).lower()):
                    reason = "the label names a different article in the same folder"
                    resolved = cand
            if not reason:
                continue

            findings.append({
                "source_id": n["id"], "source_title": n["title"], "source_slug": n["slug"],
                "raw": raw, "target": target, "target_title": tgt["title"], "target_slug": tgt["slug"],
                "display": display, "desired_display": tseg, "fixed": _desired_label(target, tgt["title"]),
                "resolved_title": resolved["title"] if resolved else None,
                "resolved_slug": resolved["slug"] if resolved else None,
                "reason": reason,
            })
    return findings


def fix_note_link(conn, note_id: int, target: str, display: str) -> bool:
    """Correct one [[Target|Display]] in a note: re-derive the right label from the live
    target and rewrite the matching link. Versioned/undoable. Returns whether it changed."""
    from . import notes as notes_svc  # lazy — notes imports this module

    row = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (note_id,)
    ).fetchone()
    if not row:
        return False
    tgt = conn.execute(
        "SELECT title FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NULL", (target,)
    ).fetchone()
    if not tgt:
        return False
    fixed = _desired_label(target, tgt["title"])

    changed = False

    def repl(m):
        nonlocal changed
        if (_norm(m.group(1)).lower() == target.lower()
                and m.group(2) is not None and _norm(m.group(2)) == display):
            changed = True
            return fixed
        return m.group(0)

    new_content = WIKILINK_FULL_RE.sub(repl, row["content_md"])
    if not changed or new_content == row["content_md"]:
        return False
    notes_svc.upsert_note(conn, row["title"], new_content, note_id=note_id,
                          source="link-audit", version_note="link label fix", fire_events=False)
    return True

