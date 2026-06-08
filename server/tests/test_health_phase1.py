"""Phase 1 — backend readiness state machines + search.py keyword-fallback fix.

Covers: embeddings/audio readiness transitions, the audio Settings-reload regression,
the lock discipline (readiness() must not take _model_lock), and that pure-semantic
search returns [] (not a 500) while hybrid still returns keyword hits when the
embedding model is unavailable.
"""
import importlib
import threading
import time

import pytest


# --------------------------------------------------------------------------- #
# embeddings.readiness
# --------------------------------------------------------------------------- #

@pytest.fixture
def emb(monkeypatch):
    """Fresh embeddings module state for each test (it caches a process-global model)."""
    from app.services import embeddings
    embeddings._model = None
    embeddings._set_state("unknown")
    yield embeddings
    embeddings._model = None
    embeddings._set_state("unknown")


def test_embeddings_unknown_before_load(emb):
    assert emb.readiness()["state"] == "unknown"
    assert emb._model is None  # reading readiness must NOT trigger a load


def test_embeddings_warming_then_ready(emb, monkeypatch):
    class FakeModel:
        def embed(self, texts):
            return []
    import fastembed
    monkeypatch.setattr(fastembed, "TextEmbedding", lambda *a, **k: FakeModel())
    emb._get_model()
    assert emb.readiness()["state"] == "ready"
    assert emb._model is not None


