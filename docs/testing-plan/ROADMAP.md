# JBrain Testing — End-to-End Roadmap (now → full coverage)

> **⚠️ Partly superseded — kept for the plan of record.** Stages **1–3** (the
> `./jt` system), **2** (CI gating every PR), **6** (Playwright system/e2e), and
> **7** (the `flows` tier + Android JUnit baseline) have **shipped**. The "Stage 0"
> snapshot below (no CI, no e2e, framework not built) no longer reflects reality.
> What remains live is the ongoing coverage-filling work (Stages **4–5**, **8**)
> and the **ratchet-only floors** principle. See `CLAUDE.md` for the current
> Definition of Done.

Merges the two deliverables already in this repo:
- **`docs/coverage-audit/COVERAGE_REPORT.md`** — *where the gaps are* (backend 79% but uneven;
  frontend 8%; no system/e2e; no CI).
- **`docs/testing-plan/PLAN.md`** — *the common framework* to build the tests on (`./jt`,
  shared `unit`/`integration` vocabulary, per-domain coverage gates).

This roadmap sequences them into one path. Two halves: **build the system** (Stages 1–3,
~days) then **fill the coverage** (Stages 4–7, ~weeks, the real work).

---

## Current state (Stage 0 — done)
| | Now |
|---|---|
| Backend | 79% lines, 808 tests — but `rebuild_engine` 35%, `trips` 26%, `llm` 43%, `lab_vision` 52%, `chat_relay` 68%, `architect` 461 stmts uncovered |
| Frontend | **8%** lines — every feature page/component at 0% |
| System/E2E | none |
| CI | none (tests never run on push/PR) |
| Framework | designed & red-teamed, not yet built |

---

## The path

| Stage | What | Effort | Gate / outcome |
|---|---|---|---|
| **1. Build the common system** | Commit `./jt`; `server/pyproject.toml` (markers + `fail_under=79`); flat vitest coverage config + commit `@vitest/coverage-v8`; dev deps (`pytest-cov`,`pytest-timeout`). **Zero test edits.** | ~1 day | `./jt` runs both suites; 808+72 still green |
| **2. Wire CI** | `.github/workflows/test.yml` — 2 jobs (back, front), per-domain floors at today's baselines | ~0.5 day | **Every PR runs tests.** Highest-leverage step — stops silent regressions |
| **3. Adopt the taxonomy** | Backend: one `pytestmark` line/file (`unit`/`integration`, tag threaded tests `concurrency`). Frontend: `renderWithProviders` + shared test-utils; relocate 3 root tests | ~1 day | `./jt unit` means one thing across domains |
| **4. Fill backend high-risk gaps** *(audit P1)* | Write tests for `lab_vision`, `llm` provider layer, `rebuild_engine`/`rebuild_runs`, `chat_relay`/`chat_share`, `architect` tool paths, `trips`. Ratchet `fail_under` 79→~85 | ~1–2 wks | Medical/security/async holes closed |
| **5. Build frontend coverage** *(audit P2)* | First 5 components (`Chat`, `SharesPage`+`EncryptedChat`, `NotePage`, `RebuildPanel`, `CalendarPage`); **introduce MSW** here; then sweep remaining pages. Ratchet floor 7→~50 | ~2–3 wks | 8% → ~50%+; real user flows unit/integration-tested |
| **6. System/E2E layer** *(audit P3)* | Stand up **Playwright** in `e2e/`; register a **fake LLM provider**; cover canonical journeys: capture→Wiki, Full-Brain→Apply, Research read-only, share→encrypted-chat round-trip, run-workflow→review card. Add `e2e` CI job | ~1 wk initial, ongoing | **This is what makes "system testing covers the code base" true** |
| **7. Workflows + Android** *(audit P4)* | `flows` pytest tier (validate every `workflows/*.yaml`/`actions/*.yaml` + a few integration runs); Android JUnit for `NoteQueue`/`UploadWorker`/`LocationService` | ~3–4 days | Automation + native baseline |
| **8. Steady state** | Backend fixture convergence (net code deletion); ratchet floors to targets; grow e2e journeys with each feature | ongoing | Coverage only goes up (CI ratchet) |

---

## Coverage trajectory (the target)
```
            backend    frontend   e2e flows   CI
now          79% (uneven)  8%       0          none
after S1-3   79%           8%       0          ✅ gating at baseline
after S4     ~85%          8%       0          ratcheted
after S5     ~85%          ~50%     0          ratcheted
after S6     ~85%          ~50%     core journeys ✅
target       85%+          60%+     all canonical flows + critical workflows
```
Floors only ever **ratchet up** — once a number is reached CI won't let it regress.

---

## Critical path & dependencies
- **Stages 1→2→3 are strictly ordered** and unlock everything; do them first, back-to-back.
- **Stage 2 (CI) is the single highest-value step** — without it, every test written later can
  silently rot. Don't defer it.
- **Stages 4 and 5 run in parallel** (different people/areas) once Stage 3 lands.
- **Stage 6 (e2e) depends only on Stage 1–2**, not on 4/5 — it can start early and is the
  literal answer to the original question ("does system testing cover the code base?").
- MSW is pulled in exactly when Stage 5 needs it (not before — verified it needs an undici
  smoke-test). `just`/xdist/coverage-aggregation stay deferred unless a trigger fires (PLAN §6).

## Definition of done
1. `./jt` is the one command; CI gates every PR per-domain. 2. Backend ≥85% with no
high-risk module under ~70%. 3. Frontend ≥60% with the major pages covered. 4. A Playwright
`system` suite exercises every canonical user journey against the real stack. 5. Critical
workflows validated by the `flows` tier. 6. Coverage floors ratcheted to those numbers so
they can't regress.

## Rough total
**~1 week** to a CI-gated common system on today's coverage (Stages 1–3), then **~5–7 weeks**
of test-writing to reach the targets (Stages 4–7), parallelizable to ~3–4 calendar weeks with
two people. Stage 1–3 deliver disproportionate value (regressions caught) for the smallest cost.
