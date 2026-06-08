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

from . import embeddings, nickname_lexicon, people

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
    """Tokenize a name: lowercase, strip punctuation, and remove title words."""
    parts = _NONWORD.sub(" ", (name or "").lower()).split()
    return [p for p in parts if p and p not in _TITLES]


def normalize(name: str) -> str:
    """Return a stable merge key: lowercased, title-words and punctuation stripped, tokens joined."""
    return " ".join(_tokens(name))


def _distinctive(tok: str) -> bool:
    """Return True when ``tok`` is a real word (likely a surname) rather than an initial."""
    return len(tok) >= 3            # a real word (likely a surname), not an initial


def _acronymish(norm: str) -> bool:
    """Return True when ``norm`` looks like an acronym (single token, 2–6 alpha chars)."""
    return " " not in norm and 2 <= len(norm) <= 6 and norm.isalpha()


def _initials(name: str) -> str:
    """Return the initials string for ``name`` (first character of each token joined)."""
    return "".join(t[0] for t in _tokens(name) if t)


def _collect(conn, limit: int) -> dict:
    """Collect entity occurrences from note_analysis into per-type/norm buckets.

    Args:
        conn: SQLite connection.
        limit: Maximum number of note_analysis rows to scan.

    Returns:
        Nested dict: type -> {norm -> {tokens, count, notes: set, raws: Counter}}.
    """
    rows = conn.execute(
        "SELECT a.note_id, a.entities_json FROM note_analysis a "
        "JOIN notes n ON n.id = a.note_id "
        "WHERE n.deleted_at IS NULL AND n.kind IN ('entry','daily') AND n.kb_ingest = 1 "
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


def _merge_map(clusters: dict, *, merges: dict | None = None, splits: set | None = None) -> dict:
    """Build a type -> {norm -> canonical_norm} mapping via union-find.

    Unions subset-name variants that share a distinctive token, blocked by token so
    not every pair is compared.

    Durable user rulings override the heuristic (pure — passed in, never read here):

    - ``splits``: frozenset({norm_a, norm_b}) pairs that must NEVER be co-grouped.
      Enforced as a hard invariant: a union (heuristic, acronym, or forced merge) is
      refused if joining the two components would put any split pair together, so a
      bridge variant can't transitively re-unite them. Split wins over a directly-
      contradicting merge (last-writer-wins is resolved in entity_decisions.add).
    - ``merges``: {member_norm -> canonical_norm} pre-resolved to the terminal
      canonical (see entity_decisions.load_merges). Forced unions fire even without a
      shared token. A member absent from this pass's clusters is DORMANT — it seeds
      union-find so an existing side still binds, but a merge whose both sides are
      absent materializes no entity. A group's forced canonical wins over the
      most-tokens rule.

    Args:
        clusters: Output of ``_collect``.
        merges: Forced merge decisions, member_norm -> terminal canonical_norm.
        splits: Set of frozenset({norm_a, norm_b}) pairs forbidden from co-grouping.

    Returns:
        Nested dict: type -> {norm -> canonical_norm}.
    """
    merges = merges or {}
    splits = splits or set()
    out: dict = defaultdict(dict)
    forced_canon = set(merges.values())          # norms that win canonical when present
    # norm -> set of norms it must never be co-grouped with (both directions).
    split_adj: dict = defaultdict(set)
    for pair in splits:
        if len(pair) == 2:
            x, y = tuple(pair)
            split_adj[x].add(y)
            split_adj[y].add(x)
    for typ, norms in clusters.items():
        # Seed union-find with cluster norms PLUS any merge norm referenced, so a forced
        # union can bind even when one side is named only in a decision. A node with no
        # cluster is dormant: it gets no entity unless it shares a group with a real cluster.
        nodes = set(norms)
        for m, c in merges.items():
            nodes.add(m); nodes.add(c)
        parent = {n: n for n in nodes}
        comp = {n: {n} for n in nodes}           # root -> set of member norms (split checking)

        def find(x):
            """Find with path compression for the union-find structure."""
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _split_blocked(a, b):
            """Return True when merging components of a and b would violate a split ruling."""
            # Refuse if joining the two components would put any user-split pair together.
            ca, cb = comp[find(a)], comp[find(b)]
            return any(not split_adj[x].isdisjoint(cb) for x in ca if x in split_adj)

        def union(a, b):
            """Union the components of a and b in the union-find structure."""
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            parent[rb] = ra
            comp[ra] |= comp[rb]
            del comp[rb]

        person_like = typ in {"person", "animal"}
        by_token: dict = defaultdict(list)
        for n in norms:
            for t in clusters[typ][n]["tokens"]:
                if _distinctive(t):
                    by_token[t].append(n)
        for group in by_token.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if _split_blocked(a, b):           # user forbade co-grouping these
                        continue
                    ta, tb = clusters[typ][a]["tokens"], clusters[typ][b]["tokens"]
                    # A conflicting generational suffix (Jr vs Sr, or one carrying a suffix
                    # and the other not) is a strong "different person" signal for people —
                    # block the union on BOTH the raw-subset and nickname paths, so a
                    # father/son who differ only by suffix never auto-merge.
                    if person_like and nickname_lexicon.suffixes(ta) != nickname_lexicon.suffixes(tb):
                        continue
                    if ta <= tb or tb <= ta:          # one name subsumes the other
                        union(a, b)
                    elif person_like:
                        # Nickname-aware fallback (person/animal only): map given-name tokens
                        # to a canonical form (jeff→jeffrey, bob→robert) and retry the subset
                        # test. The shared distinctive token that put a,b in this bucket is a
                        # real surname (raw, unmapped), so this only fires for same-surname
                        # variants.
                        ma, mb = nickname_lexicon.map_tokens(ta), nickname_lexicon.map_tokens(tb)
                        if ma <= mb or mb <= ma:
                            union(a, b)
        # Acronym union: "ttp" ↔ "thrombotic thrombocytopenic purpura" (initials match).
        def _rep(n):
            """Return the most-common raw surface form for cluster norm ``n``."""
            raws = clusters[typ][n]["raws"]
            return raws.most_common(1)[0][0] if raws else n
        expansions = [n for n in norms if " " in n]
        for ac in norms:
            if not _acronymish(ac):
                continue
            for ex in expansions:
                if _split_blocked(ac, ex):
                    continue
                if _initials(_rep(ex)) == ac:
                    union(ac, ex)
                    break
        # Forced user merges: union member -> canonical even with no shared token — but a
        # split still wins (we never co-group a split pair, even on an explicit merge).
        for member, c in merges.items():
            if not _split_blocked(member, c):
                union(member, c)
        for grp in comp.values():
            present = [m for m in grp if m in norms]   # drop dormant merge-only nodes
            if not present:
                continue
            forced = [m for m in present if m in forced_canon]
            pool = forced or present                   # a forced canonical wins the group
            canon = max(pool, key=lambda m: (len(clusters[typ][m]["tokens"]), clusters[typ][m]["count"]))
            for m in present:
                out[typ][m] = canon
    return out


def _fold_owner(conn, clusters: dict, mapping: dict) -> None:
    """Merge the owner's self-references into their named person entity.

    The owner is the note-taker, so first-person facts are extracted under the owner's
    real name (once set), but stray mentions like "the owner"/"me"/"I" also appear.
    Points those placeholder person-clusters at the owner's real-name cluster so the
    index holds ONE owner entity (displayed under the real name, with placeholders as
    aliases). No-op until the owner has set a real name that actually appears as a
    person entity; re-analysis is what makes first-person resolve to that name.

    Args:
        conn: SQLite connection (read-only: used to fetch the owner record).
        clusters: Output of ``_collect``, mutated in place (person clusters).
        mapping: Output of ``_merge_map``, mutated in place (person mappings).
    """
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
    """Reaggregate the entity index from note_analysis.

    Upserts by (type, normalized_key) so entity IDs are stable across rebuilds.
    Replaces each entity's mentions and aliases, prunes vanished entities, links
    each to its kb article, applies owner name overrides, and refreshes embeddings.

    Args:
        conn: SQLite connection (commits internally).
        limit: Maximum number of note_analysis rows to scan.

    Returns:
        Total number of entity (type, canonical_norm) pairs after this rebuild.
    """
    from . import entity_decisions
    clusters = _collect(conn, limit)
    # Durable user rulings (type='person' — the only type with an identity UI). merges/splits
    # are member-norm/pair scoped, so passing the person set to _merge_map is safe: those norms
    # never collide with another type's clusters, and dormant merge norms materialize nothing.
    merges = entity_decisions.load_merges(conn, type="person")
    splits = entity_decisions.load_splits(conn, type="person")
    decided_aliases = entity_decisions.load_aliases(conn, type="person")
    mapping = _merge_map(clusters, merges=merges, splits=splits)
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

    # Fold explicit user 'alias' decisions onto their canonical entity (person type). The
    # canonical must already exist as an entity this pass (an alias for a vanished entity is
    # inert) — mirrors merges: decisions never materialize an entity on their own.
    for canon_norm, pairs in decided_aliases.items():
        agg = canon.get(("person", canon_norm))
        if agg is None:
            continue
        for alias_norm, alias_display in pairs:
            # Never fold an alias the user has SPLIT apart from this canonical — a split is a
            # hard "different person" ruling and must win over a stale/competing alias row.
            if (alias_norm and alias_norm != canon_norm
                    and frozenset({canon_norm, alias_norm}) not in splits):
                agg["aliases"].setdefault(alias_norm, alias_display)

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
    _apply_overrides(conn)            # owner source-of-truth names win over frequency

    # Self-heal the owner's nickname aliases: an owner who set their name BEFORE the alias
    # feature shipped never re-saved Owner settings, so their name/declared-aliases were never
    # seeded as durable alias decisions. reconcile_owner is idempotent + split-gated and binds
    # the owner to their kb/People article (needs the entity→article bindings above to exist),
    # so re-running it on every rebuild backfills those decisions. Lazy import breaks the
    # import cycle (wiki_build imports entity_index lazily too); reconcile_owner never calls
    # rebuild, so there's no recursion. Best-effort — it must never break a rebuild. The seeded
    # decisions are loaded by decided_aliases at the START of the NEXT rebuild, so they fold into
    # entity_aliases one rebuild later (expected, acceptable eventual-consistency).
    name = (people.owner_name(conn) or "").strip().lower()
    if name and name not in ("me", "the owner"):
        try:
            from . import wiki_build
            wiki_build.reconcile_owner(conn)
        except Exception:
            pass

    _sync_embeddings(conn)            # embed AFTER overrides so vectors use the corrected name
    conn.commit()
    return len(seen_keys)


def set_canonical_override(conn, article_title: str, canonical_name: str,
                           source_note_id: int | None = None) -> None:
    """Record an owner source-of-truth override for an entity's display name.

    Keyed by the kb article title (a stable key that survives normalize() diacritic or
    spelling forks). Durable — rebuild() re-applies it via _apply_overrides — and patches
    the live entity immediately if one is already linked. No LLM or embeddings; safe on
    the request path.

    Args:
        conn: SQLite connection.
        article_title: Title of the kb article that backs the entity.
        canonical_name: The owner-specified display name to record.
        source_note_id: Optional note ID that is the source of the correction; when
            that note is soft-deleted, the override auto-reverts.
    """
    article_title = (article_title or "").strip()
    canonical_name = (canonical_name or "").strip()
    if not article_title or not canonical_name:
        return
    conn.execute(
        "INSERT INTO entity_overrides (article_title, canonical_name, source_note_id) "
        "VALUES (?,?,?) ON CONFLICT(article_title) DO UPDATE SET "
        "canonical_name=excluded.canonical_name, source_note_id=excluded.source_note_id, "
        "created_at=datetime('now')",
        (article_title, canonical_name, source_note_id))
    _apply_one_override(conn, article_title, canonical_name)


def _apply_one_override(conn, article_title: str, canonical_name: str) -> None:
    """Set the linked entity's display name to the override value.

    Preserves both the prior name and the corrected name as aliases so both spellings
    still resolve in search and routing. Idempotent.

    Args:
        conn: SQLite connection.
        article_title: Title of the kb article whose linked entity is updated.
        canonical_name: New display name to apply.
    """
    ent = conn.execute("SELECT id, canonical_name FROM entities WHERE article_title=?",
                       (article_title,)).fetchone()
    if not ent:
        return                        # no entity linked yet; rebuild will apply it later
    old = ent["canonical_name"]
    if old and normalize(old) != normalize(canonical_name):
        conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_norm, alias_display) "
                     "VALUES (?,?,?)", (ent["id"], normalize(old), old))
    conn.execute("UPDATE entities SET canonical_name=?, updated_at=datetime('now') WHERE id=?",
                 (canonical_name, ent["id"]))
    conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_norm, alias_display) "
                 "VALUES (?,?,?)", (ent["id"], normalize(canonical_name), canonical_name))


