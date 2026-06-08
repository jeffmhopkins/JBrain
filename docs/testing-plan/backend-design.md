# JBrain Backend Testing Standard

**Status:** Proposed standard
**Scope:** `server/` (FastAPI + SQLite + Anthropic/OpenAI SDK + fastembed)
**Date:** 2026-06-08

This document defines the opinionated testing standard for the JBrain backend. It is written so a
single cross-domain test framework (one `test` entrypoint, shared markers, shared coverage config)
can plug in identically for backend and frontend.

It is grounded in what the suite already does well — temp-SQLite + TestClient integration tests,
universally-mocked embeddings, a per-test LLM client-cache reset, `pytest.importorskip` guards, and
time-based polling for async workers — and formalizes those patterns into rules, plus the missing
config (`pyproject.toml`, markers, coverage floor, CI command).

Current baseline (from the coverage audit, treated as given): `server/app` ≈ 15.6k stmts, 33
routers, 62 services; `server/tests/` = 36 files, ~808 passing tests, ~79% line coverage; no
`pytest.ini`/`pyproject` config, no markers, no coverage config, no CI.

---

## 1. Test taxonomy

Three tiers, selected by a single required marker per test module. Every test file declares exactly
one tier marker at module scope; the tier is a property of the *file*, not the individual test.

| Tier | Marker | What qualifies | DB | LLM / embeddings | Speed target |
|---|---|---|---|---|---|
| **unit** | `@pytest.mark.unit` | One module/function, no app wiring. Pure logic: math, parsing, normalization, token expansion, slug rules, schema-version comparison. No `TestClient`, no `init_db()`. May use an in-memory `sqlite3` connection it builds itself. | none or self-built `:memory:` | not imported | < 50 ms |
| **integration** | `@pytest.mark.integration` | Exercises real app wiring against a temp SQLite DB: a router via `TestClient`, or a service that reads/writes the DB through `app.db`. The default tier — most of the existing 36 files are here. | temp file or `:memory:` via the canonical fixture | **mocked** (canonical fixtures) | < 1 s typical |
| **system** | `@pytest.mark.system` | Multi-component behavior that needs real concurrency, threads, on-disk WAL contention, background workers draining, or coalescing. Inherently slower and/or timing-based. Examples today: `test_freeze_regression.py` (two threads contend for the WAL write lock), the threaded-worker paths in `test_async_entity_rebuild.py`. | **on-disk** temp DB (required for multi-connection contention) | mocked | up to a few s; polling allowed |

Rules:

- **Exactly one tier marker per file**, at the top: `pytestmark = pytest.mark.integration`.
- A file that mixes a slow threaded path with fast inline paths (e.g. `test_async_entity_rebuild.py`)
  marks the **file** `integration` and tags the slow threaded tests individually with
  `@pytest.mark.system` (markers stack; the stricter tier wins for selection).
- The "real network is never touched" rule is enforced by an autouse guard (see §3), not by tier.
- `unit` is a strict subset: if a test imports `app.main`, constructs a `TestClient`, or calls
  `db.init_db()`, it is **not** unit.

This maps cleanly onto a cross-domain framework: the same three marker names (`unit` / `integration`
/ `system`) are reused on the frontend, so `test --unit` means the same thing everywhere.

---

## 2. Directory & naming conventions

**Keep a single flat `server/tests/` directory. Do not mirror `app/`.**

Justification:

- The codebase has 33 routers × 62 services, but tests are organized by **feature/behavior**, not by
  module — e.g. `test_freeze_regression.py` spans `image_analysis`, `audio_transcription`,
  `embeddings`, and `db`; `test_async_entity_rebuild.py` spans routers + `entity_index` +
  `entity_rebuild`. A mirror layout (`tests/services/test_embeddings.py`) would force these
  cross-cutting suites into an arbitrary home and fight the existing structure.
- A flat directory keeps the 36 → ~40 files discoverable with one `ls`, and the migration cost of
  re-homing 808 tests into a mirror tree is not worth it.
