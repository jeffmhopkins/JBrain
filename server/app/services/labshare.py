"""Lab-share lifecycle (owner side): mint a kind='labs' link, attach an owner-approved scope
(analyte allow-list + date window), activate it (the approval gate — inert until then), and
manage recipient sessions + audit. The recipient-facing AI lives in a SEPARATE, import-isolated
module (labshare_ai.py); this module is owner-trusted plumbing.

Security note: `analytes_json` is the allow-list and THE exposed boundary. Activation refuses an
empty allow-list (default-deny), so a link can never go live exposing "everything".
"""
from __future__ import annotations

import json
import secrets

from ..db import get_conn, submit_write
from . import share as share_svc


def allowed_analytes(spec) -> set[str]:
    """Return the exposed analyte allow-list from a labshare spec.

    This is the ONLY thing that gates what a recipient can read.

    Args:
        spec: labshare_specs row with an 'analytes_json' field.

    Returns:
        Set of analyte slug strings; empty set if the field is absent or malformed.
    """
    try:
        return {str(a) for a in json.loads(spec["analytes_json"] or "[]") if a}
    except Exception:  # noqa: BLE001
        return set()


def get_spec(conn, link_id: int):
    """Fetch the labshare spec row for a given share link.

    Args:
        conn: Database connection.
        link_id: Share link ID.

    Returns:
        labshare_specs row, or None if not found.
    """
    return conn.execute("SELECT * FROM labshare_specs WHERE share_link_id = ?", (link_id,)).fetchone()


def create(conn, *, analytes, window_from=None, window_to=None, allow_chat=True,
           intro="", topics="", persona_voice="", label=None, ttl_days=14, bind=True,
           single_use=False, max_turns=30, max_total_replies=120) -> tuple[str, int]:
    """Mint a lab-share link and its DRAFT spec. The link is inert until activate() is called.

    Args:
        conn: Database connection.
        analytes: Iterable of analyte slug strings to include in the allow-list.
        window_from: Optional ISO date lower bound for shared results.
        window_to: Optional ISO date upper bound for shared results.
        allow_chat: Whether to enable the AI chat interface for recipients.
        intro: Optional introductory message shown to recipients.
        topics: Optional topic scope restriction for the AI.
        persona_voice: Optional tone/role instruction for the AI (rules are not overrideable).
        label: Optional human-readable label for the link.
        ttl_days: Link time-to-live in days.
        bind: Whether to bind the link to the requester's IP.
        single_use: Whether the link expires after one session.
        max_turns: Maximum AI turns per recipient session.
        max_total_replies: Maximum total AI replies across all sessions.

    Returns:
        Tuple of (token, link_id).
    """
    token, link_id = share_svc.create_labshare_link(conn, label=label, ttl_days=ttl_days, bind=bind)
    conn.execute(
        "INSERT INTO labshare_specs (share_link_id, analytes_json, window_from, window_to, allow_chat, "
        "intro, topics, persona_voice, bind, single_use, max_turns, max_total_replies) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (link_id, json.dumps(sorted({str(a) for a in (analytes or []) if a})),
         window_from or None, window_to or None, 1 if allow_chat else 0,
         intro or "", topics or "", persona_voice or "", 1 if bind else 0,
         1 if single_use else 0, max(1, int(max_turns)), max(1, int(max_total_replies))))
    return token, link_id


def set_scope(conn, link_id: int, *, analytes=None, window_from=..., window_to=..., allow_chat=None) -> None:
    """Adjust a draft spec's scope before activation, or narrow it after activation.

    Removing an analyte takes it out of reach on the next read, like research's
    remove_approved. Uses sentinel '...' to distinguish 'not provided' from 'set to None'.

    Args:
        conn: Database connection.
        link_id: Share link ID.
        analytes: New analyte allow-list, or None to leave unchanged.
        window_from: New window start (ISO date, None, or '...' to leave unchanged).
        window_to: New window end (ISO date, None, or '...' to leave unchanged).
        allow_chat: New chat-enable flag, or None to leave unchanged.
    """
    sets, params = [], []
    if analytes is not None:
        sets.append("analytes_json = ?"); params.append(json.dumps(sorted({str(a) for a in analytes if a})))
    if window_from is not ...:
        sets.append("window_from = ?"); params.append(window_from or None)
    if window_to is not ...:
        sets.append("window_to = ?"); params.append(window_to or None)
    if allow_chat is not None:
        sets.append("allow_chat = ?"); params.append(1 if allow_chat else 0)
    if not sets:
        return
    params.append(link_id)
    conn.execute(f"UPDATE labshare_specs SET {', '.join(sets)} WHERE share_link_id = ?", params)