def _apply_overrides(conn) -> None:
    """Re-apply all active owner name overrides after rebuild recomputes display names.

    Skips any override whose source note was soft-deleted, so the entity name auto-reverts
    to the frequency-derived value — the revert path for an undone correction.

    Args:
        conn: SQLite connection.
    """
    rows = conn.execute(
        "SELECT o.article_title, o.canonical_name FROM entity_overrides o "
        "LEFT JOIN notes n ON n.id = o.source_note_id "
        "WHERE o.source_note_id IS NULL OR n.deleted_at IS NULL").fetchall()
    for ov in rows:
        _apply_one_override(conn, ov["article_title"], ov["canonical_name"])


_LABEL = dict(_TYPE_LABELS)


def _entity_embed_text(name: str, typ: str, aliases: list[str], lead: str) -> str:
    """Build the embedding input string for an entity.

    Combines the entity's name, human-readable type, aliases, and (when available)
    its KB article's lead sentence so descriptive queries can reach it.

    Args:
        name: Canonical display name.
        typ: Entity type key (e.g. 'person', 'org').
        aliases: List of alias display strings.
        lead: First line of the entity's KB article, or an empty string.

    Returns:
        Combined embedding string (capped at 500 characters).
    """
    parts = [name]
    label = _LABEL.get(typ, typ)
    if label:
        parts.append(f"({label})")
    if aliases:
        parts.append("a.k.a. " + ", ".join(aliases[:4]))
    if lead:
        parts.append(lead)
    return " — ".join(parts)[:500]


