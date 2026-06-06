"""Scope enforcement for Research links — the security boundary.

The exposed boundary of a research link is its spec's APPROVED note-id allowlist
(`approved_ids`), never the live filter (`scope_json`). The filter only *surfaces
candidates* for the owner to approve; it never gates retrieval. Every retrieval here
constrains to the allowlist AND re-verifies each returned id ∈ allowlist, so a note
the owner hasn't approved is unreachable even if it is the best semantic/keyword match.

Read-only ≠ scoped: the read-only connection still *can* read the whole DB, so the
scope is enforced here, per query, not by the connection. This module is the single
place that touches note content for a recipient; keep it small and well-tested.
"""
from __future__ import annotations

import json

import numpy as np

from . import embeddings


# --- spec accessors ---------------------------------------------------------

def _ids(spec, col: str) -> set[int]:
    try:
        return {int(x) for x in json.loads(spec[col] or "[]")}
    except Exception:
        return set()


def approved_ids(spec) -> set[int]:
    """The exposed allowlist — the ONLY thing that gates retrieval."""
    return _ids(spec, "approved_ids_json")


def _scope(spec) -> dict:
    try:
        return json.loads(spec["scope_json"] or "{}")
    except Exception:
        return {}


# --- candidate detection (the FILTER; never a retrieval gate) ---------------

def filter_match_ids(conn, scope: dict) -> set[int]:
    """Notes matching the owner's candidate filter: folder prefixes AND/OR explicit
    note titles, optional kinds. Empty/root prefixes match NOTHING (F10) — an empty
    filter can't expose the whole brain. This only surfaces candidates; approval is
    what exposes."""
    prefixes = [p.strip().strip("/") for p in (scope.get("prefixes") or [])]
    prefixes = [p for p in prefixes if p]                       # drop "", "/", whitespace
    titles = [t.strip().strip("/") for t in (scope.get("titles") or [])]
    titles = [t for t in titles if t]
    if not prefixes and not titles:
        return set()
    kinds = [k for k in (scope.get("kinds") or []) if k]
    clauses, params = [], []
    for p in prefixes:
        clauses.append("(n.title = ? OR n.title LIKE ?)")
        params += [p, p + "/%"]
    for t in titles:                                            # exact single-note match
        clauses.append("n.title = ?")
        params.append(t)
    sql = "SELECT n.id FROM notes n WHERE n.deleted_at IS NULL AND (" + " OR ".join(clauses) + ")"
    # PHI firewall: personal-health pages (kb/Health/<Person>) are never surfaced as research
    # candidates, even if the owner's prefix would match — so they can't be approved into the
    # allowlist and reach a recipient. Default-deny at the surfacing layer (defence-in-depth on
    # top of the approved-id gate). scoped_search applies the same drop on retrieval.
    sql += " AND lower(n.title) NOT LIKE 'kb/health/%'"
    if kinds:
        sql += " AND n.kind IN (%s)" % ",".join("?" * len(kinds))
        params += kinds
    return {r["id"] for r in conn.execute(sql, params).fetchall()}


def candidate_ids(conn, spec) -> set[int]:
    """Filter matches the owner has NOT yet approved or dismissed — the pending tray."""
    return filter_match_ids(conn, _scope(spec)) - approved_ids(spec) - _ids(spec, "dismissed_ids_json")


# --- scoped retrieval (the only path that returns note content) -------------

