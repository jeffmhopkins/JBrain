"""Unit guard: the first-run embedding-model download must be time-bounded.

fastembed pulls the ONNX weights via huggingface_hub on first use, inside _model_lock and (on the
chat path) inside a tool's worker thread. With no per-read deadline a stalled CDN read would hang
that download forever — wedging the turn and serialising every other embedding caller behind the
held lock. Importing the module sets HF_HUB_DOWNLOAD_TIMEOUT so a stalled read errors instead.
"""
import os

import pytest

pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.unit


def test_hf_download_timeout_default_is_set_and_positive():
    import app.services.embeddings  # noqa: F401 — import sets the env default at module load
    val = os.environ.get("HF_HUB_DOWNLOAD_TIMEOUT")
    assert val is not None, "embeddings import must bound the first-run model download"
    assert float(val) > 0


def test_hf_download_timeout_respects_an_operator_override(monkeypatch):
    # setdefault must never clobber an operator's explicit value. Simulate a pre-set env and a
    # fresh import, then assert the override survives.
    import importlib
    monkeypatch.setenv("HF_HUB_DOWNLOAD_TIMEOUT", "7")
    import app.services.embeddings as emb
    importlib.reload(emb)
    assert os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] == "7"
