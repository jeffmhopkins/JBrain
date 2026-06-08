# Suggest revisions — final recommendation

Synthesis of a 5-research → 5-plan → red-team → 3-hybrid → red-team funnel.
Full artifacts live beside this file (see `README.md`). This doc is the decision-ready summary.

## Implementation status

**Owner decisions (locked):** v1 = full autonomous truth-seeking agent (H2); note privacy =
title-prefix inference (no durable flag yet — see the residual-risk note below); UX = unify
Suggest-revisions with rebuild's Guide from the start; proceed with Phase 1 now.

**Phase 1 — shared hardening on the existing rebuild — DONE** (backend gate green, 1038 passed):
- *Date tokens*: `clock.tokens_in/malformed_tokens/dropped_tokens`; malformed `@t[...]` surfaced
  via `validate_structure` (live rebuild + batch build + maintain); DATES directive added to
  `wiki_revise`. (commit `1eb5625`)
- *People linking*: `entity_index.rebuild(sync_embeddings=False)` run once at rebuild-session
  start (offloaded, guarded) so new/renamed People pages link at draft time. (commit `06b8935`)
- *Promotion parity*: `wiki_build.promote()` (link_owner + surface_aliases + normalize link
  labels + flag_ungrounded_reference) wired into `finalize_rebuild`; the two network-bound steps
  (link_medications, link_places) deliberately excluded from Accept. (commit `5f3d587`)

**⚠ Residual risk to revisit at Phase 2.5:** the locked combination *full autonomous agent +
title-prefix privacy* is the leak vector RT3 flagged — an agent reading raw notes mid-conversation,
with privacy judged only by `kb/Health/`·`kb/Finance/` title prefixes, can pull a mis-filed/free-text
private fact into a public/shareable article. Phase-2.5 mitigations (no durable flag required):
mandatory user-approval gate on every inbound fact, default-deny when the target article is
public/Reference, and the firewall built as one swappable seam so a durable per-note sensitivity
flag can drop in later. To confirm with the owner before the truth-seeking layer ships.

**Phase 2 — the unified conversational targeted-edit loop — DONE** (backend 1041 + frontend 810,
typecheck clean):
- *Backend* (commit `5c72b65`): `run_suggest` seeds the current article + curated sources +
  read-only backlinks into one user turn and streams the revised article through the existing
  `_generate` tail (inheriting all Phase-1 hardening); follow-ups reuse `run_guide`.
  `_load_backlinks` carries the **inbound PII firewall** (no private-domain prose into a public
  article). New `wiki_suggest` prompt + `build_suggest_prompt`; `RebuildRun.kind`/`base_content`;
  router `start?mode=suggest` + `POST /{run_id}/suggest`.
- *Frontend* (commit `4ca75d2`): the note menu's "Rebuild page now" → **"Edit with AI"**, opening
  the unified panel in suggest mode (gather → curate → talk→edit→talk loop). First message →
  `/suggest`, follow-ups → `/guide`; sources optional; rebuild-from-scratch still reachable;
  diff-against-original by default.

**Next — Phase 2.5 (gated on the owner safety check):** the autonomous truth-seeking tool agent.
This is the layer carrying the locked-but-flagged risk (full agent + title-prefix privacy). To
build behind a per-fact approval gate + default-deny on public/Reference targets + the swappable
firewall seam — confirm mitigations before shipping.

