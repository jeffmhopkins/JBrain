# JBrain — Test Coverage Gap Report

**Date:** 2026-06-08
**Method:** Multi-agent audit. Four analysis agents mapped features/intent across the
full stack (backend API, backend services, frontend + automations + Android) and
inventoried existing tests; coverage tooling (`pytest-cov`, `vitest --coverage`) measured
real line/branch numbers. This report joins *features → tests* and flags gaps.

**Supporting detail** (in this folder):
- `features-backend-api.md` — 115+ HTTP endpoints across 20+ routers, with intent + risk.
- `features-backend-services.md` — 62 services (~21k SLOC) by domain cluster, with risk.
- `features-frontend-automations.md` — PWA (26 pages / 25+ components), 21 workflows, Android.
- `test-inventory.md` — every existing test file → what it exercises → type.
- `backend-coverage.json` / `web-cov/` — raw machine-readable coverage.

---

## 1. Headline

| Surface | Tests | Measured line coverage | Verdict |
|---|---|---|---|
| **Backend** (`server/app`, ~15.6k stmts) | 808 passing (pytest) | **78.6%** | Solid breadth; specific high-risk modules are weak |
| **Frontend** (`web/src`, ~16k LOC) | 72 (10 vitest files) | **7.8%** | Effectively untested — every page/feature component at 0% |
| **System / E2E** | **0** | — | No Playwright/Cypress; no end-to-end flow ever exercised |
| **CI** | **none** | — | `.github/workflows/` only builds the APK + Pages; tests never run on push/PR |
| **Workflows** (`workflows/*.yaml`, `actions/`) | partial (via service tests) | — | Trigger→action pipelines largely unexercised end-to-end |
| **Android** (`android/`) | **0** | — | No instrumentation/unit tests |

**The core finding:** the question was "does our *system* testing completely cover the
code base?" The honest answer is **no, on two axes**:
1. **Depth on the backend is good but uneven** — 79% overall masks a cluster of
   high-risk modules sitting at 26–54% (rebuild engine, trips, the LLM provider layer,
   lab vision, chat relay, staging/share routers).
2. **There is no *system* layer at all.** Every test is a unit or in-process API
   integration test against a temp SQLite DB with the LLM and embeddings mocked. The
   frontend, the PWA↔API contract, the encrypted-chat crypto path, the scheduled
   workflows, and the Android relay are never driven end-to-end. "System testing" in the
   sense of *a real user flow through real components* does not currently exist.

---

## 2. Coverage matrix (feature → coverage)

Legend: ✅ well covered · 🟡 partial / shallow · 🔴 little or none · ⚫ zero

### Backend domains

