"""Durable person-identity decisions — the user's merge/split/alias rulings.

`entity_index` re-derives the entities/aliases tables from `note_analysis` on every
rebuild, so a purely heuristic merge evaporates the next pass. This append-only ledger
makes the user's choices STICK: `entity_index.rebuild()` loads these rows and folds them
into its union-find (forced merges, blocked splits) and into `entity_aliases`.

Row conventions (keys are stored already-normalized via `entity_index.normalize`):
- kind='merge' : union `norm_a` into the canonical. `canonical` = the surviving
  normalized key (and is itself a member of the group). `norm_b` is unused.
- kind='split' : forbid the heuristic auto-union of the UNORDERED pair
  {norm_a, norm_b}. Order-insensitive.
- kind='alias' : attach an extra alias to a canonical entity. `norm_b` = the canonical
  entity's normalized key (the anchor); `norm_a` = the alias's normalized key;
  `display_a` = the alias label to show.

Conn-first, like reviews.py / people.py — callers own the transaction (commit).
"""
from __future__ import annotations

from . import entity_index


def add(conn, kind, type="person", norm_a=None, norm_b=None, display_a=None,
        display_b=None, canonical=None, author="user", source=None) -> int:
    """Record a decision (normalizing all keys first) and return its id.

    Refuses a degenerate self-pair (norm_a == norm_b). Enforces mutual exclusion: a
    'merge' on a pair deletes any 'split' on that same unordered pair, and vice-versa.
    De-dupes: an identical (kind,type,norm_a,norm_b,canonical) row is not re-inserted —
    its existing id is returned.
    """
    na = entity_index.normalize(norm_a or "")
    nb = entity_index.normalize(norm_b or "") or None
    canon = entity_index.normalize(canonical or "") or None
    if not na:
        raise ValueError("norm_a is required")
    if nb is not None and na == nb:
        raise ValueError("norm_a and norm_b must differ")
    if kind == "merge" and canon and na == canon:
        raise ValueError("a merge's member and canonical must differ")  # no self-loop

    # Mutual exclusion on the unordered pair: a merge cancels a prior split and vice-versa.
    if kind in ("merge", "split"):
        pair = sorted([p for p in (na, nb, canon) if p])
        opposite = "split" if kind == "merge" else "merge"
        for r in conn.execute(
            "SELECT id, norm_a, norm_b, canonical FROM entity_decisions WHERE kind=? AND type=?",
            (opposite, type),
        ).fetchall():
            other = sorted([p for p in (r["norm_a"], r["norm_b"], r["canonical"]) if p])
            if other == pair:
                conn.execute("DELETE FROM entity_decisions WHERE id=?", (r["id"],))

    existing = conn.execute(
        "SELECT id FROM entity_decisions WHERE kind=? AND type=? AND norm_a=? "
        "AND IFNULL(norm_b,'')=IFNULL(?,'') AND IFNULL(canonical,'')=IFNULL(?,'')",
        (kind, type, na, nb, canon),
    ).fetchone()
    if existing:
        return existing["id"]

    cur = conn.execute(
        "INSERT INTO entity_decisions (kind, type, norm_a, norm_b, display_a, display_b, "
        "canonical, author, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, type, na, nb, display_a, display_b, canon, author, source),
    )
    return cur.lastrowid


def remove(conn, decision_id: int) -> None:
    conn.execute("DELETE FROM entity_decisions WHERE id=?", (decision_id,))


def load_merges(conn, type="person") -> dict:
    """{member_norm -> TERMINAL canonical_norm} from all 'merge' rows of this type.

    Chains are resolved so a merge X->Y plus Y->Z maps BOTH X and Y to the terminal Z (the
    user's last-chosen target wins the display), with a cycle guard. The terminal maps to
    itself, so set(values) is exactly the set of terminal canonicals."""
    immediate: dict = {}
    for r in conn.execute(
        "SELECT norm_a, canonical FROM entity_decisions WHERE kind='merge' AND type=?", (type,)
    ).fetchall():
        immediate[r["norm_a"]] = r["canonical"] or r["norm_a"]

    def terminal(n):
        seen = {n}
        while n in immediate and immediate[n] != n and immediate[n] not in seen:
            n = immediate[n]
            seen.add(n)
        return n

    out: dict = {}
    for member in immediate:
        t = terminal(member)
        out[member] = t
        out[t] = t
    return out


def load_splits(conn, type="person") -> set:
    """{frozenset({norm_a, norm_b})} — pairs the heuristic must NOT auto-union."""
    out: set = set()
    for r in conn.execute(
        "SELECT norm_a, norm_b FROM entity_decisions WHERE kind='split' AND type=?", (type,)
    ).fetchall():
        if r["norm_a"] and r["norm_b"]:
            out.add(frozenset({r["norm_a"], r["norm_b"]}))
    return out


def load_aliases(conn, type="person") -> dict:
    """{canonical_norm -> [(alias_norm, alias_display), ...]} from 'alias' rows.

    An alias row anchors to its canonical via norm_b (the canonical entity's normalized
    key); norm_a holds the alias key and display_a the label."""
    out: dict = {}
    for r in conn.execute(
        "SELECT norm_a, norm_b, display_a FROM entity_decisions WHERE kind='alias' AND type=?", (type,)
    ).fetchall():
        if not r["norm_b"] or not r["norm_a"]:
            continue
        out.setdefault(r["norm_b"], []).append((r["norm_a"], r["display_a"] or r["norm_a"]))
    return out


def list_for(conn, type="person") -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, type, norm_a, norm_b, display_a, display_b, canonical, author, "
        "source, created_at FROM entity_decisions WHERE type=? ORDER BY id", (type,)
    ).fetchall()]
