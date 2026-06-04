"""Canonical entity index — aggregated from the note_analysis entities.

A DERIVED index (like FTS): one row per real-world person/org/place/thing, with name
variants merged conservatively. Merge rule: same type, and one name's token set is a
subset of the other's AND they share a distinctive (non-initial) token — so
"Summer Hopkins" folds into "Summer E. Hopkins", but "John Smith" and "John Doe" don't.

It feeds the KB outline (recurring entities + co-occurrence drive the article set and
Groups/household clustering) and a browse view. Upserted by (type, normalized_key) so
entity ids stay stable across rebuilds.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "mx", "rev", "fr", "the"}
_NONWORD = re.compile(r"[^a-z0-9]+")
_TYPE_LABELS = [("person", "People"), ("org", "Organizations"), ("place", "Places"),
                ("thing", "Things"), ("condition", "Conditions"), ("medication", "Medications"),
                ("procedure", "Procedures"), ("event", "Events"), ("concept", "Concepts")]


def _tokens(name: str) -> list[str]:
    parts = _NONWORD.sub(" ", (name or "").lower()).split()
    return [p for p in parts if p and p not in _TITLES]


def normalize(name: str) -> str:
    """A stable merge key: lowercased, title-words/punctuation stripped, tokens joined."""
    return " ".join(_tokens(name))


def _distinctive(tok: str) -> bool:
    return len(tok) >= 3            # a real word (likely a surname), not an initial


def _collect(conn, limit: int) -> dict:
    """type -> {norm -> {tokens, count, notes:set, raws:Counter}} from note_analysis."""
    rows = conn.execute(
        "SELECT a.note_id, a.entities_json FROM note_analysis a "
        "JOIN notes n ON n.id = a.note_id "
        "WHERE n.deleted_at IS NULL AND n.kind IN ('entry','daily') "
        "ORDER BY a.note_id LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    clusters: dict = defaultdict(lambda: defaultdict(
        lambda: {"tokens": frozenset(), "count": 0, "notes": set(), "raws": Counter()}))
    for r in rows:
        try:
            ents = json.loads(r["entities_json"] or "[]")
        except Exception:  # noqa: BLE001
            continue
        seen = set()
        for e in ents:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()
            typ = (str(e.get("type") or "").strip().lower() or "thing")
            norm = normalize(name)
            if not norm or (typ, norm) in seen:
                continue
            seen.add((typ, norm))
            c = clusters[typ][norm]
            c["tokens"] = frozenset(_tokens(name))
            c["count"] += 1
            c["notes"].add(r["note_id"])
            c["raws"][name] += 1
    return clusters


def _merge_map(clusters: dict) -> dict:
    """type -> {norm -> canonical_norm}, unioning subset-name variants that share a
    distinctive token. Blocked by token so we don't compare every pair."""
    out: dict = defaultdict(dict)
    for typ, norms in clusters.items():
        parent = {n: n for n in norms}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        by_token: dict = defaultdict(list)
        for n in norms:
            for t in clusters[typ][n]["tokens"]:
                if _distinctive(t):
                    by_token[t].append(n)
        for group in by_token.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    ta, tb = clusters[typ][a]["tokens"], clusters[typ][b]["tokens"]
                    if ta <= tb or tb <= ta:          # one name subsumes the other
                        ra, rb = find(a), find(b)
                        if ra != rb:
                            parent[rb] = ra
        members: dict = defaultdict(list)
        for n in norms:
            members[find(n)].append(n)
        for grp in members.values():
            canon = max(grp, key=lambda m: (clusters[typ][m]["count"], len(clusters[typ][m]["tokens"])))
            for m in grp:
                out[typ][m] = canon
    return out


def rebuild(conn, limit: int = 20000) -> int:
    """(Re)aggregate the entity index from note_analysis. Upserts by (type, key) so ids
    are stable, replaces each entity's mentions, prunes entities that vanished, and links
    each to its kb article (if one exists). Returns the entity count."""
    clusters = _collect(conn, limit)
    mapping = _merge_map(clusters)

    canon: dict = defaultdict(lambda: {"notes": set(), "raws": Counter()})
    for typ, norms in clusters.items():
        for norm, c in norms.items():
            cn = mapping[typ].get(norm, norm)
            agg = canon[(typ, cn)]
            agg["notes"] |= c["notes"]
            agg["raws"].update(c["raws"])

    seen_keys = set()
    for (typ, cn), agg in canon.items():
        display = agg["raws"].most_common(1)[0][0]
        notes = agg["notes"]
        seen_keys.add((typ, cn))
        conn.execute(
            "INSERT INTO entities (type, canonical_name, normalized_key, note_count, updated_at) "
            "VALUES (?,?,?,?, datetime('now')) "
            "ON CONFLICT(type, normalized_key) DO UPDATE SET "
            "canonical_name=excluded.canonical_name, note_count=excluded.note_count, updated_at=excluded.updated_at",
            (typ, display, cn, len(notes)),
        )
        eid = conn.execute("SELECT id FROM entities WHERE type=? AND normalized_key=?", (typ, cn)).fetchone()["id"]
        conn.execute("DELETE FROM entity_mentions WHERE entity_id=?", (eid,))
        conn.executemany("INSERT OR IGNORE INTO entity_mentions (entity_id, note_id) VALUES (?,?)",
                         [(eid, nid) for nid in notes])

    for r in conn.execute("SELECT id, type, normalized_key FROM entities").fetchall():
        if (r["type"], r["normalized_key"]) not in seen_keys:
            conn.execute("DELETE FROM entities WHERE id=?", (r["id"],))   # mentions cascade

    _link_articles(conn)
    conn.commit()
    return len(seen_keys)


