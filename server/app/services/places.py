"""Place (geofence) ↔ loc/<name> note pairing.

A place is a named geofence row in `places`; its page is a `loc/<name>` note
(kind='place'). `ensure_note` materialises that note — creating it, restoring a
soft-deleted tombstone, or adopting an existing one — and links it back to the
place, so every saved place shows up in the Wiki "Places" tab, not just the Map
panel. Callers own the transaction: this never commits and raises on error so the
caller's rollback unwinds cleanly. Returns the note slug (or None if no such place).
"""
import json
import re

from . import notes as notes_svc
from . import geo, entity_index


def _loc_title(name: str) -> str:
    """Return the canonical 'loc/<name>' note title for a place name.

    Args:
        name: Place name.

    Returns:
        Title string in 'loc/<name>' form.
    """
    return f"loc/{name.strip().strip('/')}"


def _note_by_slug(conn, slug: str):
    """Return a non-deleted note row by slug, or None.

    Args:
        conn: Database connection.
        slug: Note slug to look up.

    Returns:
        Note row with id, slug, and content_md, or None.
    """
    if not slug:
        return None
    return conn.execute(
        "SELECT id, slug, content_md FROM notes WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()


def ensure_note(conn, place_id: int) -> str | None:
    """Create, find, or restore the loc/<name> note backing a place and link it back.

    Callers own the transaction: this function never commits and raises on error so
    the caller's rollback unwinds cleanly.

    Args:
        conn: Database connection.
        place_id: ID of the place whose note should be ensured.

    Returns:
        Note slug string, or None if no such place exists.
    """
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
    """Return the saved geofence row that a place name refers to, or None.

    Matches by normalised name/alias first; else by coordinates — coord-stamped
    mention-notes for the entity that fall inside a geofence vote for it, handling
    name drift like 'the house' -> the saved 'Home'.

    Args:
        conn: Database connection.
        name: Place name or alias to resolve.

    Returns:
        Places row dict, or None if no geofence matches.
    """
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
    """Ensure a kb/Places article carries exactly one marked location box.

    Idempotent: replaces a stale box if present. Versioned.

    Args:
        conn: Database connection.
        art_title: Title of the kb/Places knowledge article.
        gf: Geofence row dict with lat, lon, and name.
        addr: Optional reverse-geocode result dict; may be None.

    Returns:
        True if the article body was changed.
    """
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
    """Add a marked back-link from the loc/ note to its knowledge article.

    Additive, idempotent, and versioned. Never rewrites the owner's existing content.

    Args:
        conn: Database connection.
        note_slug: Slug of the loc/ geofence note.
        art_title: Title of the corresponding knowledge article to link back to.
    """
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
    """Link each kb/Places article to its saved geofence and back.

    For each kb/Places article that maps to a saved geofence, adds a location box
    (coords + reverse-geocoded address + a link to its loc/ note) and a back-link
    on the loc/ note. Also de-forks: when two or more articles map to the same
    geofence they represent the same place, so a merge hint is recorded.
    Deterministic, link-only, idempotent, and cached.

    Args:
        conn: Database connection.

    Returns:
        Dict with keys checked, linked, and merge_hints.
    """
    from . import geocode, article_talk
    arts = conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND title LIKE 'kb/Places/%'"
    ).fetchall()
    checked = linked = 0
    by_gf: dict[int, list] = {}
    for a in arts:
        checked += 1
        gf = geofence_for(conn, a["title"].split("/")[-1])
        if not gf:
            continue
        by_gf.setdefault(gf["id"], []).append((a["title"], gf))
        addr = geocode.reverse(conn, gf["lat"], gf["lon"]) if geocode.enabled() else None
        if _apply_box(conn, a["title"], gf, addr):
            linked += 1
        if gf["note_slug"]:
            _ensure_loc_link(conn, gf["note_slug"], a["title"])
    # De-fork: two+ differently-named articles for one saved place → a merge hint (a logged
    # 'restructure' item — deduped, never nags) targeting the geofence-named article.
    merge_hints = 0
    for lst in by_gf.values():
        if len(lst) < 2:
            continue
        gf = lst[0][1]
        titles = [t for t, _ in lst]
        canon = next((t for t in titles
                      if entity_index.normalize(t.split("/")[-1]) == entity_index.normalize(gf["name"])),
                     titles[0])
        for t in titles:
            if t == canon:
                continue
            body = json.dumps({"op": "merge", "target": canon,
                               "rationale": f"same saved place “{gf['name']}” as {canon}"})
            article_talk.record(conn, t, [{"kind": "restructure", "body": body}], author="ai")
            merge_hints += 1
    conn.commit()
    return {"checked": checked, "linked": linked, "merge_hints": merge_hints}


def unsaved_places(conn) -> list[str]:
    """Return kb/Places article titles with no matching saved geofence.

    These are candidates the owner could 'save as a place' for trail tracking.
    Cheap: no geocoding.

    Args:
        conn: Database connection.

    Returns:
        List of article title strings.
    """
    return [a["title"] for a in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND title LIKE 'kb/Places/%' ORDER BY title").fetchall()
        if not geofence_for(conn, a["title"].split("/")[-1])]


def create_place(conn, name: str, lat: float, lon: float, radius_m: int = 150) -> dict:
    """Create a saved geofence and its loc/ note, or return an existing place of the same name.

    The caller is responsible for committing.

    Args:
        conn: Database connection.
        name: Place name (trimmed to 80 characters).
        lat: Latitude of the geofence centre.
        lon: Longitude of the geofence centre.
        radius_m: Geofence radius in metres (clamped to 20–20000).

    Returns:
        Dict with keys id, name, note_slug, and existing (True if already existed).
    """
    name = (name or "").strip()[:80]
    existing = conn.execute("SELECT id FROM places WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        return {"id": existing["id"], "name": name, "note_slug": ensure_note(conn, existing["id"]),
                "existing": True}
    pid = conn.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES (?,?,?,?)",
                       (name, float(lat), float(lon), max(20, min(int(radius_m), 20000)))).lastrowid
    return {"id": pid, "name": name, "note_slug": ensure_note(conn, pid), "existing": False}