| Feature / domain | Key modules | Coverage | Notes |
|---|---|---|---|
| Note entry / write / routing | `notes.py`, routers `notes`,`medical`,`financial` | ✅ 87–94% | 351 tests; destination routing, versioning, source attribution all asserted |
| Entity identity (merge/split/alias) | `entity_index` 96%, `entity_decisions` 93% | ✅ | Strong: durable decisions, rebuild invariants, search expansion |
| Wiki build / synthesis | `wiki_build` 91%, `wikilinks` 93%, `wiki_guides` 95% | ✅ | High line coverage; *quality* of LLM output still human-judged only |
| Calendar / events / recurrence | `calendar.py` 79%, router 76% | ✅ | 81 tests: extraction, rrule expansion, classification |
| Search (hybrid FTS + vector) | `search.py` 80%, `embeddings` 89% | ✅ | Embeddings mocked; fusion + fallback tested |
| Lab parsing (text PDF) | `lab_parse` 80%, `lab_series` 90% | 🟡 | Geometry parsing tested; fewer real-document fixtures |
| **Lab vision (image/scan OCR)** | `lab_vision` **52%** | 🔴 | Identity verification + hallucination guards under-exercised; high risk (medical) |
| **Architect agent loop** | `architect.py` 70% **(461 stmts uncovered — biggest absolute gap)** | 🟡 | Tool-dispatch + streaming paths partially covered; many tool branches not |
| **LLM provider layer** | `llm.py` **43%** | 🔴 | Provider switching (Anthropic/OpenAI/xAI), retries, error paths mostly untested |
| **KB rebuild engine** | `rebuild_engine` **35%**, `rebuild_runs` 57%, router `rebuild` **27%** | 🔴 | Long-running async rebuild orchestration barely tested |
| **Trips / geo-trail derivation** | `trips.py` **26%** | 🔴 | Trip segmentation logic almost entirely uncovered |
| Audio/video transcription | `audio_transcription` **66%** | 🟡 | Whisper mocked; frame-sampling + error paths thin |
| Sharing (public + admin) | router `share` 69%, `share_admin` 75%, `staging` 67% | 🟡 | Token/bind/CSRF tested for happy path; many admin branches uncovered |
| **Encrypted chat relay** | `chat_relay` **68%**, `chat_share` 76% | 🔴 | Relay path + crypto envelope handling shallow; security-sensitive |
| Reference loop (capture→promote) | `reference_*` 83–91% | ✅ | 23 tests across lifecycle |
| Push notifications | `push.py` 81% | 🟡 | `pywebpush`/`http-ece` can't build in this env → encryption signing path not run |
| App wiring / startup | `main.py` **26%**, `db.py` 72% | 🔴 | Startup, migration wiring, lifespan rarely exercised |

### Frontend (PWA) — ⚫ across the board

Only utility/status modules are touched (`StatusDot` 90%, `Toaster` 100%, `SearchPage`
64%, `Icon` 100%). **Every feature surface is at 0%:**

| Feature | Component | Coverage |
|---|---|---|
| Compose box + 3 modes (Entry/Research/Full Brain) | `Chat.tsx` (955 lines) | ⚫ 0% |
| Note view / edit / history / diff | `NotePage` (492), `MarkdownDiff` | ⚫ 0% |
| Calendar (day/week/month) | `CalendarPage` (570) | ⚫ 0% |
| Map / location trail / geofence | `MapPage` (609) | ⚫ 0% |
| Shares + encrypted chat (Web Crypto) | `SharesPage` (496), `EncryptedChat` (218), `SharePage` (226) | ⚫ 0% |
| KB rebuild UI | `RebuildPanel` (480) | ⚫ 0% |
| Workflows editor | `WorkflowsPage` (418) | ⚫ 0% |
| System settings / model picker / backup-restore | `SystemPage` (369), `ModelPicker` | ⚫ 0% |
| Labs charts / import / share | `LabsPage`, `LabChart`, `LabImportPanel` | ⚫ 0% |
| Entities / knowledge graph | `EntitiesPage`, `GraphPage` | ⚫ 0% |
| SQL console | `SqlConsole` | ⚫ 0% |

### Automations & Android

| Area | Coverage | Notes |
|---|---|---|
| Event-triggered workflows (`analyze-new-note`, link audit) | 🟡 | Underlying services tested; trigger→action wiring not |
| Cron workflows (`calendar-alarms`, `daily-consolidate`) | 🔴 | TZ handling + watermark/backfill idempotency untested |
| Manual workflows (`wiki-build`, `promote-recurrences`) | 🟡 | Service logic partially covered; pipeline execution not |
| Android phone (capture, relay, location, upload) | ⚫ | No tests |
| Wear OS (capture tile, phone relay, note queue) | ⚫ | No tests; queue lost on app kill (untested failure mode) |

---

## 3. Prioritized gaps (what to fix, in order)

**P0 — Make the suite trustworthy & repeatable**
1. **Add a test CI workflow** (`.github/workflows/test.yml`) running `pytest` + `vitest run`
   on every push/PR. Today nothing runs tests automatically — regressions ship silently.
2. **Track coverage in CI** with a floor (e.g. backend ≥78%, ratchet up; web has nowhere to
   go but up). Add `pytest-cov` + `@vitest/coverage-v8` to dev deps + config.