def _link_articles(conn) -> None:
    """Point each entity at the kb article whose title leaf matches its canonical name."""
    conn.execute("UPDATE entities SET article_title = NULL")
    leaf_map: dict = {}
    for k in conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL").fetchall():
        leaf_map.setdefault(k["title"].split("/")[-1].lower(), k["title"])
    for e in conn.execute("SELECT id, canonical_name FROM entities").fetchall():
        t = leaf_map.get(e["canonical_name"].lower())
        if t:
            conn.execute("UPDATE entities SET article_title=? WHERE id=?", (t, e["id"]))


def _partners(conn, entity_id: int, k: int) -> list[str]:
    rows = conn.execute(
        "SELECT e.canonical_name AS name, COUNT(*) AS c FROM entity_mentions m1 "
        "JOIN entity_mentions m2 ON m2.note_id = m1.note_id AND m2.entity_id != m1.entity_id "
        "JOIN entities e ON e.id = m2.entity_id "
        "WHERE m1.entity_id = ? GROUP BY m2.entity_id ORDER BY c DESC, name LIMIT ?",
        (entity_id, k),
    ).fetchall()
    return [r["name"] for r in rows]


def roster(conn, per_type: int = 40, partners: int = 3) -> str:
    """A compact roster of recurring entities (by type, with co-occurring partners) for
    the outline prompt. The ubiquitous owner entity (in nearly every note) is dropped so
    it doesn't drown the co-occurrence signal."""
    total = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE deleted_at IS NULL AND kind IN ('entry','daily')"
    ).fetchone()["c"]
    ceiling = int(total * 0.6) if total > 20 else total + 1
    lines: list[str] = []
    for typ, label in _TYPE_LABELS:
        rows = conn.execute(
            "SELECT id, canonical_name, note_count FROM entities "
            "WHERE type=? AND note_count < ? ORDER BY note_count DESC, canonical_name LIMIT ?",
            (typ, ceiling, per_type),
        ).fetchall()
        if not rows:
            continue
        lines.append(f"{label}:")
        for r in rows:
            ps = _partners(conn, r["id"], partners)
            tail = f"; often with: {', '.join(ps)}" if ps else ""
            lines.append(f"- {r['canonical_name']} ({r['note_count']} notes{tail})")
    return "\n".join(lines)


def index(conn, type: str | None = None, q: str | None = None, limit: int = 500) -> list[dict]:
    sql = ("SELECT id, type, canonical_name, note_count, article_title FROM entities WHERE 1=1")
    args: list = []
    if type:
        sql += " AND type = ?"; args.append(type)
    if q:
        sql += " AND canonical_name LIKE ?"; args.append(f"%{q}%")
    sql += " ORDER BY note_count DESC, canonical_name LIMIT ?"; args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def notes_for(conn, entity_id: int) -> dict | None:
    """An entity plus the notes that mention it (for the browse view)."""
    e = conn.execute(
        "SELECT id, type, canonical_name, note_count, article_title FROM entities WHERE id=?",
        (entity_id,),
    ).fetchone()
    if not e:
        return None
    notes = conn.execute(
        "SELECT n.id, n.title, n.slug, n.created_at FROM entity_mentions m "
        "JOIN notes n ON n.id = m.note_id AND n.deleted_at IS NULL "
        "WHERE m.entity_id = ? ORDER BY n.created_at DESC",
        (entity_id,),
    ).fetchall()
    return {**dict(e), "notes": [dict(n) for n in notes]}


def note_ids_for_name(conn, name: str) -> list[int]:
    """Note ids for the canonical entity matching a name (any type), best match first."""
    norm = normalize(name)
    if not norm:
        return []
    row = conn.execute(
        "SELECT id FROM entities WHERE normalized_key=? ORDER BY note_count DESC LIMIT 1", (norm,)
    ).fetchone()
    if not row:
        return []
    return [m["note_id"] for m in conn.execute(
        "SELECT note_id FROM entity_mentions WHERE entity_id=?", (row["id"],)).fetchall()]
