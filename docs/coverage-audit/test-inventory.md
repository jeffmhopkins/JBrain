# Test Inventory & Coverage Audit — JBrain Project

**Audit Date:** 2026-06-08  
**Scope:** ALL automated tests (backend pytest + frontend vitest), NO untested code analysis.

---

## Backend Tests: pytest (server/tests/)

### Inventory: 36 Test Files, 714 Total Tests

| Test File | Tests | Type | Modules/Routers Covered | Mocks & Fixtures |
|-----------|:-----:|------|--------------------------|------------------|
| **test_api.py** | 351 | API-integration (TestClient) | auth, notes, workflows, labs, KB write, wiki-links, staging, corrections, chat-share, attachments, search, entity decisions, reference candidates, person dedup | Embeddings ⊘, LLM ⊘, Anthropic SDK ⊘, temp-DB, access-key auth |
| **test_calendar.py** | 81 | Migration + API | calendar service, event extraction, date classification, rrule expansion, lab series analytes, date projection | Embeddings ⊘, LLM ⊘, temp-DB |
| **test_alias_linking.py** | 35 | API-integration (TestClient) | entity identity, KB linking, alias surfaces, entity-aware search, write prompts, merge/split decisions | Embeddings ⊘, LLM ⊘, temp-DB |
| **test_entity_identity.py** | 31 | API-integration (TestClient) | entity_index, entity_rebuild, entity_decisions, durable identity persistence, alias canonicalization | Embeddings ⊘, Anthropic SDK ⊘, temp-DB, monkeypatch |
| **test_span_search.py** | 24 | API-integration (TestClient) | entity index span detection, alias-aware search (entity_expand), cross-document entity references | Embeddings ⊘, temp-DB |
| **test_reference_loop.py** | 23 | DB-direct (SQLite in-memory) | external-reference capture, promotion lifecycle, source-mention linking, reference-type classification, host-pinning (NLM) | None (deterministic, no LLM) |
| **test_auto_continue.py** | 22 | API-integration (TestClient) | article_talk (LIVE rebuild), truncation recovery, auto-continue on token-max, streaming response handling | Embeddings ⊘, LLM ⊘, temp-DB |
| **test_note_flags.py** | 21 | API-integration (TestClient) | per-note governance (kb_ingest, tool_access), flag persistence, KB synthesis scoping, assistant tool visibility | Embeddings ⊘, LLM ⊘, temp-DB |
| **test_architect_truth.py** | 20 | DB-direct (SQLite in-memory) | architect service, stale-answer guard, fabricated [[link]] sanitization, freshness invariants for chat replies | Anthropic SDK ⊘ (response mocked) |
| **test_health_phase1.py** | 13 | API-integration (TestClient) | embeddings readiness state machine, search fallback (keyword when embeddings unavailable), audio-model state transitions | Embeddings ⊘, Anthropic SDK ⊘, thread-safety |
| **test_health_phase2.py** | 16 | API-integration (TestClient) | capabilities assembler, /api/system/status (public skeleton + full doc), soft-auth, provider declarations, credential checks | Embeddings ⊘, LLM ⊘, Anthropic SDK ⊘ |
| **test_person_dedup.py** | 12 | API-integration (TestClient) | /api/entities dedup proposals, approve/reject/undo cycle, candidate gating, durable decisions | Embeddings ⊘, LLM ⊘ (model stub), Anthropic SDK ⊘ |
| **test_lab_parse.py** | 14 | Pure-unit | lab_parse service, PDF word-box geometry, value-to-column mapping, analyte merging, wrapped/missing cells | None (deterministic, synthetic data) |
| **test_research_labs_ai.py** | 11 | DB-direct (SQLite) | research_labs_ai assistant, tool-call scoping (hardcoded tools only), response validation, recipient isolation, field censorship | Embeddings ⊘, LLM ⊘ (request validation) |
| **test_redirects.py** | 14 | API-integration (TestClient) | wiki_build redirect creation, merged-article conversion, old URL resolution, search declutter, entity-index hiding | Embeddings ⊘, Anthropic SDK ⊘, temp-DB |
| **test_wikilinks.py** | 8 | Pure-unit | wikilinks service, [[link]] parsing, alias display, wiki-label normalization, path-form expansion | None (string parsing) |
| **test_chat_share.py** | 10 | DB-direct (SQLite in-memory) | share_link chat, channel encryption, key wrapping (blind relay), backlog persistence, ephemeral cleanup | None (cryptographic, no LLM) |
| **test_nickname_lexicon.py** | 8 | Pure-unit | nickname_lexicon service, canonical token mapping (bidirectional), diminutive-to-formal grouping, ambiguous-form exclusion | None (static data) |
| **test_lab_share_scope.py** | 5 | DB-direct (SQLite in-memory) | lab_share_scope security boundary, analyte allow-list (default-deny), brain-identity field censorship (note id/slug/title/encounter) | None (security logic) |
| **test_rebuild_refs_links.py** | 8 | API-integration (TestClient) | wiki_build article rebuild, citation-title repair, in-memory add-link backstop, source-mention seeding, References lint | Embeddings ⊘, temp-DB |
| **test_search_alias_expand.py** | 6 | API-integration (TestClient) | search.hybrid_notes entity expansion, alias-to-canonical query rewrite, owner-context channels | Embeddings ⊘, temp-DB |
| **test_corrections.py** | 10 | API-integration (TestClient) | source-of-truth corrections, talk-item promotion to entry note, article-talk linking | Embeddings ⊘, Anthropic SDK ⊘, temp-DB |
| **test_batch_truncation.py** | 6 | API-integration (TestClient) | batch-writer truncation guard, token-cap recovery, trailing section preservation | Embeddings ⊘, LLM ⊘, Anthropic SDK ⊘, temp-DB |
| **test_async_entity_rebuild.py** | 8 | API-integration (TestClient) | /api/entities merge/split/alias endpoints, durable decision recording, coalesced background worker | Embeddings ⊘, Anthropic SDK ⊘, temp-DB, threading |
| **test_entity_reconcile_api.py** | 7 | API-integration (TestClient) | /api/entities merge/split/aliases HTTP routes, identity decisioning, auth/access control | Embeddings ⊘, Anthropic SDK ⊘, temp-DB |
| **test_staged_verify.py** | 6 | DB-direct (SQLite in-memory) | staged_verify service, [[link]] hygiene, dead-link neutralization, user warnings | None (deterministic) |
| **test_lab_vision.py** | 5 | Unit with mocks | lab_vision service, OCR corpus validation, JSON schema coercion, OCR-faithfulness plumbing | Model/OCR stubbed |
| **test_freeze_regression.py** | 4 | Regression | audio/image worker thread safety, embedding compute (no SQLite lock held), failed-embed recovery with terminal summary | Embeddings ⊘, threading |
| **test_foldback_coalesce.py** | 5 | Regression | multi-attachment note-analysis fold-back, coalesced LLM re-analysis, attachment-context hash guard | LLM ⊘, threading |
| **test_stale_pending.py** | 9 | Regression | stuck analysis_status='pending' recovery, watchdog reap protection, worker/reaper race condition | Embeddings ⊘, LLM ⊘, threading, SQLite transactions |
| **test_owner_alias_backfill.py** | 4 | API-integration (TestClient) | entity_decisions auto-heal on rebuild, owner nickname self-healing, declared-alias seeding | Embeddings ⊘, temp-DB |
| **test_pipeline_lock_release.py** | 2 | Regression | scheduled pipeline (wiki_update/wiki_maintain), LLM call off write-lock, responsive assistant messaging | None (lock discipline) |
| **test_maintain_alias_offer.py** | 2 | API-integration (TestClient) | wiki_maintain prompt substitution, {known_aliases} placeholder, maintain_one assembly | Embeddings ⊘, LLM ⊘, temp-DB |
| **test_message_steps_migration.py** | 2 | Migration | v44→v45 schema upgrade, message_steps table creation, migration idempotency | None |
| **test_source_note_id_migration.py** | 3 | Migration | v46→v47 partial-index fix, article_talk.source_note_id column addition, boot crash prevention | None |
| **test_geo.py** | 2 | Unit | geo service | None |

