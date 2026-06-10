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

pytestmark = pytest.mark.integration

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    chat_relay.reset()                       # hubs are process-global keyed by link_id
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    yield c
    chat_relay.reset()


@pytest.fixture(autouse=True)
def writer_on(conn, monkeypatch):
    """Route the chat-share single-writer seam at this isolated in-memory ``conn``.

    The converted write functions (create/finalize/append/store_file/close) now persist
    through ``submit_write`` on the dedicated writer connection (``db.get_conn()`` on the
    writer thread). These tests inject their own ``:memory:`` connection with no app
    bootstrap, so the real writer would open an unrelated file DB. Patch the module seams
    to run the write unit inline against the test ``conn`` instead, preserving the test's
    isolation while exercising the refactored code path.
    """
    from concurrent.futures import Future

    def _inline(fn):
        """Run the write unit inline, mirroring submit_write's Future contract."""
        f: Future = Future()
        try:
            f.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 — mirror submit_write's contract
            f.set_exception(exc)
        return f

    monkeypatch.setattr(cs, "get_conn", lambda: conn)
    monkeypatch.setattr(cs, "submit_write", _inline)
    return conn


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


def test_pending_chat_is_inert_until_finalized(conn):
    """An AI-drafted chat link has no key yet (wraps empty, pending_setup=1) and is
    unreachable by a recipient until the owner finalizes it in the browser."""
    from fastapi import HTTPException
    token, link_id = cs.create_pending_channel(
        conn, persist=True, otp_required=True, label="Chat with Sarah", owner_name="Jeff", ttl_days=0)
    conn.commit()
    ch = cs.get_channel(conn, link_id)
    assert ch["pending_setup"] == 1 and ch["owner_wrap"] == "" and ch["guest_wrap"] == ""
    assert cs.owner_detail(conn, link_id)["pending_setup"] is True
    link = share_svc.resolve_active_link(conn, token)
    with pytest.raises(HTTPException):                       # recipient can't open a keyless draft
        cs.channel_for_link(conn, link)
    # Owner's browser mints the key and supplies the wraps.
    cs.finalize_channel(conn, link_id, owner_wrap="OW", guest_wrap="GW")
    conn.commit()
    assert cs.owner_detail(conn, link_id)["pending_setup"] is False
    assert cs.channel_for_link(conn, link)["guest_wrap"] == "GW"


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


# --- size / status guards on the write paths ---------------------------------

def test_oversized_message_rejected(conn):
    """The per-message ceiling is enforced on the OPAQUE ciphertext (the server can't
    see plaintext), and on the iv length — either over-limit is a 413."""
    from fastapi import HTTPException
    _, link_id = _mk(conn)
    with pytest.raises(HTTPException) as ei:                 # ciphertext over ~512 KB plaintext
        cs.append_message(conn, link_id, "owner", "i", "x" * 700_001)
    assert ei.value.status_code == 413
    with pytest.raises(HTTPException) as ei2:                # iv blown past 64 chars
        cs.append_message(conn, link_id, "owner", "y" * 65, "c")
    assert ei2.value.status_code == 413
    # Nothing oversized ever lands in the store.
    assert cs.backlog(conn, link_id) == []


def test_store_file_oversized_and_closed_rejected(conn):
    from fastapi import HTTPException
    _, link_id = _mk(conn)
    with pytest.raises(HTTPException) as ei:                 # over the 100 MB (+tag headroom) cap
        cs.store_file(conn, link_id, "iv", b"z" * (cs.MAX_CHAT_FILE_BYTES + 1))
    assert ei.value.status_code == 413
    cs.close_channel(conn, link_id)
    with pytest.raises(HTTPException) as ei2:                # no uploads to an ended chat
        cs.store_file(conn, link_id, "iv", b"z")
    assert ei2.value.status_code == 409


def test_get_file_missing_is_404(conn):
    from fastapi import HTTPException
    _, link_id = _mk(conn)
    with pytest.raises(HTTPException) as ei:
        cs.get_file(conn, link_id, 12345)                   # no such file id in this channel
    assert ei.value.status_code == 404


# --- close: 404 + already-closed idempotence ---------------------------------

