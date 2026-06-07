"""Shared pytest setup.

The LLM layer caches one SDK client per (provider, credentials) for the process lifetime so
it isn't reconstructed — and leaked (httpx pool / sockets / FDs) — on every call. That cache is
module-global and therefore survives across tests: a client built by one test would otherwise be
handed back to a later test that monkeypatches the SDK client class, so its stub never runs. Clear
the cache around each test to keep that production optimisation from bleeding between tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_llm_client_cache():
    try:
        from app.services import llm
    except Exception:  # llm's deps aren't importable for this test → nothing to reset
        yield
        return
    llm._client_cache.clear()
    yield
    llm._client_cache.clear()
