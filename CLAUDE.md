# CLAUDE.md — working agreement for this repo

Guidance for anyone (human or AI) changing JBrain. The **Testing — Definition of
Done** below is an honor-system policy, not a hard gate: CI reports results but does
not block merges. Follow it anyway — it's why the suite stays trustworthy.

## Project map
- `server/` — FastAPI + SQLite backend (routers, services). Tests in `server/tests/`.
- `web/` — React + Vite PWA. Colocated `*.test.tsx` next to source.
- `e2e/` — Playwright system tests (real PWA + API, LLM faked at the boundary).
- `android/` — Kotlin phone/watch capture client (JVM unit tests via Robolectric).
- `workflows/`, `actions/` — declarative automations (validated by the `flows` tier).
- Testing design & coverage history: `docs/testing-plan/` and `docs/coverage-audit/`.

## One command for tests — `./jt`
```
./jt            # the gate: backend (minus concurrency) + frontend
./jt back [..]  # pytest (passes args, e.g. ./jt back -k notes)
./jt front      # vitest run
./jt unit       # the fast `unit` tier across both domains
./jt cov        # both domains with coverage + per-domain floors
./jt e2e        # build the PWA + run Playwright (LLM faked)
```
Native commands still work: `cd server && pytest`, `cd web && npm test`.

## Test taxonomy (one vocabulary)
- **unit** — isolated, no DB/network/server. Backend marker `@pytest.mark.unit`;
  frontend pure-logic `*.test.ts`.
- **integration** — real intra-domain wiring, externals mocked (LLM via the `llm`
  module seam, embeddings stubbed; frontend via MSW). The default tier.
- **concurrency** *(backend only)* — real threads / WAL contention; runs serially.
- **flows** — validates every `workflows/*.yaml` + `actions/*.yaml`.
- **system / e2e** — Playwright in `e2e/`, real stack, LLM faked.

## Testing — Definition of Done (apply to EVERY change)
A change is not "done" until:
1. **Tests exist for the change.** New feature → new tests in the right tier. Bug
   fix → a test that fails before the fix and passes after. Put the test where its
   peers live (see the taxonomy); follow the patterns in neighbouring tests
   (canonical fixtures/seams server-side; `renderWithProviders` + MSW client-side).
2. **`./jt` is green** for the domain(s) you touched (run `./jt e2e` too if you
   changed a user-facing flow or the API contract behind one).
3. **Coverage does not regress.** The floors are `fail_under` in
   `server/pyproject.toml` and `thresholds` in `web/vitest.config.ts`. Never lower a
   floor to make CI pass. When your work pushes real coverage comfortably above the
   floor, **ratchet the floor up** in the same PR.
4. **Production code is the only thing changed for behaviour** — don't weaken a test
   to make it pass; fix the code or the test's expectation honestly.
5. **No real network/LLM/secrets in tests.** Mock at the module seam; the LLM is
   faked at the boundary in e2e (`e2e/fake_llm.py`), never a real key.

## CI (informational, per-domain)
`.github/workflows/test.yml` runs four jobs on every PR — `back`, `front`, `e2e`,
`android` — each enforcing its own coverage floor and reporting pass/fail. Read the
results before merging; a red check means the Definition of Done isn't met yet.

## Conventions
- Don't commit generated artifacts (coverage/, server/static/, build/, node_modules/)
  — they're gitignored.
- Backend LLM calls go through the `app.services.llm` seam; tests mock there, never
  the SDK. Embeddings are always stubbed in tests.
- Keep tests deterministic: frozen/pinned time, no arbitrary sleeps, seeded randomness.