- `conftest.py` at `server/tests/` already provides the one global fixture; a flat tree keeps fixture
  resolution obvious.

Conventions:

- **Files:** `test_<feature>.py` (behavior/feature-named). Regression files keep a `test_<bug>_regression.py`
  or `test_<feature>_regression.py` shape; migration files keep `test_<change>_migration.py`. Both are
  already in use and should stay.
- **Functions:** `test_<subject>_<expected_behavior>` — assertive, full-sentence-ish, as the suite
  already does (`test_v45_migration_is_idempotent`, `test_image_worker_holds_no_lock_during_embed`).
  No bare `test_1` / `test_foo`.
- **Classes:** optional, only to group fixtures/parametrization for one subject:
  `class TestSlugGeneration:`. Default is module-level functions — do not introduce classes for
  organization alone.
- **Helpers:** module-private `_helper()` functions (leading underscore), as today
  (`_seed_note_and_image`, `_legacy_v44_conn`). Shared helpers that >2 files need move into
  `tests/fixtures/` as importable modules or fixtures, never copy-pasted.
- **Mapping to module under test:** a docstring at the top of each file states what it covers and any
  non-obvious invariant being pinned (the suite already does this well — keep it mandatory). One file
  may cover several modules; that is expected and fine.

`tests/fixtures/` holds data fixtures (e.g. the existing `time_tokens.json` parity fixture) and any
shared fixture *modules* imported via `conftest`. Keep it.

---

## 3. Fixture & mock strategy

All shared fixtures live in **`server/tests/conftest.py`** (already the single source). The goal:
every integration/system test gets a deterministic, offline, fast environment with **one** way to
build the DB+client and **one** way to mock the LLM.

### 3.1 The determinism baseline (autouse)

These run for every test, no opt-in:

- **`_reset_llm_client_cache` (existing, keep):** clears `app.services.llm._client_cache` before and
  after each test so a cached SDK client never leaks across tests.
- **`_no_network` (new, autouse):** monkeypatch `socket.socket.connect` (and
  `socket.create_connection`) to raise `RuntimeError("network blocked in tests")`, with `127.0.0.1`
  / `::1` / unix sockets allowed (TestClient and on-disk SQLite need loopback). This makes "no real
  network" a hard guarantee instead of a convention — any test that forgets to mock the LLM fails
  loudly instead of hitting api.anthropic.com.
- **`_seed_randomness` (new, autouse):** `random.seed(0)` per test. (NumPy/torch are pulled in by
  fastembed but embeddings are mocked, so no extra seeding needed.)
- **`_frozen_clock` (new, opt-in, not autouse):** see §3.4.

### 3.2 Canonical `db` + `client` fixtures (new — replaces the per-file copies)

Today every integration file hand-rolls the same ~25-line setup (set env, `get_settings.cache_clear()`,
mock six embedding functions, reset `db._initialized` / `db._local`, `init_db()`, `ensure_access_key()`,
reset the share rate-limiter). This is duplicated across ~20 files and drifts (see the comments in
`test_api.py` about workers bypassing the `upsert` stub). Standardize it into conftest fixtures:

```python
# conftest.py (sketch — names are the standard)
TEST_KEY = "test-access-key-1234567890"

@pytest.fixture
def temp_db_env(tmp_path, monkeypatch):
    """Set env for an isolated temp DB + access key; clear the settings cache."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("JBRAIN_ACCESS_KEY", TEST_KEY)
    monkeypatch.setenv("BRAIN_NAME", "Test Brain")
    monkeypatch.setenv("JBRAIN_DOMAIN", "localhost")
    from app.config import get_settings
    get_settings.cache_clear()
    yield tmp_path

@pytest.fixture
def mock_embeddings(monkeypatch):
    """Universally stub fastembed so no test loads the real model. Single source of truth."""
    from app.services import embeddings as e
    for name in ("upsert_note_embedding", "delete_note_embedding",
                 "upsert_attachment_embeddings", "delete_attachment_embeddings",
                 "delete_entity_embedding", "write_attachment_embeddings"):
        monkeypatch.setattr(e, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(e, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(e, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(e, "embed_attachment_chunks", lambda *a, **k: [])
    monkeypatch.setattr(e, "embed_many",
                        lambda ts: [[0.0] * e.EMBEDDING_DIM for _ in ts])
    return e

@pytest.fixture
def db(temp_db_env, mock_embeddings):
    """Initialized temp DB. Resets process-global db state; seeds the access key."""
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    from app import auth
    auth.ensure_access_key()
    # reset process-global rate limiter so public requests don't bleed across tests
    from app.services import share as _share
    _share._HITS.clear()
    yield db
    db._local.__dict__.clear()

@pytest.fixture
def client(db):
    """Authenticated TestClient against the temp DB."""
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})

@pytest.fixture
def ondisk_db(temp_db_env, mock_embeddings):
    """Same as `db` but guarantees an on-disk path (system tier: thread/WAL contention)."""
    # temp_db_env already uses an on-disk path; system tests depend on it being a real file.
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    yield db
    db._local.__dict__.clear()
```

An integration test becomes `def test_x(client): ...`; a service test becomes `def test_y(db): ...`.
The six-line embedding stub block disappears from every file.

### 3.3 Canonical LLM mock (new — the most important standardization)

The `llm` module exposes a stable, narrow seam at module level:

```
llm.has_credentials() -> bool
llm.complete(messages, *, system=None, model=None, max_tokens=1024) -> str
llm.complete_with_meta(...) -> tuple[str, str | None]      # (text, stop_reason)
llm.complete_with_tools(...) -> tuple[str, list[ToolCall], dict | None]
llm.get_provider(model=None) -> Provider
```

**Every test mocks at this seam — never the Anthropic/OpenAI SDK directly.** This matches all 23
existing files that touch the LLM and keeps mocks provider-agnostic (Anthropic vs xAI/OpenAI is an
internal detail of `get_provider`). Standardize the mock into one fixture so the 23 ad-hoc
`monkeypatch.setattr(llm, "complete", ...)` blocks converge:

```python
@pytest.fixture
def mock_llm(monkeypatch):
    """Deterministic offline LLM. Default: credentials present, fixed reply.

    Usage:
        def test_x(client, mock_llm):
            mock_llm.set_reply("the canned answer")          # llm.complete / complete_with_meta
            mock_llm.set_tool_reply(text="", tools=[...])    # llm.complete_with_tools
            assert mock_llm.calls  # recorded (messages, kwargs) per call
    """
    from app.services import llm

    class _MockLLM:
        def __init__(self):
            self.reply = ""
            self.stop_reason = None        # "end_turn" | "max_tokens" | "length" | None
            self.tool_reply = ("", [], None)
            self.has_creds = True
            self.calls = []
        def set_reply(self, text, *, stop_reason=None):
            self.reply, self.stop_reason = text, stop_reason
        def set_tool_reply(self, *, text="", tools=None, meta=None):
            self.tool_reply = (text, tools or [], meta)

    m = _MockLLM()

    def _complete(messages, **k):
        m.calls.append((messages, k)); return m.reply
    def _complete_with_meta(messages, **k):
        m.calls.append((messages, k)); return (m.reply, m.stop_reason)
    def _complete_with_tools(messages, **k):
        m.calls.append((messages, k)); return m.tool_reply

    monkeypatch.setattr(llm, "has_credentials", lambda: m.has_creds)
    monkeypatch.setattr(llm, "complete", _complete)
    monkeypatch.setattr(llm, "complete_with_meta", _complete_with_meta)
    monkeypatch.setattr(llm, "complete_with_tools", _complete_with_tools)
    return m
```

Notes on the standard:

- The default reply is the empty string and `has_credentials()` is `True`, so a test that only needs
  "the LLM exists but I don't care what it says" just declares the fixture.
