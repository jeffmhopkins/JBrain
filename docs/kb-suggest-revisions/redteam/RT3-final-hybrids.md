# RT3 — Final red-team: the three hybrids (H1, H2, H3)

Adversarial head-to-head of the three hybrid plans for "Suggest revisions," verified
against the real tree on 2026-06-08. The job: confirm each hybrid actually fixes the
earlier fatal flaws (not just claims to), rate intent delivery against the owner's
yardstick, surface residual risk, and deliver ONE recommendation plus the open decisions
the owner must make. Harsh, specific, decisive.

**The yardstick (RT2, the owner, verbatim):**
> "I talk to the AI, it makes changes to the article, shows the article draft, I talk
> some more, it edits more etc until I accept. the AI should be **truth seeking for
> salient facts**, my input will be for guiding structure or bad formatting, or
> correcting AI assumptions."

Felt-experience tests: **T1** = "showed me the draft" is a *clean, low-noise* view;
**T2** = truth-seeking is an *active verb* (the AI goes and finds a salient fact).

---

## 0. Ground-truth re-verified (the facts the verdict turns on)

I re-read the code rather than trusting the plans. All three plans' core citations hold:

- **`_generate` wipes `run.draft = ""`** at `rebuild_engine.py:295`, then streams a *full*
  body; auto-continue lives at `:309-363`; the hardening tail is `:366-398`. Confirmed.
- **`run_redraft` pops a trailing assistant turn UNCONDITIONALLY** at
  `rebuild_engine.py:491-492`, then unwinds CONTINUE_PROMPT scaffolding at `:496-501`.
  A hand-seeded BASE assistant turn *would* be popped. The bug is real; all three address
  it. Confirmed.
- **`hybrid_notes` has NO privacy filter** (`search.py:36-82`) — returns `{id,title,slug}`
  for any matching note. Gather strips only `kb/` and uses `require_kb_ingest=True`
  (`rebuild_engine.py:177-178`); neither is a privacy gate. Confirmed.
- **The privacy predicates are ALL title-prefix-only.** `is_private_title`
  (`wiki_guides.py:148-161`) matches `kb/health/` or `kb/finance/`; `is_health_title`
  (`:127-136`) matches `kb/health/`; `domain_for_title` (`:103-117`) parses `kb/<Domain>/`.
  **None of them can classify a raw note** (an `entry`/`daily` note has no `kb/` prefix).
  Confirmed — this is the load-bearing fact for the H2/H3 firewall critique.
- **Raw notes carry NO privacy column.** `db.py:914-926`: `kb_ingest` is the *only*
  per-note governance flag, and it gates KB-source eligibility, not privacy. Confirmed.
- **`vitals` has a nullable `note_id`** (`db.py:1012-1018`); `encounters`/`vitals` are
  identity-keyed clinical extractions. A note *can* be joined to vitals **only if it
  produced an extracted vital** — a free-text daily log that merely mentions a diagnosis
  has no vitals row. Confirmed — H2's "health-table" classifier is partial.
- **`note_ids_for_name`** (`entity_index.py:671-693`) and `entity_mentions` exist, so
  H2's "entity-linked-to-a-private-person" inference is *implementable*, but it depends on
  the entity index having resolved that person to a *private article_title* — itself a
  heuristic with gaps. Confirmed.
- **`finalize_rebuild` already runs `entity_index.rebuild` on Accept** (`wiki_build.py:1718`),
  which calls networked `_sync_embeddings` (`entity_index.py:360`) inside the lock — so the
  `promote_one` latency worry is moot and the cheap session-start rebind is genuinely needed.
  Confirmed for all three.
- **llm.py topology:** Anthropic appends `final.content` (signed thinking + tool_use
  blocks) verbatim at `llm.py:404`; xAI appends an OpenAI dict with `tool_calls` at
  `:676`. Non-interchangeable. Confirmed — the two-transcript discipline H2 relies on is
  mechanically sound *if* the thinking-off + disposable discipline holds.

**Net:** no hybrid grossly misreads the code. The decisive differences are about *which
risk each one chooses to take on*, not about factual errors.

