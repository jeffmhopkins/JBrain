# KB Maintenance & Manual Rebuild — Build Spec (v2, gauntlet-revised)

Status: **READY FOR APPROVAL.** v1 went through a feasibility gauntlet and a
design-coherence red-team; this v2 folds in every finding. Grounding citations are
`file:line` against the tree at writing.

## 0. Goal & position

Make the KB **maintenance-owned**: the destructive full rebuild (`wiki_build`,
`actions/wiki_build.yaml:12-13`) runs **once as a bootstrap, never on a schedule**. A
**scrub** (a bounded batch the scheduler re-invokes each tick) keeps the *whole* wiki
current from the last run; per-article tools handle targeted fixes.

Four intents, stated **honestly** (the gauntlet corrected v1's overclaims):
1. **Link everything reachable** — every write MUST link a mention whose article is in its
   *relevant candidate set* (graph-neighborhood + leaf/alias text scan), plus a
   reciprocity pass so new articles get linked *from* their neighbors. This is strong
   local linking, **not** a global guarantee (see §9.3).
2. **No duplicate *articles*** — detect/merge near-duplicate articles; dedup orphans
   *before* spawning. (Cross-*article* duplication of the same *fact* is a known partial
   limit — §9.4.)
3. **Clean & concise** — summary style, supersede stale facts, trim (the kept `cb6de74`
   prompt rules).
4. **Self-sufficient maintenance** — no reliance on a recurring rebuild; the scrub
   terminates and stays quiet on a no-op night.

**Honest limit (the one compromise):** per-article, context-blind ops cannot reconstruct
the outline's whole-corpus taxonomy partition. The full rebuild is **demoted, not
deleted** — kept as a manual, owner-invoked **"Reorganize."** A read-only
`taxonomy_health` report (§1.10) tells the owner *when* a Reorganize is overdue, so
lock-in is monitored, not silent.

---

## 1. New primitives

Registered in `pipeline.py` `_PRIMITIVES` (~`:1086`) unless noted. Each new primitive
returns `{ok: bool, …}`; the scrub uses `ok` for watermark-hold logic (§2).

### 1.1 `scoped_known_titles(conn, title, budget=400) -> list[(title, scope)]`  ⟵ ENABLER
Replaces the silent **alphabetical `[:600]`** slice (`wiki_build.py:282`, `:473`) with a
*relevant* set: **backlinks** (links→this) ∪ **co-citers** (articles citing this
article's source notes) ∪ **entity-neighbors** (articles for entities this article
mentions) ∪ **domain/prefix siblings**, deduped, capped at `budget`.
- **Scope text:** derived on the fly from each candidate's **lead sentence** (first line of
  `content_md`) — outline `scope` is ephemeral and **not persisted** (feasibility finding
  #10), so we do NOT depend on stored scope and add **no schema column**.
- This is a **1-graph-hop neighborhood** query (§9.3). `check_needed_links` does NOT use
  this cap — deterministic matching scans the full leaf/alias map.

### 1.2 `rebuild_article(title, instructions=None)`  ⟵ "Rebuild this article" (Tool 1)
**Regenerate in place — never a literal wipe. Runs under the §4 lock, in ONE transaction**
(capture→soft_delete→write→commit; a crash can't half-delete):
1. Capture **sources = prior-citations ∪ entity-index** (`extract_links` non-`kb/`→ids,
   the `maintain_one` pattern `wiki_build.py:461-466`; ∪ `note_ids_for_name(leaf)` `:305`).
   **Search is review-suggestion only, never a seed.**
2. Stash pre-wipe `content_md` + open-directive bodies (undo basis).
3. Carry **OPEN `directive`/`conflict` items** into `write_one`'s `instructions` (requires
   the §5 `{instructions}` prompt fix).
4. `notes.soft_delete(id)` (NOT `reset()`).
5. `write_one(art, instructions, known_titles=scoped_known_titles(...))`.
6. **On `ok`:** `upsert_note(kind=kb)` revives the *same row* (slug + version history kept;
   inbound links re-resolve via `resolve_dangling_links`, `notes.py:401` — verified).
   **On quarantine:** auto-restore the prior version **AND record an open `todo`
   ("rebuild quarantined — manual review") + bump an attempt counter** (F8) so a chronically
   failing article surfaces instead of silently reverting to stale content.
7. **Reciprocity pass (F3):** run `check_needed_links(mode=propose)` over the article AND
   its `scoped_known_titles` neighbors, so existing neighbors get a proposed inbound link
   to the rebuilt article (not just one-way).
8. Post: `rebuild_entity_index`; `write_disambiguation`; `flag_dead_links` (last).
- Talk ledger is title-keyed and **preserved**. Owner-triggered (per-article button).

### 1.3 `create_article(subject)`  ⟵ load-bearing; **dedup BEFORE spawn (F1)**
Replaces the "add it on the next rebuild" nudge (`wiki_build.py:768`). Order matters:
1. **Collapse the orphan set first:** within the change window, dedup orphans by
   `entity_index.normalize` + the merge map (`entity_index.py:114-127`) so "TTP" and
   "Thrombotic Thrombocytopenic Purpura" become one subject, not two articles.
2. **Collision check vs existing KB:** if a leaf-normalized scan (the `check_needed_links`
   basis) finds an existing title equal to the subject → **fold/route to that article or
   `merge`, do NOT spawn.**
3. **Fold-or-spawn gate:** `< new_subject_min` notes or a clear best-fit existing article →
   fold the fact in; else choose `kb/<Domain>/<Sub>/<Name>`, sources via
   `note_ids_for_name`, `write_one`, save, reciprocity pass (§1.2 step 7), `relink`.

### 1.4 `merge_articles(titles, into)`
Union sources → `write_one` under `into` → `soft_delete` others → **rewrite inbound
`[[old]]→[[into]]` via `_rename_inbound_links` (`notes.py:66`, verified)**. 🔴 Never rely
on `flag_dead_links` (`_neutralize_links` `:233` *unwraps* inbound links to plain text).
**Hysteresis keyed on the inverse pair (F6):** `merge(A1,A2→A)` is blocked only if
`split(A→A1,A2)` happened within **K=3** scrub runs — not any merge touching A1/A2. A
**second** blocked attempt escalates to a Review card.

### 1.5 `split_article(title)`
LLM proposes `{keep, [child→source_ids]}` → `write_one` each → rewrite cross-links →
`refresh_index`. Same inverse-pair hysteresis (K=3) + second-block→Review-card.

### 1.6 `recategorize_article(title, new_title)`
Rename/move via `upsert_note(new title)` + `_rename_inbound_links` + `refresh_index` +
`relink`. Triggered by a no-LLM SQL sibling-count crossing the 3-article threshold (ports
outline rule C2, `prompts.yaml:415-424`). **Limit (F7):** only *adds* subcategories; does
**not** collapse an over-split tree or re-domain a mis-filed article, and the subcategory
*name* can drift run-to-run → that's what `taxonomy_health` (§1.10) watches.

### 1.7 `check_needed_links(title=None, mode="propose")`  ⟵ "Check for needed links" (Tool 2)
Deterministic *add-link* backstop. Leaf/alias→title map (`_link_articles` basis `:218-230`
+ aliases). Scan body on word boundaries, **masking** existing `[[…]]`, code
fences/inline backticks, and footnote-definition lines `[^sN]:` (linking there breaks
`citation_issues`, `pipeline.py:401`). 🔴 **Refuse** any leaf in
`entity_index.ambiguous_terms` (`:322`) — link to its `_disambig` page if present, else
flag — and refuse single-token common-word leaves.
- **Default `mode=propose`** (review card, schedulable like `kb_audit`); **`mode=auto`** is
  owner-triggered, one **versioned** write/article, **validates each target is live in the
  same transaction under the §4 lock** (F4). No reciprocal/See-also edits (backlinks are
  derived, `notes.py:408`). Not subject to the 600 cap.

### 1.8 `refresh_index()`
`build_index_md` over live non-`_` kb titles → upsert `kb/_index` (verified standalone:
`build_index_md` needs only an articles list, `wiki_build.py:108`). No LLM. Wired into
every structural op + the scrub.

### 1.9 `relink` (composition, not new)
`rebuild_entity_index` + `write_disambiguation` + `flag_dead_links` — already coded, fired
after structural ops.

### 1.10 `taxonomy_health() -> report`  ⟵ NEW (F7), read-only, no LLM, schedulable
Surfaces drift so "rare manual Reorganize" becomes *triggered*, not guessed: orphan-prefix
subcategories, articles whose domain disagrees with their entity type, subcategory-name
churn over recent runs, sibling counts thrashing the threshold. Past thresholds it posts a
single Review card "Reorganize recommended: <reasons>." Same SQL machinery as the
recategorize trigger, in read-only mode.

### 1.11 `research_article(title)`  ⟵ semantic Reference-link SUGGESTER (gauntlet-constrained)
Surfaces EXISTING Reference articles related to an article's salient concepts **by meaning**
— catching relationships the deterministic `check_needed_links` (§1.7) misses because the
body never names the leaf ("even if not hard-linked"). It is the partial answer to §9.3 and
an **adjunct that feeds `rebuild_article`** — wiring an old article into the current
Reference tree. A design + red-team gauntlet rejected the naive auto-linker; this is the
disciplined form, and the three guardrails are **non-negotiable**:

- **Nomination = distance < τ AND a deterministic corroborator.** `embeddings.semantic_search`
  (`embeddings.py:174`) returns raw top-k with **no relevance cutoff** — embedding-alone
  nomination *is* the sprawl §9.3 deferred. So a candidate Reference article qualifies only
  if it is both semantically near (`distance < τ`) **and** corroborated by a deterministic
  signal already in the design — a shared backlink/co-citer/entity/prefix from
  `scoped_known_titles` (§1.1) or an `entity_index` link. First-hop only, `kb/Reference/*`
  only.
- **Propose-only; the human is the adversary.** It **never auto-writes**, is **off the scrub
  hot path**, and **never gates the watermark**. The LLM "is this relevant?" judgment
  colludes with the embedding selector (it confirms the bias), so the non-colluding gate is
  the **owner's accept/reject** on a Review card — or, as the red-team preferred, it surfaces
  *inside `rebuild_article`* via the already-blessed "Search is review-suggestion only, never
  a seed" hook (§1.2 step 1). Routes through `check_needed_links`'s ambiguous-term +
  common-word refusals and dedups against existing links (no flip-flop with `flag_dead_links`).
- **Grounding firewall — a link or nothing.** Its only possible mutation is wrapping a phrase
  **already in the body** in `[[kb/Reference/…]]` (or a deferred links-only See-also). It may
  add **no prose, fact, definition, or "researched context"** — enforced in code (the
  `_bad_links` candidate-membership + substring-of-body checks), and any added body content
  would trip `flag_ungrounded_reference` (`wiki_build.py:546`). This honors the GROUNDING +
  1-HOP rules (`prompts.yaml:450,397`) by construction.

**It links; it does not fix stale *content* (F6).** A genuinely stale article is wrong in its
*facts* — only `rebuild_article` (from sources) fixes that. `research_article` is the
link-enrichment adjunct, not a substitute, and must not make a stale article merely *look*
maintained. **Ship propose-only and measure human accept-rate before ever discussing auto.**

---

## 2. The scrub cycle (`actions/wiki_scrub.yaml`)

**No self-loop** — the action engine has no `while/until` construct (feasibility finding
#6). The scrub is **one bounded batch per run; the scheduler re-invokes it each tick** and
the watermark + the open `restructure`/orphan queues carry the backlog across runs (this
is exactly how `update_batch` already drains). A multi-day backlog drains over N ticks; a
quiet night is a no-op.

Per-run steps:
```
0. analyze_pending(200) → analyze_note (loop)              # backfill analysis
1. rebuild_entity_index                                     # routing basis, no LLM
2. wiki_update(limit)                                       # changes → existing articles
3. create_article for orphans (deduped per §1.3) ≥ min
4. consume actionable `restructure` items: split / merge
5. recategorize for SQL sibling-threshold crossings
6. write_disambiguation ; check_needed_links(mode=propose) ; refresh_index ;
   flag_dead_links   ← runs ONCE, LAST, after all structural ops settle (F4)
7. advance watermark over the leading ALL-SUCCESS prefix
8. taxonomy_health() (cheap; posts a card only past thresholds)
```

**Termination & anti-livelock (F2):**
- The loop/queue is defined over **ACTIONABLE** items only. A `restructure` item blocked by
  hysteresis (§1.4/1.5) is **resolved-as-deferred** (`resolve_with` "blocked until run N"),
  so it leaves the open set rather than re-presenting forever.
- A **per-subject attempt counter**: any `create`/`split`/`merge` that fails or is blocked
  **twice** is parked to a Review card (human), not re-burned.
- 🔴 Watermark holds: a `create_article`/`split`/`merge` whose `ok=False` adds its
  triggering change to `bad_articles` (mirrors `update_batch` `:737-742`) so an uncovered
  subject is never skipped.

---

## 3. Data model

- **`article_talk`**: add kind **`restructure`** to `_KINDS` (`article_talk.py:19`) and the
  `schema.sql` kind comment. Do **NOT** add it to `OPEN_KINDS` (`:21`); `maintain_batch`'s
  `WHERE kind IN ('conflict','question','todo','directive')` (`wiki_build.py:611`) then
  skips it automatically (verified). Body is JSON `{op:"split"|"merge"|"fold", target?,
  rationale}` — `record` stringifies fine (`:56`).
- **`kb_structure_log(title, op, inverse_pair_key, at, attempts)`** — small new table for
  inverse-pair hysteresis (§1.4/1.5) and the attempt counters (§2). (Or fold into
  `article_talk` `decision` entries; a dedicated table is cleaner.)
- **Watermark**: unchanged (`meta['kb_incremental:since']`).

---

## 4. Concurrency & locking  🔴 (feasibility finding #9)

A plain `meta` flag is **not atomic across connections** (scheduler thread vs manual-run
thread use different connections — verified race). Use a **dedicated lock table**:
`CREATE TABLE IF NOT EXISTS kb_locks (key TEXT PRIMARY KEY, held_at TEXT, ttl_ms INTEGER)`
claimed by `INSERT … ON CONFLICT DO NOTHING` + `rowcount` check (the atomic claim pattern;
TTL reclaims a dead holder), released in a `finally`. Every KB-mutating action
(`rebuild_article`, scrub, Reorganize, `check_needed_links` auto) takes the lock first.
`rebuild_article` additionally wraps capture→delete→write in **one transaction**.

---

## 5. Prompt changes

**Fix the latent bug (feasibility #2):** add a `{instructions}` placeholder to `wiki_write`
(`prompts.yaml:442`) and the `.replace()` in `write_one` (`:284-289`) — without it
`rebuild_article` can't honor directives.

**De-reliance (kill "at rebuild"):**
- `wiki_maintain` `cb6de74` hints (`:571-585`): keep per-article restraint, but emit a
  structured `restructure` item (`{op,target}`) "**for the next scrub**", and delete the
  "merge at rebuild" / "at next rebuild" phrasing.
- `wiki_outline` (`:393`, `:415-424`): the fold-count-of-1 + 3+-subcategory rules move into
  `create_article` (§1.3) and the scrub recategorize (§1.6); the outline keeps them only
  for the manual Reorganize.
- Code/comments: `wiki_build.py:768` "add it on the next rebuild" → "folded in by
  maintenance"; `kb/_index` "Rebuilt whenever the KB is rebuilt" (`:116`) → "maintained
  continuously by the scrub"; `actions/wiki_update.yaml` "full rebuild remains the source
  of truth" → the scrub is.

**Keep:** the `cb6de74` LINK-EVERYTHING / STAY-CONCISE / SUPERSEDE additions.

---

## 6. Demote the full rebuild to manual "Reorganize"
Keep `wiki_build` disabled/run-only; surface it as **"Reorganize knowledge base"**, a rare
human-triggered global re-cluster. It remains the only op that re-partitions the taxonomy.

---

## 7. Phasing

- **Phase 1** (self-contained): the **deterministic gate pipeline** (§10: promote existing
  lint/structure/collision/grounding signals to block/fail/advisory + a bounded 2-revise
  loop, zero new LLM calls) + `scoped_known_titles` (kill the 600 cap, lead-sentence
  scope) + `rebuild_article` (in-place, one transaction, quarantine→open-item) +
  `check_needed_links` + the `{instructions}` prompt fix + prompt de-reliance + the **lock
  table** (§4). Prereqs from feasibility: `{instructions}`, lock table.
- **Phase 2** (self-sufficiency): `create_article` (dedup-before-spawn, sharing the §10
  taxonomy-collision gate) + the scrub (scheduler-driven bounded batch) + the
  `restructure` kind + watermark-holds-on-failure + attempt counters + `taxonomy_health`.
- **Phase 3** (structure): `merge`/`split`/`recategorize` with `_rename_inbound_links`
  rewrite + inverse-pair hysteresis (K=3) + second-block→Review; demote rebuild→Reorganize;
  **+ the two narrow LLM review gates** (§10.3: near-duplicate adjudication on
  create/rebuild + a budgeted sampled grounding audit — never on the hot scrub path);
  **+ `research_article` (§1.11) propose-only**, surfaced inside `rebuild_article`,
  corroborated + grounded, with accept-rate measured before any `auto`.

---

## 8. Testing

- `scoped_known_titles`: returns the neighborhood set (backlinks/co-citers/entity/sibling),
  lead-sentence scope, no alphabetical truncation of an in-neighborhood target.
- `rebuild_article`: preserves talk + version history + inbound links; **quarantine →
  prior restored AND open todo recorded**; sources = citations ∪ entity-index (search
  excluded); honors a directive via `{instructions}`; reciprocity pass proposes an inbound
  link from a neighbor.
- `create_article`: **two same-subject orphans collapse to one article (F1)**; folds a
  count-of-1; spawns a recurring subject; watermark holds on failure.
- `check_needed_links`: links an unambiguous match; refuses ambiguous + common-word; never
  edits code/quote/footnote zones; auto validates target live.
- `merge`/`split`: inbound `[[old]]→[[into]]` rewritten (NOT unwrapped); inverse-pair
  hysteresis blocks the inverse but allows a non-inverse move; second block → card.
- scrub: drains a backlog across ticks; a hysteresis-blocked item is resolved-deferred and
  does not re-present; `flag_dead_links` runs once, last; no add/remove link flip-flop.

---

## 9. Accepted limitations / open decisions

1. **Taxonomy lock-in** — maintenance can't globally re-partition; manual Reorganize is the
   eraser, with `taxonomy_health` (§1.10) as the "when overdue" signal.
2. **Subcategory naming drift** — `recategorize` names wander; bounded by SQL trigger +
   `taxonomy_health`.
3. **Linking is neighborhood + leaf-scan, not global (F3)** — two related articles that
   share no backlink/citation/entity/prefix *and* never name each other's leaf won't
   *deterministically* auto-link. Reciprocity passes cover the common cases. The semantic
   gap is **partially** closed by `research_article` (§1.11) — but only as a *propose-only*,
   deterministically-corroborated, human-approved suggester (pure embedding-similarity
   auto-linking stays out of scope, by design). A fully-global auto guarantee remains
   deliberately unbuilt.
4. **Cross-article *fact* duplication (F5)** — per-article maintenance can't see other
   articles' bodies, so the same fact restated in 3 articles can drift to 3 values under
   per-article SUPERSEDE. Mitigation: prefer a single canonical home + links; optionally a
   read-only "shared-fact divergence" report keyed on co-cited notes (deferred).
5. **Backwards-dated notes** below the watermark are never seen by the scrub — only
   Reorganize / `rebuild_article` catches them.
6. **Compounding transcription drift** — antidote is periodic `rebuild_article`, optionally
   auto-triggered by a divergence heuristic (like `flag_ungrounded_reference`).
7. **Tunables to settle in Phase 2/3:** `K` (hysteresis, default 3), `new_subject_min`,
   `scoped budget` (default 400), attempt-counter park threshold (default 2),
   `taxonomy_health` thresholds.

---

## 10. Review architecture: stepped *deterministic* gates, not an LLM gauntlet

Two adversarial reviews (a designer + a contrarian red-team) **converged**: decompose the
pre-commit review into focused, ordered **gates**, but make them **deterministic**; reject
per-article LLM "taxonomy"/"intent" review on the hot path. Rationale, grounded in code:
- Today's `write_one`/`maintain_one` is already *"N deterministic checks + ONE bounded,
  non-regressing revise"* — the revision is kept only if it doesn't increase errors
  (`wiki_build.py:315`,`:518`), so it **cannot oscillate**.
- **LLM-judging-LLM is only adversarial when the judge has information the writer lacked**
  (a tool, retrieval, a different model, or data). A same-model pass reading the same
  `{sources}` is an *echo* — it rubber-stamps shared blind spots or thrashes a revise loop.
- The scrub is the **hottest, continuously re-invoked loop** with a hard fan-out cap
  (`max_articles`, `wiki_build.py:713`). An unconditional 4-gate LLM pipeline is a 2.5–9×
  call multiplier there, mostly spent interrogating already-clean drafts.

### 10.1 The gate model
Insert a pure function `run_gates(conn, candidate) -> {commit, verdicts, feedback, talk,
restructure_hints}` between a candidate `content_md` and `upsert_note`. Each gate returns:
- **block** → quarantine (build) / restore-prior (maintenance) + open `todo` + attempt
  counter (the §1.2 F8 pattern);
- **fail** → re-enter a **bounded revise loop (max 2 iterations, shared counter)** carrying
  that gate's specific feedback as the `wiki_revise` `{issues}` block;
- **advisory** → commit anyway, record an `article_talk` note (the `flag_dead_links`
  log-don't-block pattern, `:371`).

**Commit ⟺ no surviving `block` ∧ no surviving `fail` after ≤2 revise iterations.** This is
a strict generalization of today's `ok = not errors`. Anti-thrash invariant (generalizes
the `:315` ratchet): a revise iteration is kept only if it **reduces** at least one
fail/block and **increases none** — else discard and stop.

### 10.2 The gates — deterministic first, fail-fast (zero new LLM calls in the common case)
| Gate | Kind | Reuses | Verdict mapping |
|---|---|---|---|
| **Lint** | deterministic | `validate_structure` + `citation_issues` + `_bad_links` | citation/PII → block; dead link → fail-then-advisory+neutralize |
| **Structure shape** | deterministic | `validate_structure` required/recommended sections | missing lead/required → fail; recommended → advisory |
| **Taxonomy collision/dedup** | deterministic | `entity_index.normalize` + leaf map + `ambiguous_terms` | exact/normalize collision → block→fold/merge (this **is** `create_article`'s §1.3 dedup, lifted into the shared function); ambiguous title → advisory `_disambig` |
| **Intent: padding** | deterministic | `flag_ungrounded_reference` body/source ratio | gross padding → fail (trim) |
Ordering: the four above (all free) run first and fail-fast. A structured **self-report**
piggybacked on the existing draft `talk` block (`prompts.yaml:479`) can *nominate*
structure/intent/near-dup flags at **zero extra calls**, so the expensive passes fire only
when something already looks wrong.

### 10.3 The only justified LLM gates — narrow, off the hot path
Run **only on create/rebuild and a separately-budgeted *sampled* audit**, never on every
scrub edit, and **never gating the scrub's `ok`/watermark** (LLM nondeterminism must not
undermine idempotency):
1. **Near-duplicate adjudication** — embeddings nominate a candidate; the LLM is fed the
   candidate articles' **bodies** (which `write_one` only ever sees as *titles*) → genuinely
   new information → a *real* adversary. Verdict: block→merge/fold on create; advisory
   `restructure` hint on maintenance.
2. **Per-claim grounding critic** — *deferred / optional.* The red-team's strongest target
   (collusion, no ground truth); only worth building if given a *different* lens (a cheaper
   second model or retrieval). Lean **no** — the deterministic ratio + the GROUNDING prompt
   rule already cover the honest slice.

### 10.4 What we explicitly do NOT build
Per-article **LLM taxonomy review** (category error — taxonomy is whole-corpus; the
answerable slice is a `SELECT COUNT(*)` sibling check + `taxonomy_health` + manual
Reorganize) and **unconditional LLM intent review** (vibes/echo). Humans (Review cards) are
the only non-colluding adversary and stay reserved for the genuine judgment calls.

### 10.5 Orchestration
`run_gates` lives **in-function** in `wiki_build.py` (the loop needs a shared counter +
short-circuit the linear recipe engine, `pipeline.py:1-13`, deliberately lacks). The recipe
decides *which* articles are processed; `run_gates` decides *whether one commits*. The
scrub's per-edit maintenance path runs **deterministic gates only**, preserving its call
budget and the watermark-hold-on-failure semantics unchanged.
