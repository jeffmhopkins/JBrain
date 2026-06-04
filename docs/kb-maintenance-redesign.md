# KB Maintenance & Manual Rebuild — Build Spec

Status: **DRAFT for approval** (no code yet). Synthesizes a multi-agent + adversarial
review. Grounding citations are `file:line` against the tree at the time of writing.

## 0. Goal & position

Make the knowledge base **maintenance-owned**: the destructive full rebuild
(`wiki_build`, `actions/wiki_build.yaml:12-13` "DESTRUCTIVE … DISABLED, run-only") runs
**once as a bootstrap and is never scheduled**. A looping **scrub** keeps the *whole*
wiki correct from the last run; per-article tools handle targeted fixes.

Four standing intents the design must serve, on every write:
1. **Link everything** that has an article (not merely "don't dangle").
2. **No duplicated data** (dedup within an article; detect/merge duplicate articles).
3. **Clean & concise** (summary style, supersede stale facts, trim — not a diary).
4. **Self-sufficient maintenance** (no reliance on a recurring rebuild).

**Honest limit (agreed):** per-article, context-blind operations *cannot* reconstruct
the outline's whole-corpus taxonomy partition. So the full rebuild is **demoted, not
deleted** — kept as a **manual, owner-invoked "Reorganize"** for global re-clustering
only. The scrub becomes the source of truth; "Reorganize" is the rare human-triggered
eraser for structural divergence.

---

## 1. New primitives (the agreed missing tools)

All are `pipeline.py` primitives (registered in `_PRIMITIVES`, ~`:1086`) unless noted.
Each reuses existing engine functions; the genuinely new logic is called out.

### 1.1 `scoped_known_titles(conn, title, budget=400) -> list[(title, scope)]`  ⟵ ENABLER
Replaces the silent `[:600]` **alphabetical** slice in `write_one` (`wiki_build.py:282`)
and `maintain_one` (`:473`). Returns a *relevant* candidate set with one-line scopes:
- **backlinks** — kb articles linking to `title` (`links` table, target=this).
- **co-citers** — kb articles citing any of this article's source notes.
- **entity-neighbors** — articles for entities mentioned in this article
  (`entity_index` mentions → `article_title`).
- **domain/parent siblings** — same `kb/<Domain>/<Sub>/…` prefix.
Deduped, capped at `budget`, each annotated with its scope (the one-line gist stored at
outline time). **`check_needed_links` does NOT use this cap** — deterministic matching
scans the full title/alias map.

> Why first: intent #1 ("link everything") is now *mandatory* in the prompts (commit
> `cb6de74`) but **unsatisfiable past 600 articles** with an alphabetical slice. This is
> the highest-leverage fix and a prerequisite for everything below.

### 1.2 `rebuild_article(title, instructions=None)`  ⟵ "Rebuild this article" button (Tool 1)
**Regenerate in place — never a literal wipe.** One transaction (see §4 lock):
1. Read live article; capture **sources = prior-citations ∪ entity-index**
   (`wikilinks.extract_links` non-`kb/` → note ids, the `maintain_one` pattern
   `wiki_build.py:461-466`) ∪ `entity_index.note_ids_for_name(leaf)` (`:305`). **Search
   is review-suggestion only, never a seed** (else the article sprawls past "1-hop",
   `prompts.yaml:396-404`).
2. Stash pre-wipe `content_md` + open-directive bodies in `meta` (undo basis).
3. Carry **OPEN `directive`/`conflict` items** into `write_one`'s `instructions` so they
   aren't dropped. (Requires the §5 prompt fix — `wiki_write` ignores `instructions`
   today.)
4. `notes.soft_delete(id)` the single article (NOT `wiki_build.reset()`).
5. `write_one(conn, art, instructions, known_titles=scoped_known_titles(...))` where
   `art={title,domain,scope,sources}`.
6. **On `ok`:** `upsert_note(kind=kb)` — revives the *same row* (same slug + version
   history; inbound links re-resolve via `resolve_dangling_links`, `notes.py:401`).
   **On quarantine (`ok=False`):** **auto-restore the prior version** (do NOT leave a
   hole — single-article quarantine = data loss otherwise).
7. Post-passes: `record_talk`; `rebuild_entity_index`; `write_disambiguation`;
   `flag_dead_links`.
- **Talk ledger (`article_talk`) is title-keyed and survives** — never cleared.
- **Run-only / owner-triggered** (per-article button), like `wiki_build`.
- Known limitation: only *this* article is rewritten, so reciprocal links depend on
  surviving inbound links; in a large KB it can be a partial island until others are
  re-linked (mitigated by `check_needed_links`).

### 1.3 `create_article(subject)`  ⟵ load-bearing for self-sufficiency
Outline-for-one. Replaces the "add it on the next rebuild" nudge (`wiki_build.py:768`):
- Input: an orphan subject (entity or note-cluster) from the scrub's change window.
- **Fold-or-spawn gate** (ports outline intent C1): if the subject has `< new_subject_min`
  notes OR a clear best-fit existing article, **fold the fact into that article** instead
  of spawning (reuses the `_nudge_new_subjects` min-notes gate `:761`).