def test_close_unknown_channel_is_404(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cs.close_channel(conn, 999)
    assert ei.value.status_code == 404


def test_close_is_idempotent_and_keeps_persisted_history(conn):
    """A second close is a no-op over the DB (status already 'closed') but still
    re-publishes the 'closed' event; persisted backlog is untouched on either call."""
    _, link_id = _mk(conn, persist=True)
    cs.append_message(conn, link_id, "owner", "i", "c")
    cs.close_channel(conn, link_id)
    closed_at = conn.execute("SELECT closed_at FROM chat_channels WHERE share_link_id=?",
                             (link_id,)).fetchone()["closed_at"]
    cs.close_channel(conn, link_id)                          # second call: no DB mutation
    again = conn.execute("SELECT status, closed_at FROM chat_channels WHERE share_link_id=?",
                         (link_id,)).fetchone()
    assert again["status"] == "closed" and again["closed_at"] == closed_at
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages WHERE share_link_id=?",
                        (link_id,)).fetchone()["c"] == 1


# --- finalize / owner_detail / channel_for_link 404s -------------------------

def test_finalize_unknown_channel_is_404(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cs.finalize_channel(conn, 999, owner_wrap="OW", guest_wrap="GW")
    assert ei.value.status_code == 404


def test_owner_detail_unknown_channel_is_404(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cs.owner_detail(conn, 999)
    assert ei.value.status_code == 404


def test_owner_detail_exposes_owner_wrap_token_and_saved_slug(conn):
    """The OWNER view (not the public one) hands back the owner-side wrapped key + token
    so this device can unseal, and surfaces the saved-note slug once a transcript exists."""
    _, link_id = _mk(conn)
    conn.execute("INSERT INTO notes (slug, title, content_md) VALUES ('saved-chat','notes/Chat','b')")
    nid = conn.execute("SELECT id FROM notes WHERE slug='saved-chat'").fetchone()["id"]
    conn.execute("UPDATE chat_channels SET saved_note_id=? WHERE share_link_id=?", (nid, link_id))
    conn.commit()
    d = cs.owner_detail(conn, link_id)
    assert d["owner_wrap"] == "OWNER_SEALED"                 # owner-side wrap is the device's to unseal
    assert d["token"] and d["url"] and d["url"].endswith(d["token"])
    assert d["saved_note_slug"] == "saved-chat"
    assert d["present"] == {"owner": False, "guest": False}


def test_owner_detail_ignores_soft_deleted_saved_note(conn):
    # A saved note that was later trashed must not surface a stale slug.
    _, link_id = _mk(conn)
    conn.execute("INSERT INTO notes (slug, title, content_md, deleted_at) "
                 "VALUES ('gone','notes/Chat','b', datetime('now'))")
    nid = conn.execute("SELECT id FROM notes WHERE slug='gone'").fetchone()["id"]
    conn.execute("UPDATE chat_channels SET saved_note_id=? WHERE share_link_id=?", (nid, link_id))
    conn.commit()
    assert cs.owner_detail(conn, link_id)["saved_note_slug"] is None


# --- presence + debounced owner push -----------------------------------------

def test_handle_presence_pushes_owner_when_guest_waits_alone(conn, monkeypatch):
    """When a recipient is present with no owner attached, the owner gets one push —
    debounced so a flapping recipient can't spam it. Uses the guest_name in the title."""
    pushes = []
    monkeypatch.setattr(cs.push, "notify", lambda title, body, url: pushes.append((title, body, url)))
    _, link_id = _mk(conn)
    conn.execute("UPDATE chat_channels SET guest_name='Sarah' WHERE share_link_id=?", (link_id,))
    conn.commit()

    async def scenario():
        await chat_relay.subscribe(link_id, "guest")        # guest waiting, no owner
        cs.handle_presence(conn, link_id)
        cs.handle_presence(conn, link_id)                   # immediate re-call is debounced away
    import asyncio
    asyncio.run(scenario())

    assert len(pushes) == 1                                 # exactly one push despite two calls
    title, body, url = pushes[0]
    assert "Sarah is waiting" in title
    assert url == f"/shares/chat/{link_id}"


def test_handle_presence_no_push_when_owner_present(conn, monkeypatch):
    pushes = []
    monkeypatch.setattr(cs.push, "notify", lambda *a, **k: pushes.append(a))
    _, link_id = _mk(conn)

    async def scenario():
        await chat_relay.subscribe(link_id, "owner")
        await chat_relay.subscribe(link_id, "guest")        # owner IS attached → no waiting push
        cs.handle_presence(conn, link_id)
    import asyncio
    asyncio.run(scenario())
    assert pushes == []


def test_handle_presence_skips_push_for_closed_channel(conn, monkeypatch):
    pushes = []
    monkeypatch.setattr(cs.push, "notify", lambda *a, **k: pushes.append(a))
    _, link_id = _mk(conn)
    cs.close_channel(conn, link_id)

    async def scenario():
        await chat_relay.subscribe(link_id, "guest")
        cs.handle_presence(conn, link_id)                   # channel ended → never push
    import asyncio
    asyncio.run(scenario())
    assert pushes == []


# --- save_to_brain: full write path + 404 ------------------------------------

def test_save_to_brain_unknown_channel_is_404(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cs.save_to_brain(conn, 999, transcript_md="x", title=None, attachments=[], guest_name=None)
    assert ei.value.status_code == 404


def test_save_to_brain_writes_note_with_provenance_and_attachment(conn, monkeypatch):
    """The owner's CLIENT-DECRYPTED transcript becomes a normal note filed under
    notes/chats/, kept OUT of KB synthesis (kb_ingest=0), with a provenance footer and any
    decrypted attachments. The note/attachment seams are stubbed so the test stays free of
    the embedding/FTS pipeline while still exercising the real SQL in save_to_brain."""
    import base64
    _, link_id = _mk(conn)

    captured = {}

    def fake_upsert(c, title, body, **kw):
        captured["title"] = title
        captured["body"] = body
        captured["kw"] = kw
        c.execute("INSERT INTO notes (slug, title, content_md) VALUES (?, ?, ?)",
                  ("chat-saved", title, body))
        return c.execute("SELECT id FROM notes WHERE slug='chat-saved'").fetchone()["id"]

    monkeypatch.setattr(cs.notes_svc, "upsert_note", fake_upsert)
    # save_to_brain does `from . import attachments as att_svc` at call time → patch the module.
    from app.services import attachments as att_mod
    added = []
    monkeypatch.setattr(att_mod, "add_attachment",
                        lambda c, nid, name, mime, raw: added.append((nid, name, mime, raw)))

    attach = [{"name": "photo.png", "mime": "image/png",
               "data": base64.b64encode(b"RAWBYTES").decode()},
              {"name": "empty", "mime": "text/plain", "data": ""}]   # empty blob is skipped
    out = cs.save_to_brain(conn, link_id, transcript_md="hello world", title="My Chat",
                           attachments=attach, guest_name="Dana")

    assert out == {"note_slug": "chat-saved", "already_saved": False}
    assert captured["title"].startswith("notes/chats/")            # filed under notes/chats/
    assert "_Encrypted chat with Dana._" in captured["body"]       # provenance footer added
    assert captured["kw"].get("create_only") is True
    # Saved chat is excluded from evergreen KB synthesis.
    nid = conn.execute("SELECT id FROM notes WHERE slug='chat-saved'").fetchone()["id"]
    assert conn.execute("SELECT kb_ingest FROM notes WHERE id=?", (nid,)).fetchone()["kb_ingest"] == 0
    # The channel now points at the saved note (so a re-save short-circuits).
    assert conn.execute("SELECT saved_note_id FROM chat_channels WHERE share_link_id=?",
                        (link_id,)).fetchone()["saved_note_id"] == nid
    # Only the non-empty attachment was committed.
    assert added == [(nid, "photo.png", "image/png", b"RAWBYTES")]


def test_save_to_brain_empty_transcript_gets_placeholder(conn, monkeypatch):
    _, link_id = _mk(conn)

    def fake_upsert(c, title, body, **kw):
        c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('empty-chat', ?, ?)", (title, body))
        return c.execute("SELECT id FROM notes WHERE slug='empty-chat'").fetchone()["id"]

    monkeypatch.setattr(cs.notes_svc, "upsert_note", fake_upsert)
    cs.save_to_brain(conn, link_id, transcript_md="   ", title=None, attachments=None, guest_name=None)
    body = conn.execute("SELECT content_md FROM notes WHERE slug='empty-chat'").fetchone()["content_md"]
    assert "_(no messages)_" in body
    assert "_Encrypted chat with a guest._" in body               # defaults the guest label


# --- owner-side listing + SSE envelope ---------------------------------------

def test_list_channels_returns_typed_rows(conn):
    _, keep = _mk(conn, persist=True, otp=True)
    _, eph = _mk(conn, persist=False, otp=False)
    rows = cs.list_channels(conn)
    assert {r["link_id"] for r in rows} >= {keep, eph}
    by_id = {r["link_id"]: r for r in rows}
    assert by_id[keep]["persist"] is True and by_id[keep]["otp_required"] is True
    assert by_id[eph]["persist"] is False and by_id[eph]["otp_required"] is False
    for r in rows:
        assert isinstance(r["pending_setup"], bool)
        assert r["url"].endswith(r["token"])              # share URL carries the token


def test_sse_envelope_encodes_event_type_and_json():
    out = cs.sse({"type": "message", "seq": 3, "ct": "OPAQUE"})
    assert out.startswith("event: message\n")
    assert '"ct": "OPAQUE"' in out
    assert out.endswith("\n\n")


# --- relay: subscribe/unsubscribe + publish fan-out edges ---------------------

def test_unsubscribe_drops_subscriber_and_clears_presence(conn):
    """Detaching the last subscriber removes it from the hub and presence goes false —
    the relay holds no stale references after a side leaves."""
    _, link_id = _mk(conn)

    async def scenario():
        h, sub = await chat_relay.subscribe(link_id, "owner")
        assert chat_relay.present(link_id) == (True, False)
        chat_relay.unsubscribe(h, sub)
        assert sub not in h.subs
        return chat_relay.present(link_id)

    import asyncio
    assert asyncio.run(scenario()) == (False, False)


def test_publish_excludes_sender_and_delivers_to_other_side(conn):
    """A published event reaches every subscriber EXCEPT the excluded one (the sender's
    own SSE stream), so a sender doesn't echo their message back to themselves."""
    _, link_id = _mk(conn)

    async def scenario():
        ho, owner = await chat_relay.subscribe(link_id, "owner")
        hg, guest = await chat_relay.subscribe(link_id, "guest")
        chat_relay.publish(link_id, {"type": "message", "seq": 1}, exclude=owner)
        # The guest receives it; the excluded owner queue stays empty.
        delivered = await asyncio.wait_for(guest.queue.get(), timeout=1)
        return delivered, owner.queue.empty()

    import asyncio
    event, owner_empty = asyncio.run(scenario())
    assert event["seq"] == 1
    assert owner_empty is True


def test_publish_no_hub_or_no_loop_is_noop(conn):
    """publish() to a channel with no hub (never subscribed) — or a hub that never
    captured a loop — is a safe no-op, never raising."""
    _, link_id = _mk(conn)
    chat_relay.publish(link_id, {"type": "message"})        # no hub yet → returns early
    # Create a hub but never subscribe (loop stays None) → still a no-op.
    h = chat_relay.hub(link_id)
    assert h.loop is None
    chat_relay.publish(link_id, {"type": "message"})


def test_publish_swallows_runtime_error_when_loop_shutting_down(conn):
    """If the channel's event loop is tearing down, call_soon_threadsafe raises
    RuntimeError; the blind relay swallows it (best-effort delivery) rather than letting
    a closing browser crash a send from the threadpool."""
    _, link_id = _mk(conn)

    async def attach():
        await chat_relay.subscribe(link_id, "owner")        # seeds hub.loop

    import asyncio
    asyncio.run(attach())                                   # loop is now closed

    h = chat_relay.hub(link_id)
    assert h.loop is not None and h.loop.is_closed()
    # Must not raise even though the captured loop is dead.
    chat_relay.publish(link_id, {"type": "message", "seq": 99})


def test_next_seq_seeds_from_persisted_max_after_restart(conn):
    """After a process restart the in-memory hub seq is None; next_seq seeds it once from
    the persisted MAX(seq) so ordering survives across processes for persisted channels."""
    _, link_id = _mk(conn, persist=True)
    for _ in range(3):
        cs.append_message(conn, link_id, "owner", "i", "c")  # persisted seq 1..3
    chat_relay.reset()                                       # simulate a restart (hub gone)
    nxt = chat_relay.next_seq(
        link_id,
        lambda: conn.execute("SELECT COALESCE(MAX(seq),0) m FROM chat_messages WHERE share_link_id=?",
                             (link_id,)).fetchone()["m"])
    assert nxt == 4                                          # resumes after the persisted max


# --- save_to_brain: remaining declassify edge branches ------------------------

def test_save_to_brain_recreates_note_when_saved_note_was_hard_deleted(conn, monkeypatch):
    """saved_note_id points at a note that no longer exists (hard-deleted / purged): the
    short-circuit finds no live row and falls through to write a fresh note."""
    _, link_id = _mk(conn)
    conn.execute("UPDATE chat_channels SET saved_note_id=4242 WHERE share_link_id=?", (link_id,))
    conn.commit()

    def fake_upsert(c, title, body, **kw):
        c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('rebuilt', ?, ?)", (title, body))
        return c.execute("SELECT id FROM notes WHERE slug='rebuilt'").fetchone()["id"]

    monkeypatch.setattr(cs.notes_svc, "upsert_note", fake_upsert)
    out = cs.save_to_brain(conn, link_id, transcript_md="hi", title=None, attachments=[], guest_name="Sam")
    assert out["already_saved"] is False and out["note_slug"] == "rebuilt"
    # Channel is repointed at the freshly created note.
    nid = conn.execute("SELECT id FROM notes WHERE slug='rebuilt'").fetchone()["id"]
    assert conn.execute("SELECT saved_note_id FROM chat_channels WHERE share_link_id=?",
                        (link_id,)).fetchone()["saved_note_id"] == nid


def test_save_to_brain_does_not_duplicate_existing_provenance_footer(conn, monkeypatch):
    """If the client-supplied transcript already carries the provenance marker, the server
    must NOT append a second footer (the 'in body' branch is skipped)."""
    _, link_id = _mk(conn)

    def fake_upsert(c, title, body, **kw):
        c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('prov', ?, ?)", (title, body))
        return c.execute("SELECT id FROM notes WHERE slug='prov'").fetchone()["id"]

    monkeypatch.setattr(cs.notes_svc, "upsert_note", fake_upsert)
    transcript = "msg\n\n---\n_Encrypted chat with Dana already noted._\n"
    cs.save_to_brain(conn, link_id, transcript_md=transcript, title="t",
                     attachments=[], guest_name="Dana")
    body = conn.execute("SELECT content_md FROM notes WHERE slug='prov'").fetchone()["content_md"]
    assert body.count("_Encrypted chat") == 1               # no second footer appended


def test_save_to_brain_swallows_bad_attachment_without_losing_transcript(conn, monkeypatch):
    """A non-decodable attachment raises during base64/add — caught per-file so one bad
    file never loses the transcript. The transcript note still commits; the bad file does
    not, and a following good file still lands."""
    import base64
    _, link_id = _mk(conn)

    def fake_upsert(c, title, body, **kw):
        c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('rescued', ?, ?)", (title, body))
        return c.execute("SELECT id FROM notes WHERE slug='rescued'").fetchone()["id"]

    monkeypatch.setattr(cs.notes_svc, "upsert_note", fake_upsert)
    from app.services import attachments as att_mod
    added = []

    def maybe_add(c, nid, name, mime, raw):
        if name == "boom":                                   # explode on this one only
            raise ValueError("decode/store failure")
        added.append((name, raw))

    monkeypatch.setattr(att_mod, "add_attachment", maybe_add)

    attach = [{"name": "boom", "mime": "x", "data": base64.b64encode(b"AA").decode()},
              {"name": "ok.png", "mime": "image/png", "data": base64.b64encode(b"GOOD").decode()}]
    out = cs.save_to_brain(conn, link_id, transcript_md="kept", title=None,
                           attachments=attach, guest_name="Sam")
    assert out["note_slug"] == "rescued"                     # transcript survived the bad file
    assert added == [("ok.png", b"GOOD")]                    # the good file still committed
