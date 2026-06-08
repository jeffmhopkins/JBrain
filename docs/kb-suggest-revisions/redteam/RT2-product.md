# RT2 — Red-Team: Product / UX / Scope / Sequencing / Shippability

Adversarial review of plans A–E for the "Suggest revisions" feature, judged against the
**owner's stated intent** and shipping reality. Harsh, specific, fair. Critique only.

## The yardstick (quote it, hold every plan to it)

> "I talk to the AI, it makes changes to the article, shows the article draft, I talk
> some more, it edits more etc until I accept. the AI should be **truth seeking for
> salient facts**, my input will be for **guiding structure or bad formatting, or
> correcting AI assumptions**."

Locked decisions: mechanism = **targeted edits guided by user**; review UX =
**conversational loop, NOT per-suggestion cards**; backlinks = **read-only context**; fix
scope = **fold in shared hardening** (dates, people-linking, formatting/promotion parity).
The owner *explicitly complained* that today's "rebuild page now" gets people-linking and
the auto-updating date format wrong — so **fixing those on the existing rebuild is itself
a delivered product win**, weighted heavily below.

Two felt-experience tests fall straight out of the quote:
- **T1 "showed me the draft" must be a clean, low-noise view.** The owner pictures the
  article quietly changing where he asked, not a wall of re-rendered prose every turn.
- **T2 "truth seeking for salient facts" is an active verb.** He wants the AI to *go find*
  a fact when a salient one comes up, not merely re-shuffle a pre-curated source list.

---

## Per-plan scorecard

Ratings: 1 (poor) – 5 (excellent). "TTV1" = time to first genuinely useful version that
the owner would actually use.

| Plan | Intent fit (felt loop) | Truth-seeking (T2) | Clean diff (T1) | TTV1 | Bug-fix delivery (early?) | Regression risk | UX coherence | Degrades gracefully |
|---|---|---|---|---|---|---|---|---|
| **A** minimal full re-emit | 3 | 2 | **2** | **5** | **5** (PR1 ships all 3 fixes) | **5** (lowest) | 3 | 5 (always shippable) |
| **B** structured patch | 4 | 2 | **5** | 3 | 4 (PR2/PR5 mid) | 3 | 3 | 3 (apply layer all-in) |
| **C** tool agent | **5** | **5** | 4 | 3 | 4 (PR1 early) | 2 (topology risk) | 4 | **5** (tool-less PR2 floor) |
| **D** shared-core first | 3 | 2 | 2 | **2** (front-loads) | **5** (but *only after* a 4-path refactor) | **2** (4 writer paths) | 3 | 3 |
| **E** section-scoped | 4 | 2 | **4** | 3 | 4 (PR2 early) | 3 (footnote coupling) | 3 | 4 (section lib reusable) |

### Why each landed there

**A — Minimal full re-emit.** Honest, fast, low-risk. But it *fails the felt experience on
its own terms*. It re-streams the WHOLE article every turn (`rebuild_engine.py:455-461`
pattern), so T1 ("showed me the draft" = clean) becomes "showed me a wall of re-rendered
text, find the change yourself." A leans on diff-view-on-by-default to paper over this — a
real mitigation, but the owner asked for fluid edits, not a diff-reading chore. On T2 it
cannot truth-seek mid-conversation; it can only re-arrange the deterministically-seeded
source set. **A's saving grace is PR1**: `enforce_date_tokens` + `rebind` + `promote_one`
folded into the *shared* chokepoints fix the owner's two actual complaints on the existing
rebuild in week one, independent of the new mode. That is the single most valuable
deliverable in the entire bake-off, and A ships it fastest with the least blast radius.

**B — Structured patch.** Nails T1: deterministic apply against a carried-forward working
draft means an untouched span is byte-identical, diffs are surgical. This is the cleanest
"showed me the draft" of any plan. But it does **not** truth-seek (T2 = 2): the model only
describes changes to material already in context. And B buys the clean diff with the
feature's highest *incidental* complexity — a parse/match/whitespace-fallback/retry/ambiguity
engine (`edit_ops.py`) whose defining failure mode is the model paraphrasing its own anchor.
B's own §9.1 admits "a turn where *everything* fails feels inert." That inert-turn risk
directly attacks the felt loop: the owner says something, and nothing visibly happens. B is
a strong *engineering* answer to a UX problem the owner didn't frame as a patch problem.

