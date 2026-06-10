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

from ..db import get_conn, submit_write
from . import chat_relay
from . import notes as notes_svc
from . import push
from . import share as share_svc

# Recipient file caps: the stored blob is AES-GCM ciphertext (plaintext + 16-byte tag),
# so allow a little headroom over the 100 MB attachment ceiling the rest of the app uses.
MAX_CHAT_FILE_BYTES = 100 * 1024 * 1024 + 4096
_PUSH_DEBOUNCE_SECONDS = 45.0          # don't re-push "someone's waiting" more often than this


def _utcnow() -> str:
    """Return the current UTC time as a 'YYYY-MM-DD HH:MM:SS' string.

    Returns:
        UTC timestamp string.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- Owner: create -----------------------------------------------------------

def create_channel(conn, *, owner_wrap: str, guest_wrap: str, persist: bool, otp_required: bool,
                   label: str | None, ttl_days: int | None, single_use: bool,
                   owner_name: str | None = None) -> tuple[str, int]:
    """Mint a chat link and its channel row holding the wrapped keys.

    Chat is always browser-bound (strictly 1:1). The raw channel key is never seen
    here — owner_wrap/guest_wrap are sealed client-side.

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        owner_wrap: Owner's sealed copy of the channel key.
        guest_wrap: Guest's sealed copy of the channel key.
        persist: Retain encrypted message history after the session.
        otp_required: Require a one-time passcode before the guest can connect.
        label: Optional human-readable label for the Shares page.
        ttl_days: Expiry in days from now, or None for no expiry.
        single_use: Revoke after the first guest connection.
        owner_name: Owner's display name shown to the guest.

    Returns:
        A (token, link_id) tuple for the new share link.
    """
    def _unit() -> tuple[str, int]:
        c = get_conn()
        token = share_svc.mint_token()
        exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
        cur = c.execute(
            "INSERT INTO share_links (token, note_id, scope, kind, label, bind, expires_at) "
            "VALUES (?, NULL, 'view', 'chat', ?, 1, " + ("datetime('now', ?))" if exp else "NULL)"),
            (token, label) + ((exp,) if exp else ()),
        )
        link_id = cur.lastrowid
        c.execute(
            "INSERT INTO chat_channels (share_link_id, persist, otp_required, owner_wrap, guest_wrap, owner_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (link_id, 1 if persist else 0, 1 if otp_required else 0, owner_wrap, guest_wrap,
             (owner_name or "").strip()[:80] or None),
        )
        c.commit()
        return token, link_id

    return submit_write(_unit).result()


def create_pending_channel(conn, *, persist: bool, otp_required: bool, label: str | None,
                           owner_name: str | None, ttl_days: int | None) -> tuple[str, int]:
    """Mint a DRAFT chat link with empty key wraps, pending owner finalization.

    The channel key can only be generated in the owner's browser (zero-knowledge), so
    pending_setup=1 until the owner finalizes it in Shares. Mirrors guided/research
    draft→activate. A pending link is inert to recipients (no key exists yet).

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        persist: Retain encrypted message history after the session.
        otp_required: Require a one-time passcode before the guest can connect.
        label: Optional human-readable label for the Shares page.
        owner_name: Owner's display name shown to the guest.
        ttl_days: Expiry in days from now, or None for no expiry.

    Returns:
        A (token, link_id) tuple for the new share link.
    """
    def _unit() -> tuple[str, int]:
        c = get_conn()
        token = share_svc.mint_token()
        exp = f"+{int(ttl_days)} days" if (ttl_days and int(ttl_days) > 0) else None
        cur = c.execute(
            "INSERT INTO share_links (token, note_id, scope, kind, label, bind, expires_at) "
            "VALUES (?, NULL, 'view', 'chat', ?, 1, " + ("datetime('now', ?))" if exp else "NULL)"),
            (token, label) + ((exp,) if exp else ()),
        )
        link_id = cur.lastrowid
        c.execute(
            "INSERT INTO chat_channels (share_link_id, persist, otp_required, owner_wrap, guest_wrap, "
            "owner_name, pending_setup) VALUES (?, ?, ?, '', '', ?, 1)",
            (link_id, 1 if persist else 0, 1 if otp_required else 0, (owner_name or "").strip()[:80] or None),
        )
        c.commit()
        return token, link_id

    return submit_write(_unit).result()


def finalize_channel(conn, link_id: int, *, owner_wrap: str, guest_wrap: str) -> None:
    """Complete a draft chat link by storing the browser-generated wrapped keys.

    Clears pending_setup so the link becomes usable. No-op-safe if already finalized.

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        link_id: The share_links.id to finalize.
        owner_wrap: Owner's sealed copy of the channel key.
        guest_wrap: Guest's sealed copy of the channel key.

    Raises:
        HTTPException: 404 if the channel does not exist.
    """
    def _unit() -> None:
        c = get_conn()
        ch = get_channel(c, link_id)
        if ch is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        c.execute(
            "UPDATE chat_channels SET owner_wrap=?, guest_wrap=?, pending_setup=0 WHERE share_link_id=?",
            (owner_wrap, guest_wrap, link_id))
        c.commit()

    submit_write(_unit).result()


def get_channel(conn, link_id: int):
    """Fetch the chat_channels row for a share link.

    Args:
        conn: Database connection.
        link_id: The share_links.id to look up.

    Returns:
        The chat_channels row, or None if not found.
    """
    return conn.execute("SELECT * FROM chat_channels WHERE share_link_id = ?", (link_id,)).fetchone()


def channel_for_link(conn, link) -> dict:
    """Build the recipient-facing channel descriptor for an active chat link.

    Returns no secrets the link fragment can't already unlock. guest_wrap is opaque
    without the fragment, so it is safe to expose over the public API.

    Args:
        conn: Database connection.
        link: The resolved share_links row.

    Returns:
        Dict with kind, persist, otp_required, guest_wrap, guest_name, and status.

    Raises:
        HTTPException: 404 if the channel is missing, inactive, or still pending setup.
    """
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
    """Allocate the next sequence number, optionally persist, and fan out the message event.

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        link_id: The share_links.id for the channel.
        sender: 'owner' or 'guest'.
        iv: AES-GCM initialization vector (hex/base64, max 64 chars).
        ciphertext: Encrypted message body (max ~700 KB).

    Returns:
        The broadcast event dict with type, seq, sender, iv, ct, and at fields.

    Raises:
        HTTPException: 409 if the channel has ended.
        HTTPException: 413 if iv or ciphertext exceeds size limits.
    """
    def _unit() -> dict:
        c = get_conn()
        ch = get_channel(c, link_id)
        if ch is None or ch["status"] != "active":
            raise HTTPException(status_code=409, detail="This chat has ended.")
        if len(iv) > 64 or len(ciphertext) > 700_000:    # ~512 KB plaintext ceiling per message
            raise HTTPException(status_code=413, detail="Message too large.")
        seq = chat_relay.next_seq(
            link_id,
            lambda: c.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM chat_messages WHERE share_link_id = ?",
                              (link_id,)).fetchone()["m"],
        )
        at = _utcnow()
        if ch["persist"]:
            c.execute(
                "INSERT INTO chat_messages (share_link_id, seq, sender, iv, ciphertext) VALUES (?, ?, ?, ?, ?)",
                (link_id, seq, sender, iv, ciphertext))
        col = "last_owner_at" if sender == "owner" else "last_guest_at"
        c.execute(f"UPDATE chat_channels SET {col} = ? WHERE share_link_id = ?", (at, link_id))
        c.commit()
        return {"type": "message", "seq": seq, "sender": sender, "iv": iv, "ct": ciphertext, "at": at}

    event = submit_write(_unit).result()
    chat_relay.publish(link_id, event)
    return event


def backlog(conn, link_id: int, after_seq: int = 0) -> list[dict]:
    """Return persisted ciphertext history after a given sequence number.

    Empty for ephemeral channels, which retain nothing.

    Args:
        conn: Database connection.
        link_id: The share_links.id for the channel.
        after_seq: Only return messages with seq > this value (resume support).

    Returns:
        List of message event dicts (type, seq, sender, iv, ct, at).
    """
    rows = conn.execute(
        "SELECT seq, sender, iv, ciphertext, created_at FROM chat_messages "
        "WHERE share_link_id = ? AND seq > ? ORDER BY seq",
        (link_id, after_seq)).fetchall()
    return [{"type": "message", "seq": r["seq"], "sender": r["sender"],
             "iv": r["iv"], "ct": r["ciphertext"], "at": r["created_at"]} for r in rows]


def store_file(conn, link_id: int, iv: str, blob: bytes) -> int:
    """Persist one encrypted file blob and return its id.

    The sender references the id inside an encrypted message, so the filename and
    MIME type never reach the server.

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        link_id: The share_links.id for the channel.
        iv: AES-GCM initialization vector.
        blob: Raw encrypted file bytes (max 100 MB + 4096).

    Returns:
        The new chat_files.id.

    Raises:
        HTTPException: 409 if the channel has ended.
        HTTPException: 413 if the blob exceeds MAX_CHAT_FILE_BYTES.
    """
    def _unit() -> int:
        c = get_conn()
        ch = get_channel(c, link_id)
        if ch is None or ch["status"] != "active":
            raise HTTPException(status_code=409, detail="This chat has ended.")
        if len(blob) > MAX_CHAT_FILE_BYTES:
            raise HTTPException(status_code=413, detail="File too large (100 MB max).")
        cur = c.execute(
            "INSERT INTO chat_files (share_link_id, iv, blob, byte_size) VALUES (?, ?, ?, ?)",
            (link_id, iv, blob, len(blob)))
        c.commit()
        return cur.lastrowid

    return submit_write(_unit).result()


def get_file(conn, link_id: int, file_id: int):
    """Fetch one encrypted file blob scoped to its channel.

    Scoping ensures a token can never reach another channel's files.

    Args:
        conn: Database connection.
        link_id: The share_links.id for the channel (scope guard).
        file_id: The chat_files.id to retrieve.

    Returns:
        Row with iv and blob fields.

    Raises:
        HTTPException: 404 if the file does not exist or belongs to a different channel.
    """
    row = conn.execute(
        "SELECT iv, blob FROM chat_files WHERE id = ? AND share_link_id = ?", (file_id, link_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return row


# --- Presence + push ---------------------------------------------------------

def handle_presence(conn, link_id: int) -> None:
    """Broadcast current presence state and debounced-push the owner when a guest is waiting.

    Called whenever a subscriber attaches or detaches. The push is suppressed if the
    owner was notified within _PUSH_DEBOUNCE_SECONDS.

    Args:
        conn: Database connection.
        link_id: The share_links.id for the channel.
    """
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
    """End the chat: mark it closed, revoke the link, purge ephemeral blobs, and notify both sides.

    Args:
        conn: Superseded by the single-writer connection; the whole write runs in a
            ``submit_write`` unit on its own connection.
        link_id: The share_links.id for the channel to close.

    Raises:
        HTTPException: 404 if the channel does not exist.
    """
    def _unit() -> None:
        c = get_conn()
        ch = get_channel(c, link_id)
        if ch is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        if ch["status"] != "closed":
            c.execute("UPDATE chat_channels SET status = 'closed', closed_at = datetime('now') "
                      "WHERE share_link_id = ?", (link_id,))
            share_svc.revoke_link(c, link_id)
            if not ch["persist"]:                   # ephemeral: nothing is kept after close
                c.execute("DELETE FROM chat_messages WHERE share_link_id = ?", (link_id,))
                c.execute("DELETE FROM chat_files WHERE share_link_id = ?", (link_id,))
            c.commit()

    submit_write(_unit).result()
    chat_relay.publish(link_id, {"type": "closed"})


def save_to_brain(conn, link_id: int, *, transcript_md: str, title: str | None,
                  attachments: list[dict], guest_name: str | None) -> dict:
    """Commit the owner's client-decrypted transcript and attachments into the brain as a note.

    Idempotent per channel: a second call returns the existing note (auto-save may fire
    from more than one owner device). This is the only point plaintext ever reaches the
    server, and only because the owner chose to keep it.

    Args:
        conn: Database connection.
        link_id: The share_links.id for the channel.
        transcript_md: Owner-decrypted Markdown transcript text.
        title: Optional note title; defaults to 'Chat with {guest} — {date}'.
        attachments: List of {name, mime, data (base64)} dicts to save alongside.
        guest_name: Guest display name used in the note title and footer.

    Returns:
        Dict with note_slug and already_saved fields.

    Raises:
        HTTPException: 404 if the channel does not exist.
    """
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
    # Decode the attachment blobs BEFORE the write unit (cheap, but keeps base64 work
    # off the writer) and drop empties; the note + its attachments + the channel pointer
    # commit together so a re-save short-circuits cleanly.
    import base64
    files: list[tuple[str, str, bytes]] = []
    for a in (attachments or []):
        try:
            raw = base64.b64decode(a.get("data") or "")
        except Exception:                            # noqa: BLE001 — one bad file never loses the transcript
            continue
        if raw:
            files.append(((a.get("name") or "file")[:200],
                          a.get("mime") or "application/octet-stream", raw))

    def _unit() -> int:
        """Create the saved-chat note + its attachments + channel pointer on the writer.

        Returns:
            The new/owning notes.id for the saved transcript.
        """
        c = get_conn()
        nid = notes_svc.upsert_note(c, note_title, body, create_only=True,
                                    source="shared", version_note=f"encrypted chat with {who}")
        # Saved chats default to Research-only: the assistant may read them when asked
        # (tool_access stays 1), but an outsider-co-authored transcript is NOT folded into
        # evergreen KB articles. The owner can opt a chat back into the KB per-note.
        c.execute("UPDATE notes SET kb_ingest = 0 WHERE id = ?", (nid,))
        # add_attachment self-serialises via submit_write; called from inside this writer-thread
        # unit it runs INLINE (the re-entrant path), so the attachment rows land on this same
        # connection within this transaction rather than tripping the deadlock guard. One bad
        # file is swallowed so it never loses the transcript (parity with the prior loop).
        for name, mime, raw in files:
            try:
                att_svc.add_attachment(c, nid, name, mime, raw)
            except Exception:                        # noqa: BLE001 — one bad file never loses the transcript
                continue
        c.execute("UPDATE chat_channels SET saved_note_id = ? WHERE share_link_id = ?", (nid, link_id))
        c.commit()
        return nid
    note_id = submit_write(_unit).result()
    row = conn.execute("SELECT slug FROM notes WHERE id = ?", (note_id,)).fetchone()
    return {"note_slug": row["slug"], "already_saved": False}


def owner_detail(conn, link_id: int) -> dict:
    """Return everything the owner's chat view needs to attach to a channel.

    Includes the wrapped key for the owner's device to unseal, channel settings,
    current presence state, and whether the transcript still needs an auto-save.

    Args:
        conn: Database connection.
        link_id: The share_links.id for the channel.

    Returns:
        Dict with link_id, token, url, owner_wrap, persist, otp_required, pending_setup,
        status, guest_name, owner_name, saved_note_slug, expires_at, and present.

    Raises:
        HTTPException: 404 if the channel does not exist.
    """
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
    """Return the owner-side list of chat channels (active + closed) for the Shares page.

    Args:
        conn: Database connection.

    Returns:
        List of channel detail dicts, most recent first (up to 100).
    """
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
    """Encode one event in the SSE envelope used across the app.

    Args:
        event: Dict with at least a 'type' key.

    Returns:
        SSE-formatted string ending with double newline.
    """
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
