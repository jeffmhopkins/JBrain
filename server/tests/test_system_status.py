"""Unit tests for the capabilities() health snapshot — local_llm subsystem.

Verifies the local LLM appears as an INFORMATIONAL subsystem and in the providers
map, without becoming the authoritative `llm` predicate. The service modules
capabilities() imports lazily are replaced with light fakes in sys.modules so the
test never pulls heavy deps (sqlite_vec/pywebpush) and touches no DB/network.
"""
import sys
import types

import pytest

from app.services import system_status

pytestmark = pytest.mark.unit


def _fake_module(**attrs):
    m = types.ModuleType("fake")
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture()
def fakes(monkeypatch):
    """Replace the deps capabilities() imports with light fakes (no heavy imports).

    capabilities() does ``from . import embeddings, ... local_models`` (resolved as
    attributes of the app.services package) and ``from ..config import get_settings`` /
    ``from ..db import get_conn``. Patching the package attributes short-circuits the
    real (heavy) submodule imports; config/db are faked in sys.modules.
    """
    import app.services as svc

    local_state = {"readiness": lambda: {"state": "ready", "model": "qwen2.5:7b"}}
    has_local = {"v": False}

    monkeypatch.setattr(svc, "embeddings", _fake_module(readiness=lambda: {"state": "ready"}), raising=False)
    monkeypatch.setattr(svc, "audio_transcription",
                        _fake_module(readiness=lambda: {"state": "ready"}), raising=False)
    monkeypatch.setattr(svc, "push", _fake_module(public_key=lambda: None), raising=False)
    monkeypatch.setattr(svc, "geocode", _fake_module(enabled=lambda: False), raising=False)
    monkeypatch.setattr(svc, "llm", _fake_module(has_credentials=lambda: True), raising=False)
    monkeypatch.setattr(svc, "local_models",
                        _fake_module(readiness=lambda: local_state["readiness"]()), raising=False)

    class _Settings:
        has_anthropic = True
        has_xai = False

        @property
        def has_local(self):
            return has_local["v"]

    monkeypatch.setitem(sys.modules, "app.config", _fake_module(get_settings=lambda: _Settings()))
    conn = types.SimpleNamespace(execute=lambda *_: types.SimpleNamespace(fetchone=lambda: (1,)))
    monkeypatch.setitem(sys.modules, "app.db", _fake_module(get_conn=lambda: conn))

    return {"local_state": local_state, "has_local": has_local}


def test_capabilities_includes_local_llm_subsystem(fakes):
    caps = system_status.capabilities()
    assert caps["local_llm"]["state"] == "ready"
    assert caps["local_llm"]["model"] == "qwen2.5:7b"


def test_local_llm_does_not_gate_authoritative_llm(fakes):
    fakes["local_state"]["readiness"] = lambda: {"state": "unavailable"}
    caps = system_status.capabilities()
    assert caps["llm"]["state"] == "ready"           # cloud creds → authoritative ready
    assert caps["local_llm"]["state"] == "unavailable"


def test_providers_map_includes_local(fakes):
    fakes["has_local"]["v"] = True
    caps = system_status.capabilities()
    assert caps["llm"]["providers"] == {"anthropic": True, "xai": False, "local": True}


def test_local_llm_probe_failure_is_safe(fakes):
    def _boom():
        raise RuntimeError("kaboom")

    fakes["local_state"]["readiness"] = _boom
    caps = system_status.capabilities()
    # _safe() converts a raising probe into a 'failed' dict; the doc is still served.
    assert caps["local_llm"]["state"] == "failed"
    assert "kaboom" in caps["local_llm"]["last_error"]