def activate(conn, link_id: int) -> bool:
    """Advance a draft spec to active — the owner approval gate.

    Refuses an empty allow-list (default-deny) so a link can never go live without an
    explicit analyte selection.

    Args:
        conn: Database connection.
        link_id: Share link ID.

    Returns:
        True if the spec was activated; False if it was not in draft state or had no analytes.
    """
    spec = get_spec(conn, link_id)
    if not spec or spec["status"] != "draft" or not allowed_analytes(spec):
        return False
    conn.execute("UPDATE labshare_specs SET status='active' WHERE share_link_id = ?", (link_id,))
    return True


# --- recipient sessions + audit --------------------------------------------

def session_for(conn, link_id: int, secret: str):
    """Fetch a recipient session by link ID and cookie secret.

    Args:
        conn: Database connection.
        link_id: Share link ID.
        secret: Session cookie secret token.

    Returns:
        labshare_sessions row, or None if not found.
    """
    return conn.execute(
        "SELECT * FROM labshare_sessions WHERE share_link_id = ? AND secret = ?",
        (link_id, secret)).fetchone()


def start_session(conn, link_id: int, *, name=None, client_ip=None,
                  claim_bind: bool = False) -> tuple[int, str, str | None]:
    """Create a recipient session, optionally claiming a bind link, in one write unit.

    Runs the bind-claim (when ``claim_bind`` is set), the session INSERT, and the
    link ``touch`` together on the dedicated writer connection so the route holds no
    write across them. The caller passes only reads beforehand; cookies are set from
    the returned secrets.

    Args:
        conn: Database connection (request conn; reads only — the writes happen on the
            writer connection inside the unit).
        link_id: Share link ID.
        name: Optional recipient display name.
        client_ip: Optional client IP address for audit.
        claim_bind: When True, mint and store a bind secret to lock this link to the
            calling browser (an unclaimed bind link).

    Returns:
        Tuple of (session_id, session_secret, bind_secret) where bind_secret is the
        newly minted bind token when ``claim_bind`` is True, else None.
    """
    def _unit() -> tuple[int, str, str | None]:
        c = get_conn()
        bind_secret = None
        if claim_bind:
            bind_secret = share_svc.mint_token()
            c.execute("UPDATE share_links SET bind_secret=?, bound_at=datetime('now') WHERE id=?",
                      (bind_secret, link_id))
        secret = secrets.token_urlsafe(32)
        cur = c.execute(
            "INSERT INTO labshare_sessions (share_link_id, secret, name, client_ip, last_at) "
            "VALUES (?,?,?,?, datetime('now'))", (link_id, secret, name, client_ip))
        c.execute("UPDATE share_links SET last_used_at = datetime('now') WHERE id = ?", (link_id,))
        c.commit()
        return cur.lastrowid, secret, bind_secret

    return submit_write(_unit).result()


def record_turn(conn, session_id: int, *, transcript_json: str, charted: list[str] | None = None,
                denied: int = 0) -> None:
    """Persist a turn's transcript and audit data.

    Records which analytes were charted/served and counts out-of-scope attempts so the
    owner can see exactly what was asked and shown.

    Args:
        conn: Database connection.
        session_id: Session row ID.
        transcript_json: Full conversation transcript as a JSON string.
        charted: Analyte slugs whose charts were served in this turn, or None.
        denied: Number of out-of-scope requests in this turn.
    """
    extra = ""
    params: list = [transcript_json]
    if charted:
        # Merge into the audit set of analytes ever served to this session.
        existing = conn.execute("SELECT charted_json FROM labshare_sessions WHERE id=?", (session_id,)).fetchone()
        seen = set(json.loads(existing["charted_json"] or "[]")) if existing else set()
        seen.update(charted)
        extra += ", charted_json = ?"; params.append(json.dumps(sorted(seen)))
    if denied:
        extra += ", denied_count = denied_count + ?"; params.append(int(denied))
    params.append(session_id)
    conn.execute(
        f"UPDATE labshare_sessions SET transcript_json = ?, turn_count = turn_count + 1, "
        f"last_at = datetime('now'){extra} WHERE id = ?", params)


def audit(conn, link_id: int) -> list[dict]:
    """Return owner-facing audit data: per-session summary of what was asked and shown.

    Args:
        conn: Database connection.
        link_id: Share link ID.

    Returns:
        List of session summary dicts ordered by created_at DESC, each with keys: id,
        name, status, turns, charted, denied, client_ip, created_at, last_at.
    """
    rows = conn.execute(
        "SELECT id, name, status, turn_count, charted_json, denied_count, client_ip, created_at, last_at "
        "FROM labshare_sessions WHERE share_link_id = ? ORDER BY created_at DESC", (link_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "turns": r["turn_count"],
             "charted": json.loads(r["charted_json"] or "[]"), "denied": r["denied_count"],
             "client_ip": r["client_ip"], "created_at": r["created_at"], "last_at": r["last_at"]}
            for r in rows]
