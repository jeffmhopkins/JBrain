"""Encrypted share-link chat (kind='chat') — the server side of a blind relay.

The owner mints a chat link; the recipient opens it and the two talk in real time.
End-to-end encryption is done entirely in the browser (see web/src/crypto.ts): the
server stores only the two *wrapped* copies of the channel key and opaque AES-GCM
ciphertext for every message/file. It can decrypt none of it.

What the server DOES do: relay live traffic (chat_relay), persist the encrypted
backlog when the channel is 'persist', track presence so it can push the owner when
a recipient is waiting, and — on close — accept the owner's CLIENT-DECRYPTED
transcript and write it into the brain as a normal note (the deliberate "declassify"
step that keeps zero-knowledge intact while still feeding analysis).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from . import chat_relay
from . import notes as notes_svc
from . import push
from . import share as share_svc

# Recipient file caps: the stored blob is AES-GCM ciphertext (plaintext + 16-byte tag),
# so allow a little headroom over the 100 MB attachment ceiling the rest of the app uses.
MAX_CHAT_FILE_BYTES = 100 * 1024 * 1024 + 4096
_PUSH_DEBOUNCE_SECONDS = 45.0          # don't re-push "someone's waiting" more often than this


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- Owner: create -----------------------------------------------------------

def create_channel(conn, *, owner_wrap: str, guest_wrap: str, persist: bool, otp_required: bool,
                   label: str | None, ttl_days: int | None, single_use: bool,
                   owner_name: str | None = None) -> tuple[str, int]:
    """Mint a chat link (always browser-bound — chat is strictly 1:1) and its channel
    row holding the wrapped keys. Returns (token, link_id). The raw channel key is never
    seen here: owner_wrap/guest_wrap are sealed client-side."""
    token = share_svc.mint_token()
    exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
    cur = conn.execute(
        "INSERT INTO share_links (token, note_id, scope, kind, label, bind, expires_at) "
        "VALUES (?, NULL, 'view', 'chat', ?, 1, " + ("datetime('now', ?))" if exp else "NULL)"),
        (token, label) + ((exp,) if exp else ()),
    )
    link_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chat_channels (share_link_id, persist, otp_required, owner_wrap, guest_wrap, owner_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (link_id, 1 if persist else 0, 1 if otp_required else 0, owner_wrap, guest_wrap,
         (owner_name or "").strip()[:80] or None),
    )
    return token, link_id


def create_pending_channel(conn, *, persist: bool, otp_required: bool, label: str | None,
                           owner_name: str | None, ttl_days: int | None) -> tuple[str, int]:
    """Mint a DRAFT chat link the AI set up. The channel key can only be generated in the
    owner's browser (zero-knowledge), so the wraps are left empty and pending_setup=1 until
    the owner FINALIZES it in Shares (one tap) — mirroring guided/research's draft→activate.
    A pending link is inert to recipients (no key exists yet)."""
    token = share_svc.mint_token()
    exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
    cur = conn.execute(
        "INSERT INTO share_links (token, note_id, scope, kind, label, bind, expires_at) "
        "VALUES (?, NULL, 'view', 'chat', ?, 1, " + ("datetime('now', ?))" if exp else "NULL)"),
        (token, label) + ((exp,) if exp else ()),
    )
    link_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chat_channels (share_link_id, persist, otp_required, owner_wrap, guest_wrap, "
        "owner_name, pending_setup) VALUES (?, ?, ?, '', '', ?, 1)",
        (link_id, 1 if persist else 0, 1 if otp_required else 0, (owner_name or "").strip()[:80] or None),
    )
    return token, link_id


def finalize_channel(conn, link_id: int, *, owner_wrap: str, guest_wrap: str) -> None:
    """Complete a draft chat link: store the browser-generated wrapped keys and clear the
    pending flag, so the link becomes usable. No-op-safe if already finalized."""
    ch = get_channel(conn, link_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    conn.execute(
        "UPDATE chat_channels SET owner_wrap=?, guest_wrap=?, pending_setup=0 WHERE share_link_id=?",
        (owner_wrap, guest_wrap, link_id))


def get_channel(conn, link_id: int):
    return conn.execute("SELECT * FROM chat_channels WHERE share_link_id = ?", (link_id,)).fetchone()


def channel_for_link(conn, link) -> dict:
    """The recipient-facing channel descriptor (no secrets the fragment can't already
    unlock). guest_wrap is opaque without the link fragment, so it's safe to expose."""
    ch = get_channel(conn, link["id"])
    if ch is None or ch["status"] != "active" or ch["pending_setup"]:
        raise HTTPException(status_code=404, detail="This link isn't available.")
    return {
        "kind": "chat",
        "persist": bool(ch["persist"]),
        "otp_required": bool(ch["otp_required"]),
        "guest_wrap": ch["guest_wrap"],
        "guest_name": ch["guest_name"],
        "status": ch["status"],
    }


