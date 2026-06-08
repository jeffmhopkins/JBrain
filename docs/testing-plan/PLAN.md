# JBrain Common Testing Framework — Plan (hardened)

Authoritative plan. Supersedes `PLAN-DRAFT.md`. Synthesizes the three domain designs
(`backend-design.md`, `frontend-design.md`, `harness-design.md`) and the two red-team
passes (adoption/ergonomics + technical correctness). Every red-team finding is folded in;
see §7 for the audit trail.

---

## 1. The idea (one paragraph)

One committed command — **`./jt`** ("JBrain Test") — runs tests across domains through **one
shared vocabulary**: `unit` / `integration` (and later `system`). Each domain keeps its
native runner (pytest, vitest), but the same words select the same scope everywhere. `./jt`
is a plain ~40-line POSIX shell script at the repo root — **no new dependency, no PATH/alias
setup** — and the existing `pytest` / `npm test` commands keep working unchanged. Tests are
migrated **additively** (markers, config), never rewritten; the 808 backend + 72 frontend
tests stay green throughout.

**Design principle the red team forced:** ship the *smallest* thing that delivers the ask
("unit testing within each domain" + "a common framework, named as easy as possible"), and
defer everything aspirational behind explicit triggers (§6). Half of the original draft —
`just`, Playwright/e2e, MSW, a `flows` YAML tier, Android JUnit, coverage aggregation,
pytest-xdist — is **deferred**, not in the first cut.

---

## 2. Name & entrypoint — committed `./jt` (no `just`)

A single executable `./jt` at the repo root (`chmod +x`, plain `sh`, `case "$1"`). Rationale
(both red teams): a root `./jt` is discoverable from a fresh clone with zero setup, has no
PATH/alias friction, and is one source of truth — versus the draft's three-file
`just`+shim+`test.sh` triple that would silently drift. `just` buys recipe-dependencies we
don't need at this size; revisit only if the script gets painful.

```sh
#!/usr/bin/env sh
# jt — JBrain Test. One command, every domain. Run from anywhere in the repo.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
cmd="${1:-all}"; shift 2>/dev/null || true
back() { ( cd "$ROOT/server" && python -m pytest "$@"; ); }
front(){ ( cd "$ROOT/web"    && npm run "$@"; ); }
case "$cmd" in
  all)         back -m "not concurrency" && front test ;;
  back)        back "$@" ;;
  front)       front test ;;
  unit)        back -m unit --no-cov && front test ;;   # "unit" means the same word both sides
  cov)         back --cov=app --cov-report=term-missing && front test:cov ;;
  watch)       case "${1:-front}" in back) back -f ;; *) front test:watch ;; esac ;;
  *)           echo "usage: ./jt [all|back|front|unit|cov|watch]"; exit 2 ;;
esac
```

The native commands stay first-class and documented (README "Development"):
`cd server && pytest`, `cd web && npm test`. `./jt` is a convenience wrapper, not a
replacement. (Optionally add `./jt all` as a smoke line to `.claude/hooks/session-start.sh`.)

---

## 3. Shared taxonomy (one vocabulary)

| Tier | Means (same idea in every domain) | Backend | Frontend |
|---|---|---|---|
| **unit** | one module/function/component, isolated, hermetic, no DB/network/server | `@pytest.mark.unit` | `*.test.ts` pure-logic (convention) |
| **integration** | real intra-domain wiring, externals (LLM/embeddings/network) mocked | `@pytest.mark.integration` (default) | RTL render + mocked `fetch` (convention) |
| **system** *(deferred)* | whole product across domains, real browser + real API | — | Playwright in `e2e/` (§6) |

Plus one **backend-local** tag, deliberately **not** named `system` (red-team #3 — avoid the
collision where "system" meant both in-process thread tests *and* cross-domain e2e):

| `@pytest.mark.concurrency` | backend tests that need real threads / on-disk WAL contention / background workers (e.g. `test_freeze_regression.py`, the threaded paths in `test_async_entity_rebuild.py`). Run **serially** (`-n0`). |

