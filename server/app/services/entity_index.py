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

import hashlib
import json
import re
from collections import Counter, defaultdict

from . import embeddings, people

_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "mx", "rev", "fr", "the"}
_NONWORD = re.compile(r"[^a-z0-9]+")
_TYPE_LABELS = [("person", "People"), ("animal", "Animals"), ("org", "Organizations"),
                ("place", "Places"), ("thing", "Things"), ("work", "Media"),
                ("condition", "Conditions"), ("medication", "Medications"),
                ("procedure", "Procedures"), ("event", "Events"), ("concept", "Concepts")]
# Person mentions that mean "the note-taker" rather than a distinct individual. They are
# folded into the owner's NAMED entity so a stray "Owner"/"the owner"/"me"/"I" never forks
# from e.g. "Jeff". ("the owner" normalizes to "owner" — "the" is a stripped title word.)
_OWNER_ALIAS_NORMS = {"owner", "me", "myself", "i", "self", "narrator"}


def _tokens(name: str) -> list[str]:
    parts = _NONWORD.sub(" ", (name or "").lower()).split()
    return [p for p in parts if p and p not in _TITLES]


def normalize(name: str) -> str:
    """A stable merge key: lowercased, title-words/punctuation stripped, tokens joined."""
    return " ".join(_tokens(name))


def _distinctive(tok: str) -> bool:
    return len(tok) >= 3            # a real word (likely a surname), not an initial


def _acronymish(norm: str) -> bool:
    return " " not in norm and 2 <= len(norm) <= 6 and norm.isalpha()


def _initials(name: str) -> str:
    return "".join(t[0] for t in _tokens(name) if t)


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
        # Acronym union: "ttp" ↔ "thrombotic thrombocytopenic purpura" (initials match).
        def _rep(n):
            raws = clusters[typ][n]["raws"]
            return raws.most_common(1)[0][0] if raws else n
        expansions = [n for n in norms if " " in n]
        for ac in norms:
            if not _acronymish(ac):
                continue
            for ex in expansions:
                if _initials(_rep(ex)) == ac:
                    ra, rb = find(ac), find(ex)
                    if ra != rb:
                        parent[rb] = ra
                    break
        members: dict = defaultdict(list)
        for n in norms:
            members[find(n)].append(n)
        for grp in members.values():
            # Canonical = the most descriptive name (most tokens), then most-mentioned.
            canon = max(grp, key=lambda m: (len(clusters[typ][m]["tokens"]), clusters[typ][m]["count"]))
            for m in grp:
                out[typ][m] = canon
    return out


def _fold_owner(conn, clusters: dict, mapping: dict) -> None:
    """Merge the owner's self-references into their NAMED person entity.

    The owner is the note-taker, so first-person facts get extracted under the owner's
    name (once they've set one) but stray mentions like "the owner"/"Owner"/"me"/"I" also
    appear. This points those placeholder person-clusters at the owner's real-name cluster
    so the index holds ONE owner entity (displayed under the real name, the placeholders
    becoming its aliases). No-op until the owner has set a real name AND it actually shows
    up as a person entity — re-analysis is what makes first-person resolve to that name."""
    o = people.owner(conn)
    real = normalize(o["name"]) if o else ""
    if not real or real in _OWNER_ALIAS_NORMS:
        return
    person = clusters.get("person", {})
    if real not in person:
        return
    pm = mapping["person"]
    canon = pm.get(real, real)
    for n in list(person):
        if n != real and (n in _OWNER_ALIAS_NORMS or pm.get(n, n) in _OWNER_ALIAS_NORMS):
            pm[n] = canon


def rebuild(conn, limit: int = 20000) -> int:
    """(Re)aggregate the entity index from note_analysis. Upserts by (type, key) so ids
    are stable, replaces each entity's mentions, prunes entities that vanished, and links
    each to its kb article (if one exists). Returns the entity count."""
    clusters = _collect(conn, limit)
    mapping = _merge_map(clusters)
    _fold_owner(conn, clusters, mapping)

    canon: dict = defaultdict(lambda: {"notes": set(), "raws": Counter(), "aliases": {}, "display": None})
    for typ, norms in clusters.items():
        for norm, c in norms.items():
            cn = mapping[typ].get(norm, norm)
            agg = canon[(typ, cn)]
            agg["notes"] |= c["notes"]
            agg["raws"].update(c["raws"])
            top = c["raws"].most_common(1)[0][0] if c["raws"] else norm
            if norm == cn:                          # the canonical cluster sets the display name
                agg["display"] = top
            else:                                   # a merged variant → an alias
                agg["aliases"][norm] = top

    seen_keys = set()
    for (typ, cn), agg in canon.items():
        display = agg["display"] or agg["raws"].most_common(1)[0][0]
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
        conn.execute("DELETE FROM entity_aliases WHERE entity_id=?", (eid,))
        conn.executemany("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_norm, alias_display) VALUES (?,?,?)",
                         [(eid, an, ad) for an, ad in agg["aliases"].items()])

    for r in conn.execute("SELECT id, type, normalized_key FROM entities").fetchall():
        if (r["type"], r["normalized_key"]) not in seen_keys:
            embeddings.delete_entity_embedding(conn, r["id"])
            conn.execute("DELETE FROM entities WHERE id=?", (r["id"],))   # mentions cascade

    _link_articles(conn)
    _sync_embeddings(conn)
    conn.commit()
    return len(seen_keys)


