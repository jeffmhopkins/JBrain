"""Unit tests for the Ollama management client + local-model readiness state.

The module talks to Ollama's HOST API over stdlib urllib. These tests mock
``urllib.request.urlopen`` at the module seam — no real network, no real Ollama —
and install a fake Settings object, so they stay pure (no DB, no sockets).
"""
from types import SimpleNamespace

import pytest

from app.services import local_models as lm

pytestmark = pytest.mark.unit


def _settings(**over):
    base = dict(
        llm_local_enable=True,
        llm_local_base_url="http://ollama:11434/v1",
        llm_local_admin_url="http://ollama:11434",
        llm_local_model="qwen2.5:7b",
        llm_timeout_seconds=120.0,
    )
    base.update(over)
    ns = SimpleNamespace(**base)
    ns.has_local = bool(ns.llm_local_enable and ns.llm_local_base_url)
    return ns


@pytest.fixture()
def patch_settings(monkeypatch):
    def _set(**over):
        s = _settings(**over)
        monkeypatch.setattr(lm, "get_settings", lambda: s)
        return s
    return _set


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the process-level readiness singleton between tests."""
    lm._set_state("unknown", err=None, model=None)
    yield


class _FakeResp:
    """Minimal urlopen() stand-in: context manager + read()/status + line iteration."""

    def __init__(self, *, body=b"", status=200, lines=None, exc=None):
        self._body = body
        self.status = status
        self._lines = lines or []
        self._exc = exc

    def __enter__(self):
        if self._exc:
            raise self._exc
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


def _patch_urlopen(monkeypatch, handler):
    """Install a fake urlopen that delegates to handler(url_or_req, **kwargs)."""
    monkeypatch.setattr(lm.urllib.request, "urlopen", handler)


# --- list_models / model_present -------------------------------------------

def test_list_models_parses_tags(patch_settings, monkeypatch):
    patch_settings()
    body = b'{"models":[{"name":"qwen2.5:7b","size":4700000000},{"name":"llava:7b","size":4500000000}]}'
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _FakeResp(body=body))
    out = lm.list_models()
    assert [m["name"] for m in out] == ["qwen2.5:7b", "llava:7b"]
    assert out[0]["size"] == 4700000000


def test_list_models_unreachable_returns_empty(patch_settings, monkeypatch):
    patch_settings()

    def _boom(url, timeout=None):
        raise OSError("connection refused")

    _patch_urlopen(monkeypatch, _boom)
    assert lm.list_models() == []


def test_model_present_matches_exact_and_base(patch_settings, monkeypatch):
    patch_settings()
    body = b'{"models":[{"name":"qwen2.5:7b","size":1}]}'
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _FakeResp(body=body))
    assert lm.model_present("qwen2.5:7b") is True
    assert lm.model_present("qwen2.5") is True       # base match
    assert lm.model_present("llama3.1:8b") is False
    assert lm.model_present("") is False


# --- pull_model -------------------------------------------------------------

def test_pull_model_streams_progress_and_sets_ready(patch_settings, monkeypatch):
    patch_settings()
    lines = [b'{"status":"pulling manifest"}\n',
             b'{"status":"downloading","completed":50,"total":100}\n',
             b'{"status":"success"}\n']
    _patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResp(lines=lines))
    events = list(lm.pull_model("qwen2.5:7b"))
    assert events[0]["status"] == "pulling manifest"
    assert events[1]["completed"] == 50
    assert lm.readiness()["state"] == "ready"
    assert lm.readiness()["model"] == "qwen2.5:7b"


def test_pull_model_error_event_marks_failed(patch_settings, monkeypatch):
    patch_settings()
    lines = [b'{"error":"no such model"}\n']
    _patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResp(lines=lines))
    list(lm.pull_model("nope:1b"))
    assert lm.readiness()["state"] == "failed"
    assert "no such model" in lm.readiness()["last_error"]


def test_pull_model_raises_on_transport_error(patch_settings, monkeypatch):
    patch_settings()

    def _boom(req, timeout=None):
        raise OSError("refused")

    _patch_urlopen(monkeypatch, _boom)
    with pytest.raises(OSError):
        list(lm.pull_model("qwen2.5:7b"))
    assert lm.readiness()["state"] == "failed"


# --- warm / readiness -------------------------------------------------------

def test_warm_absent_when_disabled(patch_settings, monkeypatch):
    patch_settings(llm_local_enable=False)
    # No network must be touched when local is off.
    called = {"n": 0}

    def _should_not_call(*a, **k):
        called["n"] += 1
        raise AssertionError("network touched while local disabled")

    _patch_urlopen(monkeypatch, _should_not_call)
    assert lm.warm()["state"] == "absent"
    assert called["n"] == 0


def test_warm_unavailable_when_unreachable(patch_settings, monkeypatch):
    patch_settings()

    def _boom(url, timeout=None):
        raise OSError("down")

    _patch_urlopen(monkeypatch, _boom)
    assert lm.warm()["state"] == "unavailable"


def test_warm_pulling_when_model_missing(patch_settings, monkeypatch):
    patch_settings(llm_local_model="qwen2.5:7b")
    # Reachable but the model isn't pulled yet (empty list + a 200 on the re-probe).
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _FakeResp(body=b'{"models":[]}', status=200))
    assert lm.warm()["state"] == "pulling"


def test_warm_ready_when_model_present(patch_settings, monkeypatch):
    patch_settings(llm_local_model="qwen2.5:7b")
    body = b'{"models":[{"name":"qwen2.5:7b","size":1}]}'
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _FakeResp(body=body))
    assert lm.warm()["state"] == "ready"


def test_readiness_is_in_memory_no_network(patch_settings, monkeypatch):
    patch_settings()
    lm._set_state("ready", model="qwen2.5:7b")

    def _should_not_call(*a, **k):
        raise AssertionError("readiness touched the network")

    _patch_urlopen(monkeypatch, _should_not_call)
    snap = lm.readiness()
    assert snap["state"] == "ready" and snap["model"] == "qwen2.5:7b"
    assert "since" in snap


# --- pull_events (typed SSE mapping) ---------------------------------------

def test_pull_events_maps_progress_and_done(patch_settings, monkeypatch):
    patch_settings()
    monkeypatch.setattr(lm, "pull_model", lambda name: iter([
        {"status": "pulling manifest"},
        {"status": "downloading", "completed": 50, "total": 100},
        {"status": "success"},
    ]))
    evts = list(lm.pull_events("qwen2.5:7b"))
    assert evts[0] == {"type": "status", "status": "pulling manifest"}
    assert evts[1] == {"type": "progress", "completed": 50, "total": 100, "status": "downloading"}
    assert {"type": "done"} in evts
    assert evts[-1] == {"type": "done"}


def test_pull_events_maps_error(patch_settings, monkeypatch):
    patch_settings()
    monkeypatch.setattr(lm, "pull_model", lambda name: iter([{"error": "no such model"}]))
    evts = list(lm.pull_events("nope"))
    assert {"type": "error", "message": "no such model"} in evts
    assert evts[-1] != {"type": "done"}     # errored → no trailing done


def test_pull_events_transport_error_becomes_terminal_error(patch_settings, monkeypatch):
    patch_settings()

    def _boom(name):
        raise OSError("refused")
        yield  # pragma: no cover — make it a generator

    monkeypatch.setattr(lm, "pull_model", _boom)
    evts = list(lm.pull_events("qwen2.5:7b"))
    assert evts == [{"type": "error", "message": "refused"}]


# --- delete_model -----------------------------------------------------------

def test_delete_model_success(patch_settings, monkeypatch):
    patch_settings()
    _patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResp(status=200))
    assert lm.delete_model("qwen2.5:7b") is True


def test_delete_model_failure_returns_false(patch_settings, monkeypatch):
    patch_settings()

    def _boom(req, timeout=None):
        raise OSError("down")

    _patch_urlopen(monkeypatch, _boom)
    assert lm.delete_model("qwen2.5:7b") is False
    assert lm.delete_model("") is False


# --- hardware / describe_models --------------------------------------------

def test_hardware_reports_usable_ram(patch_settings, monkeypatch):
    patch_settings()
    monkeypatch.setattr(lm, "_total_ram_bytes", lambda: 32 * 1024 ** 3)
    hw = lm.hardware()
    # 32GB total minus the 8GB reserve → 24GB usable.
    assert hw["total_ram_bytes"] == 32 * 1024 ** 3
    assert hw["usable_ram_bytes"] == 24 * 1024 ** 3
    assert "cpu_only" in hw


def test_hardware_unknown_ram_is_zero(patch_settings, monkeypatch):
    patch_settings()
    monkeypatch.setattr(lm, "_total_ram_bytes", lambda: 0)
    assert lm.hardware()["usable_ram_bytes"] == 0


def test_describe_models_computes_fit_and_warn(patch_settings, monkeypatch):
    patch_settings(llm_local_model="qwen2.5:7b")
    monkeypatch.setattr(lm, "_total_ram_bytes", lambda: 12 * 1024 ** 3)  # ~12GB → ~6GB usable
    body = ('{"models":['
            '{"name":"qwen2.5:7b","size":4700000000},'
            '{"name":"llama3.3:70b","size":40000000000}]}').encode()
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _FakeResp(body=body))
    out = lm.describe_models()
    assert out["running"] is True
    by = {m["name"]: m for m in out["models"]}
    assert by["qwen2.5:7b"]["fits"] is True          # ~6GB est ≤ usable
    assert by["llama3.3:70b"]["fits"] is False        # 52GB est > usable
    # The configured model carries the live readiness state; others are 'ready'.
    assert by["llama3.3:70b"]["state"] == "ready"


def test_describe_models_unreachable(patch_settings, monkeypatch):
    patch_settings()

    def _boom(url, timeout=None):
        raise OSError("down")

    _patch_urlopen(monkeypatch, _boom)
    out = lm.describe_models()
    assert out["running"] is False
    assert out["models"] == []
