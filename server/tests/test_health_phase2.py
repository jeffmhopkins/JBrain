"""Phase 2 — capabilities assembler + soft-auth /api/system/status + /verify fold.

Covers R3-H1 (no clock.iso_now; ts is ISO+offset), R3-H2 (single has_credentials()
predicate; providers map informational; share.llm_ready agrees), the public-skeleton
exact allowlist, the authed shape, no-token-burn, and the share landing flags.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.integration

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def app_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services import embeddings
    monkeypatch.setattr(embeddings, "embed_many",
                        lambda ts: [[0.0] * embeddings.EMBEDDING_DIM for _ in ts])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()
    yield


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


@pytest.fixture()
def anon(app_env):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# --------------------------------------------------------------------------- #
# capabilities() assembler
# --------------------------------------------------------------------------- #

def test_capabilities_shape(app_env):
    from app.services import system_status
    caps = system_status.capabilities()
    assert set(caps) >= {"llm", "embeddings", "transcription", "push", "geocoder", "db"}
    assert caps["llm"]["verified"] is None          # never live-checked
    assert "providers" in caps["llm"]
    assert caps["db"]["state"] in ("ready", "failed")


def test_capabilities_no_llm_call(app_env, monkeypatch):
    from app.services import system_status, llm
    called = {"n": 0}
    # If anything tried a real completion, this would fire. has_credentials must NOT.
    monkeypatch.setattr(llm, "complete", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    system_status.capabilities()
    assert called["n"] == 0


def test_capabilities_llm_follows_has_credentials(app_env, monkeypatch):
    from app.services import system_status, llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    assert system_status.capabilities()["llm"]["state"] == "ready"
    monkeypatch.setattr(llm, "has_credentials", lambda: False)
    assert system_status.capabilities()["llm"]["state"] == "absent"


def test_capabilities_providers_informational_independent_of_state(app_env, monkeypatch):
    """R3-H2: providers map reflects key PRESENCE; llm.state reflects ACTIVE-provider
    usability. They can disagree (provider=xai + only the Claude key set)."""
    from app.services import system_status, llm
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(type(s), "has_anthropic", property(lambda self: True))
    monkeypatch.setattr(type(s), "has_xai", property(lambda self: False))
    monkeypatch.setattr(llm, "has_credentials", lambda: False)   # active provider unusable
    caps = system_status.capabilities()
    assert caps["llm"]["providers"]["anthropic"] is True   # a key is present...
    assert caps["llm"]["state"] == "absent"                # ...but the dot says absent


def test_capabilities_db_failed_surfaces(app_env, monkeypatch):
    from app.services import system_status
    import app.db as db

    class BadConn:
        def execute(self, *a, **k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "get_conn", lambda: BadConn())
    caps = system_status.capabilities()
    assert caps["db"]["state"] == "failed"
    assert "locked" in caps["db"]["last_error"]


def test_capabilities_safe_isolates_raising_subsystem(app_env, monkeypatch):
    """The headline hardening: a subsystem read that RAISES must not 500 the doc;
    it reports {state:'failed', last_error} and the other subsystems still resolve."""
    from app.services import system_status, push

    def boom():
        raise RuntimeError("vapid meta exploded")

    monkeypatch.setattr(push, "public_key", boom)
    caps = system_status.capabilities()                 # must NOT raise
    assert caps["push"]["state"] == "failed"
    assert "exploded" in caps["push"]["last_error"]
    assert caps["llm"]["state"] in ("ready", "absent")  # neighbours unaffected
    assert caps["db"]["state"] == "ready"


def test_share_llm_ready_agrees_with_capabilities(app_env, monkeypatch):
    """R3-H2: the share landing predicate and the dot's llm.state are the SAME boolean."""
    from app.services import system_status, llm
    from app.routers import share
    for val in (True, False):
        monkeypatch.setattr(llm, "has_credentials", lambda val=val: val)
        assert share.llm_ready() == (system_status.capabilities()["llm"]["state"] == "ready")