- Else: choose `kb/<Domain>/<Sub>/<Name>` (domain from `note_analysis`; sub from existing
  siblings), sources via `note_ids_for_name`, `write_one`, save, `relink`.

### 1.4 `merge_articles(titles, into)`
Union sources → `write_one` under `into` → `soft_delete` the others →
**rewrite inbound `[[old]] → [[into]]`** via the existing `_rename_inbound_links`
(`notes.py`) path. 🔴 **Never rely on `flag_dead_links`** — `_neutralize_links`
(`wiki_build.py:233`) *unwraps* every inbound link to plain text, silently losing the
connection. Hysteresis: refuse to merge a title that was `split` within the last K scrubs.

### 1.5 `split_article(title)`
LLM proposes a partition `{keep_title, [child_title → source_ids]}` from the article +
its cited sources → `write_one` each → rewrite cross-links → `refresh_index`. Hysteresis:
refuse to split a title merged within the last K scrubs (anti-oscillation).

### 1.6 `recategorize_article(title, new_title)`
Rename/move = `upsert_note` to the new title + `_rename_inbound_links` + `refresh_index` +
`relink`. Triggered by a **no-LLM SQL sibling count** crossing the 3-article threshold
(ports the outline sub-categorize rule C2). Accepted limit: the *subcategory name* is a
judgment that can drift run-to-run.

### 1.7 `check_needed_links(title=None, mode="propose")`  ⟵ "Check for needed links" (Tool 2)
Deterministic backstop (the *add-link* complement to `flag_dead_links`):
- Build a leaf/alias→title map (`entity_index._link_articles` basis `:218-230` + aliases).
- Scan body for known leaves on **word boundaries**, **masking exclusion zones**:
  existing `[[…]]`, code fences/inline backticks, and **footnote-definition lines**
  `[^sN]: [[…]]` (citations — linking there breaks `citation_issues`, `pipeline.py:401`).
- 🔴 **Refuse** any leaf in `entity_index.ambiguous_terms` (`:322`) — link to its
  `kb/_disambig/<Term>` page if present, else flag — and refuse single-token
  common-word/unconfirmed-proper-noun leaves ("Smith", "Park", "Running").
- `mode=propose` (default): a Review card listing proposed links (read-only, schedulable
  like `kb_audit`). `mode=auto`: one **versioned** write per article, **owner-triggered
  only**.
- **No reciprocal/See-also edits** — backlinks are derived (`notes.py:408`).
- Not subject to the 600 cap (deterministic) — can fix links the writer couldn't reach.

### 1.8 `refresh_index()`
`build_index_md` over all live non-`_` kb titles → upsert `kb/_index`. No LLM. Wired into
every structural op (create/merge/split/recategorize) and the scrub.

### 1.9 `relink` (composition, not new)
`rebuild_entity_index` + `flag_dead_links` + `write_disambiguation` — already coded; just
fire after structural ops so entity→article, dead links, and disambig stay consistent.

---

## 2. The scrub cycle (`actions/wiki_scrub.yaml`, replaces scheduled reliance)

Watermark-driven, **no `reset`**, brings the whole wiki current since the last run:

```
0. analyze_pending(200) → analyze_note (loop)        # backfill analysis (as wiki_update)
1. rebuild_entity_index                               # routing basis (no LLM)
2. wiki_update(limit per run)                         # changes → EXISTING articles
3. create_article for each orphan subject ≥ min       # G1 (replaces the nudge)
4. consume recorded restructure items: split_article / merge_articles   # G2/G3
5. recategorize_article for sibling-threshold crossings (SQL-triggered)  # G4
6. refresh_index ; rebuild_entity_index ; write_disambiguation ; flag_dead_links
7. advance watermark only over the leading all-success prefix
```

**Coverage guarantees:**
- **Keep the per-run `max_articles` cap; LOOP the recipe** until `changes==0` and no
  orphans/restructure items remain. A multi-day backlog drains in bounded passes; cost
  per transaction stays small. (No "raise the cap to infinity" — that breaks affordability
  + atomicity.)
- **Watermark = "deferred ≠ skipped"** discipline is preserved from `update_batch`
  (`wiki_build.py:736-742`). 🔴 **New rule:** a `create_article`/`split`/`merge` *failure*
  must add the triggering change to `bad_articles` so the watermark holds — else an
  uncovered subject is skipped forever.
- **Restructure items are consumed once and resolved** (not re-burned every run).

Scheduling: the scrub replaces today's `wiki-update` + `wiki-maintain` schedules (or runs
nightly as one job). `wiki-build` stays **disabled/manual**.

---

## 3. Data model

- **`article_talk`**: add a kind **`restructure`** whose `body` is JSON
  `{op:"split"|"merge"|"fold", target?, rationale}`. It is **consumed only by the scrub
  (step 4)** and is **excluded from `maintain_batch`'s open-item set** (which stays
  `conflict|question|todo|directive`) so maintenance never tries (and fails) to "address"
  it. Resolved via `resolve_with` once executed.
- **Hysteresis**: a lightweight `kb_structure_log(title, op, at)` (or reuse `decision`
  talk entries) so merge/split can check "was the inverse done within K runs."