_AKA_LEAD_RE = re.compile(r"^\*Also known as:.*\*$")


def _article_leads(conn) -> dict:
    """Return a mapping of kb article title to its lead sentence for embedding context.

    The lead is the first non-blank, non-heading line. Skips an '*Also known as: ...*'
    line so the real lead — not the alias line surface_aliases inserts under the H1 —
    feeds the entity's embedding.

    Args:
        conn: SQLite connection.

    Returns:
        Dict mapping article title to lead sentence string (up to 200 chars each).
    """
    out: dict = {}
    for a in conn.execute(
        "SELECT title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND redirect_to IS NULL"
    ).fetchall():
        for line in (a["content_md"] or "").splitlines():
            st = line.strip()
            if not st or st.startswith("#") or _AKA_LEAD_RE.match(st):
                continue                              # skip blanks, headings, and the AKA line
            out[a["title"]] = st[:200]
            break
    return out


def _sync_embeddings(conn) -> None:
    """Refresh each entity's semantic embedding vector.

    Skips entities whose embed text is unchanged (compared via entities.embed_hash).
    Batched so only changed entities hit the embedder.

    Args:
        conn: SQLite connection.
    """
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
    """Link each entity to its kb article by normalized key or alias match.

    Uses the same robust basis as note_ids_for_name (normalized key OR any alias)
    rather than a raw-string leaf match, which would miss common variants (e.g.
    entity 'Thrombotic Thrombocytopenic Purpura' vs article leaf 'TTP' or
    aliased/merged names) and silently leave article_title NULL.

    Args:
        conn: SQLite connection.
    """
    from . import wiki_guides
    conn.execute("UPDATE entities SET article_title = NULL")
    leaf_map: dict = {}      # normalized article leaf -> full title (first wins)
    # Exclude redirects: an entity must point at the CANONICAL article, never a merged-away
    # redirect row (which would route browse/disambiguation to a one-line redirect marker).
    for k in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL"
    ).fetchall():
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
    """Return the top-k entity names most frequently co-mentioned with ``entity_id``."""
    rows = conn.execute(
        "SELECT e.canonical_name AS name, COUNT(*) AS c FROM entity_mentions m1 "
        "JOIN entity_mentions m2 ON m2.note_id = m1.note_id AND m2.entity_id != m1.entity_id "
        "JOIN entities e ON e.id = m2.entity_id "
        "WHERE m1.entity_id = ? GROUP BY m2.entity_id ORDER BY c DESC, name LIMIT ?",
        (entity_id, k),
    ).fetchall()
    return [r["name"] for r in rows]


