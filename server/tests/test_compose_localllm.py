"""Shape tests for the optional local-LLM docker-compose wiring.

Pure YAML parsing — no Docker, no network, no model download — so it runs in the
`back` job and guards the opt-in `localllm` services against silent drift (profile,
volume, one-shot puller, and the api's host-gateway alias for BYO-host mode).
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(_COMPOSE.read_text())


def test_ollama_service_behind_localllm_profile(compose):
    svc = compose["services"]["ollama"]
    assert svc["profiles"] == ["localllm"]
    assert svc["image"].startswith("ollama/ollama")
    assert "ollama-models:/root/.ollama" in svc["volumes"]


def test_ollama_pull_is_one_shot_and_waits_for_healthy(compose):
    svc = compose["services"]["ollama-pull"]
    assert svc["profiles"] == ["localllm"]
    assert svc["restart"] == "no"
    assert svc["depends_on"]["ollama"]["condition"] == "service_healthy"


def test_ollama_models_volume_declared(compose):
    assert "ollama-models" in compose["volumes"]


def test_api_has_host_gateway_for_byo_host_mode(compose):
    assert "host.docker.internal:host-gateway" in compose["services"]["api"]["extra_hosts"]


def test_updater_inherits_compose_profiles(compose):
    env = compose["services"]["updater"]["environment"]
    assert "COMPOSE_PROFILES" in env