def test_embeddings_importerror_unavailable(emb, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "fastembed":
            raise ImportError("no fastembed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    with pytest.raises(ImportError):
        emb._get_model()
    r = emb.readiness()
    assert r["state"] == "unavailable"
    assert "fastembed" in (r["last_error"] or "")


def test_embeddings_other_exception_failed(emb, monkeypatch):
    import fastembed

    def boom(*a, **k):
        raise RuntimeError("disk full while downloading")

    monkeypatch.setattr(fastembed, "TextEmbedding", boom)
    with pytest.raises(RuntimeError):
        emb._get_model()
    r = emb.readiness()
    assert r["state"] == "failed"
    assert "disk full" in (r["last_error"] or "")


def test_embeddings_last_error_truncated(emb):
    emb._set_state("failed", "x" * 9999)
    assert len(emb.readiness()["last_error"]) == 200


def test_embeddings_readiness_does_not_block_behind_model_lock(emb):
    """A poll must return even while a (simulated) multi-hundred-MB load holds _model_lock."""
    emb._set_state("warming")
    hold = threading.Event()
    released = threading.Event()

    def holder():
        with emb._model_lock:
            hold.set()
            released.wait(timeout=5)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert hold.wait(timeout=5)
    start = time.time()
    r = emb.readiness()            # must NOT need _model_lock
    assert time.time() - start < 1.0
    assert r["state"] == "warming"
    released.set()
    t.join(timeout=5)


# --------------------------------------------------------------------------- #
# audio_transcription.readiness — incl. the Settings-reload regression
# --------------------------------------------------------------------------- #

@pytest.fixture
def aud(monkeypatch):
    from app.services import audio_transcription as at
    at._model = None
    at._model_key = None
    at._set_state("unknown")
    # Deterministic want = ("base", "int8") unless a test overrides.
    monkeypatch.setattr(at, "audio_model", lambda: "base")
    monkeypatch.setattr(at, "audio_compute_type", lambda: "int8")
    yield at
    at._model = None
    at._model_key = None
    at._set_state("unknown")


def _fake_whisper(monkeypatch, calls=None):
    """Inject a fake faster_whisper.WhisperModel so _get_model can 'load' without deps."""
    import sys
    import types

    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model, device="cpu", compute_type="int8"):
            if calls is not None:
                calls.append((model, compute_type))

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return mod


def test_audio_unknown_before_load(aud):
    assert aud.readiness()["state"] == "unknown"


def test_audio_warming_then_ready(aud, monkeypatch):
    _fake_whisper(monkeypatch)
    aud._get_model()
    r = aud.readiness()
    assert r["state"] == "ready"
    assert r["model"] == "base" and r["compute_type"] == "int8"


def test_audio_reload_reports_warming_before_reload(aud, monkeypatch):
    """Round-3 H... regression: editing the model in Settings must flip readiness to
    'warming' immediately (cached key != live want), not stay a stale 'ready'."""
    calls = []
    _fake_whisper(monkeypatch, calls)
    aud._get_model()
    assert aud.readiness()["state"] == "ready"
    # Owner switches the model in Settings:
    monkeypatch.setattr(aud, "audio_model", lambda: "small")
    assert aud.readiness()["state"] == "warming"     # BEFORE the reload happens
    aud._get_model()                                 # now actually reload
    assert aud.readiness()["state"] == "ready"
    assert calls == [("base", "int8"), ("small", "int8")]


def test_audio_importerror_unavailable_before_raise(aud, monkeypatch):
    import sys
    # Ensure importing faster_whisper fails.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(aud.TranscriptionUnavailable):
        aud._get_model()
    r = aud.readiness()
    assert r["state"] == "unavailable"
    assert r["model"] is None and r["compute_type"] is None


def test_audio_failed_reload_surfaces_failure_not_false_ready(aud, monkeypatch):
    # First load succeeds.
    _fake_whisper(monkeypatch)
    aud._get_model()
    assert aud.readiness()["state"] == "ready"
    # Switch model; reload raises a non-import error.
    monkeypatch.setattr(aud, "audio_model", lambda: "small")
    import sys
    import types
    mod = types.ModuleType("faster_whisper")

    def boom(*a, **k):
        raise RuntimeError("corrupt download")

    mod.WhisperModel = boom
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    with pytest.raises(RuntimeError):
        aud._get_model()
    # The reload was attempted and failed: surface 'failed' + the error, NOT a stale
    # 'ready'. (_model_key kept its old value, so we never report the new model usable.)
    r = aud.readiness()
    assert r["state"] == "failed"
    assert "corrupt download" in (r["last_error"] or "")


# --------------------------------------------------------------------------- #
# search.py — pure-semantic returns [] (not 500); hybrid still returns keyword
# --------------------------------------------------------------------------- #
# NOTE: the importorskip lives INSIDE the client fixture (not at module level) so
# the dependency-free embeddings/audio readiness tests above still run when only the
# search-integration deps (sqlite_vec/fastapi/anthropic) are missing.

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("fastapi")
    pytest.importorskip("anthropic")
    import os
    import tempfile
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
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "upsert_attachment_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_attachment_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "embed_many",
                        lambda ts: [[0.0] * embeddings.EMBEDDING_DIM for _ in ts])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()

    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def _semantic_boom(monkeypatch):
    from app.services import embeddings

    def boom(*a, **k):
        raise RuntimeError("embeddings warming")

    monkeypatch.setattr(embeddings, "semantic_search", boom)
    monkeypatch.setattr(embeddings, "semantic_search_attachments", boom)
    monkeypatch.setattr(embeddings, "semantic_search_entities", boom)


def test_search_semantic_empty_when_embeddings_unavailable(client, monkeypatch):
    _semantic_boom(monkeypatch)
    r = client.get("/api/search", params={"q": "anything", "mode": "semantic"})
    assert r.status_code == 200          # NOT a 500
    assert r.json() == []


def test_search_hybrid_returns_keyword_when_embeddings_unavailable(client, monkeypatch):
    # Seed a note that matches by keyword (FTS).
    created = client.post("/api/notes/entry",
                          json={"text": "a unique zebra term here", "title": "Zebra crossing"})
    assert created.status_code == 200, created.text

    _semantic_boom(monkeypatch)

    r = client.get("/api/search", params={"q": "zebra", "mode": "hybrid"})
    assert r.status_code == 200          # degrades to keyword, no 500
    titles = [row.get("title") or "" for row in r.json()]
    assert any("Zebra crossing" in t for t in titles), titles