# --- Messaging ---------------------------------------------------------------

def append_message(conn, link_id: int, sender: str, iv: str, ciphertext: str) -> dict:
    """Allocate the next seq, persist the ciphertext for 'persist' channels, and fan the
    event out to the other side. Returns the broadcast event (so the sender's own POST can
    echo the assigned seq)."""
    ch = get_channel(conn, link_id)
    if ch is None or ch["status"] != "active":
        raise HTTPException(status_code=409, detail="This chat has ended.")
    if len(iv) > 64 or len(ciphertext) > 700_000:        # ~512 KB plaintext ceiling per message
        raise HTTPException(status_code=413, detail="Message too large.")
    seq = chat_relay.next_seq(
        link_id,
        lambda: conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM chat_messages WHERE share_link_id = ?",
                             (link_id,)).fetchone()["m"],
    )
    at = _utcnow()
    if ch["persist"]:
        conn.execute(
            "INSERT INTO chat_messages (share_link_id, seq, sender, iv, ciphertext) VALUES (?, ?, ?, ?, ?)",
            (link_id, seq, sender, iv, ciphertext))
    col = "last_owner_at" if sender == "owner" else "last_guest_at"
    conn.execute(f"UPDATE chat_channels SET {col} = ? WHERE share_link_id = ?", (at, link_id))
    conn.commit()
    event = {"type": "message", "seq": seq, "sender": sender, "iv": iv, "ct": ciphertext, "at": at}
    chat_relay.publish(link_id, event)
    return event


def backlog(conn, link_id: int, after_seq: int = 0) -> list[dict]:
    """Persisted ciphertext history after `after_seq` (resume support). Empty for
    ephemeral channels, which keep nothing."""
    rows = conn.execute(
        "SELECT seq, sender, iv, ciphertext, created_at FROM chat_messages "
        "WHERE share_link_id = ? AND seq > ? ORDER BY seq",
        (link_id, after_seq)).fetchall()
    return [{"type": "message", "seq": r["seq"], "sender": r["sender"],
             "iv": r["iv"], "ct": r["ciphertext"], "at": r["created_at"]} for r in rows]


def store_file(conn, link_id: int, iv: str, blob: bytes) -> int:
    """Persist one encrypted file blob; returns its id (the sender then references it
    inside an encrypted message, so the filename/mime never reach the server)."""
    ch = get_channel(conn, link_id)
    if ch is None or ch["status"] != "active":
        raise HTTPException(status_code=409, detail="This chat has ended.")
    if len(blob) > MAX_CHAT_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (100 MB max).")
    cur = conn.execute(
        "INSERT INTO chat_files (share_link_id, iv, blob, byte_size) VALUES (?, ?, ?, ?)",
        (link_id, iv, blob, len(blob)))
    conn.commit()
    return cur.lastrowid