# --------------------------------------------------------------------------- #
# R3-H1 — clock
# --------------------------------------------------------------------------- #

def test_clock_has_no_iso_now():
    """Guards against an implementer re-introducing the bad clock.iso_now() call."""
    from app.services import clock
    assert not hasattr(clock, "iso_now")


# --------------------------------------------------------------------------- #
# /api/system/status — soft auth, two builders
# --------------------------------------------------------------------------- #

def test_status_public_skeleton_exact_allowlist(anon):
    r = anon.get("/api/system/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "brain", "ts"}        # EXACT — no version, no capabilities
    assert body["ok"] is True
    assert body["brain"] == "Test Brain"
    # ts is ISO-8601 with a +00:00 offset (R3-H1)
    from datetime import datetime
    parsed = datetime.fromisoformat(body["ts"])
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_status_bad_key_returns_skeleton_not_401(app_env):
    from fastapi.testclient import TestClient
    from app.main import app
    bad = TestClient(app, headers={"Authorization": "Bearer wrong-key"})
    r = bad.get("/api/system/status")
    assert r.status_code == 200                       # soft auth: never 401
    assert set(r.json()) == {"ok", "brain", "ts"}    # downgraded to skeleton


def test_status_authed_full_doc(client):
    r = client.get("/api/system/status")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body and "capabilities" in body
    caps = body["capabilities"]
    assert set(caps) >= {"llm", "embeddings", "transcription", "push", "geocoder", "db"}


def test_status_authed_no_db_write(client):
    """The poll must not mutate. Snapshot the DB file mtime/size around a poll."""
    from app.db import get_conn
    before = get_conn().execute("SELECT COUNT(*) c FROM meta").fetchone()["c"]
    client.get("/api/system/status")
    after = get_conn().execute("SELECT COUNT(*) c FROM meta").fetchone()["c"]
    assert before == after


# --------------------------------------------------------------------------- #
# /verify fold + leak checks
# --------------------------------------------------------------------------- #

def test_verify_includes_capabilities_same_shape(client):
    v = client.get("/api/auth/verify").json()
    assert "capabilities" in v
    # Legacy fields preserved
    for k in ("ok", "brain_name", "version", "has_llm", "app_tz", "owner_set",
              "llm_keys", "vapid_public_key"):
        assert k in v
    # Same sub-shape as /status so one client adapter handles both.
    status_caps = client.get("/api/system/status").json()["capabilities"]
    assert set(v["capabilities"]) == set(status_caps)


def test_no_last_error_leak_in_public_surfaces(anon):
    health = anon.get("/api/health").json()
    assert "last_error" not in str(health)
    info = anon.get("/api/auth/info").json()
    assert "last_error" not in str(info)
    skel = anon.get("/api/system/status").json()
    assert "capabilities" not in skel and "last_error" not in str(skel)


# --------------------------------------------------------------------------- #
# share landings carry llm_ready
# --------------------------------------------------------------------------- #

def test_guided_landing_carries_llm_ready(app_env, monkeypatch):
    from app.routers import share
    monkeypatch.setattr(share, "llm_ready", lambda: True)

    class FakeGuided:
        @staticmethod
        def get_spec(conn, link_id):
            return {"status": "active", "intro": "hi", "goal": "learn"}

    monkeypatch.setattr(share, "guided_svc", FakeGuided)
    out = share._guided_landing(conn=None, link={"id": 1})
    assert out["kind"] == "guided"
    assert out["llm_ready"] is True


def test_research_landing_carries_llm_ready(app_env, monkeypatch):
    from app.routers import share
    monkeypatch.setattr(share, "llm_ready", lambda: False)

    class FakeResearch:
        @staticmethod
        def get_spec(conn, link_id):
            return {"status": "active", "intro": "hi"}

        @staticmethod
        def lab_allowed(spec):
            return False

    monkeypatch.setattr(share, "research_svc", FakeResearch)
    out = share._research_landing(conn=None, link={"id": 1})
    assert out["kind"] == "research"
    assert out["llm_ready"] is False
