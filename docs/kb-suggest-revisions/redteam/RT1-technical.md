# RT1 — Red-team (technical / correctness / safety lens)

Adversarial review of plans A–E for the live "Suggest revisions" feature, against the
real tree on 2026-06-08. I verified every load-bearing code citation each plan makes
(see "Ground-truth checks" at the end); flagged misreads are called out inline. Severity:
**CRITICAL** = can corrupt an article, leak private data, or break cross-provider resume;
**MAJOR** = a real correctness/robustness gap that needs a designed fix before v1;
**MINOR** = a smaller hazard or under-specified detail.

The fixed design (BASE preserved, CONTEXT = sources + read-only backlinks, targeted-edit
LOOP, folded-in date/people/promotion hardening) is taken as given. I attack the
*mechanisms*, not the goal.

---

## 0. Ground-truth that reshapes the scoring (verified, all plans must reckon with)

1. **`_generate` wipes the draft every turn.** `rebuild_engine.py:295` `run.draft = ""`
   then streams a *full* body. **Any plan that "carries the working draft forward"
   (B, C, E) MUST NOT route its edit turn through `_generate`** — it must write a sibling
   generator and reuse only the *hardening tail* (`:362-398`). A (full re-emit) is the
   only plan that can reuse `_generate` verbatim. B/C/E are aware (B §1.7, C §1.1, E §3),
   but this is the single biggest "looks like reuse, isn't" trap and it deletes much of
   the "thin" claim for B/C/E.

2. **`finalize_rebuild` ALREADY runs `entity_index.rebuild(conn)` on every Accept**
   (`wiki_build.py:1718`), and `entity_index.rebuild` calls the **networked**
   `_sync_embeddings` (`entity_index.py:360`). So Accept is *already* doing slow,
   network-touching work inside the KB write lock today. Consequence: (a) the
   `promote_one`-on-the-request-path latency worry (B §5c, D §9.4) is largely moot — the
   lock already blocks on embeddings; (b) the "cheap rebind at session start" fix is still
   needed and correct, because the *draft-time* link offering (during the conversation)
   reads stale bindings — Accept-time rebuild is too late to help the live loop. Every
   plan gets O1 right in principle.

3. **`_mask_spans` does NOT mask `@t[...]` date tokens** (`wiki_build.py:1830-1831` masks
   fenced/inline code, `[[links]]`, `[^id]:` lines only). The deterministic linker can in
   principle insert a `[[link]]` inside a token's date arg. Probability is near-zero (a
   linkable leaf ≥4 chars colliding with an ISO date), but it means the **date-enforce →
   add-links ordering all plans specify is the *safe* one** (tokenize first, then link),
   and a hybrid must keep that order. MINOR but universal.

4. **The section regexes E leans on (`_SECTION_RE`, `wiki_guides.py:39`) match `^##\s+`
   anywhere — including inside ```code fences.** `validate_structure` tolerates this
   because it only counts/locates; a *splicer* that splits on it will mis-segment an
   article containing a fenced `## ` and corrupt the splice. This is E's and B's `section`
   op's sharpest latent bug (both flag it; neither's core regex handles it yet).

5. **`add_links_to_content` links only the FIRST match per target across the WHOLE body**
   (`:770` `break`). Correct for all plans *because* they all re-run it on the full body —
   but it means a section/patch plan can never assume "link the mention I just added"; the
   backstop may have already spent that target elsewhere. Not a defect, a constraint.

6. **The gather `search_notes` tool returns note titles to the model but only
   title+date in the streamed tool_result** (`rebuild_engine.py:185`), and excludes `kb/`
   hits (`:178`) but **does NOT exclude private Health/Finance notes** — `hybrid_notes`
   has no privacy filter (`search.py:36-76`). This is the load-bearing fact for the PII
   attack on Plan C (§4 below).

---

## Per-plan findings

### Plan A — Minimal full-re-emit extension

