# JBrain Cross-Domain Testing Harness — Design

A unifying **harness + conventions** layer so every domain (Python/pytest backend,
TypeScript/vitest PWA, Kotlin/Android, YAML automation) is tested through **one
memorable command**, with a **shared taxonomy**, **consistent directory layout**,
**unified CI**, and **aggregated coverage**.

These languages cannot share a single test *runner*. "Common framework" therefore
means: one dispatcher command, one vocabulary, one CI workflow, one coverage view.

---

## 0. Repo reality check (what exists today)

| Domain | Location | Runner | Tests today |
|---|---|---|---|
| Backend | `server/` | pytest 8.3.4 (+ httpx) in `requirements-dev.txt` | 38 files in `server/tests/`, `conftest.py`, no markers/config yet |
| Frontend | `web/` | vitest 2.1.9, config in `web/vitest.config.ts`, `npm test` = `vitest run` | 10 `*.test.{ts,tsx}` colocated in `src/` |
| Android | `android/` | Gradle (Kotlin DSL), JDK 17, modules `:app` + `:wear` | none yet; no `testImplementation` in `libs.versions.toml` |
| Workflows | `workflows/` (+ `actions/`) | none | 21 workflow YAMLs, 39 action YAMLs; parsed by `server/app/services/workflows.py` |

Existing CI: `.github/workflows/android-apk.yml` (build APK), `pages.yml` (build/deploy
PWA). **Neither runs tests.** No root task-runner (`just`/`make`/root `package.json`)
exists. The SessionStart hook (`.claude/hooks/session-start.sh`) already installs
server Python deps (runtime + dev + vision) and web `node_modules` on the web
container, so the harness can assume those are present in-session.

**Environment caveats (load-bearing for CI):**

- `pywebpush==2.0.0` pulls `py-vapid` + `http-ece`, which need `cryptography` native
  builds; these do **not** reliably build in the ephemeral container. Backend tests
  must tolerate its absence (import-guard the push module) — the harness installs a
  best-effort path and never hard-fails on it.
- `fastapi`/`pydantic` (and `pydantic-settings`) **must** be installed before backend
  tests import the app, or collection fails. CI installs `requirements.txt` +
  `requirements-dev.txt` first, explicitly.

---

## 1. Recommended NAME — **`jt`** ("JBrain Test")

The user wants it "named as easy as possible." It becomes a command, a directory, and
a CI job name, so it must be short, typeable, and non-clashing.

### Candidates

| Name | Type cost | Says as | Clash risk | Notes |
|---|---|---|---|---|
| **`jt`** ✅ | 2 chars | "jay-tee" | none on PATH (checked: not a common tool) | "JBrain Test"; fits `jt`, `jt unit`, `jt cov` |
| `check` | 5 | "check" | mild (generic) | reads as a verb; nice but longer |
| `t` | 1 char | "tee" | high — too generic, easy to shadow | tempting but collides with shell habits |
| `brain-test` | 10 | "brain test" | none | descriptive but long to type |
| `verify` | 6 | "verify" | none | good verb, longer |
| `jbt` | 3 | "jay-bee-tee" | none | clear but a touch clunky to say |