---

## 1. Non-negotiables scorecard (PASS / PARTIAL / FAIL, with evidence)

RT1's seven: (1) transcript tool-free / tools only in disposable thinking-off transcript;
(2) re-validate the FULL article every turn; (3) backlinks read-only, never in
`run.sources`; (4) inbound PII firewall keyed on the edited page's domain; (5) date-enforce
before add-links, tokens only via `clock` round-trip; (6) `promote_one` idempotent on
Accept; (7) cheap session-start entity rebind without `_sync_embeddings`.

### H1 — Safe core, layered capability

| # | Verdict | Evidence |
|---|---|---|
| 1 | **PASS** | `tools=[]`, thinking on, identical to Guide (`rebuild_engine.py:320-321`); seeded BASE turn is plain text; ships the "no block-content / no `tool_calls`" regression test (§7.2). Airtight by construction. |
| 2 | **PASS** | `harden_draft` runs on the whole re-emitted body every turn (§2). Full re-emit means there is *no* patch-in-isolation path. |
| 3 | **PASS** | Backlinks injected as a labeled prompt block, never added to `run.sources`/`run.known`; tested (§4.3, §7.2). |
| 4 | **PARTIAL** | v1 has **no autonomous inbound channel** (no tool search; new notes only via user-gated `run_gather`), which is the *right* way to be leak-free. But H1 *also* asserts a "domain-keyed inbound citation gate" that neutralizes private-titled citations on a public target (§3). That gate only catches `[[kb/Health/...]]`-shaped citations — i.e. it inherits the same title-only blind spot. It is honest that v1 "almost never hits this." The seam is built; the firewall is real but **shallow** (titles, not raw-note prose) — acceptable *because v1 has no path that reads raw-note prose*. |
| 5 | **PASS** | Order-of-ops step 1 (dates) precedes step 3 (links); `enforce_date_tokens` round-trips only; `time_tokens.json` twin pin asserted (§2a, §7.2). Matches verified `_mask_spans` behavior. |
| 6 | **PASS** | `promote_one` in `finalize_rebuild` after `entity_index.rebuild`; run-twice-identical test; 409-retry safe (§2b). |
| 7 | **PASS** | `rebind_entities` = `_link_articles` + owner-alias fold, no `_sync_embeddings`; reuses private-safe `_link_articles:553`; called in `run_suggest_start` AND `run_draft` (§2c). |

