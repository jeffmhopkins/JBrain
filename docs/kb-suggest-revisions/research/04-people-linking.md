# R4 — People linking, promotion, entity index, and the "doesn't link people correctly" bug

Scope: the deterministic people-linking pipeline, the root cause of under-linking on
**rebuild** specifically, and how the new live "Suggest revisions" mode should enforce
linking without breaching the PII firewall. All citations are `file:line` against the
state read on 2026-06-08.

---

## 1. The full people-linking pipeline

There are **two cooperating halves**: an *advisory* prompt instruction (the model is
asked to link) and a *deterministic backstop* (`add_links_to_content`, which links what
the model left plain). They share one source of truth for what is linkable —
`entity_index.alias_surface` — so the offered set never drifts from the protected set.

### 1a. Prompt instruction (advisory)

- The writer prompt is `actions.wiki_write` in `prompts.yaml:835`. The relevant blocks:
  - CROSS-LINKS rule (`prompts.yaml:855-860`): *"when a mention DOES match an EXISTING
    ARTICLES title … you MUST link it, not leave it plain."* — limited to titles in the
    `{known_titles}` list.
  - KNOWN ALIASES block (`prompts.yaml:901-904`): tells the model that a nickname on the
    left should link to the article on the right keeping the nickname as display
    (`[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]`).
- The prompt is assembled once in `wiki_build.build_write_prompt` (`wiki_build.py:784`),
  which fills:
  - `{known_titles}` ← `scoped_known_titles(...)` (`wiki_build.py:808`, def at `:1511`).
  - `{known_aliases}` ← `known_aliases_block(conn, others)` (`wiki_build.py:810`, def at
    `:1478`) — only aliases whose canonical article is in the scoped title set
    (`wiki_build.py:1504-1507`).
- `write_one` (single-shot/batch) duplicates this assembly inline
  (`wiki_build.py:861-873`); the rebuild engine calls the shared `build_write_prompt`
  (`rebuild_engine.py:435`). **Both paths produce the same prompt + alias block.**

### 1b. Deterministic backstop — `add_links_to_content` (`wiki_build.py:711`)

This is the guarantee. It runs in memory (no DB write) so callers can apply it to a
staged draft and persist with the single article write.

- Top self-guard (`wiki_build.py:733-734`): **refuses entirely** if the TARGET article is
  `is_private_title` (Health/Finance) or `domain_for_title == "Reference"`. Returns the
  body unchanged. (PII firewall — a shareable/Reference page must never link People.)
- Pass 1 — canonical article leaves (`wiki_build.py:756-771`):
  - builds `leafmap` from `_known_titles` (`:1471`), **skipping private titles**
    (`:738-739`);
  - `ambiguous` = leaves mapping to ≥2 titles ∪ `entity_index.ambiguous_terms`
    (`:741-746`);
  - `linkable()` refuses if leaf is ambiguous, `len(leaf) < 4`, or a single-word
    `_STOP_LEAVES` member (`:748-754`; stop list at `:1811`);
  - masks code/links/footnote lines via `_mask_spans` (`:757`, def `:1817`), skips
    already-linked targets (`:758,762`), and **links only the FIRST match per target**
    (`break` at `:770`).
- Pass 2 — registered aliases (`wiki_build.py:773-779`) via `_alias_link_props`
  (`:672`): iterates `entity_index.alias_surface` (`:695`); skips self/already-linked/
  already-seen targets (`:696`); one link per canonical target across all aliases
  (`seen_targets`, `:694,706`); round-trip verifies `normalize(surface)==alias_norm`
  and `len(surface) >= 4` (`:703`); re-masks against the post-Pass-1 body so it never
  nests a link inside a Pass-1 link.

### 1c. Where the backstop fires

- `write_one`: `wiki_build.py:937` (after the dead-link neutralize backstop).
- Rebuild engine: `rebuild_engine.py:387`, inside `_generate`, on the joined draft.
- A live **Guide/steer turn** (`run_guide`, `rebuild_engine.py:440`) appends a steer
  user turn and re-streams the FULL article through `_generate` — so `:387` re-runs the
  backstop **on every edit turn**. This is the key reuse for the new mode (§4).
- Standalone proposal/auto path: `check_needed_links` (`wiki_build.py:1836`), same logic.

### 1d. What `alias_surface` requires to link a person (`entity_index.py:855`)

