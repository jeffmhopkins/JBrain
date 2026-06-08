# H3 — Phased convergence (balanced; the likely recommendation)

**Stance.** Ship the owner's value *early* while staying on RT1's safe ground, and
**explicitly converge** the new conversational mode with the existing rebuild over time.
H3 resolves the RT1↔RT2 tension by **phasing**, not by choosing a side:

- **Phase 1** delivers RT2's highest-weighted product win — the date + people-linking
  bug fixes on the *existing* "Rebuild page now" — as a small, loop-agnostic PR, *before*
  the new feature exists. (RT2 §1; A's PR1 shape; D's helpers minus D's 4-path refactor.)
- **Phase 2** ships the conversational "Suggest revisions" loop on the **safest mechanism
  RT1 endorses** — full-article re-emit on a carry-forward generator (A's loop, on D's
  shared core) — paired with a strong **clean-diff UI** so the full re-emit reads as
  BASE-with-a-change (RT2's T1), plus a **firewalled, user-approved candidate-fact
  surface** that honors the owner's "truth-seeking" intent (RT2's T2) *without* C's
  inbound-firewall hole or per-turn corruption risk.
- **Phase 3** optimizes (B's pure exact-match edit-ops behind the same panel) and
  **converges** the two conversational loops (RT2 §4: Suggest revisions becomes *the*
  conversational editing surface, eventually replacing rebuild's Guide step).

This is the **best risk-adjusted path**: the owner sees his reported bugs fixed in week
one (Phase 1), a complete and *safe* conversational editor next (Phase 2), and the
token-cost/UX-coherence polish last (Phase 3) — each phase independently shippable, risk
rising only as payoff rises. It defers B's edit-ops and the loop convergence to Phase 3
(see §1 for why that's acceptable) and ships a **conservative-but-real** truth-seeking
affordance rather than C's autonomous agent (see §4 for why that's the right trade).

All `file:line` citations are against the tree on 2026-06-08 and were verified against the
real code (see the verification notes inline).

---

## 1. The three phases — scope boundaries, what ships, why this ordering

| Phase | Ships | Mechanism | RT-driven rationale |
|---|---|---|---|
| **P1 — Shared hardening core** | `enforce_date_tokens` + `harden_draft` + `promote_one` + session-start `rebind`, wired into the **existing** rebuild (`_generate` tail + `finalize_rebuild`) | deterministic, no new UI, no new LLM stage | RT2 §1: the owner's two literal complaints (wrong date format; doesn't link people) fixed on the page he already uses, **before** the feature. RT1: characterization-tested, zero corruption surface. |
| **P2 — Conversational loop + candidate-facts** | "Suggest revisions" panel: BASE preserved, carry-forward full-article re-emit, clean-diff UI vs BASE, firewalled user-approved candidate-fact surface, Accept | full re-emit on a **carry-forward generator** (NOT `_generate`'s wipe); deterministic candidate-fact discovery on a **privacy-filtered seam** | RT1: A's loop is the lowest-risk mechanism (no anchor/splice/fence corruption). RT2 T1/T2: clean diff + real (gated) truth-seeking. |
| **P3 — Optimize + converge** | B's **pure exact-match-only** `edit_ops.py` behind the same panel (token-cheap clean edits); converge Suggest with rebuild's Guide step | exact-match-only ops, fuzzy fallback **removed**, `section` op fence-masked | RT1: B's pure core is the most testable; the fuzzy fallback and unmasked `section` are dropped as RT1 demands. RT2 §4: one conversational surface, not two. |

**Why this ordering maximizes risk-adjusted owner value.** RT1 ranks A<D<B<E<C on technical
risk and names A's full-re-emit loop on D's shared `harden_draft` as the *safest v1
mechanism* (RT1 "Guidance for the hybrid round"). RT2 ranks C>A>E>B>D on product/intent but
concedes (RT2 §1, §3) that the **bug-fix delivery** is the single most valuable, nearly
plan-independent deliverable, and that **truth-seeking should be built as an additive seam
on a tool-less core**. H3 takes both literally: Phase 1 banks RT2's weighted win on RT1's
safest substrate; Phase 2 ships the safe loop with a *bounded* truth-seeking affordance
(not C's agent); Phase 3 buys the token-cost and coherence improvements *after* the owner is
already getting value.

**What H3 defers, and why it's acceptable:**
- **B's edit-ops (P3, not P2).** The clean-diff UI (§6) already makes a full re-emit read as
  a targeted edit, so the owner gets the *felt* "showed me the draft" experience in P2
  without paying B's anchor-matching complexity. Edit-ops are then a pure *cost* optimization
  (fewer output tokens per turn), layerable behind the same panel with no re-architecture —
  exactly RT1's recommended sequencing ("layer a structured edit-ops mode behind the same
  panel later").
- **C's autonomous tool agent (never; replaced by the candidate-fact surface in §4).** RT1
  disqualifies C as written (CRITICAL PII leak; no inbound firewall exists). H3 delivers
  *most* of C's intent — the AI surfaces a salient fact it found in a source/backlink — via a
  privacy-filtered, user-approved surface that **cannot** weave a private note's prose into a
  public article. This is strictly safer than C and avoids C's per-turn cost/model misread.
- **Loop convergence (P3, not P1).** Shipping a second panel in P2 with intent-revealing copy
  is fine short-term; converging it with Guide is a UX-coherence refinement (RT2 §4) best done
  once the loop's shape is proven.

---

## 2. Phase 1 — the shared hardening core (borrow D, minus D's refactor)

Phase 1 is **loop-agnostic**: it fixes the existing rebuild and lays the substrate the loop
will inherit. It deliberately does **not** do D's 4-path (`write_one`/`maintain_one`/
`_generate`/`finalize_rebuild`) refactor — RT1 §D and RT2 §1/§5 both flag that as
front-loaded regression risk (`maintain_one` gaining `add_links_to_content` is a real output
change, not "byte-identical" — RT1 D MAJOR) for a parity win the owner never asked for. H3
wires the three fixes into the **two chokepoints the live + nightly rebuild already share**
and stops there; `maintain`/`nightly` parity is a noted Phase-3+ follow-up.

### 2a. `harden_draft` — factor the existing `_generate` tail into one helper
New `server/app/services/hardening.py` with
`harden_draft(conn, title, draft, *, known, source_titles, base_tokens=None) -> HardenResult`.
It is **exactly** the sequence `_generate` already runs at `rebuild_engine.py:366-398`,
factored out so `_generate` and the new loop generator (§3) cannot drift:

```
draft = enforce_date_tokens(draft, base_tokens=base_tokens).body   # NEW — see 2b, runs FIRST
bad   = wiki_build._bad_links(conn, draft, known)                  # rebuild_engine.py:368
draft, bad = wiki_build._repair_citation_titles(draft, bad, source_titles)  # :372-374
draft = wiki_build._neutralize_links(draft, set(bad))  (+ talk notes)       # :376-380
draft, _ = wiki_build.add_links_to_content(conn, title, draft)     # :387 people-link backstop
lint  = wiki_guides.validate_structure(title, draft)               # :392
```

**Scope boundary (RT1 D MAJOR):** Phase 1 routes **only `_generate`** through
`harden_draft` (`base_tokens=None` for classic rebuild → identical behavior). `write_one`
and `maintain_one` are **untouched** in P1. This caps the blast radius to one already-tested
call site, proven by the characterization test in §9.

**Date-enforce-BEFORE-add-links ordering (RT1 §0.3, non-negotiable #5 — verified):**
`_mask_spans` (`wiki_build.py:1817-1833`) masks fenced/inline code, `[[links]]`, and
`[^id]:` lines — it does **NOT** mask `@t[...]` tokens (confirmed). So `enforce_date_tokens`
must run *first*, producing tokens before `add_links_to_content` could (vanishingly rarely)
insert a `[[link]]` inside a token's date arg.

### 2b. `enforce_date_tokens` — Option A + Option D (defer B to P2 hardening)
New helper next to `clock.py` (so both twins stay honest — R3 §6). Ships the two
**zero-coverage, near-zero-false-positive** checks (R3 §3 "the cheapest high-value wins"):

1. **Option A — malformed-token linter.** Flag any `@t\s*[\[{(]`-shaped substring **not**
   matched by `clock._TOKEN_RE` (`clock.py:105`) or whose date arg fails `clock._to_dt`
   (`clock.py:127`): `@t{age:…}`, `@t[born:…]`, `@t[age:1986]`. Surface as a `lint` finding
   (advisory in the human-in-loop modes; never auto-blocks Accept — R3 §3, RT2 weighting).
2. **Option D — BASE token-preservation guard** (`base_tokens` present → loop only, skipped
   for classic rebuild). Stored in P1's helper API but only *exercised* in P2 (§3); P1 lands
   the code + unit test so P2 is a pure consumer.

**Twin invariant (RT1 non-negotiable #5 — verified):** `enforce_date_tokens` only ever
*produces* tokens via `clock` round-trip; it never touches expansion semantics, so
`server/tests/fixtures/time_tokens.json` and the `clock.expand_tokens` ↔ `time.ts:
expandTimeTokens` byte-for-byte pin (`test_api.py:1209-1211`) stay untouched. A test
asserting the fixture is unchanged is itself valuable.

Option B (adjacency auto-rewrite, round-trip-guarded) is **deferred to P2** — it's MEDIUM
risk and the conversational loop lets the user just *say* "make that a live value." Folding
DATES into `wiki_revise` (`prompts.yaml:908-932`, R3 §2) ships in P1 (benefits the batch
writer; one-block addition).

### 2c. Session-start `rebind` — cheap, embeddings-free (fixes H1)
New `entity_index.rebind(conn)` = `_link_articles` (`entity_index.py:529`) + `reconcile_owner`
fold (`wiki_build.py:1074`, materialized in-call so first-session correctness holds — R4 O1
caveat) **without** the networked `_sync_embeddings` (`entity_index.py:360`). Called once at
the start of `run_draft` in P1 (so the *existing* rebuild benefits) and at suggest-session
start in P2. This binds a freshly-promoted/renamed `kb/People/<X>` so its nickname surface
reaches both `{known_aliases}` and the Pass-2 backstop.

**Why session-start rebind is still needed even though Accept already rebuilds (RT1 §0.2 —
verified):** `finalize_rebuild` already calls `entity_index.rebuild(conn)` on every Accept
(`wiki_build.py:1718`), which calls the networked `_sync_embeddings`. That fixes the
*persisted* article post-Accept, but the **live loop reads bindings during the conversation**
— before Accept. Rebind at session start is what makes the draft-time link offering correct.
PII firewall preserved for free: `_link_articles` already excludes private titles
(`entity_index.py:553`), `alias_surface` keeps drop-rule (v) (R4 O5).

### 2d. `promote_one` — Accept-path parity, in the shared chokepoint
New `wiki_build.promote_one(conn, title)` called inside `finalize_rebuild` **right after**
`entity_index.rebuild` (`wiki_build.py:1718`) — so classic Accept, suggest Accept, and
nightly rebuild **all** inherit promotion parity in one insert (R2 §6, the "big one" for the
whole family). It runs the per-article subset of `actions/wiki_build.yaml:82-108` the
single-article paths skip: `link_owner`, `surface_aliases`, `link_medications`, `link_places`,
`normalize_link_labels`, `flag_ungrounded_reference`. These are already deterministic,
idempotent, cached, link-only. P1 calls the existing corpus-wide functions (cheap +
idempotent); title-scoping is a later optimization if profiling demands.

**Idempotency on Accept (RT1 non-negotiable #6 — verified):** Accept can be retried after a
409; `surface_aliases`/`_apply_aka_line` (`wiki_build.py:1137`) rebuild the AKA line each
call. A `test_promote_one` run-twice-identical assertion locks this in. Ordering inside
`finalize_rebuild`: `entity_index.rebuild` → `promote_one` → final `flag_dead_links` if
`promote_one` added links; `surface_aliases` runs last among body-mutators (it owns the AKA
line — R3 §5).

### 2e. Characterization tests (prevent P1 regression)
`server/tests/test_hardening_characterization.py` (NEW): for a seeded DB + fixed
article/sources, snapshot the current `_generate` body + lint + talk via the `_drain` +
`FakeProvider` recipe (`test_rebuild_engine.py:36-128`), assert byte-identical after the
`harden_draft` extraction with `base_tokens=None`. This is the regression net for routing
`_generate` through the new helper. (Lighter than D's 3-path golden apparatus because P1
only re-routes one call site.)

---

## 3. Phase 2 — the conversational loop (carry-forward, not the wipe)

### 3a. The carry-forward generator (avoid `_generate`'s wipe — RT1 §0.1, verified)
`_generate` sets `run.draft = ""` at `rebuild_engine.py:295` then streams a *full* body. A
naive "carry the draft forward" can't reuse `_generate` as-is. **But** A's mechanism (the
model re-emits the *whole* article every turn, seeded by BASE in the transcript) is
compatible with the wipe — because the carried-forward state lives in `run.messages` (the
seed assistant BASE turn), not in `run.draft`. H3 still writes a **sibling generator**
`_generate_suggest` rather than reusing `_generate` verbatim, for two reasons: (1) it threads
`base_tokens` into `harden_draft` for Option D; (2) it must **not** run the auto-continue
`CONTINUE_PROMPT` scaffolding the way `_generate` does without the redraft guard (§3c). It
reuses `_generate`'s streaming body, the `_extract_talk`/`_strip_fence` extraction
(`:366`), and the `harden_draft` tail — sharing the safe, already-tested machinery while
owning the two loop-specific deltas.

**Engine entry points** (both in `rebuild_engine.py`, sharing `_generate_suggest`):
- `run_suggest_start(run, conn)` — twin of `run_draft` (`:401`). Calls `entity_index.rebind`;
  loads deterministic seed sources via `rebuild_sources`/`_load_sources` (no gather agent, no
  curate UI — the user curates conversationally); loads backlinks (§3d); seeds
  `base_tokens = clock._TOKEN_RE.findall(BASE)`; **seeds the transcript** as
  `[{user: SUGGEST_SEED_PROMPT}, {assistant: BASE_BODY}]` and stages `run.draft = BASE_BODY`
  via a **synthetic `seeded` event with NO LLM call** (so the panel shows the unchanged
  article instantly).
- `run_suggest_turn(run, conn, instruction)` — twin of `run_guide` (`:440`). Appends ONE user
  turn (the targeted-edit steer, §7) and runs `_generate_suggest`. The model re-emits the
  full revised article; `harden_draft` runs on the whole body; emits `done` + `edit_summary`.

### 3b. Transcript safety (RT1 non-negotiable #1 — verified airtight)
The loop uses **`tools=[]`, thinking on**, exactly like draft/guide (`rebuild_engine.py:320`).
No `apply_edit` tool ever enters `run.messages`, so the "no tool_use blocks to preserve —
trivially safe" invariant (`rebuild_engine.py:9-15`) holds *by construction* (RT1 A: "airtight").
The seed assistant BASE turn is a **plain-text** block (not a provider-signed thinking/tool
block), so priming with it is safe. Every turn ends with a full regenerated draft — exactly
the shape `run_redraft` and auto-continue assume.

**Add the regression guard RT1 mandates for every stance:** a test asserting `run.messages`
contains only `{"role","content": str}` turns — never list-content, never a `tool_calls`
key — and a code comment at the seed/append sites documenting the invariant (borrowed from
C's discipline; RT1 "surviving ideas / from C").

### 3c. Fix A's `run_redraft` seed bug (RT1 A MAJOR — verified)
`run_redraft` (`rebuild_engine.py:466-503`) pops a trailing **assistant** turn (`:491-492`)
assuming it's a generated draft. But `run_suggest_start` hand-seeds an assistant=BASE_BODY
turn that was *never streamed*. If the user's first action is "Re-draft with more room"
before any turn, `run_redraft` would pop the hand-seeded BASE and silently discard it. **Fix:**
mark the synthetic seed (a `run.seeded_base: bool` flag, or a sentinel marker on the seed
turn) and guard `run_redraft` to **refuse to pop the seed turn** — if the only assistant turn
is the seed, redraft is a no-op (there is no generated draft to re-run yet). Tested explicitly
(§9). Also defensively parse the `edit_summary` fenced block as strictly as `_extract_talk`
(RT1 A MINOR: a malformed `summary` fence must not leak into the body).

### 3d. Backlinks — read-only context, never in `run.sources` (RT1 non-negotiable #3 — verified)
New `wiki_build.backlink_titles(conn, note_id)` wrapping the **exact inbound SQL** at
`architect.py:818-822` (verified):
```sql
SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id = l.source_note_id
WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title
```
Injected into the prompt as a labeled **"Linked from (read-only context)"** block and
**NEVER** added to `run.sources` (which drives `_repair_citation_titles`,
`rebuild_engine.py:372-374`) nor to `run.known`. The hardening tail's `source_titles` derives
**only** from `run.sources`, so a near-miss `[[Backlink]]` can never be "repaired" into a
citation. Tested in §9.

### 3e. SSE additions & RebuildRun changes
Reuse the entire event vocabulary (R1 §4); full re-emit needs **no `patch`/`edit` event** —
the body arrives as `content_delta`s + `done.draft` as today. Additions:

| Event | Emitted by | Payload | Why |
|---|---|---|---|
| `run_started` (+`kind`) | router | `run_id, slug, title, base_rev, kind:"suggest", base_body, backlinks` | client seeds BASE + backlink chips |
| `seeded` (NEW) | `run_suggest_start` | `draft` (= BASE) | render the unchanged article instantly, no LLM call |
| `edit_summary` (NEW) | engine after `done` | `text` (≤1 sentence) | the real per-turn "what changed" bubble (replaces the canned ack at `RebuildPanel.tsx:237`) |
| `candidate_fact` (NEW) | engine after `done` | `{claim, source_title, snippet}` | the firewalled truth-seeking surface (§4) |
| `lint` (reused) | tail | `ok, message` | now also carries date-token/preservation findings |

`RebuildRun` (`rebuild_runs.py:27-49`) gains additive, defaulted fields (classic rebuild
unaffected): `kind: str = "rebuild"`, `base_body: str = ""`, `backlinks: list[dict] = []`,
`base_tokens: list[str] = []`, `seeded_base: bool = False`, `candidate_facts: list[dict] =
[]`. `base_hash`/staleness/Accept/one-run-per-slug/TTL key off the **live page** — unchanged
(RT1: don't widen the Accept gate). **No new status** and **no Accept-gate widening** — the
loop lands in `ready` and accepts via the existing `status in ("ready","guiding")` gate
(`rebuild.py`), avoiding E's gate-widening mistake (RT1 E MAJOR).

---

## 4. The firewalled candidate-fact truth-seeking affordance (the heart of H3's RT1↔RT2 resolution)

The owner's intent (RT2 T2): *"the AI should be truth seeking for salient facts."* C delivers
this with an autonomous agent that searches/reads private notes and **weaves their prose into
the article** — RT1's CRITICAL disqualifier, because no inbound PII firewall exists
(`hybrid_notes` has no privacy filter — `search.py:36`, verified; gather strips only `kb/`,
`rebuild_engine.py:178`, verified). H3 delivers the *intent* without the *hole*:

### 4a. The mechanism: discover → privacy-filter → surface → user-approve → targeted edit
When a turn's instruction is **fact-shaped** (the model says so, or a salient
date/number/name is in play), instead of the model autonomously reading a note and writing
its content, the engine runs a **deterministic, server-controlled** candidate-fact pass:

1. **Discover (server-side, not the model).** Run `search.hybrid_notes` for the salient term
   **through a privacy-filtered seam** (§4b) — the same offload as gather
   (`asyncio.to_thread`, `rebuild_engine.py:176`).
2. **Filter (the firewall, §5).** Drop any hit whose note is private (Health/Finance) **when
   the edited page is public**; drop `kb/` hits. What survives is a *publishable* candidate
   source for *this* target.
3. **Surface, do not weave.** Emit a `candidate_fact` event: `{claim, source_title, snippet}`
   — a short, **already-firewall-cleared** snippet from a *publishable* source, surfaced as a
   chip in the panel: *"I found this in [[Truck log]]: bought 2024-03. Include it?"* The model
   does **not** write the fact into the body on its own.
4. **User approves.** The owner clicks "Include." That approval becomes the **next turn's
   instruction** ("add that we bought the truck 2024-03, cite [[Truck log]]"), which flows
   through the normal `run_suggest_turn` → full re-emit → `harden_draft` path. The fact
   becomes a **targeted edit the owner authorized**, with a citation to a *publishable*
   source.

### 4b. The privacy-filtered seam
A new `search.hybrid_notes_public(conn, q, *, target_title)` (or a `require_public=True` flag
on `hybrid_notes`) that post-filters results with the **existing** predicates
`is_private_title`/`is_health_title`/`domain_for_title` (`wiki_guides.py:127,148,103`,
verified to exist) **when the target page is public**. This is the inbound counterpart to
`add_links_to_content`'s outbound self-guard (`wiki_build.py:733`). It is a *hard, tested
gate*, not a prompt instruction.

### 4c. Why it's firewall-safe (vs. C's hole)
- **No autonomous read of private prose.** The model never receives a private note's body to
  paraphrase. The server does the search, filters *before* anything reaches the model or UI,
  and only ever surfaces a snippet from a *publishable* source for a *public* target.
- **User-approval gate.** Nothing enters the article without an explicit owner click — there
  is no path where a fact auto-weaves into the body.
- **Citation safety.** Because the candidate's source is already publish-filtered, the
  resulting footnote (`[^s1]: [[source]]`) can only point at a publishable article — closing
  C's MAJOR "footnote to a private note" gap, since the candidate could never be private to
  begin with.
- **No per-turn corruption.** The edit still goes through full re-emit + `harden_draft` on the
  whole body — no anchor/splice/patch surface.

For a **private** target (editing `kb/Health/...`), the filter relaxes (private→private is
fine), but the same surface-and-approve gate applies, so a private note's prose still never
auto-weaves. This gives most of C's product value (RT2 T2: visible, sourced truth-seeking)
with none of C's CRITICAL inbound-firewall danger, cost/model misread (we run the cheap
deterministic search, not the synthesis model in a tool loop — RT1 C MAJOR), or
two-transcript fragility.

---

## 5. The INBOUND PII firewall design (the thing that does not exist yet — RT1 non-negotiable #4)

RT1 §0.6 / #4 (verified): the existing firewall guards **links/targets** outbound
(`add_links_to_content:733`, `alias_surface` drop-rule v, `_link_articles:553`). It does
**not** stop private *prose/facts/citations* flowing **in**. Both read-only backlinks **and**
the candidate-fact surface need a designed inbound gate. H3's firewall:

1. **A single predicate, reused everywhere.** `_inbound_allowed(target_title, candidate_title)
   -> bool` returns False when the *candidate* is private/Health/Reference and the *target* is
   public (reusing `is_private_title`/`is_health_title`/`domain_for_title`). Used by: the
   candidate-fact search filter (§4b), the backlink loader (§3d), and any future inbound path.
2. **Backlinks: titles only, gated.** `backlink_titles` returns titles for context (don't
   rename a section others cite). A backlink whose title is private is **dropped from the
   context block** when the target is public (a private backlink title — "HIV results 2024" —
   is itself sensitive; RT1 C CRITICAL notes private *titles* steer prose). H3 never reads a
   backlink *body* into the prompt in P2 (unlike C's `read_backlink` tool), so there is no
   private-body leak path at all.
3. **Candidate facts: filtered before surfacing** (§4b) — the snippet shown is already from a
   publishable source.
4. **Outbound stays intact** (RT1 O5): `harden_draft` runs `add_links_to_content` (self-guards
   the target) and `validate_structure`'s `forbid_link_prefixes` check (`wiki_guides.py:303-310`)
   on the **full** body every turn, so even an approved fact can't introduce a forbidden
   `[[kb/People/...]]` into a Reference page.
5. **Tested as a first-class gate** (§9), not a Risks-section sentence — RT1's exact demand for
   C. The test seeds a private note matching a salient term, edits a *public* page, and asserts
   the private note never appears in a `candidate_fact` event nor in any prompt.

---

## 6. Clean-diff UI; Phase-3 exact-match bolt-on; rebuild/Guide convergence

### 6a. Clean-diff UI (P2 — makes full re-emit read as a targeted edit; RT2 T1)
RT2's sharpest critique of A/D is that full re-emit "re-writes my article and I have to audit
it." H3 answers with a **diff-first panel**:
- `MarkdownDiff before={BASE} after={draft}` (`RebuildPanel.tsx:431-435,458-460`) is the
  **PRIMARY** view, `showDiff = true` by default. The owner sees a quiet article with the
  asked-for change highlighted, not a wall of prose.
- The **token-preservation guard** (Option D) + a **prose-drift signal** (RT1 "what must be
  true for A/D"): warn via `lint` when an *unmentioned* section's body changed materially, so
  silent drift is surfaced rather than relying on the human to spot it.
- Per-turn `edit_summary` bubble replaces the canned ack (`RebuildPanel.tsx:237`).

This delivers the *felt* "showed me the draft" experience in P2 — the reason B's edit-ops can
wait for P3.

### 6b. Phase-3 exact-match edit-ops bolt-on (RT1 "from B": keep the exact op, drop the fuzzy fallback)
P3 adds `server/app/services/edit_ops.py` — a **pure**, exhaustively unit-tested module (RT1:
B's best testability story) behind the *same* panel, as a token-cost optimization:
- **Exact-match-only, reject-on-ambiguous.** No whitespace-normalized fuzzy fallback (RT1
  CRITICAL: "a fuzzy:true silent mis-apply is exactly the corruption B claims to avoid" —
  dropped). 0/>1 matches → the op **fails cleanly** and the turn **falls back to full re-emit**
  (which is just P2's mechanism) — so a turn is never inert (RT2's B critique).
- **`section` op fence-masked.** `_SECTION_RE` (`wiki_guides.py:39`) matches `## ` inside code
  fences (RT1 §0.4, E/B CRITICAL); the `section` op must `_mask_spans` (`wiki_build.py:1817`)
  before locating a heading. (H3 may simply *omit* the `section` op in P3 and keep only
  exact `replace`/`insert`, since full re-emit already handles whole-section restructures —
  the minimal safe surface.)
- **All deterministic rewriters run on the final patched string**, highlights via diff, not raw
  op offsets (RT1 "from B").

Edit-ops are opt-in per turn (the model may emit ops *or* a full body; the server detects
which and the fallback is full re-emit), so P3 is additive and never blocks the loop.

### 6c. Rebuild/Guide convergence (P3 — RT2 §4, the unaddressed product question)
RT2 §4: shipping a second `NoteActionsMenu` item next to "Rebuild page now" risks two
overlapping conversational loops (rebuild's Guide step already has a talk→edit loop). H3's
convergence plan:
- **P2 (now):** differentiate with **intent-revealing copy** — "Rebuild page now (re-write from
  sources)" vs "Suggest revisions (talk to this article)".
- **P3 (converge):** make "Suggest revisions" *the* conversational editing surface and route
  rebuild's post-draft Guide step into the **same** `_generate_suggest` loop + panel — one
  surface reachable from a fresh rebuild ("now talk to it") and from an existing page ("revise
  this"). Because both are full-re-emit-on-a-transcript with the shared `harden_draft` tail,
  this is a routing/UX change, not a re-architecture.

---

## 7. Prompt design; folded date/crosslink/grounding rules

Two new `prompts.yaml` keys (R2 §3 minimal fragment reuse):
1. **`actions.wiki_suggest_seed`** — BASE is the current article (shown as the working draft);
   make **MINIMAL, TARGETED** edits; **PRESERVE** everything not asked to change, **including
   every `@t[...]` token verbatim**; output the **COMPLETE** revised article; end with a fenced
   one-sentence `summary` block. Embeds the **DATES & TIME** block (`prompts.yaml:868-874`) and
   **CROSS-LINKS** block (`:855-860`) inline so the rules survive multi-turn dilution (R2 §4).
2. **`actions.wiki_suggest_turn`** — the per-turn steer (replaces `run_guide`'s bare string at
   `rebuild_engine.py:455-460`): "targeted; preserve the rest; PRESERVE `@t[...]`; output the
   complete article; summary block" + `{instruction}`, re-stating DATES/CROSS-LINK reminders.

Truth-seeking framing: the prompt tells the model that **the system surfaces candidate facts
for the owner to approve** — the model should *flag* when a salient fact is missing/unverified
rather than inventing one. Grounding/citation rules: cite only `run.sources` titles; never
cite a backlink; `_repair_citation_titles`/`_neutralize_links` drop any footnote to a
non-source (R3 §5). AKA line: the model never hand-edits it — `surface_aliases` owns it on
Accept (R3 §5). Protected pages (`is_protected`, `wiki_guides.py:87`) and the privacy-filtered
candidate seam are enforced in code, not prose. **DATES folded into `wiki_revise`** ships in
P1 (§2b). H3 does **not** build D's full `{date_rules}`-substitution system (defer; RT1/RT2
both flag the golden-prompt whitespace battle as over-investment for this feature).

---

## 8. Frontend panel + entry point + diff + candidate-fact UI; the unified mental model

New `web/src/components/SuggestPanel.tsx` (not a fork-flag inside `RebuildPanel` — the loop is
the *primary* surface, with no gather/curate `Stage` wizard). It borrows RebuildPanel
primitives:
- `thread` chat state (`RebuildPanel.tsx:62`), footer composer + Enter-to-send (`:283-289`),
  the **stable-`onClose` ref trick** (`:78-79`) so the Modal focus effect doesn't steal the
  composer's focus, `handleDraft`'s SSE handler (`:117-134`), `sawContent` (`:73`).
- **Optimistic dual-bubble** (`Chat.tsx:587`): push the user bubble on Send, replace the canned
  ack with the streamed `edit_summary`.
- **Diff vs BASE** primary (`MarkdownDiff before={note.content_md} after={draft}`,
  `:431-435,458-460`), `showDiff = true`.
- **Candidate-fact chips:** render `candidate_fact` events as inline cards (*"Found in [[Truck
  log]]: bought 2024-03. [Include] [Dismiss]"*); "Include" sends the approval as the next turn.
- **Accept/Reject** reuse `acceptRebuild`/`rejectRebuild` (`api.ts`).

`api.ts`: a `SuggestEvent` union (reuses most of `RebuildEvent` + `seeded`/`edit_summary`/
`candidate_fact`) and thin `streamSSE`-based wrappers `suggestStart`/`suggestTurn` (do NOT
hand-roll a reader; `streamSSE` handles abort/stall/health/`\n\n` framing). **Entry point:** a
second KB-only `NoteActionsMenu` item beside "Rebuild page now" (`NotePage.tsx:265-267`), same
`rebuildNow`-style `llm.ready` pre-flight (`:119-124`), mounted near `:379-383`; `onAccepted`
navigates on rename else reloads. Intent-revealing copy now; convergence in P3 (§6c).

**Unified mental model:** "Rebuild" = throw away the draft, re-synthesize from sources;
"Suggest revisions" = keep the article, talk to it. P3 collapses these into one conversational
surface (§6c) so the owner never faces two overlapping loops permanently.

---

## 9. Test plan per tier (CLAUDE.md DoD) + coverage; effort realism

Per CLAUDE.md DoD: tests in the right tier; `./jt` green per domain; coverage never regresses
(ratchet where it climbs); no real LLM/network (mock at the `llm` seam; embeddings stubbed);
Google-style docstrings + `ruff check app`. **Effort estimates below are RT2-discounted
(+30–50% over the happy path)** for the DoD's failure-path + e2e + coverage tax.

### Phase 1
- `server/tests/test_hardening_characterization.py` (NEW) — §2e byte-identical snapshot of
  `_generate` through `harden_draft` (`base_tokens=None`).
- `server/tests/test_clock.py` (EXTEND) — `enforce_date_tokens` Option A (malformed `@t{…}`,
  `@t[age:1986]`) + Option D (dropped BASE token) + the **fixture-unchanged** assertion
  (`time_tokens.json` twin pin).
- `server/tests/test_promote_one.py` (NEW) — `finalize_rebuild` runs `promote_one`; AKA line
  present; owner link; **run-twice-identical** (idempotency); Reference target gets the
  grounding flag and **zero** People links (firewall).
- `server/tests/test_rebuild_refs_links.py` (EXTEND) — a freshly-created `kb/People/<X>` links
  after `rebind` (H1 repro, mirrors `test_owner_alias_backfill.py`); private/Reference target
  links nothing.

### Phase 2
- `server/tests/test_suggest_engine.py` (NEW, `@pytest.mark.integration`, copy
  `test_rebuild_engine.py:36-128`): seed preserves BASE (no LLM call; `base_tokens` captured;
  backlinks NOT in `run.sources`); one edit turn (re-links a dropped name; `done` +
  `edit_summary`; transcript ends with a plain assistant draft); **two turns** (resume the
  grown transcript — the transcript-hazard regression); **`run.messages` plain-only assertion**
  (§3b); **`run_redraft` seed-bug guard** (redraft before any turn does NOT discard BASE —
  §3c); date Option D drop → `lint`; malformed token → `lint`; **candidate-fact firewall** (a
  private note matching a salient term never appears in a `candidate_fact` event nor in any
  prompt when the target is public — §5); no-creds / `fail_on_turn` / `run.cancelled` paths.
- `server/tests/test_search_public.py` (NEW, `@pytest.mark.unit` where pure) —
  `hybrid_notes_public`/`_inbound_allowed`: private hit dropped for a public target, kept for a
  private target; `kb/` excluded.
- `web/src/components/SuggestPanel.test.tsx` (NEW, copy `RebuildPanel.test.tsx:1-53`): seed
  renders BASE, diff default-on, no spurious LLM call; a turn (optimistic user bubble; draft
  re-renders; `edit_summary` bubble); a **second** turn; `candidate_fact` chip → Include sends
  the next turn; `lint` banner; Accept→`acceptRebuild`, stale 409, Reject.

### Phase 3
- `server/tests/test_edit_ops.py` (NEW, `@pytest.mark.unit`) — exact `replace`/`insert`,
  `nth`, delete (`with:""`); **failure paths** (`anchor_not_found`, `ambiguous` on N>1,
  `nth_out_of_range`); **no fuzzy fallback exists** (assert a near-miss anchor fails, never
  mis-applies); `section` op (if kept) fence-masked.
- `server/tests/test_suggest_engine.py` (EXTEND) — ops turn applies; a failed/ambiguous op
  **falls back to full re-emit** (never inert).
- Convergence: extend the rebuild Guide test to assert it routes through `_generate_suggest`.

### e2e (each phase that touches the user-facing flow)
`e2e/` Playwright (LLM faked at `e2e/fake_llm.py`, never a real key): P2 — open KB page →
"Suggest revisions" → send instruction → fake LLM returns an edited body → diff shows the
change → Accept → page reloads. Exercises the real SSE bridge + Accept/lock the integration
tier bypasses.

### Coverage ratchet (CLAUDE.md DoD #3)
New modules (`hardening.py`, `enforce_date_tokens`, `promote_one`, `rebind`, the suggest
engine, `edit_ops.py`) arrive with focused tests; once real coverage sits comfortably above
the floor, **ratchet `fail_under` in `server/pyproject.toml` and the `thresholds` in
`web/vitest.config.ts` up in the same PR**. Never lower a floor. RT5 §5a notes the existing
guide loop is currently untested (`guideStream` mocked but never scripted) — the new
`SuggestPanel` test scripts a real loop, *raising* covered branches; land panel + tests in the
same PR to avoid a `thresholds` regression.

### Effort (RT2-discounted)
- **P1:** PR1 (`harden_draft` extraction + characterization) ~1.5d; PR2 (`enforce_date_tokens`
  A+D + `rebind` + `promote_one` + `wiki_revise` DATES + tests) ~2.5d.
- **P2:** PR3 (suggest engine + `run_redraft` guard + backlinks + SSE + transcript-safety
  tests) ~3d; PR4 (candidate-fact seam + inbound firewall + `hybrid_notes_public` + tests)
  ~2.5d; PR5 (SuggestPanel + api.ts + entry + candidate-fact UI + vitest/MSW) ~3d; PR6 (e2e +
  ratchet) ~1d.
- **P3:** PR7 (`edit_ops.py` pure + tests) ~2.5d; PR8 (ops wired behind panel + fallback +
  tests) ~2d; PR9 (Guide→suggest convergence + e2e) ~2d.
- **Total ≈ 4.5–5.5 weeks** across three independently shippable phases (P1 ~1 week ships the
  bug fixes; P2 ~2 weeks ships the feature; P3 ~1.5 weeks optimizes + converges).

---

## 10. The SEVEN RT1 non-negotiables — enumerated and answered

1. **Transcript stays tool-free in `run.messages` (or tools only in a disposable thinking-OFF
   transcript).** ✅ The loop runs `tools=[]`, thinking on (`rebuild_engine.py:320`); no
   `apply_edit` tool exists. The candidate-fact pass is a **server-side deterministic search**,
   not a model tool — nothing tool-shaped touches `run.messages`. The "no block-content / no
   `tool_calls` in `run.messages`" regression test ships in P2 (§3b, §9).
2. **Re-validate on the FULL article every turn.** ✅ `harden_draft` runs `enforce_date_tokens`
   → citation/dead-link tail → `add_links_to_content` → `validate_structure` on the **whole**
   re-emitted body every turn (§2a). Full re-emit means there is no section/patch-in-isolation
   path in P2; P3's ops also run the tail on the whole patched string (§6b).
3. **Backlinks are read-only context, NEVER in `run.sources`.** ✅ `backlink_titles` →
   prompt-only "Linked from" block; `run.sources` is curated/seed primary notes only;
   `source_titles` derives only from `run.sources` (§3d). Tested.
4. **PII firewall on the way IN, not just OUT.** ✅ The inbound firewall (§5): a single
   `_inbound_allowed` predicate gates the candidate-fact search and the backlink loader;
   backlink *bodies* are never read into the prompt in P2; outbound `add_links_to_content` +
   `forbid_link_prefixes` stay intact. Tested as a first-class gate.
5. **Date-enforce BEFORE add-links; `enforce_date_tokens` only produces tokens (twin/fixture
   untouched).** ✅ `harden_draft` runs `enforce_date_tokens` first (§2a, §2b), because
   `_mask_spans` doesn't mask `@t[...]` (verified `wiki_build.py:1830-1831`). The helper only
   round-trips through `clock`; `time_tokens.json` + the `clock`↔`time.ts` pin
   (`test_api.py:1209-1211`) stay byte-for-byte; a fixture-unchanged test asserts it.
6. **`promote_one` idempotent on the Accept path.** ✅ Placed in `finalize_rebuild`
   (`wiki_build.py:1718`) so live + nightly inherit it; `surface_aliases`/`_apply_aka_line`
   rebuild the AKA line each call; a run-twice-identical test locks idempotency (Accept can
   retry after a 409).
7. **Cheap entity rebind at session start (no `_sync_embeddings`), reusing `_link_articles`.** ✅
   `entity_index.rebind` = `_link_articles` (`entity_index.py:529`, already private-safe at
   `:553`) + `reconcile_owner` fold, **without** `_sync_embeddings` (`:360`). Called at
   `run_draft` (P1, fixes existing rebuild) and suggest start (P2). Needed because the live loop
   reads bindings *before* Accept's full rebuild (RT1 §0.2, verified).

---

## Honest assessment

**Biggest strength.** H3 is the only stance that banks RT2's highest-weighted, nearly
plan-independent win (the date + people-link bug fixes on the existing rebuild) in week one on
RT1's safest substrate, *and* still honors the owner's "truth-seeking" intent — via a
firewalled, user-approved candidate-fact surface that gives most of C's product value with
none of C's CRITICAL inbound-PII danger, cost/model misread, or two-transcript fragility. Every
mechanism it ships in P1/P2 sits on RT1's lowest-risk corner (full re-emit on a shared
`harden_draft` tail; no anchor/splice/fence/tool corruption surface), and it answers RT1's
seven non-negotiables explicitly.

**Biggest risk.** Multi-turn **prose drift** under full re-emit (RT1 A's defining MAJOR): the
model can silently reword untouched prose across a long session. H3 mitigates with the
diff-first UI, Option D token-preservation, and a prose-drift `lint` signal — but the human
remains the ultimate reviewer until P3's edit-ops make untouched spans byte-identical. The
candidate-fact surface also adds a fact-shaped-instruction detection heuristic that can
mis-fire (surface a fact the owner didn't want, or miss one); it's non-blocking and
user-gated, so the failure mode is mild noise, not corruption.

**What it trades away.** (1) **Per-turn token cost** in P1/P2 — full re-emit re-streams the
whole article every turn; B's cheaper edit-ops wait for P3. Acceptable because the clean-diff
UI already delivers the *felt* targeted-edit experience, and `_clamp_tokens` + the TTL caps
bound the cost. (2) **The fully autonomous truth-seeking agent** — H3 deliberately does not let
the AI read-and-weave a note's prose unprompted; the owner must approve each candidate fact.
This is less "magical" than C's demo but is the only version that doesn't leak private content
into shareable articles. (3) **`maintain`/`nightly` people-link parity** — deferred past the
two shared Accept/`_generate` chokepoints to avoid D's 4-path regression risk; a noted
follow-up, not a prerequisite for the owner's feature.
