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
    PENDING proposal (nothing is sent) and return status='pending' so the caller can ask the owner.
    For an EXISTING row, returns the STORED term — so if the owner edited the term on approval, the
    caller fetches the owner's edited term, not the model's original phrasing."""
    norm = _norm(term)
    if not norm:
        return {"status": "denied", "id": None, "term": term}
    row = conn.execute("SELECT id, status, term FROM external_lookups WHERE tool=? AND norm_key=?",
                       (tool, norm)).fetchone()
    if row:
        return {"status": row["status"], "id": row["id"], "term": row["term"]}
    cur = conn.execute("INSERT INTO external_lookups (tool, term, norm_key) VALUES (?,?,?)",
                       (tool, term, norm))
    conn.commit()
    return {"status": "pending", "id": cur.lastrowid, "term": term}


def decide(conn, lookup_id: int, *, approve: bool, term: str | None = None) -> dict | None:
    """Record the owner's decision; returns {tool, term} (the term to fetch) or None. `term` lets the
    owner EDIT what's sent (e.g. trim a PHI-laden question to a clean topic). On an edited approval we
    also pre-approve the edited term's own key, so whether the model re-calls its original phrasing or
    the edited topic, it matches an approved row and fetches the edited term."""
    row = conn.execute("SELECT tool, term, norm_key FROM external_lookups WHERE id=?", (lookup_id,)).fetchone()
    if not row:
        return None
    final = (term or "").strip() or row["term"]
    status = "approved" if approve else "denied"
    conn.execute("UPDATE external_lookups SET status=?, term=?, decided_at=datetime('now') WHERE id=?",
                 (status, final, lookup_id))
    if approve and _norm(final) and _norm(final) != row["norm_key"]:
        conn.execute(
            "INSERT INTO external_lookups (tool, term, norm_key, status, decided_at) "
            "VALUES (?,?,?, 'approved', datetime('now')) "
            "ON CONFLICT(tool, norm_key) DO UPDATE SET status='approved', term=excluded.term, "
            "decided_at=datetime('now')",
            (row["tool"], final, _norm(final)))
    conn.commit()
    return {"tool": row["tool"], "term": final}
