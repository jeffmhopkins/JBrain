# R2 — Universal KB synthesis/build path, the writer prompts, and synthesis-vs-rewrite differences

Scope: the build/synthesis path, every writer prompt, and the formatting/promotion
divergences between **universal synthesis (full build)**, **live rebuild/guide**, and
**batch maintain/update**. All claims cite `file:line`.

There are effectively **four** LLM article-producing paths in the repo. The brief
asked about three; the fourth (`synthesize_wiki` / `wiki_plan`) is a *separate* legacy
synthesis engine and is the biggest formatting outlier, so it is included.

| Path | Entry point | Prompt used |
|---|---|---|
| Universal synthesis (full rebuild) | `wiki_build.write_batch` → `write_one` (`wiki_build.py:2878`, `:821`) | `actions.wiki_write` + `actions.wiki_revise` |
| Single-article rebuild (nightly) | `rebuild_article` → `write_one` (`wiki_build.py:1723`) | `actions.wiki_write` + `actions.wiki_revise` |
| Live "rebuild/guide" (conversational) | `rebuild_engine.run_draft` / `run_guide` → `_generate` (`rebuild_engine.py:401`, `:440`, `:271`) | `build_write_prompt` (= `wiki_write`) for draft; **thin steer string** for guide |
| Batch maintain / on-demand maintain | `maintain_one` (`wiki_build.py:1256`), `maintain_now` (`:1768`) | `actions.wiki_maintain` + `actions.wiki_revise` |
| Incremental synthesis (separate engine) | `synthesize_wiki.yaml` → `wiki_plan` | NOT `wiki_write` — its own prompt + `validate_citations` only |

---

## Side-by-side matrix

### 1. Which prompt is used
- **Full build / single rebuild**: `actions.wiki_write` assembled by `build_write_prompt`
  (`wiki_build.py:784-818`) — single source of truth, also used by the live draft.
  Revise pass uses `actions.wiki_revise` (`write_one`, `wiki_build.py:908-911`).
- **Live draft** (`run_draft`): identical prompt — `run.messages = [build_write_prompt(...)]`
  (`rebuild_engine.py:435`). **Live guide** (`run_guide`): NOT a structured prompt — a
  hardcoded steer appended to the running conversation: *"Revise the article per this
  guidance, using ONLY the sources already provided earlier… Output the COMPLETE revised
  article in the same Markdown format."* (`rebuild_engine.py:455-460`). It carries **no**
  DATES, CROSS-LINKS, AUTHOR, GROUNDING, or guide blocks — it relies entirely on the
  original `wiki_write` turn still being in `run.messages`.
- **Maintain**: `actions.wiki_maintain` (`wiki_build.py:1328-1336`), revise via
  `actions.wiki_revise` (`:1374-1377`).

### 2. How sources are loaded / budgeted
- **All wiki_build paths share `_load_sources`** (`wiki_build.py:412-462`),
  `SOURCE_BUDGET = 2000` chars/note (`:409`), passes RAW content (never expands `@t[...]`
  — `:441-443`), embeddings-selected excerpt for long notes (`:451-452`), attachment text
  appended cap 1200 (`:458`). Rendered by `_sources_text` (`:465-474`).
- **Build**: sources = the outline's assignment + an entity-mention safety net
  (`outline`, `:397-401`), dropping ungrounded articles (`:404`).
- **Rebuild (nightly + live)**: `rebuild_sources` (`:1640-1678`) — prior citations ∪
  `entity_index.note_ids_for_name`; the **live path adds a Stage-1 gather agent**
  (`run_gather`, `rebuild_engine.py:119`) that lets the user curate the set, then
  `run_draft` loads only the curated ids (`:417-425`).
- **Maintain**: sources = the article's *already-cited* notes (`:1298-1306`) plus
  `extra_source_ids` (new/changed) and `removed_titles` (purge) (`:1307`, `:1323`).