**Fixture Setup (all API-integration tests):**
- TestClient from fastapi.testclient with access-key Bearer auth
- Temporary SQLite DB (in /tmp)
- Embeddings service monkeypatched: `upsert_note_embedding`, `delete_note_embedding`, `semantic_search`, `embed_attachment_chunks`, `write_attachment_embeddings`, `embed_many` all stubbed
- LLM client cache cleared per test (conftest.py)
- Public-share rate limiter reset per test
- DB initialized fresh, migrations run

---

## Frontend Tests: vitest (web/src/)

### Inventory: 10 Test Files, 72 Total Tests

| Test File | Tests | Type | Units/Components Covered | Mocks |
|-----------|:-----:|------|--------------------------|-------|
| **api.getStatus.test.ts** | 5 | Unit (API client) | getStatus() fetch wrapper, HTTP status mapping (200 ok, 5xx unreachable, network errors), timeout/abort handling, bearer-key auth header | fetch() stubbed |
| **health.test.ts** | 13 | Unit (state machine) | health store reconciliation (ingest/poll/apply), needs-auth detection (skeleton + stored key), reachability axes (browser-offline, server-unreachable), LLM degradation overlay (observed-fail downgrades, self-healing), poll window expiry | none (state logic) |
| **healthPoll.test.ts** | 6 | Unit (poller lifecycle) | health polling state machine, /share/:token route carve-out (polling disabled), on/offline transitions, long-poll abort coordination | Timer mocks (vi.useFakeTimers) |
| **observedFeed.test.ts** | 10 | Unit (health integration) | api() observed-feed (neterr/lastOkAt/last5xxAt stamps), streamChat/rebuildStream feed outcomes, stall-vs-user-abort distinction, capability degradation | fetch() stubbed |
| **statusDerive.test.ts** | 10 | Unit (capability derivation) | final reachability state derivation, brain-offline vs. server states, LLM readiness gate, embeddings warm/absent transitions, observed-error overlays | none (pure logic) |
| **SearchPageGating.test.tsx** | 2 | Integration (React component) | SearchPage semantic-mode gate, fallback to hybrid when embeddings warming, semantic-button disabled state, modal behavior | fetch() stubbed, Router/React Testing Library |
| **StatusDot.test.tsx** | 5 | Integration (React component) | status indicator rendering (brain-offline, server-unreachable, ok, needs-auth), colour + panel rows, click-to-menu behavior | React Testing Library |
| **Toaster.test.tsx** | 3 | Integration (React component) | toast notification rendering, show/dismiss lifecycle | React Testing Library |
| **capabilities.test.ts** | 7 | Unit (capability API) | useCapability hook, capability-key constants (CAP_*), state-derived checks (llm.state, embeddings.state, etc.) | none |
| **toast.test.ts** | 11 | Unit (toast queue) | showToast/dismissToast lifecycle, queue dedup, auto-dismiss on timer, toast ID uniqueness | Timer mocks (vi.useFakeTimers) |