**P1 — Close the high-risk backend holes** (medical / money / security / data-mutation)
3. **`lab_vision` (52%)** — OCR identity verification + hallucination guards. A false
   positive here mis-attributes medical data. Add fixtures for mismatched DOB, garbled OCR.
4. **`llm.py` (43%)** — provider switching, retry/backoff, and error/timeout paths. Mock
   each provider's failure modes.
5. **Rebuild subsystem (`rebuild_engine` 35%, router 27%)** — async orchestration, lock
   acquire/release, partial-failure recovery, idempotent re-runs.
6. **`chat_relay`/`chat_share` (68/76%)** — the encrypted relay envelope and 1:1 bind
   locking; security-sensitive and currently shallow.
7. **`architect.py` (461 uncovered stmts)** — biggest absolute gap. Add tool-dispatch
   tests per tool with a mocked LLM, plus staging/undo and truncation-recovery branches.
8. **`trips.py` (26%)** — trip segmentation/derivation logic.

**P2 — Establish a frontend test baseline** (currently ~8%)
9. Start with the **highest-traffic, highest-logic** components, not the easy ones:
   `Chat.tsx` (mode gating + send flow), `NotePage` (edit/save/concurrency),
   `SharesPage`/`EncryptedChat` (crypto + bind). React Testing Library + mocked `api`.
10. Add tests for **state/gating logic** extracted from components where practical (pure
    functions are far cheaper to test than full renders).

**P3 — Introduce a real system/E2E layer** (the literal ask: "system testing covers the code base")
11. Stand up **Playwright** against a real running stack (FastAPI + built PWA, LLM mocked
    at the boundary). Cover the canonical flows end-to-end:
    - Capture a note (Entry mode) → it appears in Wiki with correct filing.
    - Full Brain → propose → **Apply** → note written + versioned.
    - Research mode → read-only Q&A returns results, mutates nothing.
    - Create a share link → open in a second context → encrypted chat round-trip → saved note.
    - Run a workflow ("Run now") → review card posted.
12. Add **workflow integration tests** that fire a trigger and assert the action's DB
    effects (esp. cron TZ + watermark idempotency).

**P4 — Android**
13. At minimum, unit-test `NoteQueue` persistence, `UploadWorker` retry/dedup (slug cache),
    and `LocationService` polling-state transitions.

---

## 4. The reusable workflow (how this report was produced)

This audit *is* the workflow you asked for; here it is as a repeatable recipe so it can be
re-run later (e.g. promoted to a `/test-coverage-audit` slash command):

```
Phase 0  Orchestrator: create docs/coverage-audit/<date>/, install pytest-cov + vitest v8.
Phase 1  Fan-out (parallel) — FEATURE & INTENT mappers, one per surface:
           • backend API (routers)        → features-backend-api.md
           • backend services (domain)     → features-backend-services.md
           • frontend + workflows + android→ features-frontend-automations.md
Phase 2  Fan-out (parallel) — TEST INVENTORY mapper  → test-inventory.md
         + QUANTITATIVE coverage runs (pytest-cov, vitest --coverage) → JSON.
Phase 3  Fan-in (synthesis) — join features × tests × measured coverage → THIS report:
           coverage matrix, prioritized gaps, recommended tests.
```

Re-running it after each P-tier is closed gives a moving coverage number and shrinking gap
list. Note one environment caveat reproduced here: `pywebpush`/`http-ece` won't build in the
remote container, so the push-encryption signing path can't be exercised and `fastapi`/
`pydantic` must be installed before backend coverage runs (the session-start hook's bulk
install fails on the same `http-ece` wheel).

---

## 5. One-line answer

Backend is broadly but unevenly covered (**79%**, with a dangerous tail of medical/security/
async modules at 26–54%); the frontend is effectively untested (**8%**); and there is **no
system/E2E layer and no test CI at all**. So no — system testing does not currently cover
the code base. The P0/P3 items (test CI + a Playwright flow layer) are what turn the existing
unit tests into actual system coverage.