**C — Tool agent.** The **only** plan that delivers the owner's literal intent. T2 is the
whole thesis: tools (`search_notes`/`read_source`/`read_backlink`) let the AI go *find* a
salient fact mid-conversation, and the facts panel makes grounding *visible*
(claim→source chips) — which is exactly the trust the owner is implicitly asking for when
he says "truth seeking." Edits are deterministic and targeted (`_apply_edits` on unique
anchors), so T1 is good too (4, not 5 only because rejected-anchor edits surface as "couldn't
place"). C's headline risk is the two-transcript topology (signed-thinking + tool_use
fragility, `rebuild_engine.py:9-15`), and it carries the most moving parts. **But C's PR
sequencing is its secret weapon**: PR2 is a *complete, shippable, tool-less* Suggest mode;
the tool agent (PR3) is purely additive. So C degrades to "B/D-class product" cleanly if the
tool stance proves too costly — best-of-both: highest ceiling, solid floor.

**D — Shared-core first.** Strategically correct about the *codebase* (four divergent
writer paths, real asymmetries) and wrong about the *product*. It front-loads a multi-call-site
refactor of `write_one`/`maintain_one`/`_generate`/`finalize_rebuild` + a prompt-fragment
extraction *before the owner sees anything*. TTV1 is the worst of the five: the user-visible
feature is PRs 5–6, behind PRs 1–4 of plumbing. D's defense — "PRs 2–4 ship the bug-fixes to
existing users first" — is true but **A delivers those same fixes in one small PR1 without
refactoring four paths.** D pays maximum regression risk (touching every passing writer path,
hence the characterization-test apparatus in §7a) to also fix `maintain` and `nightly`, which
the owner *never mentioned*. That is gold-plating the parity story at the cost of velocity and
safety. The loop D finally ships is *the same full-re-emit loop as A* (§4c) — so D spends the
most effort to arrive at A's UX.

**E — Section-scoped.** Clever bet: the `## Section` boundary is already a load-bearing,
linter-enforced invariant, so splicing is exact and diffs are naturally localized (good T1).
No truth-seeking (T2 = 2). E's real tax is the **References/footnote coupling** (markers
scatter across sections, defs live in one) — its own §1c/§5d admit this is "the most intricate
part of E and the likeliest place for a subtle bug." It also loses on sweeping cross-section
edits ("rename X everywhere") and degrades to whole-article LEAD editing on section-less/stub
articles — exactly the small, messy articles a personal KB has lots of. E is a good idea with
a sharp, narrow failure surface that the owner will hit on his stubbier pages.

---

## Single biggest PRODUCT risk per plan

- **A:** The owner opens it, types an edit, and gets a full re-render every turn — it *feels*
  like the old rebuild, not like "talking to the article." Risk: the new mode doesn't feel
  meaningfully different from "Rebuild → Guide," so why is it a second button? (See UX
  coherence below.)
- **B:** Anchor-miss inert turns. The owner steers, the model paraphrases its anchor, the
  apply fails, **nothing changes on screen.** A conversational tool that sometimes silently
  does nothing erodes trust fast.
- **C:** Over-eager or slow tool loops — the agent searches when it should just edit, adding
  latency and cost to a "make the intro shorter" request; plus the transcript-topology
  regression hazard if a future change appends the fact-finding transcript.