- Tests that assert the *finish reason* (batch writers checking `max_tokens`/`length` truncation, per
  `complete_with_meta`'s contract) use `set_reply(text, stop_reason="max_tokens")`.
- Tests that exercise the tool-calling path use `set_tool_reply(...)`.
- The few suites that need a fully custom provider (e.g. `test_research_labs_ai.py` patches
  `llm.get_provider`) may still do so directly — but for the 90% case (`complete` / `complete_with_meta`
  / `has_credentials`), `mock_llm` is the one and only pattern. New tests MUST NOT monkeypatch the SDK
  client classes.
- For streaming/tool seams the `mock_llm` object is the place to grow, so a future `set_stream(...)`
  lives in one file, not 23.

### 3.4 Frozen clock

A `_frozen_clock` fixture pins the app's time source to the same instant the parity fixture uses
(`2026-06-01T12:00:00+00:00`, from `tests/fixtures/time_tokens.json`). It must patch the **one**
function the app calls for "now" (e.g. `app.services.clock.now()` — confirm the exact symbol) rather
than `datetime.datetime`, so it's deterministic and cheap. Any test asserting age/relative-time
formatting (`@t[age:...]`, `since`, `until`) declares `_frozen_clock` and reuses
`time_tokens.json` as its parametrized cases, keeping Python and the TS `expandTimeTokens` in parity.
It is **not** autouse — most tests don't care about the clock, and freezing globally would mask bugs.

### 3.5 Determinism rules summary

- Embeddings: always mocked via `mock_embeddings`; never load the real fastembed model. Embed outputs
  are fixed zero-vectors of `EMBEDDING_DIM` — tests assert *plumbing* (chunk counts, FTS rows,
  status), never vector values.
- LLM: always `mock_llm` (or a deliberate `get_provider` stub); never the real SDK; never the network.
- DB: temp per test, process-global state (`db._initialized`, `db._local`, `share._HITS`, the
  entity-rebuild coalescing flags) reset by the fixture, not by hand in each test.
- Native deps: keep `pytest.importorskip("sqlite_vec")` / `("fastapi")` / `("anthropic")` at the top
  of files that need them, so a thin dev env skips rather than errors.

---

## 4. Config

Create **`server/pyproject.toml`** (preferred — single file, also a home for tooling later). If the
repo standard is `pytest.ini`, the `[tool.pytest.ini_options]` table maps 1:1 to a `[pytest]` section.

```toml
# server/pyproject.toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
# rootdir is server/, so `app` imports resolve without a src layout.
addopts = [
    "-ra",                       # summary of all non-pass outcomes
    "--strict-markers",          # unknown marker => error (forces the taxonomy)
    "--strict-config",
    "--import-mode=importlib",
    "-p", "no:cacheprovider",    # CI: no stale .pytest_cache surprises
]
markers = [
    "unit: fast, isolated, no app wiring or DB init",
    "integration: temp-SQLite + TestClient/service wiring; LLM & embeddings mocked",
    "system: real threads/WAL contention/background workers; on-disk DB; slower",
]
filterwarnings = [
    "error",                     # tests must be warning-clean
    # add explicit ignores here as third-party deprecations surface, e.g.:
    # "ignore::DeprecationWarning:dateutil.*",
]
# Async worker polling is bounded; fail a wedged test instead of hanging CI.
# (requires pytest-timeout in requirements-dev.txt)
timeout = 60
timeout_method = "thread"

[tool.coverage.run]
source = ["app"]
branch = true
omit = [
    "app/main.py",               # ASGI wiring exercised indirectly; exclude only if it skews the floor
    "*/__pycache__/*",
]
parallel = true                  # combine across pytest-xdist workers

[tool.coverage.report]
show_missing = true
skip_covered = false
fail_under = 79                  # ratchet: start at the current ~79%, never regress
exclude_lines = [
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "if __name__ == .__main__.:",
]
```

Coverage floor policy:

- Start `fail_under` at **79** (the measured baseline) so adopting the standard never blocks on a
  pre-existing gap.
- **Ratchet, don't leap:** raise `fail_under` only when real coverage rises (e.g. bump to 82 after the
  rebuild-engine and provider-layer gaps from the audit are closed). Never set a floor above current
  actual coverage — that turns CI red without a code change.
