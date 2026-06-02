"""Share links: unguessable, single-note, unauthenticated capability tokens.

A token grants access to exactly ONE note. 'view' = read it; 'edit' = read it AND
submit edit PROPOSALS (never a direct write). The token is stored as-is (so the
owner can re-copy the link from the Shares card); revoking a link kills it
instantly, so a leaked token's blast radius is one note until revoked. Every
public lookup resolves token -> note_id; no public route ever takes a note
id/slug, so a token can never reach another note.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone

from ..config import get_settings
from . import reviews as reviews_svc


def mint_token() -> str:
    return secrets.token_urlsafe(32)            # 256-bit, URL-safe


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def share_url(token: str) -> str:
    """Absolute URL a recipient opens, built from JBRAIN_DOMAIN."""
    domain = (get_settings().jbrain_domain or "localhost").rstrip("/")
    if domain.startswith("http://") or domain.startswith("https://"):
        base = domain
    else:
        scheme = "http" if domain.startswith(("localhost", "127.")) else "https"
        base = f"{scheme}://{domain}"
    return f"{base}/share/{token}"


def resolve_active_link(conn, token: str):
    """Return the share_links row (joined with note title/slug/content) for a valid,
    active, non-expired token on a live note — else None. The single chokepoint that
    every public route goes through. Uniform None means every failure looks alike."""
    if not token or len(token) < 20:            # cheap shape gate before any DB hit
        return None
    row = conn.execute(
        "SELECT sl.*, n.title, n.slug, n.kind, n.content_md, n.updated_at, n.deleted_at AS note_deleted "
        "FROM share_links sl JOIN notes n ON n.id = sl.note_id WHERE sl.token = ?",
        (token,),
    ).fetchone()
    if row is None or row["status"] != "active" or row["note_deleted"] is not None:
        return None
    if row["expires_at"] and row["expires_at"] <= _utcnow():
        return None
    return row


def touch(conn, link_id: int) -> None:
    conn.execute("UPDATE share_links SET last_used_at = datetime('now') WHERE id = ?", (link_id,))


# --- Owner: minting / listing / revoking -----------------------------------

def create_link(conn, note_id: int, scope: str, label: str | None = None,
                ttl_days: int | None = None, bind: bool = False) -> str:
    token = mint_token()
    exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
    conn.execute(
        "INSERT INTO share_links (token, note_id, scope, label, bind, expires_at) "
        "VALUES (?, ?, ?, ?, ?, " + ("datetime('now', ?))" if exp else "NULL)"),
        (token, note_id, scope, label, 1 if bind else 0) + ((exp,) if exp else ()),
    )
    return token


def create_guided_link(conn, note_id: int, label: str | None = None,
                        ttl_days: int | None = 14, bind: bool = False) -> tuple[str, int]:
    """Mint a guided AI intake link (scope='view', kind='guided'). Returns (token, link_id).
    The interview spec is attached separately via guided.create_spec; the link is inert
    to recipients until the owner activates the spec (approval #1)."""
    token = mint_token()
    exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
    cur = conn.execute(
        "INSERT INTO share_links (token, note_id, scope, kind, label, bind, expires_at) "
        "VALUES (?, ?, 'view', 'guided', ?, ?, " + ("datetime('now', ?))" if exp else "NULL)"),
        (token, note_id, label, 1 if bind else 0) + ((exp,) if exp else ()),
    )
    return token, cur.lastrowid


def reset_bind(conn, link_id: int) -> None:
    """Forget the bound browser (secret + claimer name) so the link can be accepted
    fresh (e.g. it locked to the wrong in-app browser)."""
    conn.execute("UPDATE share_links SET bind_secret=NULL, bound_at=NULL, bound_name=NULL WHERE id=?", (link_id,))


def revoke_link(conn, link_id: int) -> None:
    conn.execute("UPDATE share_links SET status='revoked', revoked_at=datetime('now') "
                 "WHERE id=? AND status='active'", (link_id,))
    _clear_pending(conn, link_id)


def _clear_pending(conn, link_id: int) -> None:
    """Supersede any pending proposal for a link and dismiss its alert."""
    conn.execute(
        "UPDATE review_items SET status='dismissed', dismissed_at=datetime('now') WHERE id IN "
        "(SELECT review_item_id FROM share_proposals WHERE share_link_id=? AND status='pending' "
        " AND review_item_id IS NOT NULL)", (link_id,))
    conn.execute("UPDATE share_proposals SET status='superseded', resolved_at=datetime('now') "
                 "WHERE share_link_id=? AND status='pending'", (link_id,))


# --- Public: submit an edit proposal ----------------------------------------

def submit_proposal(conn, link, content: str, note: str | None, name: str | None,
                    client_ip: str | None) -> dict:
    """Persist a proposed new content for the link's note. Supersedes any prior
    pending proposal for the SAME link (one pending per link). Never writes the note."""
    from fastapi import HTTPException
    if link["scope"] != "edit":
        raise HTTPException(status_code=403, detail="This link is read-only.")
    # Per-link propose cap: an edit link is the only write-ish surface, so throttle
    # it on the link itself (not just per-IP) to stop proposal spam.
    recent = conn.execute(
        "SELECT COUNT(*) AS c FROM share_proposals WHERE share_link_id=? "
        "AND created_at > datetime('now', '-60 seconds')", (link["id"],),
    ).fetchone()["c"]
    if recent >= 8:
        raise HTTPException(status_code=429, detail="Too many edits in a short time — please wait a moment.")
    n = conn.execute("SELECT id, content_md FROM notes WHERE id=? AND deleted_at IS NULL",
                     (link["note_id"],)).fetchone()
    if n is None:
        raise HTTPException(status_code=409, detail="The note no longer exists.")
    who = (name or "").strip()[:80] or "Someone"
    basis_hash = hashlib.sha256((n["content_md"] or "").encode("utf-8")).hexdigest()
    _clear_pending(conn, link["id"])            # supersede prior pending + dismiss its card
    cur = conn.execute(
        "INSERT INTO share_proposals (share_link_id, note_id, basis_hash, proposed_content, "
        "proposer_name, proposer_note, client_ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (link["id"], n["id"], basis_hash, content, who, (note or "")[:2000] or None, client_ip),
    )
    prop_id = cur.lastrowid
    note_row = conn.execute("SELECT title, slug FROM notes WHERE id=?", (n["id"],)).fetchone()
    rid = reviews_svc.create_review_item(
        conn, None,
        title=f"{who} submitted an edit to {note_row['title']}",
        message=f"{who} proposed a new version via the “{link['label'] or 'shared'}” link — accept or reject it in Shares.",
        link_slug="__shares__",                 # bell deep-links to the Shares page
    )
    conn.execute("UPDATE share_proposals SET review_item_id=? WHERE id=?", (rid, prop_id))
    return {"proposal_id": prop_id}


# In-memory per-IP throttle for public share routes (defense-in-depth; the 256-bit
# token already makes enumeration infeasible). Best-effort, bounded.
_HITS: dict[str, list[float]] = {}
_WINDOW = 60.0
_MAX = 60


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _HITS.get(ip, []) if now - t < _WINDOW]
    recent.append(now)
    _HITS[ip] = recent
    if len(_HITS) > 10_000:
        _HITS.clear()
    return len(recent) > _MAX