A person is auto-linkable **only if** there is a row in `entity_aliases` JOIN `entities`
where `e.article_title IS NOT NULL` (`:878-882`), surviving the drop rules (`:893-902`):
(i) alias maps to ≥2 article-bearing entities; (ii) alias in `ambiguous_terms`;
(iii) alias norm equals a live article leaf norm (leaf path handles it instead);
(iv) single-token given-name ("jeff") unless an explicit `entity_decisions('alias')`
ruling exists; (v) private/Reference target.

The `article_title` binding is set **only** by `entity_index._link_articles`
(`:529`), which runs **only inside `entity_index.rebuild`** (`:340`). `rebuild` itself
reaggregates from `note_analysis` (`:263`).

---

## 2. ROOT-CAUSE hypotheses for under-linking on rebuild (ranked)

### H1 (most likely) — the entity index is STALE on the rebuild path; no `entity_index.rebuild` runs before drafting

The rebuild session never refreshes the entity index. `run_gather` reads `run.known`
from `_known_titles` (`rebuild_engine.py:142`) and `run_draft` only re-reads titles
(`:433-434`); **nothing calls `entity_index.rebuild`** anywhere in `rebuild_engine.py`
(grep confirms the only `.rebuild(` calls are in `pipeline.py`, `entity_rebuild.py`, and
`wiki_build.py` recategorize/merge/split/promote paths — `wiki_build.py:1718,1988,2273,
2405`). Consequences, all of which silently suppress a legit person link:

- **A newly-promoted/renamed People page is not yet bound.** If `kb/People/<X>` was
  created or retitled since the last `entity_index.rebuild`, its entity row still has
  `article_title IS NULL`, so Pass 2 (`alias_surface`) can't offer it AND the
  `{known_aliases}` block omits it (same `alias_surface` source). Pass 1 can still match
  the *exact leaf*, but any nickname/alias surface ("Jeff" → "Jeffrey Hopkins") is lost.
- **Owner nicknames not seeded yet.** `reconcile_owner` (`wiki_build.py:1074`) seeds the
  owner's declared aliases as `entity_decisions`, but those *"fold into `entity_aliases`
  one rebuild later"* (its own docstring, `:1080-1081`, and the rebuild comment
  `entity_index.py:343-358`). On a rebuild that never triggers `entity_index.rebuild`,
  those decisions never materialize, so drop-rule (iv) (`alias_surface` `:898`) drops the
  bare first name and the owner's nickname stays plain.
- **Confirmation:** in a test/dev DB, link an existing `kb/People/<X>`, then *without*
  running `entity_index.rebuild`, call `add_links_to_content(conn, some_title, prose)`
  where `prose` uses a nickname surface for X. It will under-link. Check
  `SELECT article_title FROM entities WHERE normalized_key=normalize('X')` is NULL, and
  `alias_surface(conn)` omits the alias. Then run `entity_index.rebuild(conn)` and repeat
  — it links. (This mirrors `test_owner_alias_backfill.py`, which exercises exactly the
  rebuild-later fold.)

### H2 (likely, interacts with H1) — drop rule (iv): bare first names are never auto-linked without an explicit alias decision

`alias_surface` `:898` drops any single-token alias ("jeff", "allan", "mom") unless an
`entity_decisions('alias')` ruling backs it. People prose overwhelmingly uses bare first
names. For non-owner people (no `reconcile_owner` seeding), there is usually **no** such
ruling, so first-name mentions are *by design* left plain. This reads to the user as "the
model doesn't link people." It is partly correct behavior (first names are collision
prone), but it is the dominant reason People mentions stay plain on any path.
**Confirm:** inspect `alias_surface(conn)` output for single-token keys — they'll be
absent unless decided.

### H3 (plausible) — `ambiguous_terms` over-suppression after entity merges/families

Two same-surname relatives, a person sharing a leaf with a place/thing, or an alias
colliding with another entity's key all push the term into `ambiguous` (`:741-746`,
`:864-867`) or fail drop rule (i)/(ii) in `alias_surface` (`:895`). The article then gets
NO link for that name even when only one candidate is a real person page. Family-heavy KBs
(the JBrain use case) hit this often. **Confirm:** compare `ambiguous_terms(conn)` against
the names that went unlinked; a name appearing there explains the miss.

### H4 (plausible, leaf-path only) — `len(leaf) < 4` and `_STOP_LEAVES` refusals