| Sev | Finding | Cite | Failure scenario |
|---|---|---|---|
| — | **Transcript safety: airtight.** Edit turn is `run_guide` with a different prompt; `tools=[]`, thinking on, assistant turn appended verbatim. No tool_use ever enters `run.messages`. The invariant at `rebuild_engine.py:9-15` holds by construction. | A §1.4 | n/a — this is the safest stance on axis 1. |
| MAJOR | **Multi-turn base drift / silent token loss is the *defining* risk and A's mitigations are weak.** Full re-emit regenerates the whole body each turn from a diluting transcript; the model can silently reword untouched prose or "tidy" `@t[age:…]` → "40". | R1 §6; A §9 | Turn 7 of a long session: model rephrases a paragraph the user never mentioned; the diff-first UI helps a vigilant user but A relies on the human catching it. The token-preservation guard (Option D) catches dropped tokens but NOT prose drift. |
| MAJOR | **Seed-without-LLM "synthetic done" diverges from the redraft/auto-continue invariants.** A seeds `[user prompt, assistant=BASE_BODY]` and emits a synthetic `done` with no stream (A §1.2 step 1). But `run_redraft` (`:491-501`) and auto-continue assume the transcript ends with a *generated* turn; a hand-inserted assistant turn that was never streamed has no `TurnEnd`/stop_reason and (for Anthropic) is a plain string, not `final.content` blocks. | `llm.py:404`; A §1.2 | If the user's first action is "Re-draft with more room" before any turn, `run_redraft` pops the hand-seeded assistant BASE turn and re-asks — silently discarding BASE. Needs an explicit guard. |
| MINOR | **`edit_summary` via a fenced `summary` block** competes with `_extract_talk`'s fence stripping and could be mis-stripped or leak into the body if the model malforms it. | A §2 | Model emits ```summary without closing fence → body contains the summary text. Low prob, but the parser must be as defensive as `_extract_talk`. |
| — | Backlinks-not-in-`run.sources` invariant correctly stated (A §4). Date/people/promotion hardening correctly placed in shared chokepoints. PR1-first sequencing genuinely de-risks. | A §4,§5,§10 | Strongest sequencing of any plan. |

**Verdict:** Safest on transcript + citation integrity; weakest on the *quality* of the
edit (drift/noise). The synthetic-seed/redraft interaction is a real bug to design out.

---

### Plan B — Structured edit-ops / patched working draft

| Sev | Finding | Cite | Failure scenario |
|---|---|---|---|
| — | **Transcript safety: good.** Ops are parsed from a fenced ```json block in assistant *text*, not tool_use (B §1.6). `run.messages` stays tool-free. Correct reading of the hazard. | B §1.6 | n/a. |
| MAJOR | **Bad-anchor mis-apply is the defining failure and B's "whitespace-normalized fallback" widens it.** The fallback (B §1.3 step 2) maps a whitespace-collapsed match back to the real span — but collapsing `\s+`→` ` across the *whole draft* can make a short `find` match a span that wasn't unique in the original, then "map back" to the wrong offsets. | B §1.3 | `find:"the plan"` appears once exactly but twice after whitespace collapse (a line break joined two occurrences); fuzzy hit lands on the wrong one, marked only `fuzzy:true`. A "fuzzy" silent mis-apply is exactly the corruption B claims to avoid. **Recommend dropping the whitespace fallback** or requiring it to preserve uniqueness. |
| MAJOR | **`section` op shares E's fenced-`##` hazard and B's matcher is under-specified.** "locate `^## <Heading>` … to next `^#{1,2} `" (B §1.3) will treat a `## ` inside a ```code fence as a boundary. B §9.7 flags it as a TODO but the core algorithm doesn't mask. | B §1.3, §9.7 | A section whose body contains a fenced shell example with `## comment` gets truncated at the fake heading; the splice drops half the section. CRITICAL if shipped unmasked; MAJOR given B acknowledges it. |
| MAJOR | **Partial-apply coherence + one-retry can ship a half-edited, internally-inconsistent draft.** B §1.5 applies good ops, fails bad ones, retries once, then surfaces. The working draft is "never corrupt, only partially edited" — but a delete-without-its-paired-insert is a *semantic* corruption the hardening tail can't catch. | B §1.5 | Op A deletes "He was diagnosed in 2019."; op B (insert the corrected date) fails on a bad anchor. Result: a fact silently *removed*, lint green, user may Accept. |
| MINOR | **Transcript K=3 truncation + "summarize older turns to one line" is new, untested machinery** that diverges from the proven redraft unwind; it can desync the model's mental draft from `working_draft`. | B §1.6 | After turn 6 the model reasons about a paragraph that was summarized away; emits an anchor that no longer exists → fail. Re-grounding helps but the summarization is a new failure surface. |
| — | Date-offset safety correctly argued (rewriters run on the final string, highlight via diff not raw spans — B §5, §7.3). Backlinks-grounding invariant correct. `edit_ops.py` is genuinely the most unit-testable surface of any plan. | B §5,§7.3,§8.1 | Best deterministic-testability story. |