## The feature (locked with the owner)
A live, **conversational "Suggest revisions"** mode for KB articles, parallel to "Rebuild page now":
- **BASE = the current article, preserved** (not re-drafted from scratch).
- **Context** = curated source notes (reuse rebuild's gather/curate) **+ backlinks (read-only)**.
- **Loop**: user talks → AI makes **targeted edits** to the working draft → shows the evolving draft → user talks more → … → **Accept**. The AI is **truth-seeking for salient facts**; the user steers structure/formatting and corrects AI assumptions.
- **Folded-in hardening** (benefits the *existing* rebuild too): deterministic date-token (`@t[…]`) enforcement, people-link enforcement, and formatting/promotion parity with universal synthesis.

## What the research settled (high confidence, cited in `research/`)
- **Date-token bug root cause** (`03`): token *production* is non-deterministic — prose-only instruction plus a single advisory warning, and the live engine has **no revise loop**, so a stale literal or malformed `@t[…]` ships unchecked.
- **People-link bug root cause** (`04`, H1): the rebuild session **never calls `entity_index.rebuild`**, so newly created/renamed People pages and freshly-seeded nicknames aren't auto-linkable at draft time.
- **Promotion parity gap** (`02`): the full build runs `link_owner` / `surface_aliases` / `link_medications` / `link_places` / `normalize_link_labels` / `flag_ungrounded_reference`; **live Accept runs none of them**.
- **Engine constraints** (`01`, re-verified by red-team): `_generate` wipes `run.draft=""` and re-streams a full body (`rebuild_engine.py:295`); `run_redraft` pops the last assistant turn (`:491`) — a seed bug for any pre-seeded BASE; the resumable transcript must stay **tool-free** for cross-provider safety.
- **The critical gap** (RT1/RT3): **there is no inbound PII firewall.** `hybrid_notes` has no privacy filter (`search.py:36`), and every privacy predicate is **title-prefix-only** (`wiki_guides.py:124-161`) — **raw notes carry no privacy column** (`db.py:914-926`). Any path that pulls note prose/facts into a public/Reference article is gambling on a heuristic.

## Recommendation: **H3 (phased convergence), amended**
Ship the value the owner asked for **early and on the safest substrate**, and treat the privacy gap as a first-class prerequisite for truth-seeking rather than a heuristic to paper over.

### Phase 1 — Shared hardening, wired into the *existing* rebuild (ships the owner's bug fixes first)
- New shared core (scoped to **two call sites** — `_generate` tail + `finalize_rebuild` — *not* D's four-path refactor, to cap regression surface):
  - `enforce_date_tokens` — malformed-`@t[…]` linter (hard) + BASE token-preservation guard + round-trip-gated adjacency rewrite (research `03` options A+D, B-with-guard).
  - cheap, **embeddings-free session-start entity rebind** (research `04` H1) so People pages link at draft time.
  - `promote_one` on Accept — bundles the build's promotion steps, made idempotent (research `02`).
- Order matters: **enforce dates before `add_links_to_content`** (`_mask_spans` does not mask `@t[…]`).
- Guarded by **characterization tests** that pin existing rebuild output before/after the refactor.
- **Outcome:** the owner's date + people-linking + promotion complaints are fixed for classic rebuild **before the new feature even exists.**

### Phase 2 — The conversational loop (safe mechanism)
- BASE-preserved, **full-article re-emit** on a **sibling carry-forward generator** (do *not* reuse `_generate`'s wipe). Adopt **H2's redraft fix**: plant *no* fake assistant seed turn, so the `run_redraft` pop bug never applies. Port `_generate`'s truncation/auto-continue handling into the sibling (RT3's noted gap in H2).
- **Diff-first clean-diff UI** against BASE, so full re-emit *reads* as a targeted edit (T1 — "showed me the draft").
- Transcript stays tool-free plain text; reuse `_sse`, Accept/lock/staleness untouched.
- Backlinks load via the existing inbound-link SQL (`architect.py:818`) and are kept **out of `run.sources`** (so `_repair_citation_titles` grounding never shifts).

### Phase 2.5 / 3 — Truth-seeking, **gated on a real privacy signal** (the amendment)
- **Prerequisite:** add a **durable per-note sensitivity flag** (a column / derived-and-stored signal), replacing read-time title-prefix inference. This is the single change that actually removes the leak risk — and it later unblocks an autonomous agent (H2) safely.
- Then ship the **server-controlled, user-gated candidate-fact surface**: the server searches + privacy-filters *before anything reaches the model or UI*, and surfaces only publishable-sourced snippets as "I found this in source X — include it?" The owner's click becomes the next targeted edit. This delivers most of the truth-seeking value (T2) with no autonomous raw-prose read.

### Phase 3 — Optimize + converge
- Layer **pure exact-match-only `edit_ops`** (research B, **fuzzy fallback removed**, fenced-`##` masked) behind the same panel for token-cheap, byte-stable edits.
- Execute the **UX convergence**: fold Suggest-revisions into / replace rebuild's "Guide" step so there aren't two overlapping conversational loops (RT2 §4).

## Why not H1 or H2
- **H1** is literally "H3 minus the candidate-fact surface" — safe, but defers the owner's *first-named* requirement (truth-seeking) indefinitely.
- **H2** delivers the literal intent but concentrates the most risk in v1: a heuristic note-sensitivity classifier guarding an **autonomous** note-reading agent. Verified weak spots (a free-text health entry with no extracted clinical row classifies as public → leak) make it a narrower form of the exact flaw that disqualified the standalone tool-agent plan. Excellent **later** upgrade once the durable privacy flag exists.

## Open decisions for the owner (defaults in **bold**)
1. **Truth-seeking in v1?** → **Defer the candidate-fact surface until the note-sensitivity signal exists, then ship it (Phase 2.5).** Alternative: ship Phase-1+2 only and revisit.
2. **Note privacy — durable flag vs keep inferring?** → **Add a durable per-note sensitivity flag.** It's the one change that removes the leak risk and unblocks everything downstream.
3. **Rebuild vs Revise — two entry points or one?** → **Intent-revealing copy now, converge in Phase 3.**
4. **Clean diffs — ops now or later?** → **Full re-emit + diff-first UI in Phase 2; exact-match ops in Phase 3.**
5. **`maintain` / nightly people-link parity?** → **Defer** (never requested; D's four-path refactor is max regression risk).

## Residual risks to accept
- **Multi-turn prose drift** under full re-emit (model silently rewords untouched prose). Mitigated by the diff-first UI + token-preservation guard; fully removed only when Phase-3 exact-match ops make untouched spans byte-identical.
- Even the **server-gated candidate-fact** surface inherits the privacy-classifier's accuracy — which is exactly why decision #2 (durable flag) is the gating prerequisite, not a nice-to-have.
