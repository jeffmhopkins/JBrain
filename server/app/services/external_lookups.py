"""Owner approval gate for OUTBOUND reference lookups.

A hard, code-level gate so a tool (medical_reference) never sends a term to an external service
until the owner has seen the EXACT term and approved it — the owner can be sure no PII leaves the
system in a search query. A decision is remembered per normalized term (approve "TTP" once).
"""
from __future__ import annotations


def _norm(term: str) -> str:
    from . import entity_index
    try:
        return entity_index.normalize(term) or ""
    except Exception:  # noqa: BLE001
        return " ".join((term or "").lower().split())


def check_or_propose(conn, tool: str, term: str) -> dict:
    """Return {status, id, term} for an outbound `term`. If there's no prior decision, record a
    PENDING proposal (nothing is sent) and return status='pending' so the caller can ask the owner."""
    norm = _norm(term)
    if not norm:
        return {"status": "denied", "id": None, "term": term}
    row = conn.execute("SELECT id, status FROM external_lookups WHERE tool=? AND norm_key=?",
                       (tool, norm)).fetchone()
    if row:
        return {"status": row["status"], "id": row["id"], "term": term}
    cur = conn.execute("INSERT INTO external_lookups (tool, term, norm_key) VALUES (?,?,?)",
                       (tool, term, norm))
    conn.commit()
    return {"status": "pending", "id": cur.lastrowid, "term": term}


def decide(conn, lookup_id: int, *, approve: bool) -> dict | None:
    """Record the owner's decision; returns {tool, term} (so an approval can run the fetch), or None."""
    row = conn.execute("SELECT tool, term FROM external_lookups WHERE id=?", (lookup_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE external_lookups SET status=?, decided_at=datetime('now') WHERE id=?",
                 ("approved" if approve else "denied", lookup_id))
    conn.commit()
    return {"tool": row["tool"], "term": row["term"]}