_LABEL = dict(_TYPE_LABELS)


def _entity_embed_text(name: str, typ: str, aliases: list[str], lead: str) -> str:
    """The string an entity is embedded from: its name, human type, aliases, and (when it
    has one) its KB article's lead sentence — so a descriptive query can reach it."""
    parts = [name]
    label = _LABEL.get(typ, typ)
    if label:
        parts.append(f"({label})")
    if aliases:
        parts.append("a.k.a. " + ", ".join(aliases[:4]))
    if lead:
        parts.append(lead)
    return " — ".join(parts)[:500]


def _article_leads(conn) -> dict:
    """{kb article title -> its lead sentence} (first non-heading line), for embed context."""
    out: dict = {}
    for a in conn.execute(
        "SELECT title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL"
    ).fetchall():
        for line in (a["content_md"] or "").splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                out[a["title"]] = s[:200]
                break
    return out


def _sync_embeddings(conn) -> None:
    """Refresh each entity's semantic vector, skipping entities whose embed text is
    unchanged (entities.embed_hash). Batched; only changed entities hit the embedder."""
    leads = _article_leads(conn)
    aliases: dict = {}
    for a in conn.execute(
        "SELECT entity_id, alias_display FROM entity_aliases WHERE alias_display IS NOT NULL"
    ).fetchall():
        aliases.setdefault(a["entity_id"], []).append(a["alias_display"])
    pending = []   # (id, hash)
    texts = []
    for r in conn.execute(
        "SELECT id, type, canonical_name, article_title, embed_hash FROM entities"
    ).fetchall():
        text = _entity_embed_text(r["canonical_name"], r["type"], aliases.get(r["id"], []),
                                  leads.get(r["article_title"] or "", ""))
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if h != (r["embed_hash"] or ""):
            pending.append((r["id"], h))
            texts.append(text)
    if not pending:
        return
    vecs = embeddings.embed_many(texts)
    for (eid, h), vec in zip(pending, vecs):
        embeddings.store_entity_vector(conn, eid, vec)
        conn.execute("UPDATE entities SET embed_hash=? WHERE id=?", (h, eid))


def _link_articles(conn) -> None:
    """Point each entity at the kb article whose title leaf matches the entity — by the SAME
    robust basis as note_ids_for_name (normalized key OR any alias), not a raw-string leaf.
    A leaf-exact match missed common variants (entity 'Thrombotic Thrombocytopenic Purpura'
    vs article leaf 'TTP', or aliased/merged names), silently leaving article_title NULL and
    breaking incremental routing, disambiguation, and the browse link."""
    from . import wiki_guides
    conn.execute("UPDATE entities SET article_title = NULL")
    leaf_map: dict = {}      # normalized article leaf -> full title (first wins)
    for k in conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL").fetchall():
        # A private-domain page (kb/Health/<Person>, kb/Finance/People/<Person>) can share its leaf
        # with the person's kb/People/<Person> page. It is a PII satellite, NEVER an entity's
        # canonical article — excluding it keeps the person entity bound to their People page (the
        # SELECT has no ORDER BY, so a collision would otherwise be nondeterministic and silently
        # mis-route facts into the private page).
        if wiki_guides.is_private_title(k["title"]):
            continue
        leaf_map.setdefault(normalize(k["title"].split("/")[-1]), k["title"])
    aliases: dict = {}       # entity_id -> [alias_norm, ...]
    for a in conn.execute("SELECT entity_id, alias_norm FROM entity_aliases").fetchall():
        aliases.setdefault(a["entity_id"], []).append(a["alias_norm"])
    for e in conn.execute("SELECT id, normalized_key FROM entities").fetchall():
        for key in [e["normalized_key"], *aliases.get(e["id"], [])]:
            t = leaf_map.get(key)
            if t:
                conn.execute("UPDATE entities SET article_title=? WHERE id=?", (t, e["id"]))
                break


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
            aka = [a["alias_display"] for a in conn.execute(
                "SELECT alias_display FROM entity_aliases WHERE entity_id=? AND alias_display IS NOT NULL LIMIT 3",
                (r["id"],)).fetchall()]
            name = r["canonical_name"] + (f" (a.k.a. {', '.join(aka)})" if aka else "")
            tail = f"; often with: {', '.join(ps)}" if ps else ""
            lines.append(f"- {name} ({r['note_count']} notes{tail})")
    return "\n".join(lines)