**Recommendation: `jt`.** Two keystrokes, unambiguous within the project ("JBrain
Test"), reads naturally as `jt`, `jt unit`, `jt back`, `jt cov`, `jt watch`. The
single-char `t` is rejected (too easy to shadow / confuse). The repo-root harness file
is the **justfile**, and `jt` is provided as a 1-line shim/alias so the *spoken* name
and the *tool* are decoupled — you can switch task-runners later without changing the
name people type.

The shared CI job, the e2e dir convention, and docs all use the `jt`/"JBrain Test"
branding.

---

## 2. Single entrypoint — recommend **`just` (a `justfile`)**, fronted by a `jt` shim

### Comparison

| Option | New dep | CI container | Session hook | Sub-commands | Verdict |
|---|---|---|---|---|---|
| **`just`** | one tiny static Rust binary (`extractions/setup-just` in CI; `cargo`/`apt`/`brew` locally) | trivial to add (1 action) | trivial | first-class recipes w/ args & deps | **Recommended** |
| `make` | usually preinstalled | yes | yes | works, but `.PHONY` noise, tab-sensitive, arg passing is ugly (`make test ARGS=...`) | viable fallback |
| root npm workspace | needs Node at root for *all* domains | forces Node even for Python-only runs | heavier | scripts only, no real args | rejected — couples non-JS domains to Node |
| `./scripts/test.sh` | zero | yes | yes | hand-rolled `case` dispatch | rejected as primary — reinvents `just`; fine as the thing recipes *call* |

**Why `just`:** purpose-built command runner, clean recipe syntax with positional args
(`test target=...`), recipe dependencies, `.env` loading, and a single self-contained
binary that installs in one CI step and one hook line. It does **not** force any
language toolchain at the root (unlike a root `package.json`). It avoids `make`'s
tab/`.PHONY`/argument awkwardness. The named `jt` command is a thin shim so the
ergonomics ("name it easy") are independent of the runner choice.

### `jt` shim — `scripts/jt` (committed, `chmod +x`)

```sh
#!/usr/bin/env sh
# jt — JBrain Test. Thin front for the root justfile so the command people type
# ("jt", "jt unit", "jt cov") is decoupled from the task-runner underneath.
# Put scripts/ on PATH, or `alias jt="$(git rev-parse --show-toplevel)/scripts/jt"`.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v just >/dev/null 2>&1; then
  exec just --justfile "$ROOT/justfile" --working-directory "$ROOT" "$@"
fi
# Fallback if `just` isn't installed: drive the plain dispatcher.
exec "$ROOT/scripts/test.sh" "$@"
```

### Root `justfile`

```just
# JBrain Test (jt) — one command for every domain.
# Usage: jt            (everything: lint + unit + integration)
#        jt unit       (fast unit tier, all domains)
#        jt back|front|e2e|android|flows   (one domain)
#        jt cov        (everything + aggregated coverage report)
#        jt watch      (front watch by default; `jt watch back` for pytest-watch)
set shell := ["bash", "-cu"]

ROOT      := justfile_directory()
SERVER    := ROOT / "server"
WEB       := ROOT / "web"
E2E       := ROOT / "e2e"
ANDROID   := ROOT / "android"
COV_FLOOR := "60"   # combined-line-coverage gate; raised over time (see rollout)

# Default: the full local gate (no e2e/android — those are opt-in / CI).
default: lint unit integration

# ---- aggregate tiers (run the same NAMED tier across domains) ----------------
unit:        back-unit front-unit
integration: back-integration front-integration
# `jt test` is an explicit alias of the default for muscle memory.
test: default

# ---- backend (pytest markers: unit / integration / e2e) ----------------------
back: back-unit back-integration
back-unit:
    cd {{SERVER}} && python -m pytest -m unit -q
back-integration:
    cd {{SERVER}} && python -m pytest -m integration -q
# Whole backend suite regardless of marker (used while migrating).
back-all:
    cd {{SERVER}} && python -m pytest -q

# ---- frontend (vitest projects: unit / integration) --------------------------
front: front-unit front-integration
front-unit:
    cd {{WEB}} && npx vitest run --project unit
front-integration:
    cd {{WEB}} && npx vitest run --project integration

# ---- workflow/action YAML validation (the "flows" domain) --------------------
flows:
    cd {{SERVER}} && python -m pytest -m flows -q

# ---- system / cross-domain e2e (Playwright; real PWA + real API, LLM mocked) -
e2e:
    cd {{E2E}} && npm ci && npx playwright test

# ---- android (only when the SDK is provisioned) ------------------------------
android:
    cd {{ANDROID}} && ./gradlew testDebugUnitTest --no-daemon

# ---- lint / typecheck (cheap gate, runs first) -------------------------------
lint:
    cd {{WEB}} && npx tsc -b --noEmit
    cd {{SERVER}} && python -m pytest --collect-only -q >/dev/null

# ---- coverage (per-domain machine formats, then aggregate) -------------------
cov: cov-back cov-front cov-merge
cov-back:
    cd {{SERVER}} && python -m pytest --cov=app --cov-report=xml:{{ROOT}}/coverage/back.xml -q
cov-front:
    cd {{WEB}} && npx vitest run --coverage \
        --coverage.reporter=lcov --coverage.reportsDirectory={{ROOT}}/coverage/front
cov-merge:
    python {{ROOT}}/scripts/cov_report.py --floor {{COV_FLOOR}}

# ---- watch (TDD loop) --------------------------------------------------------
watch target="front":
    {{ if target == "back" { "cd " + SERVER + " && python -m pytest --color=yes -f -q" } \
        else { "cd " + WEB + " && npx vitest" } }}
```

`jt`, `jt unit`, `jt back`, `jt front`, `jt e2e`, `jt cov`, `jt watch` all map to the
recipes above. The optional `scripts/test.sh` (a plain `case "$1" in unit) … esac`
dispatcher calling the same underlying commands) is the zero-dependency fallback the
shim uses if `just` is ever missing.

---

## 3. Shared taxonomy & naming (one vocabulary, three tiers)

Exactly **three tiers**, spelled the same everywhere:

| Tier | Means (identical across domains) | Speed / network |
|---|---|---|
| **unit** | One module/function/component in isolation. No DB, no network, no real server, no filesystem beyond temp. Mock at the nearest boundary. | milliseconds, hermetic |
| **integration** | Several real units wired together within ONE domain (e.g. FastAPI route → service → in-memory SQLite; React component → real hook → mocked `fetch`). Real intra-domain plumbing, external services (LLM, push) mocked. | sub-second, in-process |
| **system (e2e)** | The whole product across domains: built PWA in a real browser against a real FastAPI server, LLM mocked **at the boundary**. | seconds, real stack |

A fourth, narrow tier sits beside `integration` for the automation layer:

| Tier | Means |
|---|---|
| **flows** | Validate every `workflows/*.yaml` + `actions/*.yaml`: schema/shape, the `key`/`name`/`trigger`/`action` contract, `croniter`-parseable schedules, and that each `action.type` resolves in the action registry. Pure parse/validate + a few integration runs against a stub. |

### Mapping onto runners — "unit" means the same thing in both

**pytest (backend)** — register markers and make them strict in `server/pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers"
markers = [
  "unit: isolated, no DB/network/server",
  "integration: real intra-backend wiring (app + sqlite), externals mocked",
  "e2e: cross-domain system test (rarely lives here; prefer e2e/)",
  "flows: workflow/action YAML validation",
]
```

Then `jt back-unit` → `pytest -m unit`, `jt back-integration` → `pytest -m integration`.
(During migration, unmarked tests still run via `jt back-all`.)

**vitest (frontend)** — use **vitest projects** keyed by filename suffix so the same
words select the same scope:

```ts
// web/vitest.config.ts (extended)
test: {
  environment: "jsdom",
  globals: true,
  setupFiles: ["./src/test/setup.ts"],
  projects: [
    { test: { name: "unit",        include: ["src/**/*.unit.test.{ts,tsx}",
                                             "src/**/*.test.{ts,tsx}"] } }, // legacy default = unit
    { test: { name: "integration", include: ["src/**/*.int.test.{ts,tsx}"] } },
  ],
}
```

Convention: `foo.unit.test.tsx` / `foo.int.test.tsx`. Existing bare `*.test.tsx` are
treated as **unit** until migrated. So `jt unit` runs `pytest -m unit` **and**
`vitest --project unit`, and the word "unit" is one concept project-wide.

### Directory conventions

```
server/tests/        # pytest; markers carry the tier (no per-tier subdirs needed)
web/src/**/*.test.*  # vitest colocated; .unit./.int. suffix carries the tier
e2e/                 # Playwright system tier (repo root) — see §5
android/**/test/     # Gradle JVM unit tests (testDebugUnitTest)
workflows/, actions/ # validated by the `flows` pytest tier
coverage/            # gitignored; per-domain machine reports + merged summary (§7)
```

---

## 4. E2E / system layer — **Playwright** at repo root `e2e/`

Playwright is the cross-domain system tier: it drives the **real built PWA** in a real
browser against a **real FastAPI server**, with the **LLM mocked at the boundary**
(point `LLM_*`/`ANTHROPIC` at a tiny stub server, or set the app's provider to a fake
that returns canned completions). This exercises the actual contract between PWA and
API — the thing no per-domain test can cover.

### Layout

```
e2e/
  package.json          # only playwright + @playwright/test
  playwright.config.ts  # webServer stanza boots API + serves built PWA
  tests/
    capture-note.system.spec.ts
    search.system.spec.ts
  fixtures/llm-stub/     # canned LLM responses served at the LLM boundary
```

### How `jt e2e` launches the stack

`playwright.config.ts` uses a `webServer` block so one command brings the world up:

1. Build the PWA once: `npm --prefix ../web run build` (→ `web/dist`).
2. Start FastAPI with a temp SQLite DB and `LLM_PROVIDER` pointed at the local stub:
   `uvicorn app.main:app` from `server/`, with `DB_PATH=$TMP/e2e.db`.
3. Serve `web/dist` (vite preview or a static server) at a base that proxies `/api` to
   the API.
4. Playwright waits for `/api/health` (the same endpoint the docker healthcheck uses),
   then runs `*.system.spec.ts`.

Kept out of the default `jt` gate (it's slow / needs browsers); run explicitly via
`jt e2e` and as its own CI job (§6).

---

## 5. CI — `.github/workflows/test.yml`

Parallel **jobs per domain** (clearer logs and caching than a single matrix), each
emitting a coverage artifact; a final `coverage` job downloads them, merges, and
enforces the floor.

```yaml
name: jt   # JBrain Test — the cross-domain test gate
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  # ---------------- backend ----------------
  back:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: server } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - name: Install backend deps (fastapi/pydantic FIRST — required for collection)
        run: |
          python -m pip install -U pip
          # Core runtime + test deps must exist before the app imports.
          pip install -r requirements.txt -r requirements-dev.txt pytest-cov
          # pywebpush -> py-vapid/http-ece may not build in this container; best-effort,
          # never fail the job. The push module is import-guarded in app code.
          pip install pywebpush==2.0.0 || echo "::warning::pywebpush unavailable; push tests skipped"
      - name: Unit + integration (+ flows)
        run: python -m pytest -m "unit or integration or flows" \
               --cov=app --cov-report=xml:cov-back.xml -q
      - uses: actions/upload-artifact@v4
        with: { name: cov-back, path: server/cov-back.xml }

  # ---------------- frontend ----------------
  front:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: web } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20", cache: npm, cache-dependency-path: web/package-lock.json }
      - run: npm ci
      - run: npx tsc -b --noEmit            # typecheck = part of the gate
      - run: npx vitest run --coverage --coverage.reporter=lcovonly
      - uses: actions/upload-artifact@v4
        with: { name: cov-front, path: web/coverage/lcov.info }

  # ---------------- system / e2e ----------------
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - uses: actions/setup-node@v4
        with: { node-version: "20", cache: npm }
      - name: Install API + build PWA
        run: |
          pip install -r server/requirements.txt -r server/requirements-dev.txt
          npm --prefix web ci && npm --prefix web run build
      - name: Install Playwright
        run: cd e2e && npm ci && npx playwright install --with-deps chromium
      - name: Run system tests (LLM mocked at boundary)
        run: cd e2e && npx playwright test
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: playwright-report, path: e2e/playwright-report }

  # ---------------- android (only when android/ changed) ----------------
  android:
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch' ||
        contains(toJSON(github.event.commits.*.modified), 'android/')
    defaults: { run: { working-directory: android } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with: { distribution: temurin, java-version: "17", cache: gradle }
      - run: chmod +x gradlew && ./gradlew testDebugUnitTest --no-daemon --stacktrace

  # ---------------- aggregated coverage gate ----------------
  coverage:
    needs: [back, front]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - uses: actions/download-artifact@v4
        with: { path: coverage }
      - name: Merge + enforce floor
        run: python scripts/cov_report.py --floor 60 --summary "$GITHUB_STEP_SUMMARY"
      # Optional external dashboard:
      # - uses: codecov/codecov-action@v4
      #   with: { files: coverage/cov-back/cov-back.xml,coverage/cov-front/lcov.info }
```

Notes baked in:
- `fastapi`/`pydantic` installed **before** any backend `pytest` step.
- `pywebpush` install is best-effort (`|| echo ::warning::`) — it must not fail CI.
- Android job is conditional so non-native PRs don't pay for the JDK/SDK.
- The `coverage` job is the **gate**; it fails if combined coverage dips below the floor.

---

## 6. Coverage aggregation — one combined picture

Each domain emits a standard machine format:

- backend → Cobertura **XML** (`pytest --cov --cov-report=xml`)
- frontend → **lcov** (`vitest --coverage --coverage.reporter=lcov`)

A small `scripts/cov_report.py` parses both into a common `(lines_covered,
lines_total)` per domain and prints one table — written to the CI step summary
(`$GITHUB_STEP_SUMMARY`) and stdout for `jt cov`:

```
Domain     Lines    Cov%
backend    4210/5102  82.5
frontend    611/ 940  65.0
---------------------------
COMBINED   4821/6042  79.8   (floor 60 → PASS)
```

`--floor N` exits non-zero on the **combined** number, giving one gate. Two ratchets
to choose between: a **combined** floor (simple, recommended to start) and optional
**per-domain** floors as each matures. For a richer hosted dashboard, feed the same two
files to **Codecov** (commented stub above) with per-flag breakdown (`back`, `front`) —
the local script remains the source of truth for the gate so CI has no third-party hard
dependency. Android (JaCoCo XML) and a `flows` count can be folded into the same table
later by adding parsers; the report format is open-ended by design.

---

## 7. Android & workflows — same entrypoint

**Android** (`jt android` → CI `android` job): standard Gradle JVM unit tests via
`./gradlew testDebugUnitTest` on `:app` (and `:wear`). Requires adding
`testImplementation(kotlin("test"))` / JUnit to `android/gradle/libs.versions.toml` +
the modules' `build.gradle.kts` (none today). Instrumented/emulator tests are out of
scope for the gate. The SDK is provisioned on-demand by the existing
`.claude/hooks/setup-android-sdk.sh`; CI uses `setup-java` + Gradle cache and the job is
guarded to native-touching changes (mirrors `android-apk.yml`'s `paths:` filter).

**Workflows/actions** (`jt flows` → folded into the backend CI job): a `flows`-marked
pytest module loads every `workflows/*.yaml` and `actions/*.yaml` and asserts:
- required keys present (`key`, `name`, `trigger`, `action`) and `key` is unique;
- `trigger.type == schedule` ⇒ a valid `interval_seconds` and any cron is
  `croniter`-parseable;
- `action.type` resolves against the action registry used by
  `server/app/services/workflows.py`;
- a handful of **integration** runs drive a workflow end-to-end against an in-memory DB
  with the LLM stubbed.

This reuses the backend's Python toolchain (PyYAML is already a runtime dep), so it
needs no new runner — it's just another pytest marker.

---

## 8. Phased rollout

1. **Build the harness.** Add `justfile`, `scripts/jt` (PATH/alias), `scripts/test.sh`
   fallback, `scripts/cov_report.py`, and `server/pyproject.toml` markers
   (`--strict-markers`). Add the `jt` line to the SessionStart hook so it's ready in
   every web session. No tests change yet; `jt back-all` / `jt front` run today's suites.
2. **Wire CI.** Land `.github/workflows/test.yml` running `back` + `front` jobs
   (unmarked backend via `pytest -q`, all vitest) + the `coverage` gate at a **low
   floor** (e.g. 50) so it's informational first. Keep `pages.yml`/`android-apk.yml`.
3. **Migrate backend.** Tag the 38 `server/tests` files with `unit`/`integration`
   (+ split out `flows`). Flip CI to `-m "unit or integration or flows"`. Raise floor.
4. **Migrate frontend.** Adopt vitest **projects** + `.unit.`/`.int.` suffixes; rename
   the 10 existing tests (default-treated as unit). `jt unit`/`jt integration` now mean
   one thing across both domains.
5. **Add e2e.** Stand up `e2e/` with Playwright + the `webServer` stack and the
   LLM-boundary stub; add the `e2e` CI job. Start with 1–2 smoke journeys
   (capture a note, search).
6. **Android.** Add JUnit deps + first `:app` unit tests; enable the conditional
   `android` CI job and `jt android`. Fold JaCoCo into `cov_report.py` last.

Ratchet the combined coverage floor up one notch at the end of each phase.