**Convention over ceremony (red-team #4):** backend expresses tiers via pytest markers (one
`pytestmark` line per file — idiomatic, enforced by `--strict-markers`). Frontend keeps a
**single `vitest run`** and treats the tier as a *documented convention*, not an enforced
split — **no `.unit.`/`.int.` filename suffixes and no `vitest projects`** (the latter is
invalid in vitest 2.1.9 anyway — red-team #2). If frontend tier-splitting is ever needed,
select by path glob (`vitest run src/**/*.unit.test.*`), don't rename 10 files.

---

## 4. Per-domain standards

### 4.1 Backend (`server/`)
- **`server/pyproject.toml`** (new): `testpaths=["tests"]`; `addopts` with `--strict-markers`,
  `-ra`, `--import-mode=importlib`; markers `unit` / `integration` / `concurrency`.
  - **Do NOT ship `filterwarnings=["error"]`** (red-team #1 — verified: `-W error` fails on
    the first test via `PytestUnraisableExceptionWarning` from unclosed sockets/event loop; a
    clean run is `808 passed, 4 warnings`). Omit it; revisit only after the socket/loop leaks
    are fixed.
  - `timeout`/`timeout_method` and any `--strict-config` go in **only once `pytest-timeout`
    is installed** (red-team #7), so add the deps in the same change.
- **Coverage:** `[tool.coverage]` `source=["app"]`, `branch=true`, **`fail_under=79`**
  (current baseline; ratchet up, never above actual).
- **Canonical fixtures** in `server/tests/conftest.py`: `db`, `client`, `ondisk_db`,
  `mock_embeddings` (universal fastembed stub) — these replace the ~25-line setup duplicated
  across ~20 files and are verified to match the real module seams (`db._initialized`,
  `db._local`, `share._HITS` all exist).
  - **`mock_llm` is an opt-in helper, not a universal mandate** (red-team #3 — verified
    `complete` and `complete_with_meta` are *independent* functions; many tests feed parseable
    JSON, so an empty-string default would break them). Default reply is shared, and
    `complete_with_meta`'s mock delegates to the same recorded reply; files using
    `complete_with_tools`/`get_provider` keep bespoke setup.
  - **`_frozen_clock`:** `clock.py` has **no `now()`** (red-team #4). Prefer passing
    `now=` into `expand_tokens(...)` (the seam the code already supports); where a global is
    needed, patch both `clock.now_utc` and `clock.now_local`.
- **Autouse guards:** `_no_network` (block non-loopback sockets — makes "LLM always mocked" a
  hard guarantee), `_seed_randomness` (`random.seed(0)`), keep existing
  `_reset_llm_client_cache`.
- **Dev deps** (`requirements-dev.txt`): add `pytest-cov`, `pytest-timeout`. `pytest-xdist`
  is **optional/off** by default (concurrency tier must stay serial; parallelism is a later
  optimization once global-state resets are proven xdist-safe).

### 4.2 Frontend (`web/`)
- **`vitest.config.ts`:** land the **flat** config from `frontend-design.md` §5 (the only one
  valid for 2.1.9). Add v8 coverage (`@vitest/coverage-v8` — already installed locally,
  **commit it**), reporters, `exclude` tests/`main.tsx`, and a **threshold floor of 7**
  (ratchet up). `restoreMocks`/`unstubGlobals`/`unstubEnvs: true`.
- **Scripts:** add `test:cov` (`vitest run --coverage`), `test:watch`, `test:ui`. `build`
  (`tsc -b && vite build`) stays part of the CI gate — a type error must fail CI.
- **Shared test-utils** in `web/src/test/`: `renderWithProviders(ui, {route})` → `{user,
  ...RTL}`; relocate the 3 root-level component tests next to their sources.
- **API mocking:** keep the existing **hand-rolled `vi.stubGlobal("fetch")`** pattern
  (proven by `SearchPageGating`/`api.getStatus`). **MSW is deferred** (red-team #5/#6 — not
  installed; needs an undici smoke-test and careful `onUnhandledRequest:"error"` ordering vs
  the fetch-stub tests). Add it in phase 2 when page tests multiply, not now.
- **Determinism:** fake timers for time-driven code, real WebCrypto round-trips (seed
  `getRandomValues` only when asserting exact bytes), mock geo/Notification per test, reset
  module singletons (`__reset()`/`__resetToasts()`) in `afterEach`.

---

## 5. CI & coverage policy

**`.github/workflows/test.yml`** — two jobs, **per-domain coverage floors, no aggregation**
(red-team #5 — verified combined ≈ 50–55%, so a combined floor of 60 is red on day one and a
combined ratchet is meaningless across a 15.6k/~9k split):

- **`back`:** `setup-python@3.11` → install `requirements.txt` + `requirements-dev.txt`
  **before** any pytest step (else collection fails on missing `fastapi`/`pydantic`) →
  `pywebpush` best-effort (`|| ::warning::`, never fails the job — push is import-guarded) →
  `pytest -m "not concurrency" --cov=app` (gate at `fail_under=79`) → optional `concurrency`
  step serial.
- **`front`:** `setup-node@20` → `npm ci` → `tsc -b --noEmit` (typecheck = gate) →
  `vitest run --coverage` (gate at threshold 7).
- Keep `pages.yml` / `android-apk.yml` untouched. No combined gate, no `cov_report.py`.

Coverage = **per-domain ratchet**: backend 79 → raise as the audit's weak modules
(rebuild_engine 35%, trips 26%, llm 43%, lab_vision 52%) improve; frontend 7 → raise after
the first feature components land.

---

## 6. Explicitly deferred (with the trigger that un-defers each)

| Deferred | Why now | Trigger to add it |
|---|---|---|
| **Playwright `system`/e2e** (`e2e/`) | Big lift; no Playwright yet. *(Verified feasible: `app.main:app` boots with no LLM key, `/api/health` exists; "LLM mocked at boundary" needs a **registered fake provider** in `llm._REGISTRY`, not just an env var.)* | After the per-domain gates are green and stable; it's the audit's P3. |
| **MSW** | Hand-rolled stubs already work; needs undici smoke-test | When >~5 page tests share endpoint mocking |
| **`flows` YAML tier** | No such tests exist | When a workflow regression bites |
| **Android JUnit** | No tests, no JUnit deps in `libs.versions.toml` | When `android/` logic needs guarding |
| **Coverage aggregation** (`cov_report.py`) | Per-domain floors are simpler & correct | If a single dashboard number is ever required (use Codecov flags, not a bespoke script) |
| **pytest-xdist parallelism** | Global-state/`concurrency` flake risk | When suite wall-time hurts and resets are proven worker-safe |
| **`just`** | `./jt` shell script suffices | If recipe dependencies/args get unwieldy |

---

## 7. Phased rollout

1. **Build the common system (the "new common system" you migrate onto later).** Commit
   `./jt`; add `server/pyproject.toml` (markers + `fail_under=79`, **no** warning-errors) and
   the dev deps; land the flat `vitest.config.ts` coverage block + commit `@vitest/coverage-v8`
   + scripts. **Zero test edits**; run both suites to confirm 808 + 72 stay green.
2. **Wire CI** (`test.yml`, two jobs, per-domain floors at today's baselines — informational
   ratchet). Closes the audit's "CI never runs tests" gap.
3. **Backend markers** — one `pytestmark` line per file (`unit`/`integration`, tag the
   threaded tests `concurrency`); flip CI to `-m "not concurrency"`. Verify
   `--collect-only -m …` counts sum to the total.
4. **Frontend conventions + test-utils** — add `renderWithProviders` + shared setup; relocate
   the 3 root tests; begin the audit's first 5 feature components (`Chat`, `SharesPage`+
   `EncryptedChat`, `NotePage`, `RebuildPanel`, `CalendarPage`). Raise the frontend floor.
5. **Backend fixture convergence** — migrate files onto canonical `db`/`client`/`mock_llm`
   one PR at a time (net code *deletion*; `test_api.py` last).
6. **Un-defer §6 items** as their triggers fire (Playwright first — it's the real "system"
   tier the coverage audit called for).

Phases 1–2 are the "common system." Migration (3–5) is the follow-up you flagged.

---

## 7-bis. Red-team fixes applied (audit trail)
- **`filterwarnings=error` removed** (would red CI on day one — verified).
- **vitest `projects` dropped**; flat config kept (`projects` is 3.x-only; invalid in 2.1.9).
- **Per-domain coverage floors**, no combined gate (combined ≈55% fails a 60 floor).
- **`system` marker renamed `concurrency`** for backend; `system` reserved for e2e.
- **`mock_llm` is opt-in**, `complete_with_meta` delegates; not a universal mandate.
- **`_frozen_clock` fixed** to real seams (`expand_tokens(now=)` / `now_utc`+`now_local`).
- **`just`/shim/fallback triple → single `./jt`**; MSW/e2e/flows/android/xdist/aggregation deferred.
- **Dev deps (`pytest-cov`,`pytest-timeout`) added before** `timeout`/`strict-config` are enabled.
