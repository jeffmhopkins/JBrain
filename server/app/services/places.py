"""Place (geofence) ↔ loc/<name> note pairing.

A place is a named geofence row in `places`; its page is a `loc/<name>` note
(kind='place'). `ensure_note` materialises that note — creating it, restoring a
soft-deleted tombstone, or adopting an existing one — and links it back to the
place, so every saved place shows up in the Wiki "Places" tab, not just the Map
panel. Callers own the transaction: this never commits and raises on error so the
caller's rollback unwinds cleanly. Returns the note slug (or None if no such place).
"""
import re

from . import notes as notes_svc
from . import geo, entity_index


def _loc_title(name: str) -> str:
    return f"loc/{name.strip().strip('/')}"


def _note_by_slug(conn, slug: str):
    if not slug:
        return None
    return conn.execute(
        "SELECT id, slug, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()


def ensure_note(conn, place_id: int) -> str | None:
    """Create/find/restore the loc/<name> note backing this place and link it back."""
    place = conn.execute("SELECT name, note_slug FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        return None
    title = _loc_title(place["name"])
    note = (_note_by_slug(conn, place["note_slug"]) if place["note_slug"] else None) \
        or notes_svc.get_by_title(conn, title)
    if note is None:
        # A soft-deleted note still owns the (UNIQUE) title; restore it instead of
        # colliding — re-saving a place brings its prior content back.
        tomb = conn.execute(
            "SELECT id, slug FROM notes WHERE lower(title) = lower(?) AND deleted_at IS NOT NULL",
            (title,),
        ).fetchone()
        if tomb is not None:
            notes_svc.restore(conn, tomb["id"])
            note = tomb
        else:
            nid = notes_svc.upsert_note(conn, title, f"# {place['name']}\n\n", source="user",
                                        kind="place", fire_events=False)
            note = conn.execute("SELECT slug FROM notes WHERE id = ?", (nid,)).fetchone()
    conn.execute("UPDATE places SET note_slug = ? WHERE id = ?", (note["slug"], place_id))
    return note["slug"]


# ── Reconcile saved places (geo world) with kb/Places knowledge articles ──────────────────
# Deterministic, LINK-ONLY: add a location box (coords + reverse-geocoded address + a link to
# the loc/ note) to a kb/Places article, and a back-link on the loc/ note — so the same place
# doesn't fork into divergent, unlinked pages. Never rewrites geo data or article prose.

_BOX = "<!-- placeloc -->"          # marker for the location box on a kb/Places article
_BOX_RE = re.compile(r"(?m)^<!-- placeloc -->.*$")
_LOC = "<!-- kbplace -->"           # marker for the knowledge back-link on a loc/ note
_LOC_RE = re.compile(r"(?m)^<!-- kbplace -->.*$")


def geofence_for(conn, name: str) -> dict | None:
    """The saved geofence (a `places` row) that a place name refers to, or None. Matches by
    normalized name/alias first; else by COORDINATES — the place entity's coord-stamped
    mention-notes that fall inside a geofence vote for it (handles name drift like
    'the house' -> the saved 'Home')."""
    places = [dict(p) for p in conn.execute(
        "SELECT id, name, lat, lon, radius_m, note_slug FROM places").fetchall()]
    if not places or not (name or "").strip():
        return None
    target = entity_index.normalize(name)
    for p in places:                                   # 1. name / alias
        if entity_index.normalize(p["name"]) == target:
            return p
    nids = entity_index.note_ids_for_name(conn, name)   # 2. coordinate proximity
    if not nids:
        return None
    rows = conn.execute(
        f"SELECT lat, lon FROM notes WHERE id IN ({','.join('?' * len(nids))}) "
        "AND lat IS NOT NULL AND lon IS NOT NULL", nids).fetchall()
    votes: dict[int, int] = {}
    for r in rows:
        for p in places:
            if geo.haversine_km(r["lat"], r["lon"], p["lat"], p["lon"]) * 1000.0 <= p["radius_m"]:
                votes[p["id"]] = votes.get(p["id"], 0) + 1
                break
    if not votes:
        return None
    best = max(votes, key=votes.get)
    return next(p for p in places if p["id"] == best)


def _apply_box(conn, art_title: str, gf: dict, addr: dict | None) -> bool:
    """Ensure the kb/Places article carries a single marked location box (idempotent; replaces
    a stale one). Versioned. Returns True if the body changed."""
    row = conn.execute(
        "SELECT id, content_md FROM notes WHERE title=? AND deleted_at IS NULL AND kind='kb'",
        (art_title,)).fetchone()
    if not row:
        return False
    parts = [f"{_BOX} \U0001F4CD {gf['lat']:.5f}, {gf['lon']:.5f}"]
    if addr and addr.get("address"):
        parts.append(addr["address"])
    parts.append(f"saved place [[loc/{gf['name']}]]")
    line = " · ".join(parts)
    body = row["content_md"] or ""
    new = _BOX_RE.sub(line, body) if _BOX_RE.search(body) else (body.rstrip() + "\n\n" + line + "\n")
    if new == body:
        return False
    notes_svc.upsert_note(conn, art_title, new, note_id=row["id"], kind="kb",
                          source="placeloc", version_note="placeloc: geofence location box")
    return True


def _ensure_loc_link(conn, note_slug: str, art_title: str) -> None:
    """Add a marked back-link from the loc/ geofence note to its knowledge article (additive,
    idempotent, versioned). Never rewrites the owner's existing content."""
    row = conn.execute(
        "SELECT id, title, content_md, kind FROM notes WHERE slug=? AND deleted_at IS NULL",
        (note_slug,)).fetchone()
    if not row or row["kind"] != "place":
        return
    line = f"{_LOC} Knowledge: [[{art_title}]]"
    body = row["content_md"] or ""
    new = _LOC_RE.sub(line, body) if _LOC_RE.search(body) else (
        body.rstrip() + ("\n\n" if body.strip() else "") + line + "\n")
    if new != body:
        notes_svc.upsert_note(conn, row["title"], new, note_id=row["id"], kind="place",
                              source="placeloc", version_note="placeloc: link to knowledge article")


def link_places(conn) -> dict:
    """For each kb/Places article that maps to a saved geofence, add a location box (coords +
    reverse-geocoded address + a link to its loc/ note) and a back-link on the loc/ note.
    Deterministic, link-only, idempotent, cached. Returns {checked, linked}."""
    from . import geocode
    arts = conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title LIKE 'kb/Places/%'"
    ).fetchall()
    checked = linked = 0
    for a in arts:
        checked += 1
        gf = geofence_for(conn, a["title"].split("/")[-1])
        if not gf:
            continue
        addr = geocode.reverse(conn, gf["lat"], gf["lon"]) if geocode.enabled() else None
        if _apply_box(conn, a["title"], gf, addr):
            linked += 1
        if gf["note_slug"]:
            _ensure_loc_link(conn, gf["note_slug"], a["title"])
    conn.commit()
    return {"checked": checked, "linked": linked}
