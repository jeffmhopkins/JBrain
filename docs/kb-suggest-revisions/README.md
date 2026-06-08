# Suggest revisions — design dossier

A live, conversational, targeted-edit mode for KB articles (sibling to "Rebuild page now"),
plus folded-in hardening that fixes the existing rebuild's date-format and people-linking
bugs and brings it to formatting/promotion parity with universal synthesis.

Produced by a multi-agent funnel: **5 research → 5 divergent plans → 2 red-teams → 3 hybrids → final red-team**.

## Start here
- **`RECOMMENDATION.md`** — the decision-ready summary: recommended approach (H3, amended), the phased plan, and the open decisions.

## Research (phase 1)
- `research/01-rebuild-engine.md` — engine backbone, run lifecycle, SSE protocol, extension points.
- `research/02-synthesis-vs-rewrite.md` — the four writer paths and their formatting/promotion divergences.
- `research/03-formatting-dates.md` — `@t[…]` date-token system + root cause of the wrong-date bug.
- `research/04-people-linking.md` — people-link pipeline + root cause of under-linking on rebuild.
- `research/05-frontend-tests.md` — RebuildPanel UX, SSE client, conversational precedents, test recipes.

## Plans (phase 2 — deliberately divergent stances)
- `plans/A-minimal-extension.md` — full-article guided edit, least new infra.
- `plans/B-structured-patch.md` — server-applied structured edit-ops.
- `plans/C-tool-agent.md` — tool-using, truth-seeking edit agent.
- `plans/D-shared-core.md` — shared writer-core refactor first, thin loop on top.
- `plans/E-section-scoped.md` — edit one heading-delimited section per turn.

## Red-teams + hybrids (phases 3–5)
- `redteam/RT1-technical.md` — correctness/safety/architecture critique (ranking A<D<B<E<C).
- `redteam/RT2-product.md` — intent-fit/scope/shipping critique (ranking C>A>E>B>D).
- `hybrid/H1-safe-core.md` · `hybrid/H2-firewalled-truthseeking.md` · `hybrid/H3-phased-convergence.md`
- `redteam/RT3-final-hybrids.md` — the bake-off; recommends **H3, amended**.

## The through-line
Every plan independently converged on the same folded-in hardening (`enforce_date_tokens`,
session-start entity rebind, `promote_one` on Accept) and on keeping the resumable transcript
tool-free. The real fork was the **edit mechanism** (full re-emit vs structured ops vs section
splice vs tool-agent), and the decisive constraint was the **missing inbound PII firewall** —
which is why the recommendation gates truth-seeking on a durable per-note sensitivity flag.
