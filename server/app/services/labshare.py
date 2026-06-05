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

from . import share as share_svc


def allowed_analytes(spec) -> set[str]:
    """The exposed allow-list — the ONLY thing that gates what a recipient can read."""
    try:
        return {str(a) for a in json.loads(spec["analytes_json"] or "[]") if a}
    except Exception:  # noqa: BLE001
        return set()


def get_spec(conn, link_id: int):
    return conn.execute("SELECT * FROM labshare_specs WHERE share_link_id = ?", (link_id,)).fetchone()


def create(conn, *, analytes, window_from=None, window_to=None, allow_chat=True,
           intro="", topics="", persona_voice="", label=None, ttl_days=14, bind=True,
           single_use=False, max_turns=30, max_total_replies=120) -> tuple[str, int]:
    """Mint a lab-share link + its DRAFT spec. The link is inert until activate()."""
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
    """Adjust a DRAFT spec's scope before activation (or narrow it after — removing an analyte
    takes it out of reach on the next read, like research's remove_approved)."""
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
    """Owner approval gate: draft -> active. REFUSES an empty allow-list (default-deny) so a link
    can never go live exposing nothing-or-everything. Returns True if it activated."""
    spec = get_spec(conn, link_id)
    if not spec or spec["status"] != "draft" or not allowed_analytes(spec):
        return False
    conn.execute("UPDATE labshare_specs SET status='active' WHERE share_link_id = ?", (link_id,))
    return True


# --- recipient sessions + audit --------------------------------------------

def session_for(conn, link_id: int, secret: str):
    return conn.execute(
        "SELECT * FROM labshare_sessions WHERE share_link_id = ? AND secret = ?",
        (link_id, secret)).fetchone()


def start_session(conn, link_id: int, *, name=None, client_ip=None) -> tuple[int, str]:
    """Create a recipient session, returning (session_id, cookie_secret)."""
    secret = secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO labshare_sessions (share_link_id, secret, name, client_ip, last_at) "
        "VALUES (?,?,?,?, datetime('now'))", (link_id, secret, name, client_ip))
    return cur.lastrowid, secret


def record_turn(conn, session_id: int, *, transcript_json: str, charted: list[str] | None = None,
                denied: int = 0) -> None:
    """Persist a turn's transcript + audit (which analytes were charted/served, out-of-scope
    attempts) so the owner can see exactly what was asked and shown."""
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
    """Owner-facing audit: per recipient session, what was asked/shown and when."""
    rows = conn.execute(
        "SELECT id, name, status, turn_count, charted_json, denied_count, client_ip, created_at, last_at "
        "FROM labshare_sessions WHERE share_link_id = ? ORDER BY created_at DESC", (link_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "turns": r["turn_count"],
             "charted": json.loads(r["charted_json"] or "[]"), "denied": r["denied_count"],
             "client_ip": r["client_ip"], "created_at": r["created_at"], "last_at": r["last_at"]}
            for r in rows]
