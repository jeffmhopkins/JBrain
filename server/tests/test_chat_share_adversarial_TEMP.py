"""ADVERSARIAL probe — real single-writer layer (real writer thread + on-disk DB).

Verifies: (a) 409 propagates through .result(); (b) no leaked txn on the writer
conn after a guard fires mid-unit; (c) finalize read-your-writes across the
separate request conn; (d) seq monotonicity under concurrent owner+guest appends.
"""
import os
import threading

import pytest

pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.concurrency


@pytest.fixture()
def dbenv(tmp_path, monkeypatch):
    os.environ.update(
        DB_PATH=str(tmp_path / "advchat.db"),
        JBRAIN_ACCESS_KEY="test-access-key-1234567890",
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    from app.services import chat_relay
    chat_relay.reset()
    yield db
    db._local.__dict__.clear()
    chat_relay.reset()


def test_409_then_clean_writer_no_leaked_txn(dbenv):
    from fastapi import HTTPException
    from app.services import chat_share as cs
    db = dbenv
    conn = db.get_conn()  # request conn (event-loop thread)

    token, link_id = cs.create_channel(
        conn, owner_wrap="O", guest_wrap="G", persist=True, otp_required=False,
        label=None, ttl_days=None, single_use=False, owner_name="Me")
    conn.commit()

    # Close the channel -> subsequent append must 409.
    cs.close_channel(conn, link_id)

    with pytest.raises(HTTPException) as ei:
        cs.append_message(conn, link_id, "owner", "iv", "ct")
    assert ei.value.status_code == 409

    # The writer connection must NOT carry a leaked open transaction. Probe it by
    # running a fresh write unit on the writer thread and asserting in_transaction
    # is False on entry.
    seen = {}

    def _probe():
        wc = db.get_conn()
        seen["in_txn_on_entry"] = wc.in_transaction
        wc.execute("UPDATE chat_channels SET owner_name='probe' WHERE share_link_id = ?", (link_id,))
        wc.commit()
        return "ok"

    assert db.submit_write(_probe).result() == "ok"
    assert seen["in_txn_on_entry"] is False, "writer conn leaked an open txn after 409 guard"


def test_finalize_read_your_writes_across_request_conn(dbenv):
    from app.services import chat_share as cs
    db = dbenv
    conn = db.get_conn()

    token, link_id = cs.create_pending_channel(
        conn, persist=True, otp_required=False, label=None, ttl_days=None, owner_name="Me")
    conn.commit()

    assert cs.owner_detail(conn, link_id)["pending_setup"] is True

    cs.finalize_channel(conn, link_id, owner_wrap="O2", guest_wrap="G2")
    # owner_detail reads on the REQUEST conn after the WRITER committed. WAL must
    # make the committed pending_setup=0 visible to the separate request conn.
    d = cs.owner_detail(conn, link_id)
    assert d["pending_setup"] is False, "request conn cannot see writer's committed finalize"
    assert d["owner_wrap"] == "O2"


def test_concurrent_appends_unique_seq(dbenv):
    from app.services import chat_share as cs
    db = dbenv
    conn = db.get_conn()
    token, link_id = cs.create_channel(
        conn, owner_wrap="O", guest_wrap="G", persist=True, otp_required=False,
        label=None, ttl_days=None, single_use=False, owner_name="Me")
    conn.commit()

    seqs = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i):
        c = db.get_conn()  # each thread its own request conn
        barrier.wait()
        for j in range(10):
            ev = cs.append_message(c, link_id, "owner" if i % 2 else "guest", "iv", "ct")
            with lock:
                seqs.append(ev["seq"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seqs) == 80
    assert len(set(seqs)) == 80, f"duplicate seqs: {len(seqs) - len(set(seqs))} dupes"
    assert sorted(seqs) == list(range(1, 81))