**Test Setup (all vitest):**
- vi.stubGlobal("fetch", ...) for network calls
- vi.useFakeTimers() for timer-based assertions
- React Testing Library (render, screen, fireEvent) for component tests
- MemoryRouter for routing context
- No actual server communication, all mocked

---

## System / E2E Tests

**STATUS: NONE DETECTED**

- No Playwright, Cypress, or Selenium test files found
- No `.spec.ts`, `e2e/`, or browser-automation directory
- Public /share/:token route (chat-share UI) is untested in this suite (though the server-side crypto & DB layer is covered)

---

## CI Pipeline

**Workflow Files:**
- `.github/workflows/android-apk.yml`: Builds Android/Wear OS tracker APK (no tests, only build)
- `.github/workflows/pages.yml`: Builds & deploys PWA to GitHub Pages (no tests, only npm run build)

**STATUS: NO TEST EXECUTION IN CI**

The repo has no GitHub Actions workflows that invoke:
- `pytest` (server tests)
- `npm run test` or `vitest` (frontend tests)
- Lint/format checks

Tests are runnable locally (`pytest`, `npm run test` in web/) but CI does not orchestrate them.

---

## Test Type Breakdown

| Category | Count | Examples |
|----------|:-----:|----------|
| **API-integration (TestClient)** | ~450 | test_api, test_entity_identity, test_health_phase*, test_redirects, test_corrections, test_person_dedup |
| **DB-direct (SQLite in-memory)** | ~60 | test_architect_truth, test_chat_share, test_reference_loop, test_research_labs_ai, test_lab_share_scope, test_staged_verify |
| **Pure-unit** | ~30 | test_wikilinks, test_nickname_lexicon, test_lab_parse |
| **Regression** | ~20 | test_freeze_regression, test_foldback_coalesce, test_stale_pending, test_pipeline_lock_release |
| **Migration** | ~8 | test_calendar, test_message_steps_migration, test_source_note_id_migration |
| **Frontend unit/integration (vitest)** | 72 | health, api, component renders |
| **TOTAL** | **786** | |