def roster(conn, per_type: int = 40, partners: int = 3) -> str:
    """Build a compact roster of recurring entities for the outline prompt.

    Groups entities by type with their most frequent co-occurring partners. The
    ubiquitous owner entity (present in nearly every note) is excluded so it doesn't
    drown the co-occurrence signal.

    Args:
        conn: SQLite connection.
        per_type: Maximum entities to list per type.
        partners: Number of frequent co-mention partners to include per entity.

    Returns:
        Multi-line string formatted as ``Type:\\n- Name (N notes; often with: ...)\\n...``.
    """
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
    """Query the entity index with optional type and text filters.

    Args:
        conn: SQLite connection.
        type: Optional entity type to filter by (e.g. 'person', 'org').
        q: Optional text search against canonical_name and aliases.
        limit: Maximum number of results to return.

    Returns:
        List of entity dicts ordered by note_count descending then canonical_name.
    """
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
    """Return an entity and the notes that mention it for the browse view.

    Args:
        conn: SQLite connection.
        entity_id: ID of the entity to look up.

    Returns:
        Dict with entity fields plus ``aliases`` and ``notes`` lists, or None if
        the entity does not exist.
    """
    e = conn.execute(
        "SELECT id, type, canonical_name, normalized_key, note_count, article_title FROM entities WHERE id=?",
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
    """Return note IDs for the canonical entity matching a name or alias across all types.

    Args:
        conn: SQLite connection.
        name: Raw name string; normalized internally before lookup.

    Returns:
        List of note IDs that mention the matching entity, or an empty list.
    """
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


# ---- Span-detection alias expansion (OWNER-CONTEXT ONLY) ---------------------------------

def _span_admit_single(surface: str) -> bool:
    """Return True when a single-token span surface is safe to expand on.

    Admits only distinctive tokens (len >= 4 and not a common stop-word), mirroring
    the linker's leaf gate. Short or common tokens ("home", "gym", 1–2 char keys) are
    too collision-prone to fire on mentions buried in prose. A distinctive mononym
    ("Madonna") still passes. Reuses the linker's exact stop set via a lazy import
    to avoid the wiki_build <-> entity_index import cycle.

    Args:
        surface: Normalized single-token surface string.

    Returns:
        True when the surface is safe for span-expansion.
    """
    from .wiki_build import _STOP_LEAVES
    return len(surface) >= 4 and surface not in _STOP_LEAVES


def _span_surface_set(conn, ambiguous: set[str] | None = None) -> set[str]:
    """Return the safe set of normalized name-surfaces for in-query span expansion.

    A dictionary (not NER): the conservative intersection of "resolvable by
    note_ids_for_name" and "safe to fire on a mention buried in prose":

    - Every multi-token canonical key (collision-resistant).
    - A single-token key ONLY for a person/animal mononym that is distinctive
      (len >= 4, not a stop-word) — org/thing single-token keys ("google") are
      excluded because they appear as ordinary verbs/nouns in prose.
    - Every multi-token alias (e.g. "jeff hopkins").
    - A single-token alias ONLY when the user explicitly decided it via
      entity_decisions 'alias' — bare heuristic first names are too collision-prone.

    Ambiguous surfaces (mapping to >= 2 entities) are removed. ``ambiguous`` is the
    pre-computed lower-cased set from search.py (threaded in for efficiency); when
    None it is computed here so the helper stays standalone-callable.

    Args:
        conn: SQLite connection.
        ambiguous: Optional pre-computed set of ambiguous term strings.

    Returns:
        Set of normalized surface strings safe for span expansion.
    """
    from . import entity_decisions
    surfaces: set[str] = set()
    for r in conn.execute("SELECT normalized_key AS k, type AS t FROM entities").fetchall():
        k = r["k"]
        if not k:
            continue
        if " " in k:                                    # multi-token key (any type) — collision-resistant
            surfaces.add(k)
        elif (r["t"] or "") in ("person", "animal") and _span_admit_single(k):
            # SINGLE-token key only for a person/animal mononym ("Madonna") that is also distinctive
            # (len>=4, not a stop-word). A single-token org/thing key ("google"/"costco"/"gmail") is
            # almost always a common verb/noun in prose, so we do NOT span-expand on it.
            surfaces.add(k)
    decided = {an for _canon, pairs in entity_decisions.load_aliases(conn).items()
               for an, _disp in pairs}
    for r in conn.execute(
        "SELECT DISTINCT alias_norm AS a FROM entity_aliases WHERE alias_norm IS NOT NULL"
    ).fetchall():
        an = r["a"]
        if an and (" " in an or an in decided):       # multi-token, or a user-decided single name
            surfaces.add(an)
    if ambiguous is None:
        ambiguous = {str(t.get("term", "")).lower() for t in ambiguous_terms(conn)}
    return surfaces - ambiguous


def span_entity_note_ids(conn, q: str, *, max_spans: int = 2, max_notes: int = 15,
                         ambiguous: set[str] | None = None) -> list[int]:
    """Return note IDs reached by named entity spans found inside query ``q``.

    Owner-context only. Complements whole-query expansion: "what did jeff hopkins say
    about taxes" never resolves as a whole entity, but CONTAINS "jeff hopkins". Scans
    the normalized token stream longest-match-first (so "jeff hopkins" wins over the
    bare "jeff" sub-span and its tokens are consumed), then unions the matched
    entities' mention notes via note_ids_for_name.

    Bounded for the hot path: at most ``max_spans`` distinct surfaces expand and at
    most ``max_notes`` IDs return, so span hits augment fusion without flooding it.
    ``ambiguous`` is threaded to ``_span_surface_set`` (computed once by the caller;
    None -> computed there). Returns [] when nothing safe matches. Pure read; no LLM
    or embeddings.

    Args:
        conn: SQLite connection.
        q: Raw query string to scan for entity name spans.
        max_spans: Maximum number of distinct surfaces to expand.
        max_notes: Maximum total note IDs to return.
        ambiguous: Optional pre-computed set of ambiguous term strings.

    Returns:
        List of note IDs (up to ``max_notes``) reached by entity spans in the query.
    """
    toks = _NONWORD.sub(" ", (q or "").lower()).split()
    if not toks:
        return []
    surfaces = _span_surface_set(conn, ambiguous)
    if not surfaces:
        return []
    ids: list[int] = []
    seen_ids: set[int] = set()
    spans_used = 0
    i, n = 0, len(toks)
    # Longest-match scan: at each position try the longest window (≤4 tokens) that is a known
    # surface, resolve it, then advance past the whole matched span (consume its tokens).
    while i < n and spans_used < max_spans:
        matched = 0
        for size in range(min(n - i, 4), 0, -1):
            cand = " ".join(toks[i:i + size])
            if cand in surfaces:
                for nid in note_ids_for_name(conn, cand):
                    if nid not in seen_ids:
                        seen_ids.add(nid)
                        ids.append(nid)
                        if len(ids) >= max_notes:
                            return ids
                spans_used += 1
                matched = size
                break
        i += matched or 1
    return ids


def ambiguous_terms(conn) -> list[dict]:
    """Return terms that map to two or more distinct entities.

    These are disambiguation candidates: canonical keys or aliases shared by multiple
    entities.

    Args:
        conn: SQLite connection.

    Returns:
        List of dicts with keys ``term`` and ``entities`` (list of entity dicts with
        ``id``, ``type``, ``canonical_name``, and ``article_title``).
    """
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


def alias_surface(conn) -> dict:
    """Return the safe set of aliases for auto-linking, keyed by alias_norm.

    The single source of truth for alias-aware linking: consulted by the deterministic
    linker AND the link-label hygiene allow-list, so the set that gets linked is exactly
    the set that gets protected (no drift, no nightly strip-then-re-add oscillation).

    Drop rules (a dropped alias stays plain text, never auto-linked):

    - (i)   alias maps to >= 2 distinct article-bearing entities (ambiguous);
    - (ii)  alias appears in ambiguous_terms (collides with another entity's key/alias);
    - (iii) alias norm equals a live non-redirect article leaf norm (leaf path handles it);
    - (iv)  single-token given-name alias ("jeff") UNLESS backed by an explicit
            entity_decisions 'alias' ruling — bare first names are too collision-prone;
    - (v)   private (Health/Finance) or Reference target — PII firewall.

    Args:
        conn: SQLite connection.

    Returns:
        Dict mapping alias_norm to (article_title, alias_display) tuples.
    """
    from . import wiki_guides, entity_decisions
    rows = conn.execute(
        "SELECT a.alias_norm, a.alias_display, e.normalized_key, e.article_title "
        "FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE e.article_title IS NOT NULL AND a.alias_norm IS NOT NULL"
    ).fetchall()
    by_alias: dict[str, set] = defaultdict(set)
    info: dict[str, tuple] = {}
    for r in rows:
        by_alias[r["alias_norm"]].add(r["article_title"])
        info.setdefault(r["alias_norm"], (r["article_title"], r["alias_display"] or r["alias_norm"], r["normalized_key"]))
    amb = {str(t.get("term", "")).lower() for t in ambiguous_terms(conn)}
    leaf_norms = {normalize(row["title"].split("/")[-1]) for row in conn.execute(
        "SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL").fetchall()}
    decided = {(canon, an) for canon, pairs in entity_decisions.load_aliases(conn).items()
               for an, _ in pairs}
    out: dict[str, tuple] = {}
    for an, targets in by_alias.items():
        if len(targets) >= 2 or an in amb or an in leaf_norms:        # (i)(ii)(iii)
            continue
        art, disp, canon = info[an]
        if len(an.split()) <= 1 and (canon, an) not in decided:       # (iv)
            continue
        if wiki_guides.is_private_title(art) or wiki_guides.domain_for_title(art) == "Reference":  # (v)
            continue
        out[an] = (art, disp)
    return out


def write_disambiguation_pages(conn) -> int:
    """Generate protected kb/_disambig/<Term> pages for ambiguous terms.

    Creates a disambiguation page for every term that maps to two or more entities
    each having a kb article. All existing disambiguation pages are cleared first so
    the set is fully regenerated each call.

    Args:
        conn: SQLite connection (commits internally).

    Returns:
        Number of disambiguation pages created.
    """
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


# ---- Phase 5: LLM "same person?" proposals (propose-only; never auto-merge) ---------------

def _open_proposed_pairs(conn) -> set:
    """Return unordered {norm_a, norm_b} pairs with a pending entity_merge review card.

    Used to avoid re-proposing a pair that is already awaiting the owner's decision.

    Args:
        conn: SQLite connection.

    Returns:
        Set of frozenset({norm_a, norm_b}) pairs.
    """
    out: set = set()
    for r in conn.execute(
        "SELECT payload_json FROM review_items WHERE kind='entity_merge' AND status='pending'"
    ).fetchall():
        try:
            p = json.loads(r["payload_json"] or "{}")
            a = (p.get("source") or {}).get("norm")
            b = (p.get("into") or {}).get("norm")
            if a and b:
                out.add(frozenset({a, b}))
        except Exception:  # noqa: BLE001
            continue
    return out


def _adjudicate_same_person(conn, a: dict, b: dict) -> dict:
    """Ask the LLM whether two same-surname person entities are the same individual.

    Considers aliases and frequent co-occurring partners to distinguish the same person
    from relatives or namesakes. Defensive on parse failure.

    Args:
        conn: SQLite connection (used to fetch aliases and partners).
        a: Entity dict for the first person (must have ``id`` and ``canonical_name``).
        b: Entity dict for the second person (must have ``id`` and ``canonical_name``).

    Returns:
        Dict with keys ``same`` (bool), ``confidence`` (float), and ``why`` (str).
    """
    from . import llm, prompts
    aka_a = [x["alias_display"] for x in conn.execute(
        "SELECT alias_display FROM entity_aliases WHERE entity_id=? AND alias_display IS NOT NULL", (a["id"],)).fetchall()]
    aka_b = [x["alias_display"] for x in conn.execute(
        "SELECT alias_display FROM entity_aliases WHERE entity_id=? AND alias_display IS NOT NULL", (b["id"],)).fetchall()]
    prompt = (prompts.get("actions.entity_same_person", "")
              .replace("{name_a}", a["canonical_name"])
              .replace("{aka_a}", ", ".join(aka_a) or "(none)")
              .replace("{partners_a}", ", ".join(_partners(conn, a["id"], 6)) or "(none)")
              .replace("{name_b}", b["canonical_name"])
              .replace("{aka_b}", ", ".join(aka_b) or "(none)")
              .replace("{partners_b}", ", ".join(_partners(conn, b["id"], 6)) or "(none)"))
    try:
        raw = llm.complete([{"role": "user", "content": prompt}], max_tokens=300)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        return {"same": bool(data.get("same")),
                "confidence": float(data.get("confidence") or 0),
                "why": str(data.get("why") or "")[:300]}
    except Exception:  # noqa: BLE001
        return {"same": False, "confidence": 0.0, "why": ""}


def propose_person_merges(conn, limit: int = 40, min_confidence: float = 0.7) -> dict:
    """Surface likely-duplicate people for the owner to confirm (never auto-merging).

    Candidates are pairs of distinct person entities sharing a surname (last distinctive
    token) that aren't already merged, split, or awaiting review. The LLM adjudicates
    same-person vs relative/namesake. Only high-confidence 'same' verdicts post an
    'entity_merge' Review card (approve -> durable merge; reject -> durable split).
    No-op without LLM credentials.

    Args:
        conn: SQLite connection (commits internally).
        limit: Maximum number of candidate pairs to adjudicate.
        min_confidence: Minimum LLM confidence to post a review card.

    Returns:
        Dict with keys ``candidates`` (pairs evaluated) and ``proposed`` (cards posted).
    """
    from . import entity_decisions, llm, reviews
    if not llm.has_credentials():
        return {"candidates": 0, "proposed": 0, "reason": "no LLM credentials"}
    merges = entity_decisions.load_merges(conn, "person")
    splits = entity_decisions.load_splits(conn, "person")
    open_pairs = _open_proposed_pairs(conn)

    ents = []
    for e in conn.execute(
        "SELECT id, canonical_name, normalized_key, note_count, article_title "
        "FROM entities WHERE type='person'").fetchall():
        toks = _tokens(e["canonical_name"])
        surnames = [t for t in toks if _distinctive(t)]
        if surnames:
            ents.append({**dict(e), "tokens": toks, "surname": surnames[-1]})

    by_surname: dict = defaultdict(list)
    for e in ents:
        by_surname[e["surname"]].append(e)

    def canon_of(n):
        """Return the terminal canonical norm for ``n`` according to the merge map."""
        return merges.get(n, n)

    # Bound generation, not just adjudication: skip pathologically large same-surname buckets
    # (a 200-person "Smith" family is O(N^2) noise the model can't usefully disambiguate) and
    # stop once we have a healthy multiple of `limit` candidate pairs in hand.
    cap = max(int(limit) * 5, 50)
    candidates, seen = [], set()
    for group in by_surname.values():
        if len(candidates) >= cap:
            break
        if len(group) > 40:                                    # too large to be a real "is this the same person?"
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                na, nb = a["normalized_key"], b["normalized_key"]
                if na == nb or canon_of(na) == canon_of(nb):
                    continue                                   # already the same/merged entity
                pair = frozenset({na, nb})
                if pair in splits or pair in open_pairs or pair in seen:
                    continue
                seen.add(pair)
                candidates.append((a, b))

    proposed = 0
    for a, b in candidates[:limit]:
        v = _adjudicate_same_person(conn, a, b)
        if not (v["same"] and v["confidence"] >= min_confidence):
            continue
        # Canonical = the more descriptive (more tokens), then more-mentioned, name.
        into, src = ((a, b) if (len(a["tokens"]), a["note_count"]) >= (len(b["tokens"]), b["note_count"])
                     else (b, a))
        payload = {"type": "person",
                   "source": {"id": src["id"], "norm": src["normalized_key"],
                              "display": src["canonical_name"], "article_title": src["article_title"]},
                   "into": {"id": into["id"], "norm": into["normalized_key"],
                            "display": into["canonical_name"], "article_title": into["article_title"]},
                   "why": v["why"]}
        link_slug = None
        if into["article_title"]:
            r = conn.execute("SELECT slug FROM notes WHERE title=? AND deleted_at IS NULL",
                             (into["article_title"],)).fetchone()
            link_slug = r["slug"] if r else None
        reviews.create_review_item(
            conn, None, "Same person?",
            f"Merge “{src['canonical_name']}” into “{into['canonical_name']}”? {v['why']}",
            link_slug, kind="entity_merge", payload_json=json.dumps(payload))
        proposed += 1
    conn.commit()
    return {"candidates": len(candidates), "proposed": proposed}
