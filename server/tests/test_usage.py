"""Unit tests for usage pricing — local models are free; cloud pricing unchanged."""
import pytest

from app.services import usage

pytestmark = pytest.mark.unit


def test_rates_local_ollama_id_is_free():
    # An Ollama 'name:tag' id carries a colon → zero cost.
    assert usage._rates("qwen2.5:7b") == (0.0, 0.0, 0.0, 0.0)
    assert usage._rates("llama3.1:8b") == (0.0, 0.0, 0.0, 0.0)


def test_rates_ollama_substring_is_free():
    assert usage._rates("ollama/whatever") == (0.0, 0.0, 0.0, 0.0)


def test_cost_local_model_is_zero():
    assert usage._cost("qwen2.5:7b", 1000, 1000, 0, 0) == 0.0


def test_rates_cloud_models_unchanged():
    # Regression guard: the ':' rule must not over-trigger on cloud ids.
    assert usage._rates("claude-opus-4-8") == usage._PRICES["opus"]
    assert usage._rates("claude-sonnet-4-6") == usage._PRICES["sonnet"]
    assert usage._rates("grok-4.3") == usage._PRICES["grok"]


def test_rates_unknown_cloud_model_uses_default():
    # No colon, no known key → Sonnet-class default (unchanged behaviour).
    assert usage._rates("gpt-4o") == usage._DEFAULT_PRICE


def test_cost_cloud_model_nonzero():
    c = usage._cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
    assert c == pytest.approx(3.0)