- **Cross-link candidates** all share `scoped_known_titles` (`:1511-1581`) + aliases via
  `known_aliases_block` (`:1478-1508`). **Live guide does neither** — it reuses whatever
  `run.known` was captured at draft time (`rebuild_engine.py:433-434`).

### 3. Date-token (`@t[...]`) enforcement — *prompt-only + advisory lint everywhere; NO deterministic backstop on any path*
- The directive lives in three places, all prompt-text: the shared `{general_guide}`
  "Dates & time" block (`prompts.yaml:485-491`), the `wiki_write` "DATES & TIME" block
  (`prompts.yaml:868-874`), and the `wiki_maintain` convert-to-token rule
  (`prompts.yaml:985-986`).
- **`wiki_revise` has NO DATES directive** (`prompts.yaml:908-932`) — a revise pass can
  silently drop or freeze a token.
- The only programmatic check is `validate_structure`'s `_REL_TIME_RE`
  (`wiki_guides.py:47-48`, `:318-320`) which appends a **warning** ("looks frozen — use a
  live @t[...] token"). Warnings never fail `ok` (`:327`).
- Consequence by path: **write_one / maintain_one** feed warnings into the revise loop
  (`wiki_build.py:903`, `:1371`), so a frozen-date warning *can* trigger a revise.
  **Live `_generate`** computes `validate_structure` but only emits it as advisory `lint`
  (`rebuild_engine.py:392-398`) with **no revise loop** — a frozen number in a live draft
  is never auto-corrected. There is **no deterministic date-token rewriter** anywhere.

### 4. People-link enforcement — deterministic `add_links_to_content` vs prompt-only
- **Deterministic linker `add_links_to_content`** (`wiki_build.py:711-781`): two passes
  (exact article-leaf, then registered aliases), self-guards the PII firewall
  (Reference/private targets, `:733`), masks code/links/footnotes.
- Called in **write_one** (`:937`) and **live `_generate`** (`rebuild_engine.py:387`).
- **NOT called in `maintain_one`** — verified the only call sites are `wiki_build.py:937`
  and `rebuild_engine.py:387` (grep). Maintain relies on the prompt-only "LINK EVERYTHING"
  block (`prompts.yaml:988-993`) plus the post-pass `normalize_link_labels` action
  (`actions/wiki_maintain.yaml` step 1d²) — but normalize only cleans labels, it does not
  *add* missing links.
- **Live guide** inherits the linker (it runs through `_generate`), but its revisions are
  steered by a prompt that lacks the CROSS-LINK directive (relies on context).
- All paths share the dead-link backstop chain: `_repair_citation_titles`
  (`wiki_build.py:605`) → `_bad_links` (`:546`) → `_neutralize_links` (`:571`).
  write_one (`:894-933`), maintain_one (`:1361-1364`), live (`rebuild_engine.py:368-380`).

### 5. Structure lint + revise/self-critique pass
- **write_one**: `validate_structure` then a **bounded, non-regressing revise loop** (≤2
  passes, second only on strict improvement) against errors+warnings+dead-links
  (`wiki_build.py:897-925`).
- **maintain_one**: same lint + same bounded revise loop, but keyed on **errors only**
  (`:1389` `prev,cur = len(v["errors"]), len(v2["errors"])`) — warnings don't drive it,
  and dead-links are neutralized pre-loop not fed as issues.
- **Live `_generate`**: `validate_structure` is computed and emitted as advisory `lint`
  only — **NO revise loop at all** (`rebuild_engine.py:392-398`). The human is the
  reviewer. It does add a unique **auto-continue** on truncation (`:309-358`) the batch
  paths deliberately omit (`wiki_build.py:880`).
- **synthesize_wiki / wiki_plan**: NO structure lint — only `validate_citations`
  (citation well-formedness), then write (`synthesize_wiki.yaml`).

### 6. Promotion / AKA / owner-linking / what runs after the write
This is the **largest divergence.** The full build action runs a long deterministic
promotion suite that NONE of the single-article paths run:

`actions/wiki_build.yaml` post-write steps: `link_owner`, `surface_aliases` (the "Also
known as" line), `link_medications` (MedlinePlus), `link_places` (geofence box +
back-link), `suggest_unsaved_places`, `write_disambiguation`, `flag_dead_links`,
`tidy_talk`, `normalize_link_labels`, `flag_ungrounded_reference`, `seed_kb_watermark`.

- **Live Accept** (`routers/rebuild.py:374`) calls only `finalize_rebuild`
  (`wiki_build.py:1681-1721`), which runs `entity_index.rebuild`,
  `write_disambiguation_pages`, `flag_dead_links`. **No** `link_owner`,
  `surface_aliases`, `link_medications`, `link_places`, `normalize_link_labels`, or
  `flag_ungrounded_reference`.
- **Nightly single rebuild** (`rebuild_article`, `:1755`) also calls only
  `finalize_rebuild` — same omissions.
- **Maintain action** (`actions/wiki_maintain.yaml`) runs a *partial* subset:
  `write_disambiguation`, `flag_dead_links`, `link_medications`, `tidy_talk`,
  `normalize_link_labels`, `link_places` — but **not** `link_owner`, `surface_aliases`,
  or `flag_ungrounded_reference`.
- **wiki_update action** (`actions/wiki_update.yaml`) runs the least:
  `write_disambiguation` + `flag_dead_links` only.

### 7. What gets saved / how
- **Build**: `write_batch` returns valid/quarantined; the action saves each valid via
  `write_note` (`wiki_build.yaml` step 6). Quarantined articles are surfaced, never saved
  (`wiki_build.py:2907`).
- **Live**: staged on the run, single `finalize_rebuild` write on Accept with a staleness
  hash guard (`routers/rebuild.py:371-375`); supports opt-in `rename_to` (`:351`,
  `finalize_rebuild` `:1704-1706`).
- **Nightly rebuild**: regenerate-in-place; quarantine → restore prior + open todo
  (`wiki_build.py:1758-1761`).
- **Maintain**: `_apply_maintain` only saves when `changed` (`maintain_one` returns
  `changed = revised != content.strip()`, `:1402`).

---

## Divergences that explain inconsistent formatting / promotion

1. **`add_links_to_content` is missing from `maintain_one`.** Build and live drafts get a
   deterministic people/entity-link backstop; maintained articles get only the prompt's
   "LINK EVERYTHING" plea. An LLM that leaves a name plain during maintenance keeps it
   plain — formatting drifts from freshly-built articles. (`wiki_build.py:937` &
   `rebuild_engine.py:387` are the *only* call sites.)

2. **No deterministic date-token backstop anywhere; the only check is an advisory
   warning** (`wiki_guides.py:318-320`). Build/maintain at least feed that warning into a
   revise loop; the **live path never revises on it** (`rebuild_engine.py:392`). So the
   same article rebuilt live vs. via full build can differ on `@t[...]` usage.

3. **`wiki_revise` omits the DATES directive** (`prompts.yaml:908-932`). A self-critique
   pass triggered for an unrelated lint issue can re-freeze a date the writer correctly
   tokenized.

4. **Live `run_guide` uses a bare steer string** with none of the wiki_write directive
   blocks (`rebuild_engine.py:455-460`). After several guide turns the original directive
   block is far back in context and easily diluted — guide revisions are the weakest at
   honoring date/link/PII rules.

5. **Promotion suite asymmetry (the big one).** `link_owner`, `surface_aliases`
   ("Also known as"), `flag_ungrounded_reference`, and (for live/nightly rebuild)
   `link_medications`/`link_places`/`normalize_link_labels` run on the **full build only**
   (or partially in maintain). A single-article live rebuild therefore can lose its "Also
   known as" line, its MedlinePlus refs, its place-box, and never gets owner-linked or
   grounding-audited. (`wiki_build.yaml` steps 6b–6e vs `routers/rebuild.py:374`.)

6. **Lint loop keys differ**: write_one revises on errors **and** warnings
   (`wiki_build.py:903`); maintain_one revises on **errors only** (`:1371`, `:1389`); live
   revises on **nothing**. Three different convergence behaviors for the same lint output.

7. **`synthesize_wiki`/`wiki_plan` is a wholly separate engine** with no structure lint,
   no `add_links_to_content`, no revise loop, no `@t[...]` directive enforcement beyond
   whatever its own prompt says — only `validate_citations`. It is the strongest
   formatting outlier and should arguably be unified or retired.

---

## Recommendations for the new conversational-edit ("Suggest revisions") mode

The new mode = BASE is the current article (preserved), CONTEXT = curated sources +
read-only backlinks, LOOP = targeted edits. To inherit synthesis's good behaviors:

1. **Route every produced draft (initial + each conversational turn) through the same
   deterministic backstop tail that `_generate` already runs**:
   `_repair_citation_titles` → `_bad_links` → `_neutralize_links` →
   **`add_links_to_content`** (`rebuild_engine.py:368-387`). This is the single most
   important inheritance — it is the only thing that guarantees people-link parity, and
   it already self-guards the PII firewall. The new mode should call it on *every* draft
   shown, exactly like the live engine, not just on Accept.

2. **Add a deterministic date-token pass** (does not exist yet). Since no path enforces
   `@t[...]` deterministically, the cleanest fix is to factor a small `enforce_date_tokens`
   helper that converts unambiguous frozen relative-times (matched by the existing
   `_REL_TIME_RE`, `wiki_guides.py:47`) when the source supplies an anchor, and run it in
   the same backstop tail. Until then, at minimum surface the frozen-date warning in the
   conversational UI and **fold the DATES directive into `wiki_revise`** so revise turns
   stop re-freezing dates.

3. **Give the conversational-edit turns a real structured prompt, not a bare steer.**
   Factor the directive blocks shared today by `wiki_write` and (partially) `wiki_maintain`
   into a reusable fragment — **`{date_rules}`, `{crosslink_rules}`, `{author_rules}`,
   `{grounding_rules}`, `{talk_rules}`** — and inject them into both `wiki_write`,
   `wiki_revise`, `wiki_maintain`, AND the new edit prompt. The "Suggest revisions" loop
   should be a *targeted-edit* prompt (BASE preserved) that still carries these fragments,
   rather than reusing `run_guide`'s context-only steer (`rebuild_engine.py:455`).

4. **On Accept, run the full promotion suite, not just `finalize_rebuild`.** Either have
   the new Accept call a shared `promote_one(conn, title)` that performs the per-article
   subset of the build's post-write steps (`link_owner` if it's a People page,
   `surface_aliases`, `link_medications`, `link_places`, `normalize_link_labels`,
   `flag_ungrounded_reference`), or factor those `wiki_build.yaml` steps into a single
   service function reused by build, nightly rebuild, live Accept, and the new mode. This
   closes divergence #5 for the whole family, not just the new feature.

5. **Reuse `scoped_known_titles` + `known_aliases_block` for cross-link candidates** in
   the edit prompt (as build/rebuild do, `wiki_build.py:808-810`) rather than freezing
   `run.known` at draft time the way `run_guide` does (`rebuild_engine.py:433`).

6. **Inherit the lint/revise model deliberately.** Since the new mode is conversational
   with a human in the loop, mirror `_generate`'s advisory-lint approach (surface
   errors/warnings, let the user steer) — but combine it with backstops #1–#2 so the
   *deterministic* guarantees (links, dead-link neutralize, date tokens) hold regardless
   of what the conversation does, and only the *judgment* calls (structure, prose) are left
   to the human.