- **Watermark**: unchanged (`meta['kb_incremental:since']`).

---

## 4. Concurrency & locking  🔴

Manual ops (`rebuild_article`, `Reorganize`), the scrub, and any nightly job **must be
mutually exclusive** — they all mutate `kb/` + `article_talk` on separate connections.
Failure mode (verified): if the nightly update runs between `rebuild_article`'s
soft-delete and revive, a note edit that should route to the article is dropped and the
watermark advances past it (lost until a manual rebuild).

Mechanism: a single **`meta['kb_write_lock']`** advisory flag (claimed atomically, TTL'd,
released in a `finally`), checked at the top of every KB-mutating action. SQLite is
single-writer so statements can't corrupt mid-flight, but transactions interleave — the
lock prevents the logical race. `rebuild_article`'s capture→delete→write is additionally
wrapped in **one transaction**.

---

## 5. Prompt changes

**De-reliance (remove "at rebuild" assumptions):**
- `wiki_maintain` (`prompts.yaml:571-585`, the `cb6de74` hints): keep the per-article
  restraint, but (a) change destination from *"the next full rebuild"* → *"the next
  scrub"*; (b) emit a structured `restructure` item (`{op,target}`) instead of a
  free-text `note`; (c) delete the literal "merge at rebuild" / "at next rebuild" phrasing.
- `wiki_outline` (`:393`, `:415-424`): the "prefer few solid articles / fold count-of-1"
  and "3+ → subcategory" rules move into `create_article` (fold gate) and the scrub
  (SQL-triggered recategorize) respectively; the outline prompt keeps them **only** for
  the manual Reorganize.
- Code/comments: `wiki_build.py:768` "add it on the next rebuild" → "folded in by
  maintenance"; `kb/_index` "Rebuilt whenever the KB is rebuilt" (`:116`) → "maintained
  continuously by the scrub"; `actions/wiki_update.yaml` "full rebuild remains the source
  of truth" → the scrub is.

**Fix the latent bug:** add a `{instructions}` placeholder to the `wiki_write` prompt
(`:442`) so `write_one(instructions=…)` (`:257`) actually reaches the model — required for
`rebuild_article` to honor open directives.

**Keep (correct for a maintenance-owned wiki):** the `cb6de74` LINK-EVERYTHING,
STAY-CONCISE, and SUPERSEDE additions.

---

## 6. Demote the full rebuild to manual "Reorganize"

- Keep `wiki_build` disabled/run-only; rename its surface to **"Reorganize knowledge base"**
  and document it as a rare, human-triggered global re-cluster — not a maintenance step.
- It remains the only operation that re-partitions the taxonomy (R1/R3: per-article ops
  can't).

---

## 7. Phasing

- **Phase 1 (self-contained, high value):** `scoped_known_titles` (kill the 600 cap) +
  `rebuild_article` (regenerate-in-place) + `check_needed_links` + the `{instructions}`
  prompt fix + the prompt de-reliance edits + the KB write lock.
- **Phase 2 (self-sufficiency):** `create_article` + the looping scrub cycle + the
  `restructure` kind + watermark-holds-on-create-failure.
- **Phase 3 (structure):** `merge_articles` / `split_article` / `recategorize_article`
  with inbound-link rewrite + hysteresis; demote rebuild → Reorganize.

---

## 8. Testing

- `scoped_known_titles`: returns relevant set; never silently truncates a linkable target
  the way the alphabetical cap did.
- `rebuild_article`: preserves talk + version history + inbound links; restores prior
  version on quarantine; sources = citations ∪ entity-index (search excluded); honors an
  open directive via instructions.
- `check_needed_links`: links an unambiguous match; refuses ambiguous + common-word leaf;
  never edits code/quote/footnote zones; propose vs auto.
- `create_article`: folds a count-of-1 subject; spawns a recurring one; watermark holds on
  failure.
- `merge_articles`/`split_article`: inbound `[[old]]→[[into]]` rewritten (NOT unwrapped);
  hysteresis blocks immediate re-inverse.
- scrub loop: drains a backlog across bounded passes; nothing skipped; restructure item
  consumed once.

---

## 9. Accepted limitations / open decisions

1. **Taxonomy lock-in**: maintenance can't globally re-partition; the manual Reorganize is
   the only eraser. (Accepted — that's the one compromise.)
2. **Subcategory naming drift** (recategorize names wander run-to-run). Bounded by SQL
   trigger; names are a judgment.
3. **Backwards-dated notes** (clock skew / imports with old `updated_at`) land below the
   watermark and are never seen by the scrub — only a Reorganize or `rebuild_article`
   catches them.
4. **Compounding transcription drift** (maintenance edits prior text, not raw notes) — the
   antidote is periodic per-article `rebuild_article`, optionally auto-triggered by a
   drift heuristic (article-vs-source divergence, like `flag_ungrounded_reference`).
5. **`rebuild_article` reciprocity/island** in a large KB until `check_needed_links` runs.
6. **`K` (hysteresis window)** and `new_subject_min`, `scoped budget` are tunable knobs to
   settle during Phase 2/3.