def index(conn, type: str | None = None, q: str | None = None, limit: int = 500) -> list[dict]:
    sql = ("SELECT id, type, canonical_name, note_count, article_title FROM entities WHERE 1=1")
    args: list = []
    if type:
        sql += " AND type = ?"; args.append(type)
    if q:
        sql += (" AND (canonical_name LIKE ? OR id IN "
                "(SELECT entity_id FROM entity_aliases WHERE alias_display LIKE ? OR alias_norm LIKE ?))")
        args += [f"%{q}%", f"%{q}%", f"%{normalize(q)}%"]
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
    aliases = [a["alias_display"] for a in conn.execute(
        "SELECT alias_display FROM entity_aliases WHERE entity_id=? AND alias_display IS NOT NULL", (entity_id,)).fetchall()]
    return {**dict(e), "aliases": aliases, "notes": [dict(n) for n in notes]}


def note_ids_for_name(conn, name: str) -> list[int]:
    """Note ids for the canonical entity matching a name OR alias (any type)."""
    norm = normalize(name)
    if not norm:
        return []
    row = conn.execute(
        "SELECT id FROM entities WHERE normalized_key=? ORDER BY note_count DESC LIMIT 1", (norm,)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT entity_id AS id FROM entity_aliases WHERE alias_norm=? LIMIT 1", (norm,)).fetchone()
    if not row:
        return []
    return [m["note_id"] for m in conn.execute(
        "SELECT note_id FROM entity_mentions WHERE entity_id=?", (row["id"],)).fetchall()]


def ambiguous_terms(conn) -> list[dict]:
    """Terms (canonical keys or aliases) that map to ≥2 distinct entities — disambiguation
    candidates. Returns [{term, entities:[{id,type,canonical_name,article_title}]}]."""
    rows = conn.execute(
        "SELECT term, COUNT(DISTINCT entity_id) c FROM "
        "(SELECT normalized_key AS term, id AS entity_id FROM entities "
        " UNION ALL SELECT alias_norm AS term, entity_id FROM entity_aliases) "
        "GROUP BY term HAVING c >= 2"
    ).fetchall()
    out = []
    for r in rows:
        ents = conn.execute(
            "SELECT id, type, canonical_name, article_title FROM entities WHERE normalized_key=? "
            "UNION SELECT e.id, e.type, e.canonical_name, e.article_title FROM entities e "
            "JOIN entity_aliases a ON a.entity_id=e.id WHERE a.alias_norm=?",
            (r["term"], r["term"]),
        ).fetchall()
        out.append({"term": r["term"], "entities": [dict(e) for e in ents]})
    return out


def write_disambiguation_pages(conn) -> int:
    """Generate protected kb/_disambig/<Term> pages for terms that map to ≥2 entities
    which each have an article. Regenerated each call (old ones cleared first)."""
    from . import notes as notes_svc
    for r in conn.execute(
        "SELECT id FROM notes WHERE kind='kb' AND title LIKE 'kb/_disambig/%' AND deleted_at IS NULL"
    ).fetchall():
        notes_svc.soft_delete(conn, r["id"])
    n = 0
    for amb in ambiguous_terms(conn):
        with_art = [e for e in amb["entities"] if e["article_title"]]
        if len(with_art) < 2:
            continue
        displays = [e["canonical_name"] for e in amb["entities"]]
        term = min(displays, key=len)              # the short/acronym form reads best as the title
        lines = [f"# {term}", "", f"**{term}** may refer to:", ""]
        lines += [f"- [[{e['article_title']}]] — {e['type']}" for e in with_art]
        notes_svc.upsert_note(conn, f"kb/_disambig/{term}", "\n".join(lines),
                              kind="kb", source="import", version_note="disambiguation", fire_events=False)
        n += 1
    conn.commit()
    return n