def get_file(conn, link_id: int, file_id: int):
    """Fetch one encrypted file blob — scoped to its channel so a token can never reach
    another channel's files."""
    row = conn.execute(
        "SELECT iv, blob FROM chat_files WHERE id = ? AND share_link_id = ?", (file_id, link_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return row


# --- Presence + push ---------------------------------------------------------

def handle_presence(conn, link_id: int) -> None:
    """Broadcast the current presence and, when a recipient is waiting with no owner
    attached, push the owner (debounced) so they can join in one tap. Called whenever a
    subscriber attaches or detaches."""
    owner_present, guest_present = chat_relay.present(link_id)
    chat_relay.publish(link_id, {"type": "presence", "owner": owner_present, "guest": guest_present})
    if guest_present and not owner_present:
        h = chat_relay.hub(link_id)
        now = time.monotonic()
        if now - h.last_owner_notify < _PUSH_DEBOUNCE_SECONDS:
            return
        h.last_owner_notify = now
        ch = get_channel(conn, link_id)
        if ch is None or ch["status"] != "active":
            return
        who = (ch["guest_name"] or "Someone").strip() or "Someone"
        push.notify(f"{who} is waiting to chat", "Open the encrypted chat to join.",
                    f"/shares/chat/{link_id}")


# --- Owner: close + save -----------------------------------------------------

def close_channel(conn, link_id: int) -> None:
    """End the chat: mark it closed, revoke the link (so the recipient is dropped), purge
    an ephemeral channel's transient blobs, and tell both sides over the relay."""
    ch = get_channel(conn, link_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    if ch["status"] != "closed":
        conn.execute("UPDATE chat_channels SET status = 'closed', closed_at = datetime('now') "
                     "WHERE share_link_id = ?", (link_id,))
        share_svc.revoke_link(conn, link_id)
        if not ch["persist"]:                       # ephemeral: nothing is kept after close
            conn.execute("DELETE FROM chat_messages WHERE share_link_id = ?", (link_id,))
            conn.execute("DELETE FROM chat_files WHERE share_link_id = ?", (link_id,))
        conn.commit()
    chat_relay.publish(link_id, {"type": "closed"})


def save_to_brain(conn, link_id: int, *, transcript_md: str, title: str | None,
                  attachments: list[dict], guest_name: str | None) -> dict:
    """Commit the owner's CLIENT-DECRYPTED transcript (+ any decrypted attachments) into the
    brain as a normal note. Idempotent per channel: a second call returns the existing note
    (auto-save can fire from more than one owner device). This is the only point plaintext
    ever reaches the server, and only because the owner chose to keep it."""
    from . import attachments as att_svc
    ch = get_channel(conn, link_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    if ch["saved_note_id"]:
        row = conn.execute("SELECT slug FROM notes WHERE id = ? AND deleted_at IS NULL",
                           (ch["saved_note_id"],)).fetchone()
        if row:
            return {"note_slug": row["slug"], "already_saved": True}
    who = (guest_name or ch["guest_name"] or "a guest").strip() or "a guest"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = (title or f"Chat with {who} — {day}").strip()[:120]
    note_title = notes_svc.root_title(f"chats/{base}", "notes")     # file every saved chat under notes/chats/
    body = (transcript_md or "").strip() or "_(no messages)_"
    if "_Encrypted chat" not in body:                # provenance footer → entity extraction links the person
        body = body.rstrip() + f"\n\n---\n_Encrypted chat with {who}._\n"
    note_id = notes_svc.upsert_note(conn, note_title, body, create_only=True,
                                    source="shared", version_note=f"encrypted chat with {who}")
    # Saved chats default to Research-only: the assistant may read them when asked
    # (tool_access stays 1), but an outsider-co-authored transcript is NOT folded into
    # evergreen KB articles. The owner can opt a chat back into the KB per-note.
    conn.execute("UPDATE notes SET kb_ingest = 0 WHERE id = ?", (note_id,))
    for a in (attachments or []):
        try:
            import base64
            raw = base64.b64decode(a.get("data") or "")
            if raw:
                att_svc.add_attachment(conn, note_id, (a.get("name") or "file")[:200],
                                       a.get("mime") or "application/octet-stream", raw)
        except Exception:                            # noqa: BLE001 — one bad file never loses the transcript
            continue
    conn.execute("UPDATE chat_channels SET saved_note_id = ? WHERE share_link_id = ?", (note_id, link_id))
    conn.commit()
    row = conn.execute("SELECT slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return {"note_slug": row["slug"], "already_saved": False}


def owner_detail(conn, link_id: int) -> dict:
    """Everything the owner's chat view needs to attach: the wrapped key for THIS device to
    unseal, channel settings, presence, and whether it still needs an auto-save."""
    ch = get_channel(conn, link_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    link = conn.execute("SELECT token, expires_at FROM share_links WHERE id = ?", (link_id,)).fetchone()
    owner_present, guest_present = chat_relay.present(link_id)
    saved_slug = None
    if ch["saved_note_id"]:
        r = conn.execute("SELECT slug FROM notes WHERE id = ? AND deleted_at IS NULL",
                         (ch["saved_note_id"],)).fetchone()
        saved_slug = r["slug"] if r else None
    return {
        "link_id": link_id,
        "token": link["token"] if link else None,
        "url": share_svc.share_url(link["token"]) if link else None,
        "owner_wrap": ch["owner_wrap"],
        "persist": bool(ch["persist"]),
        "otp_required": bool(ch["otp_required"]),
        "pending_setup": bool(ch["pending_setup"]),
        "status": ch["status"],
        "guest_name": ch["guest_name"],
        "owner_name": ch["owner_name"],
        "saved_note_slug": saved_slug,
        "expires_at": link["expires_at"] if link else None,
        "present": {"owner": owner_present, "guest": guest_present},
    }


def list_channels(conn) -> list[dict]:
    """Owner-side list of chat channels (active + closed) for the Shares page."""
    rows = conn.execute(
        "SELECT c.share_link_id AS link_id, c.persist, c.otp_required, c.pending_setup, c.status, "
        "       c.guest_name, c.owner_name, c.created_at, c.closed_at, c.last_guest_at, c.last_owner_at, "
        "       sl.token, sl.label, sl.expires_at, "
        "       n.slug AS saved_note_slug "
        "FROM chat_channels c JOIN share_links sl ON sl.id = c.share_link_id "
        "LEFT JOIN notes n ON n.id = c.saved_note_id AND n.deleted_at IS NULL "
        "ORDER BY c.created_at DESC LIMIT 100"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["persist"] = bool(d["persist"])
        d["otp_required"] = bool(d["otp_required"])
        d["pending_setup"] = bool(d["pending_setup"])
        d["url"] = share_svc.share_url(r["token"])
        out.append(d)
    return out


def sse(event: dict) -> str:
    """Encode one event in the same SSE envelope the rest of the app uses."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