- Per-module floors are not enforced by `fail_under` (it's whole-suite); track high-risk weak modules
  (rebuild engine, trips, LLM provider layer, lab vision, chat relay 26–54%) in the audit doc and use
  `--cov-report=term-missing` locally to drive them up.

Add to **`server/requirements-dev.txt`**:

```
pytest==8.3.4
httpx==0.28.1
pytest-cov==6.0.0
pytest-timeout==2.3.1
pytest-xdist==3.6.1        # optional; see §5
```

---

## 5. Speed / determinism rules

- **No real network — enforced, not trusted.** The autouse `_no_network` guard (§3.1) blocks all
  non-loopback sockets. This is the backstop behind the "LLM is always mocked" rule.
- **Frozen time** via the opt-in `_frozen_clock` fixture for any time-formatting assertion; the app's
  "now" comes from one patchable function, never `datetime.now()` scattered in call sites.
- **Seeded randomness:** autouse `random.seed(0)`. Embeddings are mocked so model nondeterminism is a
  non-issue.
- **Polling, bounded.** System-tier async assertions poll a status endpoint/flag with a deadline
  (`run_and_wait`, the rebuild-drain loops) — keep this pattern but cap each poll loop at a few
  seconds, and rely on the global `timeout = 60` to kill a wedged test rather than hang CI. Prefer the
  module's `run_inline` test seam (as `test_async_entity_rebuild.py` does) to make the common case
  synchronous and deterministic; reserve real-thread polling for the explicit `system` tests that are
  *about* concurrency.
- **Parallelism (pytest-xdist): default OFF, available for the full run.**
  - Tradeoff: most tests are independent (fresh `tmp_path` DB each), so xdist would cut wall time
    materially. **But** several suites mutate **process-global** state — `llm._client_cache`,
    `db._initialized`/`db._local` (thread-local, but the module flag is global), `share._HITS`, the
    entity-rebuild coalescing flags, and env vars via `os.environ`. These are safe *within* one
    worker process (reset per test) but the `system` tier specifically spins real threads and contends
    on a single on-disk WAL; running those in parallel against shared module globals is asking for
    flakes.
  - Standard: run `unit` + `integration` under `-n auto` (each xdist worker is its own process, so
    module globals are isolated per worker and the per-test resets still hold). Run `system` tests
    `-p no:xdist` (serial) via marker selection. The `test` entrypoint encodes this (§6).
  - Until every global-state reset is proven xdist-safe, it is acceptable to ship the standard with
    xdist OFF and enable it as a follow-up once the suite is green serially.

---

## 6. The single `test` entrypoint (backend)

The cross-domain framework's `test` command must, for the backend, run pytest from `server/` with the
config above. Concrete commands:

**Full run (default — what CI runs):**
```
cd server && python -m pytest --cov=app --cov-report=term-missing --cov-report=xml
```
This honors `fail_under = 79` and `--strict-markers`. It runs all three tiers serially (safe default).

**Unit-only (fast inner loop / pre-commit):**
```
cd server && python -m pytest -m unit -q --no-cov
```

**Everything except the slow concurrency tier (fast PR check):**
```
cd server && python -m pytest -m "not system" -n auto --cov=app --cov-report=term-missing
```
(`-n auto` only over `unit`+`integration`, which are process-isolated and global-state-reset-safe.)

**System tier (serial, real threads):**
```
cd server && python -m pytest -m system
```

How the entrypoint exposes the modes (the framework wires these to flags so backend & frontend share
the verbs):

| Verb | Backend command |
|---|---|
| `test` | full run + coverage + floor (the CI default) |
| `test --unit` | `pytest -m unit --no-cov` |
| `test --fast` | `pytest -m "not system" -n auto` |
| `test --system` | `pytest -m system` |
| `test --cov` | full run with `--cov-report=html` for local inspection |

**CI:** add a `.github/workflows/backend-tests.yml` that, on push/PR touching `server/**`, installs
`requirements.txt -r requirements-dev.txt` and runs the full-run command. This closes the audit's
"CI never runs tests" gap. The existing `android-apk.yml` / `pages.yml` workflows are untouched.

---

## 7. Migration impact

The existing 36 files already follow the spirit of this standard (mocked embeddings, mocked LLM at the
module seam, temp DB, importorskip guards). The migration is mostly **additive + mechanical**, not a
rewrite. Estimated churn and a safe order:

**Phase 0 — config only (zero test edits, immediate value).**
- Add `pyproject.toml`, the `requirements-dev.txt` deps, and the CI workflow.
- Run the full suite as-is to confirm ~808 pass and capture the real coverage number; set
  `fail_under` to that floor.
- Risk: `filterwarnings = ["error"]` may surface pre-existing deprecation warnings → add targeted
  `ignore` entries (don't downgrade to non-error). `--strict-markers` is a no-op until markers exist
  (Phase 2), so introduce it in the same commit as the marker pass.
- Effort: ~1 file added, 0 tests changed.

**Phase 1 — conftest fixtures (no behavior change).**
- Add `db` / `client` / `ondisk_db` / `mock_embeddings` / `mock_llm` / `_frozen_clock` fixtures and the
  autouse `_no_network` / `_seed_randomness` guards to `conftest.py`.
- Keep the existing per-file `client`/`ctx` fixtures working — the new fixtures are *available* but
  nothing is forced to use them yet. The autouse network guard is the one behavior change; run the
  full suite to catch any test that was silently relying on real loopback beyond TestClient (expected:
  none).
- Effort: ~1 file (conftest), 0 test files edited; one full-suite run to validate.

**Phase 2 — markers (mechanical, one line per file).**
- Add `pytestmark = pytest.mark.<tier>` to all 36 files. Classification is straightforward:
  - pure-logic files (`test_geo.py`, the migration files, `test_nickname_lexicon.py`,
    `test_wikilinks.py`, the `time_tokens` parity test) → `unit`.
  - everything using `TestClient`/`init_db()` → `integration` (the majority).
  - `test_freeze_regression.py` and the threaded paths in `test_async_entity_rebuild.py` /
    `test_pipeline_lock_release.py` → `system` (mark the file `integration`, tag the threaded tests
    `system`).
- Effort: 36 one-line edits + a handful of per-test `@pytest.mark.system` tags. Low risk; verified by
  `pytest --collect-only -m unit/integration/system` counts summing to the total.

**Phase 3 — fixture convergence (incremental, opportunistic).**
- Migrate files to the canonical `client` / `db` / `mock_llm` fixtures, deleting the duplicated ~25-line
  setup blocks. Do this **one file per PR**, highest-duplication first (`test_api.py` is 441 KB and the
  biggest win, but also the riskiest — do it last or split it). Each migration is behavior-preserving:
  the canonical fixtures are exactly the union of what the per-file fixtures already do.
- This is the only phase with real per-file churn (~20 files have a `client` fixture, ~23 touch the
  LLM). It can run for weeks without blocking anything else — Phases 0–2 already deliver markers,
  coverage floor, CI, and the no-network guarantee.
- Effort: ~20–25 files, ~one focused PR each; net **deletion** of code.

**Net cost:** Phases 0–2 (the load-bearing standard: config, fixtures, markers, CI, coverage floor)
are ~2 new/edited files of infrastructure plus 36 one-line marker additions — landable in a day with
one green full-suite run as the gate. Phase 3 is optional cleanup that reduces total LOC and can be
spread across normal feature work. No test logic is rewritten; the ~808 passing tests stay green
throughout.