def _fts_query(q: str) -> str:
    # Prefix-match each token; quote to neutralise FTS operators.
    toks = [t for t in (q or "").replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in toks) or '""'


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _semantic_hits(conn, ids: list[int], query: str, k: int) -> list[tuple[int, float]]:
    """Exact in-process cosine over the in-scope embeddings, scored at CHUNK level and
    collapsed to each note's best-matching chunk. NOT vec0 KNN: that is global (no
    partition key), so a post-filter could return zero useful rows when the scope is a
    small fraction of the brain (F1). Brute force over the bounded in-scope set is exact
    → 100% in-scope recall.

    Scoring chunks (vec_note_chunks ∩ in-scope note_ids), not the whole-note vector,
    means a long approved note matches on its relevant passage rather than only the
    embedder-truncated head — without widening the boundary: only chunks whose note_id
    is in `ids` are ever considered, and scoped_search re-verifies the collapsed note_id
    against `allowed`. Any in-scope note that has no chunk vectors yet (e.g. mid-backfill)
    falls back to its whole-note vec_notes vector, so recall never drops below before."""
    ph = _placeholders(len(ids))
    rows = conn.execute(
        "SELECT c.note_id AS note_id, v.embedding AS embedding "
        "FROM vec_note_chunks v JOIN note_chunks c ON c.id = v.chunk_id "
        "WHERE c.note_id IN (%s)" % ph,
        ids,
    ).fetchall()
    have = {r["note_id"] for r in rows}
    missing = [i for i in ids if i not in have]
    if missing:                                     # whole-note fallback for un-chunked notes
        rows = list(rows) + conn.execute(
            "SELECT note_id, embedding FROM vec_notes WHERE note_id IN (%s)" % _placeholders(len(missing)),
            missing,
        ).fetchall()
    if not rows:
        return []
    qv = np.asarray(embeddings.embed(query), dtype=np.float32)
    qn = float(np.linalg.norm(qv)) or 1.0
    qv = qv / qn
    note_ids = [r["note_id"] for r in rows]
    mat = np.stack([np.frombuffer(r["embedding"], dtype="<f4") for r in rows])
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat / norms[:, None]) @ qv
    best: dict[int, float] = {}                     # collapse chunks → best sim per note
    for nid, s in zip(note_ids, sims):
        s = float(s)
        if nid not in best or s > best[nid]:
            best[nid] = s
    return sorted(best.items(), key=lambda kv: -kv[1])[:k]


def scoped_search(conn, allowed: set[int], query: str, k: int = 6) -> list[dict]:
    """The single retrieval primitive for a research link. Returns up to k in-scope
    note BODIES relevant to `query` (semantic first, keyword fill), each re-verified
    to be in `allowed`. Titles/ids are NOT returned to the model — only content +
    the internal id for the server-side audit log. Output ⊆ allowed, ALWAYS."""
    if not allowed or not (query or "").strip():
        return []
    ids = list(allowed)
    ordered: list[int] = []

    # 1) semantic (exact, in-scope). Degrade to keyword-only if embeddings are unavailable.
    try:
        for nid, _ in _semantic_hits(conn, ids, query, k):
            if nid in allowed and nid not in ordered:
                ordered.append(nid)
    except Exception:
        pass

    # 2) keyword fill via scoped FTS (composes before LIMIT → sound).
    try:
        rows = conn.execute(
            "SELECT f.note_id FROM notes_fts f JOIN notes n ON n.id = f.note_id "
            "WHERE notes_fts MATCH ? AND n.deleted_at IS NULL AND n.id IN (%s) "
            "ORDER BY rank LIMIT ?" % _placeholders(len(ids)),
            [_fts_query(query), *ids, k],
        ).fetchall()
        for r in rows:
            if r["note_id"] in allowed and r["note_id"] not in ordered:
                ordered.append(r["note_id"])
    except Exception:
        pass

    out: list[dict] = []
    for nid in ordered[:k]:
        r = conn.execute(
            "SELECT id, title, content_md FROM notes WHERE id = ? AND deleted_at IS NULL", (nid,)
        ).fetchone()
        # Final re-verification AND a PHI drop: never return a kb/Health/<Person> body to a
        # recipient, even if one were somehow approved into the allowlist (crown-jewel guard).
        if r and r["id"] in allowed and not (r["title"] or "").lower().startswith("kb/health/"):
            out.append({"id": r["id"], "content": r["content_md"] or ""})
    return out