**H1 score: 6 PASS, 1 PARTIAL (#4 — shallow but sound because no raw-prose inbound path).**

### H2 — Firewalled truth-seeking with deterministic apply

| # | Verdict | Evidence |
|---|---|---|
| 1 | **PASS** | Fact-finding runs `thinking=False` on a disposable `ff` list, discarded each turn; `run.messages` gets only plain pairs; CI asserts no list-content / no `tool_calls` / no thinking block (§1.3, §9.2). Topology verified sound (`llm.py:404,676`). Strongest tool-safety story of the three. |
| 2 | **PASS** | `harden_draft` runs on the final applied string every turn (§3.3, §5). |
| 3 | **PASS** | `run.backlinks` separate; `source_titles` derives only from `run.sources` (§6.3). Tested. |
| 4 | **PARTIAL → the central risk.** The firewall *architecture* is correct (gates `search_notes` titles, `read_source`/`read_backlink` prose, citation emission, plus a user-approval candidate gate, all keyed on target domain — §2). But it **rests on a heuristic note-sensitivity classifier over un-flagged raw notes** (§2.2), and the verified code shows that classifier is leaky: (a) title-prefix only catches `kb/Health//kb/Finance/` — useless for raw notes; (b) the vitals/visits join catches only notes that produced an *extracted clinical row*, not a free-text log mentioning a diagnosis (`db.py:1012-1018`); (c) the entity-private inference depends on the entity index having resolved the mentioned person to a private `article_title`. **A free-text daily entry — "told Dr. Lee about the HIV result" — that the entity index hasn't bound to a private person, with no vitals row, classifies as PUBLIC and leaks on a public target.** H2 mitigates with default-deny *for classified-private notes* and the human candidate gate — but default-deny does nothing for a **false negative** (a note the classifier never flagged): it sails through as a candidate fact and, on a public target, can be Included by an owner who can't see it's sourced from sensitive prose. This is a *narrower* version of the exact CRITICAL RT1 raised against C. **Not FAIL** (the architecture, default-deny, and human gate materially reduce it), but it is the single largest residual risk in the entire bake-off. |
| 5 | **PASS** | Order in §5; round-trip only; twin pin asserted (§9.2). |
| 6 | **PASS** | `promote_one` in `finalize_rebuild`; idempotent; tested (§5, §9.2). |
| 7 | **PASS** | `rebind_entities`, no embeddings; reuses `_link_articles:553` (§5). |

**H2 score: 6 PASS, 1 PARTIAL (#4 — the firewall is well-designed but classifier-limited;
false-negative leak is plausible, not hypothetical).**

### H3 — Phased convergence

| # | Verdict | Evidence |
|---|---|---|
| 1 | **PASS** | Loop runs `tools=[]`, thinking on (`:320`); candidate-fact pass is a *server-side deterministic search*, NOT a model tool, so nothing tool-shaped touches `run.messages`. Regression test ships in P2 (§3b). Cleanest #1 of the three — no tool transcript exists to mismanage. |
| 2 | **PASS** | `harden_draft` on the whole re-emitted body every turn; P3 ops also harden the whole string (§2a, §6b). |
| 3 | **PASS** | `backlink_titles` → prompt-only block; never in `run.sources`/`run.known` (§3d). Tested. |
| 4 | **PASS (for what it ships).** This is H3's quiet win. The inbound surface is **deliberately minimized**: backlink *bodies are never read into the prompt* (titles only, gated), and the candidate-fact pass is a *server-controlled* `hybrid_notes_public` search that filters with `is_private_title`/`is_health_title`/`domain_for_title` *before anything reaches the model or the UI* (§4b, §5). Because the model never receives a private note's prose to paraphrase and never autonomously reads, the **attack surface is structurally smaller than H2's**. BUT: the *same title-only classifier limit* applies — `hybrid_notes_public` filtering on `is_private_title` cannot catch a raw entry-note that mentions a diagnosis but isn't filed under `kb/Health/`. So a candidate-fact snippet *could* still surface sensitive prose from a mis-classified raw note. H3 is **less exposed than H2** (no autonomous read; server controls the snippet; user-gated) but **shares the same root blind spot** and does not claim to solve it — it honestly scopes the candidate surface to *publishable* sources and leans on the title firewall. PASS for the surface it ships; the residual is a MAJOR (see §3). |
| 5 | **PASS** | Order in §2a; round-trip only; twin pin asserted (§2b, §9). |
| 6 | **PASS** | `promote_one` in `finalize_rebuild`; idempotent; tested (§2d, §9). |
| 7 | **PASS** | `entity_index.rebind`, no embeddings; reuses `_link_articles:553`; called at `run_draft` + suggest start (§2c). |

**H3 score: 7 PASS — but #4's PASS is "for the surface it ships"; the candidate-fact
snippet still inherits the title-only classifier limit (residual MAJOR, not a non-negotiable
FAIL, because the surface is server-controlled, user-gated, and never reads raw prose into
the model).**

**Scorecard summary:** H1 6P/1Partial; H2 6P/1Partial; H3 7P (with a noted residual on #4).
No hybrid FAILS a non-negotiable. The differences are in *how much firewall risk each takes
on*: **H1 takes none** (no inbound prose channel in v1), **H3 takes a small, server-gated,
user-approved amount**, **H2 takes the most** (an autonomous agent behind a heuristic
classifier).

---

## 2. Intent delivery (T1 clean draft, T2 active truth-seeking)

| | T1 — clean "showed me the draft" | T2 — active truth-seeking |
|---|---|---|
| **H1** | **3/5.** Full re-emit every turn; diff-first UI papers over it. The draft *is* a re-generation; the owner reads a diff, not a quiet article. Honest about this being its weakness. | **1/5.** Explicitly deferred. The AI cannot go find a fact mid-conversation without the user invoking regather. Delivers the owner's two *literal* bug complaints but not the *first-named* requirement. |
| **H2** | **4/5.** Deterministic exact-match ops → untouched spans byte-identical → surgical diffs. The cleanest of the three *when ops place*. Full-re-emit fallback covers misses (never inert). | **5/5.** The only hybrid that ships the literal intent: the AI *goes and finds* a salient fact mid-conversation, visible as tool steps, surfaced as a candidate chip with its source. This is exactly the owner's quote. |
| **H3** | **3.5/5.** P2 ships full re-emit + diff-first (same T1 profile as H1); P3 adds B's exact ops for true byte-identical untouched spans. The *felt* targeted-edit experience arrives in P2 via the diff; the real clean diff arrives in P3. | **3/5.** A *bounded* truth-seeking affordance: server searches, filters, and surfaces a candidate fact for the owner to Include. It is genuinely active (the system finds and offers a fact), but more **passive and mechanical** than H2's agent — it can't reason across multiple notes, follow a lead, or read a source's body to extract a nuanced fact. It satisfies the *spirit* of T2 (visible, sourced, gated) without H2's reach or risk. |

**The intent verdict.** The owner named truth-seeking *first*. **H1 fails that test
outright in v1** — it is a real, safe, valuable product, but it is not the feature the owner
described; it is "fix my two bugs + a safe talk-to-the-article loop." **H2 delivers the
literal intent** and pays for it in firewall risk. **H3 delivers most of the intent's
*value*** (the AI surfaces a fact it found, sourced, for approval) at a fraction of H2's
risk — the owner's "it went and found it in my notes and showed me" moment is preserved;
what's lost is the autonomous multi-hop agent feel.

---

## 3. Correctness of the claimed fixes

### The `_generate` draft-wipe constraint

- **H1: correct and the cleanest.** It does NOT carry a draft forward at all — every turn
  re-emits the full article, so `run.draft=""` (`:295`) followed by a full re-stream is
  *exactly the intended behavior*. BASE lives in the transcript (seeded `[user, assistant=BASE]`)
  + prompt discipline, not in a reused draft string. This is the one stance for which the
  wipe is a non-issue. **Verified sound.**
- **H2: correct but takes on more.** It writes a sibling generator and keeps `run.draft`
  *canonical*, mutated only by deterministic `apply_ops` — never re-derived from the
  transcript. This sidesteps the wipe entirely. **But** by not routing through `_generate`,
  H2 must **re-implement** auto-continue/truncation handling that `_generate` currently owns
  (`:309-363`). H2's §3.2 specifies a single-retry of failed ops and a full-re-emit fallback,
  but the **full-re-emit fallback path itself needs the auto-continue machinery** (a long
  article can truncate at the cap). H2 says the fallback "streams `content_delta` which the
  panel already handles" but does **not** spell out that the fallback generator must
  reproduce `_generate`'s `range(2)` auto-continue + `_join_continuation` + the redraft
  budget escalation. **Gap: under-specified truncation handling in the carry-forward
  generator.** Not fatal, but it is real new machinery sold as "reuse the tail."
- **H3: correct, explicitly owns the gap H2 glosses.** §3a writes `_generate_suggest` and
  *names* the two deltas it owns: threading `base_tokens`, and **not** running the
  auto-continue CONTINUE_PROMPT scaffolding the way `_generate` does without the redraft
  guard. H3 is the only plan that explicitly flags that the auto-continue + redraft
  interaction is the thing to get right in the sibling generator. **Most honest of the
  three about the carry-forward generator's real cost** — though it still must implement it.

### The `run_redraft` seed bug

- **H1: `_seed` marker on the seeded assistant turn; `run_redraft` skips a `_seed` turn**
  (§1.4). Concrete, tested fail-before/pass-after. **Sound.** One nit: the guard must also
  protect the auto-continue unwind at `:496-501`, which H1 mentions.
- **H2: avoids the bug by construction** — it never plants a fake assistant turn; the seed
  stores `run.draft` directly and keeps `run.messages` empty until the first real turn
  (§1.2). **The cleanest fix: there is no seed turn to pop.** This is genuinely superior to
  H1's marker approach.
- **H3: `seeded_base` flag + guard `run_redraft` to refuse to pop the seed turn** (§3c).
  Equivalent to H1; tested. Sound, but inherits H1's "plant a turn then guard it" pattern
  where H2 simply doesn't plant one. **H2's approach is the best on this specific bug.**

**Fix-correctness verdict:** H1 cleanest on the wipe (no carry-forward to break); H2
cleanest on the redraft seed bug (no seed turn); H3 most honest about the sibling
generator's auto-continue cost. All three are *correct*; H2/H3 take on real new generator
machinery that "reuse the tail" undersells.

---

## 4. Regression surface

All three correctly **reject D's 4-path refactor** and scope `harden_draft` to the live
chokepoints only:

- **H1** routes exactly **two** call sites through `harden_draft` (`_generate` + the new
  suggest start/turn); leaves `write_one`/`maintain_one` untouched. Smallest surface.
- **H2** refactors `_generate`'s tail to call `harden_draft` (one existing call site
  re-routed) + the new suggest engine. Same low surface.
- **H3** routes **only `_generate`** through `harden_draft` in P1 (`base_tokens=None` →
  identical behavior), explicitly defers maintain/nightly parity. Same low surface.

**Characterization-test claim credibility:** all three pin the `_generate` tail with the
`_drain` + `FakeProvider` recipe (`test_rebuild_engine.py:36-128`) and assert byte-identical
output after extraction. Because the surface is **one re-routed call site** (not D's three),
the snapshot is small and stable — **credible for all three**, materially more so than D's
"byte-identical across `write_one`/`maintain_one`/`_generate`" promise RT1 called fragile.

**Residual regression risks:**
- All three add fields to `RebuildRun` and a `kind` branch; all keep the Accept gate and
  `_LIVE` unchanged (correctly avoiding E's gate-widening). Low risk.
- **H2 alone** adds `note_privacy.py` (a new classifier touching the entity index + health
  tables) and `edit_ops.py` (a new apply engine) — two new failure families that the
  existing test suite has *no* coverage for. Its regression surface within *new* code is the
  largest, even though it doesn't touch existing paths.
- H3's P3 adds the same `edit_ops.py` risk but defers it past v1, so v1's regression surface
  equals H1's.

---

## 5. Effort / sequencing realism

| | Claimed | Honest read |
|---|---|---|
| **H1** | ~1.5–2 weeks (3 PRs) | **Most honest.** PR1 (2.5–3.5d) ships the bug-fixes standalone; PR2 (3–4d) the engine; PR3 (3–4d) frontend+e2e+ratchet. The firewall is a shallow citation gate (cheap). No new classifier, no ops engine. The estimate already loads the DoD tax. **Credible.** |
| **H2** | ~11 days discounted (4 PRs) | **Optimistic, hides the firewall-test cost.** PR3 ("truth-seeking tool layer," 4d) bundles `note_privacy.py` (three classifiers + domain matrix), `_dispatch_tool` firewall, candidate-facts, the disposable-transcript discipline, AND the firewall/citation/transcript-safety suites. RT2 already priced C's comparable scope at ~10d and called it optimistic; H2 adds the classifier + ops + candidate gate. The **firewall test suite alone** — proving no leak across title/entity/health-table classifiers × public/private targets × search/read/cite surfaces — is days, not hours. **Real number: ~14–16 days.** H2's "if PR3 is too costly, PR2 stands alone" is its honest escape hatch. |
| **H3** | ~4.5–5.5 weeks (3 phases, 9 PRs) | **Most complete, and honest *because* it's the biggest.** It prices the candidate-fact seam + inbound firewall as a *separate* PR4 (~2.5d) rather than bundling it, and defers ops/convergence to P3. The total is large but each phase is independently shippable. **Credible, and the only plan that doesn't hide the firewall in a bundled PR.** The risk is that P3 (1.5 weeks of optimize+converge) never gets prioritized — which is *fine*, because P1+P2 stand alone. |

**Who hides cost:** H2. Its single discounted number (11d) understates the firewall test
suite and the carry-forward generator's truncation handling. **Who's most honest:** H1 (by
being smallest) and H3 (by phasing and pricing the firewall separately).

---

## 6. Convergence & duplication

- **H1 and H3-P2 are nearly the same product.** Both ship A's full-re-emit loop on D's
  `harden_draft`, diff-first UI, `_seed`/`seeded_base` redraft guard, read-only backlinks,
  the shared hardening PR first. The **only material difference** is the truth-seeking
  affordance: H3 adds a server-side firewalled candidate-fact surface that H1 explicitly
  defers to "LATER-2." **H3 ≈ H1 + the candidate-fact surface + an explicit phase plan.**
  This is the most important finding of the bake-off: the two "safe" hybrids converge, and
  the candidate-fact surface is the entire delta.
- **H3-P3 ≈ H1's LATER-1** (B's pure exact-match edit-ops behind the same panel) **+ the
  Guide-loop convergence.** Same mechanism, same RT1 constraints (exact-only, fence-mask,
  fall back to full re-emit). Identical.
- **H2 is the genuine outlier**: it front-loads B's ops *and* C's agent *and* the full
  classifier into v1, where H1/H3 defer ops and (H1) defer truth-seeking entirely or (H3)
  ship a bounded server-side version.

**The superior fourth combination the three missed (partially):** **H3's phasing with H2's
candidate-fact *human gate* but a HARDENED classifier — and, critically, with the
truth-seeking layer gated behind a real fix to the raw-note privacy gap.** None of the three
proposes the one thing that would actually de-risk inbound truth-seeking: **a persisted
per-note sensitivity flag** (a `notes.sensitivity` column or an explicit owner "this note is
private" affordance), instead of inferring sensitivity from titles/entities/health-tables at
read time. Every firewall in H2 and H3 is a *heuristic over un-flagged data*; the durable
fix is to *flag the data*. The best plan is **H3's phasing + H2's human-approval gate +
H3's server-controlled (no-autonomous-read) discovery, with truth-seeking explicitly
blocked until a real note-sensitivity signal exists** — i.e. H3 as written, with the
candidate-fact surface treated as the riskier, separately-gated phase it deserves to be, and
a note-sensitivity flag added as its prerequisite.

---

## 7. Residual CRITICAL / MAJOR risks per hybrid

**H1**
- *MAJOR — under-delivers the owner's first-named requirement.* No active truth-seeking in
  v1. This is a product risk, not a correctness one, but the owner put truth-seeking *first*.
- *MAJOR — multi-turn prose drift.* Full re-emit can silently reword untouched prose; the
  diff-first UI + Option-D token guard help but the human is the backstop. Inherited by H3-P2.
- *MINOR — the v1 "inbound citation gate" is title-only* (same blind spot as everyone), but
  harmless because v1 has no raw-prose inbound path.

**H2**
- *CRITICAL (residual) — false-negative PII leak.* The note-sensitivity classifier is a
  heuristic over un-flagged raw notes; a mis-classified sensitive entry can surface as a
  candidate fact (or, if Included, land) on a public article. Default-deny protects
  *classified* notes but does nothing for a *false negative*. The human approval gate is the
  last line — and the owner may not realize a candidate is sourced from sensitive prose. This
  is a narrower form of the exact CRITICAL that disqualified C. **It is mitigated, not
  eliminated.**
- *MAJOR — under-specified truncation/auto-continue in the carry-forward generator.* The
  sibling generator must reproduce `_generate`'s auto-continue/redraft machinery; H2 glosses
  this as "reuse the tail."
- *MAJOR — largest new-code regression surface* (classifier + ops engine + tool loop = three
  new failure families) and the most optimistic effort estimate.

**H3**
- *MAJOR — candidate-fact snippet inherits the title-only classifier limit.* Server-side
  filtering on `is_private_title` cannot catch a raw entry mentioning a diagnosis; a snippet
  from a mis-classified note could surface. **Materially lower than H2** (server-controlled,
  no autonomous read into the model, user-gated, only *publishable*-sourced snippets
  surfaced) but the same root gap. Owner must accept this or gate P2's candidate surface
  behind a real note-sensitivity signal.
- *MAJOR — prose drift in P2* (inherited from full re-emit; same as H1).
- *MINOR — P3 may never ship* (true clean diffs + convergence). Acceptable: P1+P2 stand alone.
- *MINOR — fact-shaped-instruction detection heuristic can mis-fire* (surface an unwanted
  fact, or miss one); non-blocking and user-gated, so the failure mode is mild noise.

---

## 8. Head-to-head verdict

- **H1** is the safest and fastest, and the *only* one with zero inbound-firewall risk in
  v1 — but it does not deliver the feature the owner described. It is "fix my bugs + a safe
  editing loop," with truth-seeking deferred indefinitely behind a LATER label.
- **H2** delivers the literal intent and the cleanest diffs, but concentrates the most risk
  (a heuristic privacy classifier guarding an autonomous agent + the most new code + the most
  optimistic estimate) into v1. Its truth-seeking is real; its firewall is well-architected
  but resting on un-flagged data.
- **H3** is H1's safe substrate + a *bounded, server-controlled, user-gated* truth-seeking
  affordance that captures most of the intent's value at a fraction of H2's risk, phased so
  the owner gets bug-fixes (P1) and a safe loop (P2) before any of the risky surface, with
  ops + convergence deferred to P3. It is the best risk-adjusted delivery of the owner's
  *value* — and it is the one plan that openly scopes truth-seeking as a separable phase.

The decisive axis is the inbound PII firewall. RT1 made it non-negotiable; the verified code
shows **the privacy predicates are all title-only and raw notes carry no privacy flag**, so
*any* hybrid that lets the AI pull facts from raw notes is gambling on a heuristic. H1 wins
that axis by refusing the bet; H2 takes the biggest version of the bet; H3 takes a small,
server-gated, human-approved version. Given the owner's data is personal health/finance
notes, the firewall risk is the one that must not go wrong.

---

## 9. RECOMMENDATION

**Ship H3, with one mandatory amendment: gate the Phase-2 candidate-fact surface behind a
real note-sensitivity signal, or defer it to Phase 3.**

Why H3:
1. It banks RT2's highest-weighted, nearly-plan-independent win — the owner's two *literal*
   complaints (date format, people-linking) fixed on the existing rebuild in **week one**
   (P1), on RT1's safest substrate.
2. P2 ships a complete, safe conversational editor (full re-emit + diff-first + read-only
   backlinks) that delivers the felt "talk to my article, it shows me the draft" loop — the
   spine of the owner's quote — with **zero anchor/splice/fence/tool corruption surface**.
3. It honors the owner's *first-named* requirement (truth-seeking) via a server-controlled,
   user-approved candidate-fact surface — preserving the "it went and found it and showed me"
   moment — **without** H2's autonomous-agent-behind-a-heuristic-classifier risk.
4. It defers cost (B's ops) and coupling (Guide convergence) to P3, each independently
   shippable, risk rising only as payoff rises.

Why not H1: it abandons the owner's first-named requirement. H1 *is* H3-P1+P2 minus the
candidate-fact surface — so choosing H1 is just choosing "H3 without the truth-seeking,"
which the owner explicitly asked for. Keep H1's superior details (its `_seed` guard is fine;
its sequencing discipline) but ship H3's scope.

Why not H2: it front-loads the most dangerous, least-deterministic component (a heuristic
privacy classifier guarding an autonomous note-reading agent) into v1, on data where a false
negative is a real-world health/finance leak. Its intent delivery is best-in-class, but the
risk/value trade is wrong for v1. **Hold H2's design in reserve**: if and when a real
note-sensitivity flag exists, H2's co-designed firewall + agent is the right *Phase 3+*
truth-seeking upgrade. H2 is the best *eventual* answer, not the best *next* answer.

**Borrow these from the losers regardless of the above:**
- From **H2**: the *no-seed-turn* redraft fix (store `run.draft` directly, keep
  `run.messages` empty until the first real turn) — strictly cleaner than H3's
  `seeded_base` marker. Adopt it in H3-P2.
- From **H2**: the candidate-fact *human-approval gate* design (Include chips → follow-up
  targeted turn) — H3 already has this; keep H2's framing.
- From **H1**: the explicit "LATER layers bolt on behind the same panel, no rework" framing —
  it's the same as H3's phasing; use whichever wording is clearer.

---

## 10. OPEN DECISIONS for the product owner (each a crisp choice + default)

1. **Truth-seeking in v1 — ship the bounded candidate-fact surface, or defer it?**
   - (a) Ship it in P2 (H3 as written) — delivers the first-named requirement sooner, but on
     a title-only privacy classifier (residual leak risk on mis-filed raw notes).
   - (b) Defer it to P3 and ship P2 as a safe editor only (= H1) — zero inbound risk now.
   - **Default: (b)-then-(a) with a prerequisite — defer the candidate surface until a
     note-sensitivity signal exists (decision #2), then ship it.** Don't ship inbound
     truth-seeking on a heuristic over un-flagged personal notes.

2. **Note privacy — add a durable per-note sensitivity flag, or keep inferring it?**
   - (a) Add a `notes.sensitivity` column + an owner "mark private" affordance; make the
     firewall a *hard* check, not a heuristic.
   - (b) Keep inferring from title/entity/health-tables (H2/H3 as written) — faster, but
     leaky on free-text entries.
   - **Default: (a).** This is the one change that actually de-risks all inbound truth-seeking
     and unblocks H2's full agent later. Cheap relative to a leak.

3. **Two buttons or one surface — "Rebuild" vs "Suggest revisions"?**
   - (a) Ship two `NoteActionsMenu` items with intent-revealing copy now; converge later (all
     three plans' default).
   - (b) Converge immediately (replace rebuild's Guide step) — more coupling up front.
   - **Default: (a).** Differentiate copy in v1, plan convergence for H3-P3.

4. **Clean diffs now or later — full re-emit (P2) vs exact-match ops (P3)?**
   - (a) Accept full re-emit + diff-first UI in P2; add B's exact ops in P3 (H3 default).
   - (b) Build ops into v1 (H2) for byte-identical untouched spans immediately.
   - **Default: (a).** The diff-first UI delivers the *felt* targeted-edit experience; ops are
     a token-cost optimization that can wait.

5. **`maintain`/`nightly` people-link parity — now or never?**
   - (a) Defer past the two shared Accept/`_generate` chokepoints (all three plans).
   - (b) Do D's 4-path refactor for full parity.
   - **Default: (a).** The owner never asked for it; D's refactor is max regression risk for
     unrequested parity (RT1/RT2 agree).

---

## Appendix — code verified for this red-team (2026-06-08)

`rebuild_engine.py:271-398` (`_generate` + wipe `:295` + auto-continue `:309-363` + tail
`:366-398`), `:401-437` (`run_draft`), `:440-463` (`run_guide`), `:466-503` (`run_redraft`,
unconditional pop `:491-492`), `:140-209` (gather; cheap model `:149`; `kb/` strip +
`require_kb_ingest` `:177-178`). `search.py:36-82` (`hybrid_notes`, no privacy filter).
`wiki_guides.py:87-100` (`is_protected`), `:103-117` (`domain_for_title`), `:124-161`
(`is_health_title`/`is_private_title`/`PRIVATE_DOMAINS`/`_PRIVATE_PREFIXES` — all
title-prefix-only). `db.py:914-926` (`kb_ingest` only per-note flag; no privacy column),
`:1012-1018` (`vitals` with nullable `note_id`). `entity_index.py:360` (`_sync_embeddings`
networked), `:529-563` (`_link_articles`, private exclusion `:553`), `:671-693`
(`note_ids_for_name` over `entity_mentions`). `wiki_build.py:1718` (`finalize_rebuild` already
runs `entity_index.rebuild`). `llm.py:404` (Anthropic `final.content` verbatim), `:676` (xAI
`tool_calls`). Confirms: no hybrid misreads the code; the decisive constraint is the
title-only privacy predicates over un-flagged raw notes.