---

## Production Coverage Summary

### Well-Tested Areas (>10 tests each)
- **notes/entry** (write, routing, source, versioning) — 351 tests in test_api alone
- **entity_index/merge/split/alias** — 35+ tests across entity_identity, entity_reconcile_api, person_dedup
- **calendar/date extraction** — 81 tests
- **KB linking & wiki-links** — 35+ tests (alias_linking, wikilinks, search_alias_expand)
- **external references** — 23 tests (reference_loop)
- **health/readiness** — 29 tests (health_phase1, health_phase2, healthPoll)
- **corrections/talk-item promotion** — 10 tests
- **lab-share security** — 5 tests (direct DB validation)
- **api.getStatus & health state machine** — 18 tests

### Moderate Coverage (5–10 tests)
- **audio/image worker thread safety** (freeze_regression, foldback_coalesce, stale_pending)
- **chat-share encryption** (chat_share, encrypted channel relay)
- **lab parsing** (lab_parse, lab_vision)
- **redirects & article declutter** (redirects)
- **staged verification** (staged_verify, batch_truncation)
- **auto-continue on truncation** (auto_continue)
- **frontend search gating** (SearchPageGating component)

### Minimal/No Coverage
- **Workflows (wiki_update, wiki_maintain, batch services)** — largely untested beyond lock-release regression
- **End-to-end system tests** — none (no E2E/Playwright)
- **Full-stack browser-based scenarios** — none
- **CI integration** — none (tests don't run in workflows)
- **Geo service** — 2 trivial tests
- **Image/audio transcription ingestion** (beyond worker threading) — mostly stubbed
- **Android tracker/Wear OS app** — no test coverage

---

## Key Testing Patterns Observed

1. **Mock Discipline:**
   - Embeddings service universally mocked (no fastembed download)
   - LLM client cache cleared per test to prevent cross-test SDK leakage
   - Anthropic SDK NOT directly mocked; behavior tested via response shape validation
   - No real network calls in any test

2. **Auth Model:**
   - Access-key bearer token in TestClient headers
   - /api/auth/info (public info before auth check)
   - /api/auth/verify (authed full info + version)

3. **Database:**
   - Temp SQLite DB per test fixture (fresh schema, migrations run)
   - Direct SQLite in-memory for isolation tests (no ORM dependency)
   - Transaction/lock assertions via raw SQL

4. **Async/Threading:**
   - Worker thread tests use `time.sleep()` + polling (freeze_regression, foldback_coalesce, stale_pending)
   - No asyncio/pytest-asyncio; only threading module

5. **Frontend:**
   - Vitest + React Testing Library (no e2e/Playwright)
   - State-machine unit tests + component integration tests
   - Mocked fetch, fake timers, router mocks

---

## Recommendations

1. **Add CI test execution** — GitHub Actions should run `pytest` and `npm run test` on push/PR
2. **Consider E2E tests** — Playwright for key user journeys (login, note entry, search, chat-share recipient flow)
3. **Expand worker/threading coverage** — More deterministic async tests (beyond stale_pending/freeze_regression)
4. **Test migrations more thoroughly** — Only 3 migration test files; audit schema_version upgrades end-to-end
5. **Add geo service tests** — Currently 2 trivial tests; geo-trail, geotrail, location features undertested
6. **Document test run instructions** — Add to CONTRIBUTING.md or README

