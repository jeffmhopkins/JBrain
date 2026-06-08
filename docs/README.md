# JBrain docs

A small map of what lives here. Most JBrain documentation is the code itself plus
the top-level [`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md); this
folder holds testing material and a design-history archive.

## Active

- **[`testing-plan/`](./testing-plan/)** — the testing framework of record. `PLAN.md`
  is referenced by CI (`.github/workflows/test.yml`) and the test helpers; the
  `*-design.md` files capture the harness/backend/frontend rationale.
  (`ROADMAP.md` here is partly superseded — see its banner.)
- **[`coverage-audit/`](./coverage-audit/)** — the multi-agent coverage audit. A
  **point-in-time snapshot** (2026-06-08); kept as the baseline the floors were set
  against, not current numbers (see the banner in `COVERAGE_REPORT.md`).
- **[`lab-intake-plan.md`](./lab-intake-plan.md)** — the Phase-1 lab image/PDF intake
  spec; still referenced from `server/app/services/lab_vision.py`.
- **[`ROADMAP.md`](./ROADMAP.md)** — future-work notes; items self-mark as
  *IMPLEMENTED* when they ship.

## Archive

[`archive/`](./archive/) holds historical design/planning docs for features that
have since **shipped**. They're preserved for context on *why* things are built the
way they are; they are **not** kept in sync with the code and may reference paths or
plans that have since moved. See [`archive/README.md`](./archive/README.md).