Short leaves ("Ada", "Bo", "Mei", "Jo") are refused by `linkable` (`:750`), as are
single-word stop leaves like "Family"/"People"/"Team" (`:752-753`, list `:1811`). A People
page whose leaf is a short given name is never linked by Pass 1. (Pass 2 still has the
`< 4` guard at `:703`.) **Confirm:** find unlinked People whose leaf length < 4.

### H5 (lower) — "first match per target" leaves later mentions plain

Both passes `break` after the first match per target (`:770`, `:707`). This is intended
(one link per article), but a user scanning a long article sees most mentions of a person
unlinked and may report "doesn't link people." Not a bug, but a perception driver.

### H6 (edge) — masking false-positives / private-leaf collision

If a person's name happens to fall inside a footnote definition line, inline code, or an
existing link, `_mask_spans` (`:1817`) correctly skips it — but if the *only* mention is
in such a span the name is never linked. Also, a People page that shares a leaf with a
private Health/Finance satellite is excluded from `leafmap`/`_link_articles`
(`:738-739`, `entity_index.py:553`) — correct for the firewall, but means the entity binds
via leaf only if the public page wins; if binding fails the alias path is the only hope
(→ H1).

**Ranking rationale:** H1 is the structural difference between rebuild and the universal
build (the build orchestrator and the merge/split/promote/recategorize ops all call
`entity_index.rebuild` near their writes — `wiki_build.py:1718,1988,2273,2405`,
`pipeline.py:250` — whereas the rebuild session never does). H2/H3 explain residual
misses common to *all* paths. H4–H6 are narrower.

---

## 3. Why rebuild differs from the universal build

- The universal/maintenance ops rebuild the entity index right next to their writes
  (`wiki_build.py:1718` after promote, `:1988`, `:2273` merge, `:2405` split;
  `pipeline.py:250` is the standalone reindex job). So by the time a person page exists,
  its entity is bound before the next read.
- The **live rebuild engine** is the exception: `run_gather`/`run_draft`/`run_guide`
  never call `entity_index.rebuild`. They consume whatever bindings exist from the last
  nightly/maintenance rebuild. So a freshly-created or renamed People page, or a freshly
  seeded owner alias, is invisible to both the `{known_aliases}` prompt block and the
  Pass-2 backstop until an unrelated rebuild happens to run. **This is the specific
  rebuild-vs-build gap behind the bug report.**

---

## 4. Deterministic enforcement / improvement options for the new mode (and rebuild)

Ranked by value-to-risk. The new live mode already re-streams the whole article each
turn and re-runs `add_links_to_content` at `rebuild_engine.py:387` (§1c) — so most fixes
are about feeding it *fresh, correct* link data.

### O1 — Freshen entity→article bindings before a rebuild/suggest session (fixes H1)
Run a **binding-only** refresh when a session starts (and after any in-session
promote/rename). Two sizing options:
- Full `entity_index.rebuild(conn)` — correct but heavy (`_sync_embeddings` is networked;
  see `entity_rebuild.py:5` warning). Too slow for the request path.
- **Preferred:** factor out `_link_articles` + the `reconcile_owner` fold into a cheap,
  no-embeddings "rebind" entry point and call it at session start. This is exactly what
  binds `article_title` (`entity_index.py:340,529`) and materializes owner aliases
  (`wiki_build.py:1080-1081`) without the embedding cost. Risk: low — it only updates
  `entities.article_title` and `entity_aliases`; no article writes. The PII firewall is
  preserved because `_link_articles` already excludes private titles
  (`entity_index.py:553`) and `alias_surface` still applies drop rule (v).
  *(Caveat: the owner-alias fold is eventual-consistent — it folds on the rebuild AFTER
  the decisions are seeded; if we want first-session correctness, seed via
  `reconcile_owner` then run the alias materialization in the same call.)*

### O2 — Re-run `add_links_to_content` after every edit turn (already true; make it explicit + tested)
`run_guide` → `_generate` → `:387` already does this. For the new mode, keep this as a
hard post-turn step and add a test that a Guide turn re-links a name the model dropped.
Risk: none new; reuses the existing guarded path.

### O3 — Surface "unlinked known person" as a lint warning (don't auto-link aggressively)
After the backstop, compute the set of `alias_surface` / leaf names that *appear in prose
but remain plain* (re-using the `_mask_spans` + `linkable` logic) and emit a
`{"type":"lint", ...}` warning (the engine already emits lint events, e.g.
`rebuild_engine.py:381,390`). This lets the user *steer* ("link Allan everywhere")
rather than the system guessing on collision-prone first names (respects H2/H5 by
design). Risk: low; advisory only.