**Verdict:** Cheapest per turn and the most *testable* core, but anchor matching is an
adversarial surface and the whitespace fallback + unmasked `section` op are live
corruption risks. The partial-apply semantic-corruption case is the one to fear.

---

### Plan C — Tool-using truth-seeking agent (two-transcript)

> Scrutinized hardest per the brief. C's whole safety case rests on §0's two-transcript
> topology. I verified the mechanism against `llm.py`.

| Sev | Finding | Cite | Failure scenario |
|---|---|---|---|
| — | **The two-transcript claim is *mechanically correct*.** Anthropic appends `final.content` (signed thinking + tool_use blocks) verbatim (`llm.py:404,408`); xAI appends an OpenAI dict with `tool_calls` (`:675-678`) — genuinely non-interchangeable. C's disposable fact-finding transcript runs `thinking=False` (so no signed-thinking block is ever produced) and is discarded; the persisted `run.messages` gets only plain `{"role","content":str}` turns (C §0). This *is* strictly safer than today's DRAFT stage, which persists signed thinking. The §8.1 "assert run.messages has no list-content / no tool_calls key" test is the right guard. | C §0,§8.1; `llm.py:404,675` | n/a IF the discipline holds — but see the CRITICAL below. |
| CRITICAL | **PII firewall leak via `search_notes`/`read_source` into a PUBLIC article.** `hybrid_notes` has no privacy filter (`search.py:36`); the gather precedent only strips `kb/` (`:178`), not Health/Finance notes. C's agent autonomously searches and reads the owner's notes to "ground salient facts," then weaves that *content* into the article body. C gates `read_source`/`read_backlink` on private titles only "when the target is public" (C §9, line 650) — but (a) that gate is mentioned once in Risks, not in the `_dispatch_tool` spec (§1.1), and (b) `search_notes` still returns **private note titles** to the model ("HIV results 2024", "Bankruptcy plan"), which can themselves be sensitive and can steer the agent's prose even without a read. | C §1.2, §9; `search.py:36`, `rebuild_engine.py:178` | User editing the public `kb/Reference/Diabetes` article says "add when I was diagnosed". Agent `search_notes("diagnosis")` → reads `kb/Health/Jeff` (or a raw Health note), writes "diagnosed 2019 [^s1]" citing a private note into a shareable article. The firewall that protects *links* (`add_links_to_content:733`) does **not** protect *prose facts the agent copies in*. This is the single most dangerous finding across all plans. |
| MAJOR | **`_repair_citation_titles` + a private source = a citation footnote to a private note.** Even if the agent reads a private note legitimately (private→private), C's `apply_edits.facts[{claim,source}]` and the footnote machinery can emit `[^s1]: [[kb/Health/...]]`. On a public target the dead-link neutralizer would drop a *link*, but a footnote whose target IS a real (private) article won't be neutralized — it resolves. | C §1.2, R4 §5 | A real `[[kb/Finance/...]]` citation survives onto a public page. Needs the same private-target refusal as the linker, applied to citations. |
| MAJOR | **Cost/model mismatch: C runs the tool loop on `run.model` (the pinned *synthesis* model)** (C §1.1 pseudocode `model = run.model`), whereas the existing gather agent deliberately uses `llm.model_for("cheap")` (`rebuild_engine.py:149`). Fact-finding on the expensive synthesis model, multiplied by `_TURN_MAX_ITER` per turn, is a real cost regression vs. the precedent C claims to mirror. | C §1.1; `rebuild_engine.py:149` | A chatty 10-turn session runs ~30 synthesis-model tool calls. Misreads the gather precedent's model choice. |
| MINOR | **Anchor non-uniqueness in `_apply_edits`** (reject on 0/>1 matches, C §1.3) is safe but shares B's "everything failed feels inert" UX; less acute than B because the agent can re-search. | C §1.3, §9 | Repetitive article → repeated rejects. |
| — | Transcript-drift is *better* than B/E: C re-derives nothing from the transcript (the run's `draft` is canonical, the transcript is plain summaries), so no stale-mental-copy corruption (C §9). The tool-less PR2 fallback (§10) is a genuine de-risk. | C §9,§10 | Cleanest staleness story. |

**Verdict:** Highest ceiling, highest risk — and the risk is the worst *kind* (silent PII
leak into shareable content), not just code fragility. The transcript topology is sound;
the **firewall is the disqualifier unless redesigned**: a public target must forbid
reading/searching/citing private notes at all, not just gate one tool in a Risks
paragraph. Fix that and C is viable; ship it as written and it leaks.

---

### Plan D — Shared writer-core refactor first

| Sev | Finding | Cite | Failure scenario |
|---|---|---|---|
| — | **Transcript safety: identical to A** — the loop is `run_guide`-shaped, full-article, tool-less (D §4c, explicit decision). Safe. | D §4c | n/a. |
| MAJOR | **Regression blast radius across 3 live paths is the real risk, and the "byte-identical" promise is fragile.** D re-routes `_generate`, `write_one`, AND `maintain_one` through `harden_draft`. But `write_one` runs `validate_structure` *between* revise passes (`wiki_build.py:919`) and its tail order differs subtly from `_generate`'s; D §1c hand-waves "keep the revise loop, call harden_draft once at the end." Any ordering drift changes batch-writer convergence. | D §1c; `wiki_build.py:894-940` | A maintained article's link backstop now runs at a different point than its revise loop expects, changing which dead links get neutralized vs. revised → different output for thousands of nightly articles. The characterization tests (D §7a) are the only thing standing between this and a corpus-wide regression. |
| MAJOR | **`maintain_one` newly gaining `add_links_to_content` is a behavior change sold as "for free."** Maintain deliberately never linked (R2 §1). Adding it changes every maintained article's output. That may be *desirable*, but it is NOT a no-op refactor and the golden snapshots will move — D §7a's "byte-identical except the one wiki_revise diff" claim is wrong here. | D §1c, §7a | The PR3 characterization snapshot for `maintain_one` changes for every article with a bare name → reviewer must re-bless a large diff, eroding the regression net's value. |
| MINOR | **Prompt-fragment extraction "byte-identical" is brittle to whitespace.** Substituting `{date_rules}` etc. must reproduce exact bytes incl. trailing newlines or model output shifts. D §5a/§7a guard with a golden test — adequate, but it's a lot of mechanism for the suggest feature to depend on. | D §5a | A stray newline in a fragment subtly changes `wiki_write` output corpus-wide. Caught by the golden test if it exists *before* the refactor. |
| — | **Best parity story.** `promote_one` in `finalize_rebuild` fixes live Accept AND nightly in one insert; date/people fixes land for rebuild+maintain before the feature ships. Characterization-first sequencing is the correct discipline for a refactor. | D §3,§8 | Highest value-to-existing-users. |

**Verdict:** Correct *engineering* instinct (fix the family, not the feature) and the
safest loop mechanism (= A). The danger is self-inflicted: it front-loads a multi-call-site
refactor whose safety rests entirely on characterization tests that must be written
perfectly and *first*. The suggest feature itself is trivial here — D is really "do the
hardening properly" with the loop as an afterthought.

---

### Plan E — Section-scoped splice

| Sev | Finding | Cite | Failure scenario |
|---|---|---|---|
| — | Transcript safety: fenced ```jbrain-edit block, tool-free — safe like A/D (E §0). | E §0 | n/a. |
| CRITICAL | **`split_article` will mis-segment any article with a ```code fence containing a `## ` line** (the core splicer keys off `_SECTION_RE`/`_FIRST_SECTION_RE`, which match inside fences — verified `wiki_guides.py:39`). E §1a says "reuse the linter's regexes"; the linter gets away with it, a *splicer* does not. | E §0,§1a; `wiki_guides.py:39` | A KB article with a shell/markdown example (`## Usage` inside a fence) — common for technical Reference pages — splits at the fake heading; `splice_section` replaces the wrong span → **silent body corruption**. E §9.7 claims "splice never corrupts" — that claim is false until fences are masked. Must mask before any splice. |
| MAJOR | **References-footnote coupling is E's sharpest design tax and the per-turn fold is the likeliest bug.** Markers are article-global, defs live in `## References`; E folds new defs via a `--- references` sentinel inside the same edit block (E §1c). This is bespoke, multi-step parsing layered on the section splice. | E §1c, §5d, §9.2 | Model edits "Early life", adds `[^s3]` but forgets the `--- references` block → orphan marker. E catches it as a lint warning (good) but the *atomic* promise ("one prose section + its footnotes") fails whenever the model doesn't emit the sentinel. Net: E's headline "localized edit" leaks back into whole-document citation reconciliation every time a citation changes. |
| MAJOR | **Articles with no clear sections degrade to whole-LEAD editing — losing the entire premise.** `section_keys` returns `["LEAD"]` (E §9.4); a stub or flat article becomes full-blob editing with full-rewrite cost and drift — i.e. Plan A, but via a more complex code path. | E §9.4 | A short or unstructured KB page gets none of E's benefits and all of A's drift risk, through E's heavier machinery. |
| MAJOR | **E mutates the *shared* Accept gate and `is_live`/`_LIVE`** to add `"suggesting"` (E §2b, touching `rebuild.py:345`, `rebuild_runs.py:24`). A/B/C/D reuse `ready`/`guiding` and touch neither. Widening a guard the classic rebuild also uses risks letting a classic rebuild be accepted from an unintended state. | E §2b; `rebuild.py:345` | A latent bug in any future state transition now has a wider accept gate to exploit; unnecessary coupling for no benefit (E could land in `ready` like everyone else). |
| MINOR | **Duplicate-heading keying (`Heading#2`) and "model must rename/merge via NEW"** is a real article shape (`_SECTION_RE` captures text; nothing forbids dupes) that adds model-facing complexity. | E §1a | Two `## Notes` sections → model can only safely target one; user confusion. |
| MINOR | **AKA-strip-then-reassert-on-Accept** means the AKA line is *absent during the entire conversation* and only reappears post-Accept (E §1c, §5c). The diff view shows a phantom AKA deletion every turn. | E §1c | User sees "Also known as" line vanish in the live draft, worries, can't tell it's reasserted on Accept. |

**Verdict:** The "section boundary is a free exact anchor" bet is *half* true — it's exact
but NOT free, because (a) the regex isn't fence-safe (CRITICAL corruption), (b) References
coupling drags whole-document reconciliation back in every citation edit, and (c) the
common cases (flat articles, cross-section edits, reorders) all fall back to worse-than-A.
E spends the most new code to handle the most edge cases.

---

## Cross-plan ranking — technical risk (lowest risk first)

1. **A (minimal full re-emit)** — lowest *correctness/safety* risk. No anchor matching, no
   section splice, no tools, no refactor. Pays in token cost + drift, both visible in the
   diff. Its two real bugs (synthetic-seed vs. redraft; summary-fence) are small and local.
2. **D (shared-core)** — same safe loop as A, *plus* the best hardening/parity. Higher risk
   only because of the refactor blast radius, which is *controllable* with characterization
   tests. If the team has the discipline for characterization-first, D's loop == A's loop
   with better bones.
3. **B (edit-ops)** — cheapest/most-testable core, but anchor matching is an adversarial
   surface; the whitespace fallback and unmasked `section` op are live corruption risks,
   and partial-apply can silently drop a fact. Viable with the fallback removed and `section`
   fence-masked.
4. **E (section-splice)** — the fence-unsafe splicer is a CRITICAL corruption bug; the
   References coupling and flat-article degradation undercut the premise; it touches the
   shared Accept gate unnecessarily. Most code for least robustness.
5. **C (tool agent)** — highest ceiling, but carries a **CRITICAL PII leak** into shareable
   content that the plan under-specifies, plus a cost/model misread. The transcript topology
   is genuinely sound; the firewall is not. Disqualified for v1 *as written*; redeemable only
   with a hard "public target ⇒ no private search/read/cite" firewall.

---

## Fatal flaws that should disqualify a mechanism for v1

- **C, as written: autonomous private-note search/read into a public article.** A tool
  agent that can pull *any* note's content and weave it into prose breaches the PII firewall
  the rest of the system carefully maintains around *links*. The firewall must be a
  first-class, tested gate on every tool (`search_notes` must filter private notes, not just
  `kb/`; `read_source`/`read_backlink`/citation emission must refuse private targets when the
  edited page is public), not a Risks-section sentence. Without that, C cannot ship.
- **E (and B's `section` op): splitting on `^##` without masking code fences.** Until the
  splicer/section-matcher masks fenced code, it can silently corrupt any article containing a
  `## ` inside a fence. This is a v1 blocker for the section mechanism specifically.
- **B: the whitespace-normalized fuzzy fallback.** A "fuzzy:true" silent mis-apply is the
  exact corruption B's design claims to forbid. Drop it, or constrain it to uniqueness-
  preserving matches only.

None of these disqualify the *plan's other ideas* — they disqualify the specific risky
mechanism within it.

---

## Surviving ideas worth keeping from each plan

- **From A:** the full-article re-emit loop as the *safe default* (no anchor/splice risk);
  the diff-first UI as the legibility answer to drift; PR1-first shared-hardening sequencing
  (ship the date/people/promotion fixes to existing rebuild before the new mode).
- **From B:** `edit_ops.py` as a *pure, exhaustively-unit-tested* module is the best
  testability story; the **exact-match-only, reject-on-ambiguous** discipline (no guessing);
  running all deterministic rewriters on the final patched string and computing highlights
  via diff (not raw op offsets). Keep the *exact* op; drop the fuzzy fallback.
- **From C:** the **two-transcript discipline** (tool_use lives only in a disposable
  thinking-OFF transcript; persisted `run.messages` stays plain) is the correct way to add
  tools *if* tools are ever added — and the `assert run.messages has no block-content/no
  tool_calls` regression test. The `facts[{claim,source}]` surface is good UX for trust.
  The tool-less PR2 fallback is the de-risking pattern every plan should adopt.
- **From D:** factoring the hardening tail into one `harden_draft` shared by every writer
  path; `promote_one` inside `finalize_rebuild` (fixes live + nightly in one insert);
  **characterization-tests-first** for any refactor; the `{date_rules}` fold into
  `wiki_revise`.
- **From E:** the explicit, named treatment of the three document-global hazards
  (LEAD, AKA, References) — *every* plan must handle these even though E over-invests in
  section scoping; E's enumeration is the best checklist. The `NEW:`/`LEAD` sentinels are a
  clean way to address "where does this go" without offsets.

---

## Guidance for the hybrid round

**Safest mechanism for v1:** full-article re-emit (A's loop) built on D's shared
`harden_draft` core. This combination has zero anchor/splice/fence corruption surface and
zero new transcript-resume risk, and it delivers the hardening fixes to existing rebuild
too. Use the diff-first UI to make drift legible. Layer a structured *edit-ops* mode
(B's pure `edit_ops.py`, exact-match-only, fuzzy fallback removed, `section` fence-masked)
**behind the same panel later** as a cost optimization, once the safe loop ships.

**What must be true for each mechanism to be correct:**
- *Full re-emit (A/D loop):* fix the synthetic-seed/redraft interaction (don't let
  `run_redraft` pop a hand-seeded BASE turn); keep the token-preservation guard (Option D)
  AND add a prose-drift signal (e.g. warn when an *unmentioned* section's body changed).
- *Edit-ops (B):* exact + uniqueness-preserving matching only; mask code fences before any
  `section` op; never ship a delete whose paired insert failed (treat a delete+insert pair
  as atomic or surface it loudly).
- *Tool agent (C):* a hard, tested firewall — `search_notes` filters private notes;
  read/cite of private targets is refused whenever the edited page is public; two-transcript
  assertion in CI; run the tool loop on the *cheap* model, not the synthesis model.
- *Section splice (E):* fence-mask the splitter; don't widen the shared Accept gate; accept
  that flat/cross-section articles fall back to full re-emit.

**Correctness requirements ALL hybrids MUST meet (non-negotiable):**
1. **Transcript stays tool-free** in `run.messages`, or tools live ONLY in a disposable
   thinking-OFF transcript (C's pattern). Never resume a transcript carrying both signed
   thinking and tool_use (`rebuild_engine.py:9-15`; `llm.py:404,675`). Add the
   "no block-content / no tool_calls in `run.messages`" regression test regardless of stance.
2. **Re-validate on the FULL article every turn** — citations/markers↔defs, lead, AKA, PII
   firewall, dead-links, dates, people-links — never on a section/patch in isolation
   (References defs are article-global; `validate_structure`/`citation_issues` need the
   whole body).
3. **Backlinks are read-only context, NEVER in `run.sources`** (so `_repair_citation_titles`
   can't retarget them; `rebuild_engine.py:373`). Every plan states this — keep it.
4. **PII firewall on the way IN, not just OUT.** The existing firewall guards *links/targets*
   (`add_links_to_content:733`, `_link_articles:553`). A conversational edit can introduce
   private *prose/facts/citations* into a public article — a firewall the current code does
   NOT provide. Any hybrid that searches/reads/cites notes (especially C) must add an
   inbound private-content gate keyed on the edited page's domain.
5. **Date-enforce BEFORE add-links** (tokens aren't masked by `_mask_spans`), and
   `enforce_date_tokens` only ever *produces* tokens via `clock` round-trip — never touches
   expansion semantics, so `time_tokens.json` and the `clock`↔`time.ts` byte-for-byte pin
   stay untouched (`test_api.py:1209-1211`).
6. **`promote_one` must be idempotent** on the Accept path (Accept can be retried after a
   409; `surface_aliases`/`_apply_aka_line` already rebuild the AKA line each call — verify
   the rest). Place it in `finalize_rebuild` so live + nightly both inherit it.
7. **Cheap entity rebind at session start** (no `_sync_embeddings`), reusing
   `_link_articles` (already private-safe, `entity_index.py:553`) — needed because the live
   loop reads bindings during the conversation, before Accept's full rebuild.

**Hardest to test deterministically (rank):** C (tool-loop ordering + firewall + two-
transcript invariants + cross-provider shapes) > E (split/splice/fold + fence masking +
References reconciliation) > B (anchor matching, but `edit_ops.py` is pure so it's
*tractable*) > D (refactor — characterization tests make it deterministic *if* written
first) > A (full re-emit through the existing, already-tested `_generate` tail). The DoD
coverage floors are reachable for all; C is the one whose firewall branches are easiest to
leave under-tested.

---

## Ground-truth checks performed (code verified, not taken from the plans)

- `rebuild_engine.py:9-15` (transcript invariant), `:295` (`run.draft=""` wipe), `:320-321`
  (`tools=[]`, thinking), `:362-398` (hardening tail), `:455-461` (`run_guide` steer),
  `:466-503` (`run_redraft` unwind), `:149` (gather uses `model_for("cheap")`), `:178`
  (gather strips `kb/` only).
- `llm.py:404` (Anthropic appends `final.content` = signed thinking + tool_use blocks),
  `:408` (tool_use blocks live in `final.content`), `:675-678` (xAI appends OpenAI dict with
  `tool_calls`) — confirms C's non-interchangeability claim and the two-transcript necessity.
- `wiki_build.py:711-781` (`add_links_to_content`, PII self-guard `:733`, first-match `break`
  `:770`), `:1681-1721` (`finalize_rebuild` ALREADY calls `entity_index.rebuild` `:1718`),
  `:1817-1833` (`_mask_spans` does NOT mask `@t[...]`).
- `entity_index.py:360` (`_sync_embeddings` networked, inside `rebuild`), `:553`
  (`_link_articles` excludes private) — confirms cheap-rebind feasibility + Accept already
  pays the embeddings cost.
- `search.py:36-76` (`hybrid_notes` has no privacy filter) — the basis for the C PII finding.
- `wiki_guides.py:39-48` (`_SECTION_RE` matches inside fences; `_REL_TIME_RE` won't match a
  tokenized age) — confirms E/B fence hazard and the Option-B round-trip guard's soundness.
- `rebuild.py:322-383` (Accept gate `status in ("ready","guiding")`, staleness inside lock)
  — confirms E's gate-widening is a real shared-handler change others avoid.
- Verified `promote_one`, `enforce_date_tokens`, `rebind*`, and the backlink helpers do NOT
  exist yet (grep) — all plans correctly scope them as new.
- `RebuildPanel.tsx:15-16` (Stage/Phase unions) — confirms the frontend fork points.

No plan grossly misreads the code. Notable misreads flagged above: **C** uses `run.model`
where the precedent uses the cheap model (cost); **D** calls the `maintain_one` link-backstop
addition "byte-identical" when it is a real output change; **E** claims the splice "cannot
corrupt" when its regex is not fence-safe.
