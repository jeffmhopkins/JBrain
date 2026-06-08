"""Owner approval gate for OUTBOUND reference lookups.

A hard, code-level gate so a tool (medical_reference) never sends a term to an external service
until the owner has seen the EXACT term and approved it — the owner can be sure no PII leaves the
system in a search query. A decision is remembered per normalized term (approve "TTP" once).
"""
from __future__ import annotations


def _norm(term: str) -> str:
    """Normalize a lookup term for deduplication.

    Args:
        term: Raw term string to normalize.

    Returns:
        Normalized string; falls back to lowercased whitespace-collapsed form on error.
    """
    from . import entity_index
    try:
        return entity_index.normalize(term) or ""
    except Exception:  # noqa: BLE001
        return " ".join((term or "").lower().split())


def check_or_propose(conn, tool: str, term: str) -> dict:
    """Return the approval status for an outbound lookup term, creating a pending proposal if new.

    If there is no prior decision, records a PENDING proposal (nothing is sent externally)
    and returns status='pending' so the caller can ask the owner. For an existing row, returns
    the STORED term — if the owner edited the term on approval, the caller gets the owner's
    edited version, not the model's original phrasing.

    Args:
        conn: SQLite connection (commits internally when a new proposal is created).
        tool: Tool name identifying the lookup type (e.g. 'medical_reference').
        term: Raw term the tool wants to look up externally.

    Returns:
        Dict with keys: status ('pending', 'approved', or 'denied'), id (row pk), term.
    """
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
    """Record the owner's approve/deny decision for a pending lookup.

    The optional ``term`` argument lets the owner edit what is sent externally (e.g.
    trim a PHI-laden question to a clean topic). On an edited approval, the edited
    term's normalized key is also pre-approved so future calls with either phrasing
    match an approved row.

    Args:
        conn: SQLite connection (commits internally).
        lookup_id: Primary key of the external_lookups row to decide.
        approve: True to approve, False to deny.
        term: Optional owner-edited term to use instead of the original.

    Returns:
        Dict with keys: tool, term (the term to fetch), or None if the row is not found.
    """
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