- **D:** Ships the refactor, slips the feature. Four-path refactor risk + front-loading means
  the highest chance the owner waits weeks and a regression lands in `maintain`/`nightly`
  (paths he didn't ask to touch) along the way.
- **E:** "Rename X to Y everywhere" / a fact that touches lead + timeline — the one-section
  -per-turn model turns one intent into a multi-turn chore, and the footnote-fold logic
  silently misbehaves on a citation-heavy edit.

## What the owner would be DELIGHTED vs FRUSTRATED by

| Plan | Delighted by | Frustrated by |
|---|---|---|
| **A** | His date + people-link bugs fixed in week one on the page he already uses. | "This 'new' mode looks and feels like Rebuild→Guide. Where's the *talking-to-the-article* feel? Why two buttons?" |
| **B** | Crisp, surgical diffs — only what he asked changed, nothing else moved. | Turns that visibly do nothing ("couldn't place that change") when the model fumbles an anchor. |
| **C** | "I asked when we bought the truck and it *went and found it* in my notes and cited it." Visible truth-seeking = trust. | Latency on simple formatting asks; the occasional "couldn't apply automatically" on a non-unique anchor. |
| **D** | (Eventually) every page — rebuilt, maintained, nightly — gets dates/links/promotion right. | Waiting. The feature he asked for is last; for weeks he sees refactor PRs and no new button. |
| **E** | Small, focused per-section diffs; the edited section highlights. | "Change his name everywhere" taking many turns; footnote weirdness on cited pages; loses its edge on his short/stub notes. |

---

## Cross-cutting findings (the load-bearing arguments)

### 1. Bug-fix delivery is the highest-weighted product win, and it's nearly plan-independent
All five plans converge on the *same three fixes*: `enforce_date_tokens` (R3 A+D, ±B),
`entity_index.rebind` (R4 O1, the H1 root cause), `promote_one` (R2 §6). All five correctly
wire them into **shared chokepoints** (`_generate` tail / `finalize_rebuild`) so the existing
rebuild benefits. **This means the owner's two literal complaints get fixed regardless of
which loop architecture wins.** The differentiator is *how early and how safely*:
- **A and C and E** land these in an early, standalone PR1/PR2 with minimal blast radius — A
  most cheaply (no new module, no characterization apparatus).
- **D** technically lands them early too, but couples them to a 4-path refactor (PR2–4),
  paying far more regression risk for a parity win (`maintain`, `nightly`) the owner didn't
  ask for.
- **B** is fine but puts the date/link work behind a `hardening.py` extraction (its PR2),
  slightly later than A's PR1.

**Verdict: do the hardening as a small, standalone, loop-agnostic first PR (A's PR1 shape).
Do NOT gate it behind a writer-core refactor (D) or a patch engine (B).**

### 2. The clean-diff requirement (T1) splits the field — and A/D are on the wrong side
The owner's "shows the article draft" only reads as delightful if the draft is *quiet* —
visibly the same article with the asked-for change. **A and D re-emit the whole article every
turn**, so the draft is a fresh generation that can perturb untouched prose; both lean on a
diff view to compensate. **B, C, E preserve untouched spans deterministically** (B/C via
targeted apply, E via section splice), so the draft genuinely *is* BASE-with-a-change. This
is not a minor nicety — it is the difference between "the AI edited my article" and "the AI
re-wrote my article and I have to audit it." The product DNA here is **deterministic targeted
edits**, and A/D's full-re-emit is the weakest expression of the owner's stated mechanism
("targeted edits"), even though A is the fastest to ship.

### 3. Truth-seeking (T2) is C-only — and it's the part of the intent everyone else quietly drops
Read the quote again: "the AI should be truth seeking for salient facts." A/B/D/E all
interpret "Suggest revisions" as *re-arrange the curated/seed sources*. Only **C** lets the
AI *go find* a fact the owner raises mid-conversation. The other four are not wrong to defer
it (it's the riskiest piece), but **a hybrid that ships zero truth-seeking affordance is not
delivering the owner's stated intent — it's delivering a cheaper adjacent feature.** The
mitigating truth: C itself sequences truth-seeking as an *additive* layer (PR3) on top of a
tool-less core (PR2). So the right move is to **build the tool-less targeted-edit loop first
and design the seam for a truth-seeking tool to slot in** — not to pretend the requirement
doesn't exist.

### 4. UX coherence: "Rebuild" vs "Revise" — the unaddressed product question
**Every plan adds a second `NoteActionsMenu` item next to "Rebuild page now," and none of
them seriously interrogates whether that's the right mental model.** This is the biggest
shared product blind spot. Today's flow already has a conversational "Guide" loop *inside*
rebuild (`RebuildPanel.tsx:227-239`, the canned "Updated the draft — take a look." ack). So
the owner will face two buttons whose mental models overlap:
- **Rebuild page now** = throw away the draft, re-synthesize from sources, then optionally Guide.
- **Suggest revisions** = keep the draft, conversationally edit it.

These are genuinely different (regenerate vs. revise), but the *names* don't telegraph it and
the *Guide loop blurs them* (rebuild already has a talk→edit loop). Risk: the owner can't
predict which button does what. **The cleanest end-state, which RT2 endorses, is the one the
brief hints at: "Suggest revisions" should eventually REPLACE the Guide step of rebuild** —
i.e. one conversational editing surface, reachable both from a fresh rebuild ("now talk to
it") and from an existing page ("revise this"). The hybrids should at minimum *differentiate
the two entry points with intent-revealing copy* and *plan the convergence* so we don't ship
two overlapping conversational loops permanently. Plan E's per-section highlight and B/C's
targeted diffs make "Revise" feel distinct from "Rebuild"; A's full re-emit makes them feel
identical — another strike against A as the *whole* answer.

### 5. Effort realism — estimates are optimistic across the board, given the DoD
CLAUDE.md's Definition of Done (tests in the right tier, no coverage regression, ratchet
floors, e2e for user-facing flows) is a real tax that every estimate underweights:
- **A: ~1 week.** Plausible *for A's scope*, and the most credible of the five because it
  adds the least new surface. Still optimistic on the e2e + coverage-ratchet PR3.
- **B: 2–3 weeks.** Honest about being the most code, but `edit_ops.py`'s exhaustive failure-
  path suite (§8.1: ambiguous, nth-out-of-range, section-boundary, parse failure, fuzzy) plus
  the retry-loop integration tests is a lot of test surface; 3 weeks is the realistic end.
- **C: ~7.5 days.** **Optimistic.** The transcript-safety assertion suite + firewall gating on
  three tools + the two-transcript discipline are "exacting" by C's own admission; PR3 alone
  is more than the quoted 2.5d once you write the leak/regression guards properly. Call it
  ~10 days.
- **D: not given a single number, but PRs 1–6 with characterization + golden-prompt tests is
  the largest true effort here.** The "byte-identical snapshot" promise across `write_one`/
  `maintain_one`/`_generate` is expensive to establish and brittle to maintain; the prompt-
  fragment golden test will fight whitespace battles. Realistically 3+ weeks, most of it
  before the owner sees anything.
- **E: ~6 days.** **The most optimistic relative to its risk.** `kb_sections.py` round-trip
  +splice tests are tractable, but the footnote-fold + orphan-def reconciliation (§1c/§5d) is
  exactly the kind of "intricate, subtle-bug-likely" code that eats days in test and review.
  6 days ignores that. Call it ~9.

**General pattern: plans price the happy path and underprice the DoD's failure-path + e2e +
coverage obligations. Discount every estimate ~30–50%.**

### 6. Degrade-gracefully — C and A win, B and D are the most all-or-nothing
- **C** and **A** have the cleanest floors: A is always shippable (it's thin), C explicitly
  ships a tool-less product at PR2 before the risky tool agent.
- **E** has a usable floor too (the section library + hardening ship value even if the loop
  changes), but the loop itself doesn't gracefully handle its own weak cases (sweeping edits,
  stubs).
- **B** is the most all-or-nothing *within the loop*: the patch engine either works or the
  turn is inert; there's no "simpler fallback" inside B short of falling back to full re-emit
  (which is A).
- **D** is all-or-nothing on *sequencing*: its entire thesis is the refactor-first ordering,
  and if the refactor stalls or a characterization snapshot won't settle, the feature is
  blocked behind it.

---

## Guidance for the hybrid round

### The product DNA the 3 hybrids should combine
1. **Deterministic targeted edits for clean diffs (T1).** The draft must read as BASE-with-a
   -change, not a re-generation. Take this from **B** (patch apply) or **E** (section splice)
   or **C** (`_apply_edits` on unique anchors). **Reject A/D's full-re-emit as the *primary*
   loop** — it's the weakest fit for "targeted edits" and "showed me the draft," even though
   it's the fastest to stand up. (Full re-emit is an acceptable *fallback* when targeted apply
   fails, which conveniently is just A's mechanism.)
2. **A visible truth-seeking affordance (T2).** Do not drop this — it's in the owner's quote.
   Take **C's** tool seam, even if v1 ships only one tool (`read_source` / `search_notes`) or
   ships the loop tool-less *with the seam designed in* so the tool agent is additive (C PR2→PR3).
   The **facts panel** (claim→source) is a cheap, high-trust UI win — keep it.
3. **Shared hardening shipped EARLY on the existing rebuild (the weighted bug-fix).** Take
   **A's PR1**: `enforce_date_tokens` (R3 A+D, B-with-round-trip-guard), `entity_index.rebind`
   (R4 O1/H1), `promote_one` (R2 §6), each wired into a shared chokepoint so the *existing*
   "Rebuild page now" gets correct dates + people-links + promotion **before the new mode
   ships.** **Reject D's coupling of this to a 4-path refactor** — get the same fix at a
   fraction of the risk. (If a later cleanup wants `maintain`/`nightly` parity, that's an
   independent follow-up, not a prerequisite for the owner's feature.)
4. **One coherent panel + a plan to converge "Rebuild" and "Revise."** Differentiate the two
   entry points with intent-revealing copy now; design "Suggest revisions" so it can become
   *the* conversational editing surface and eventually replace rebuild's Guide step. Reuse the
   RebuildPanel primitives (`thread`, stable-`onClose` ref, `MarkdownDiff` vs BASE, footer
   composer) and replace the canned ack (`RebuildPanel.tsx:237`) with a real per-turn summary.

### Recommended sequencing so the owner sees value FAST
This ordering maximizes early, safe value and keeps each step shippable:

1. **PR1 — Shared hardening, loop-agnostic (A's PR1, NOT D's refactor).** `enforce_date_tokens`
   + `rebind` + `promote_one` into shared chokepoints. **Ships the owner's two literal bug-fixes
   on the existing rebuild in week one.** Lowest risk, highest standalone value. Ship even if the
   new mode slips.
2. **PR2 — Targeted-edit loop, tool-LESS, deterministic apply (B/C/E mechanism, C's tool-less
   sequencing).** BASE-preserved, carried-forward working draft, deterministic apply (pick the
   apply strategy in the hybrid bake-off: patch-ops vs section-splice — both beat full re-emit
   on T1), full re-emit as the *only* fallback. New panel reusing RebuildPanel primitives + the
   facts/summary bubble. **This is a complete, shippable Suggest mode.**
3. **PR3 — Truth-seeking tool layer (C's PR3), additive.** Add `search_notes`/`read_source`/
   `read_backlink` behind the two-transcript discipline, the facts panel, the firewall gates,
   and the transcript-safety regression suite. If it proves too costly/latent, PR2 stands alone
   — graceful degradation by construction.
4. **PR4 — e2e + coverage ratchet + UX-coherence pass** (entry-point copy, convergence plan
   toward replacing rebuild's Guide step).

The owner sees his bug-fixes at PR1, a usable conversational targeted-edit loop at PR2, and
the truth-seeking he literally asked for at PR3 — each independently shippable, risk rising
only as payoff rises.

---

## RT2 product ranking

1. **C (tool agent)** — only plan that delivers the owner's *full* stated intent (targeted
   edits **+** visible truth-seeking), with a clean tool-less fallback floor. Highest ceiling,
   acceptable floor. Its cost (transcript topology, latency) is real but its own sequencing
   contains it.
2. **A (minimal)** — wins decisively on **TTV1 and bug-fix delivery** (PR1 is the single most
   valuable deliverable in the bake-off) and lowest regression risk, but its full-re-emit loop
   is the *weakest* fit for "targeted edits / showed me the draft / truth-seeking." Best as the
   **hardening + fallback substrate**, not the whole answer.
3. **E (section-scoped)** — clean diffs from a real existing invariant; good for the common
   targeted-fix case. Loses on sweeping edits, stubs, and the intricate footnote coupling.
4. **B (structured patch)** — best clean-diff (T1) of any, but highest *incidental* complexity
   for a UX goal not framed as a patch problem, no truth-seeking, and an inert-turn failure mode
   that attacks the felt loop.
5. **D (shared-core first)** — strategically right about the codebase, wrong about the product:
   worst TTV1, highest regression risk (4 writer paths), gold-plates parity the owner never
   asked for, and arrives at A's UX after the most work. Its one genuinely good idea — share the
   hardening through one chokepoint — is captured far more cheaply by A's PR1.

**The winning hybrid is C's intent + C's sequencing, sitting on A's hardening PR1, using B-or-E's
deterministic targeted-edit apply for clean diffs — with a real plan to converge the two panels.**
