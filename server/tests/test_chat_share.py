"""Encrypted share-link chat: the load-bearing invariant is that the server is a BLIND
RELAY. It mints a 1:1 link, stores only opaque ciphertext + the two wrapped copies of the
channel key (never the key itself), persists a backlog only when asked, and purges an
ephemeral channel on close. Uses a raw SQLite DB from schema.sql — no app/LLM needed.
"""
import sqlite3
from pathlib import Path

import pytest

from app.services import chat_relay
from app.services import chat_share as cs
from app.services import share as share_svc

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    chat_relay.reset()                       # hubs are process-global keyed by link_id
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    yield c
    chat_relay.reset()


def _mk(conn, *, persist=True, otp=False):
    token, link_id = cs.create_channel(
        conn, owner_wrap="OWNER_SEALED", guest_wrap="GUEST_SEALED",
        persist=persist, otp_required=otp, label="x", ttl_days=7, single_use=False)
    conn.commit()
    return token, link_id


def test_mint_is_one_to_one_chat_link(conn):
    token, link_id = _mk(conn)
    row = conn.execute("SELECT kind, bind, scope, note_id FROM share_links WHERE id=?", (link_id,)).fetchone()
    assert row["kind"] == "chat"
    assert row["bind"] == 1               # strictly 1:1 — first recipient claims it
    assert row["note_id"] is None         # backs no page until (optionally) saved to the brain
    # The link resolves as a public capability and exposes ONLY the guest-side wrapped key.
    link = share_svc.resolve_active_link(conn, token)
    desc = cs.channel_for_link(conn, link)
    assert desc["guest_wrap"] == "GUEST_SEALED"
    assert "owner_wrap" not in desc       # the owner-side wrap never goes out a public route


def test_server_stores_only_ciphertext(conn):
    _, link_id = _mk(conn)
    cs.append_message(conn, link_id, "guest", "IVAAA", "CIPHERTEXT-OPAQUE")
    row = conn.execute("SELECT iv, ciphertext FROM chat_messages WHERE share_link_id=?", (link_id,)).fetchone()
    assert row["ciphertext"] == "CIPHERTEXT-OPAQUE"   # stored verbatim; the server can't read it
    # The owner-side wrap is the key sealed under the access key — never the raw key.
    ch = cs.get_channel(conn, link_id)
    assert ch["owner_wrap"] == "OWNER_SEALED"


def test_seq_is_monotonic_per_channel(conn):
    _, a = _mk(conn)
    _, b = _mk(conn)
    assert [cs.append_message(conn, a, "owner", "i", "c")["seq"] for _ in range(3)] == [1, 2, 3]
    assert cs.append_message(conn, b, "guest", "i", "c")["seq"] == 1   # independent per channel


def test_persist_keeps_backlog_ephemeral_keeps_nothing(conn):
    _, keep = _mk(conn, persist=True)
    _, eph = _mk(conn, persist=False)
    cs.append_message(conn, keep, "owner", "i", "c")
    cs.append_message(conn, eph, "owner", "i", "c")
    assert len(cs.backlog(conn, keep)) == 1
    assert cs.backlog(conn, eph) == []                # ephemeral writes no row
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages WHERE share_link_id=?", (eph,)).fetchone()["c"] == 0


def test_backlog_resume_after_seq(conn):
    _, link_id = _mk(conn)
    for _ in range(5):
        cs.append_message(conn, link_id, "owner", "i", "c")
    assert [m["seq"] for m in cs.backlog(conn, link_id, after_seq=3)] == [4, 5]


def test_files_are_channel_scoped(conn):
    _, a = _mk(conn)
    _, b = _mk(conn)
    fid = cs.store_file(conn, a, "IVF", b"ENCRYPTED-BYTES")
    assert bytes(cs.get_file(conn, a, fid)["blob"]) == b"ENCRYPTED-BYTES"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):                # another channel can't reach it
        cs.get_file(conn, b, fid)


def test_close_revokes_and_purges_ephemeral(conn):
    _, keep = _mk(conn, persist=True)
    _, eph = _mk(conn, persist=False)
    for lid in (keep, eph):
        cs.append_message(conn, lid, "owner", "i", "c")
        cs.store_file(conn, lid, "iv", b"x")
    cs.close_channel(conn, keep)
    cs.close_channel(conn, eph)
    # Both links are revoked → unreachable by the recipient.
    assert share_svc.resolve_active_link(conn, conn.execute(
        "SELECT token FROM share_links WHERE id=?", (keep,)).fetchone()["token"]) is None
    # Persisted history survives close (so it can still be saved); ephemeral is wiped.
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages WHERE share_link_id=?", (keep,)).fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages WHERE share_link_id=?", (eph,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM chat_files WHERE share_link_id=?", (eph,)).fetchone()["c"] == 0


def test_append_to_closed_channel_rejected(conn):
    _, link_id = _mk(conn)
    cs.close_channel(conn, link_id)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        cs.append_message(conn, link_id, "guest", "i", "c")


def test_save_to_brain_is_idempotent(conn):
    """A second auto-save (e.g. from another owner device) returns the existing note
    instead of writing a duplicate. Exercised via the saved_note_id short-circuit so the
    test stays free of the embedding/FTS pipeline."""
    _, link_id = _mk(conn)
    conn.execute("INSERT INTO notes (slug, title, content_md) VALUES ('chat-x','notes/Chat','b')")
    nid = conn.execute("SELECT id FROM notes WHERE slug='chat-x'").fetchone()["id"]
    conn.execute("UPDATE chat_channels SET saved_note_id=? WHERE share_link_id=?", (nid, link_id))
    conn.commit()
    out = cs.save_to_brain(conn, link_id, transcript_md="hi", title=None, attachments=[], guest_name="Sam")
    assert out == {"note_slug": "chat-x", "already_saved": True}