### O4 — Relax/repair specific guards, narrowly
- (iv) bare first names: only relax for a name that is **unambiguous in this KB** AND
  bound to exactly one People article (so it can't false-link). Better: rely on O3 +
  user steer instead of relaxing, to keep the collision guarantee.
- Short-leaf `< 4`: leave as-is for the auto path; let O3 surface short-leaf People for
  manual confirmation.
Risk: medium — relaxing (i)/(ii)/(iv) directly risks false links to the *wrong* person.
Prefer surfacing over auto-linking.

### O5 — Never weaken the PII firewall (hard invariant for all of the above)
`add_links_to_content`'s `:733` target-refusal, `alias_surface` drop rule (v) `:900`,
`_link_articles`'s private-leaf exclusion (`entity_index.py:553`), and the private skip in
`leafmap` (`:738`) must all remain. No option above touches them. Any "freshen bindings"
step must reuse these exact predicates so a Health/Finance/Reference target still links
nothing.

---

## 5. Invariants a targeted-edit turn MUST preserve

A LOOP edit turn (and its post-turn backstop) must never violate any of these — they are
already enforced; the new mode must keep applying `add_links_to_content`/`_mask_spans` and
not bypass them when patching a draft in place:

1. **Never link inside a private/Reference TARGET article.** The whole article refuses
   linking if `is_private_title(title)` or Reference (`wiki_build.py:733`). A targeted
   patch to such a page must add zero People links.
2. **Never link TO a private/Reference target.** `alias_surface` drop rule (v) `:900`;
   private titles excluded from `leafmap` (`:738`) and from `_link_articles`
   (`entity_index.py:553`). A shareable page must never point at a Health/Finance page.
3. **Never nest links.** Pass 2 re-masks against the post-Pass-1 body (`wiki_build.py:773`
   comment, `_mask_spans` covers `[[...]]` at `:1831`). A targeted edit must re-mask the
   *current* draft text, not a stale snapshot.
4. **Never link inside code or footnotes.** `_mask_spans` masks fenced/inline code and
   `[^sN]:` citation lines (`:1830-1831`). Citations link via the `## References`
   footnotes only.
5. **One link per target.** First-match `break` (`:770`, `:707`) and `seen_targets`
   (`:694`) — a targeted edit must not double-link the same person.
6. **No ambiguous / too-short / stop-word / unverified-title links.** `linkable`
   (`:748`), the `len(surface) >= 4` + round-trip `normalize` check (`:703`), and the
   `bad_links`/neutralize backstop (`:929`) so no dead `[[link]]` survives.
7. **Don't self-link.** `tgt == title` / `art == title` guards (`:762`, `:696`).

---

## 6. Pointers (files cited)

- `server/app/services/wiki_build.py` — `add_links_to_content:711`, `_alias_link_props:672`,
  `_apply_link_props:654`, `_mask_spans:1817`, `_known_titles:1471`,
  `scoped_known_titles:1511`, `known_aliases_block:1478`, `build_write_prompt:784`,
  `write_one:821` (backstop at `:937`), `link_owner:1009`, `reconcile_owner:1074`,
  `_apply_aka_line:1137`, `check_needed_links:1836`, `_STOP_LEAVES:1811`.
- `server/app/services/entity_index.py` — `normalize:39`, `rebuild:263`,
  `_link_articles:529`, `roster:579`, `note_ids_for_name:671`, `ambiguous_terms:824`,
  `alias_surface:855`.
- `server/app/services/rebuild_engine.py` — `_generate` backstop call `:387`,
  `run_gather:120` (`run.known` from `_known_titles` `:142`), `run_draft:401`
  (prompt at `:435`, no `entity_index.rebuild`), `run_guide:440`.
- `server/app/services/wiki_guides.py` — `domain_for_title:103`, `is_private_title:148`.
- `prompts.yaml` — `wiki_write:835` (CROSS-LINKS `:855`, KNOWN ALIASES `:901`).
- Tests grounding the contract: `server/tests/test_alias_linking.py`,
  `server/tests/test_owner_alias_backfill.py`, `server/tests/test_rebuild_refs_links.py`.
