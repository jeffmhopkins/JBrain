# Health Domain Split — Final Implementation Plan

Move a person's **personal medical history** out of their `kb/People/<Name>` article
(today a `## Health` section) into a new dedicated **`kb/Health/<Person>`** domain, so a
person can be shared via a link without leaking their medical data, and PHI lives behind a
single auditable firewall.

This plan is the reconciled output of three independent architect drafts (surgical/security,
robustness/edge-cases, migration/ops) put through an adversarial red-team that verified every
claim against the code. Findings are folded in; the must-fixes the red-team surfaced are
called out inline as **[RT]**.

---

## 0. Locked design decisions

1. **New top-level domain `Health`.** One note per person: `kb/Health/<Leaf>`, where `<Leaf>`
   is the *final path segment* of the person's `kb/People/...` title (so
   `kb/People/Memorial/Dr. Lee` → `kb/Health/Dr. Lee`). Pets (which are People pages) and the
   owner are included.
2. **General medical knowledge stays in `kb/Reference/Medicine/...`** (de-identified science).
   Naming encodes the split: **Health = personal/PII, Reference/Medicine = general science.**
3. **Firewall predicate = title prefix `kb/health/`** (case-insensitive), exposed as one
   shared helper `wiki_guides.is_health_title()` and reused by the lint, the share layer, and
   research scope. PHI recognition lives in exactly one place.
4. **Each person's record is its own note** — this is what makes per-note view-shares safe.
5. **No health reference in the People body**, enforced structurally: the People guide spec
   gains `forbid_link_prefixes: [kb/Health]` so the structure-lint *rejects* any People article
   that links a Health page. Reference's forbid list also gains `kb/Health`.
6. **The Health page links back** to `[[kb/People/<Name>]]` and to the general
   `[[kb/Reference/Medicine/...]]` background. The owner discovers it via backlinks / graph /
   search (all owner-only surfaces, never part of shared content).
7. **Shares of a Health note are PHI-hardened** (forced browser-bind + finite TTL), mirroring
   `create_labshare_link`.
8. **Research/Q&A shares exclude `kb/Health/`** from candidate surfacing and retrieval.
9. **Migration is deterministic, conservative, dry-run-first, idempotent, and undoable**, run
   on demand during a maintenance window (mirrors the shipped-disabled `wiki-build` reorganizer).
   Structured lab numbers in the `lab_results` table are untouched — only the KB *narrative* moves.

---

## 1. Ground truth (verified against the code)

- **Taxonomy is a list.** `wiki_guides.DOMAINS` (`wiki_guides.py:22`). `domain_for_title`,
  `guide_key/guide_title/guide_text`, `spec_for`, `seed_guides`, and `build_index_md`
  (`wiki_build.py:118`) are all **domain-name-derived** — adding `"Health"` to the list +
  a `actions.wiki_guide.health` prompt block auto-seeds `kb/Health/_Guide` and the index
  bucket with no further edits *to those functions*. ✔ verified.
- **Spec is the single source of truth for writing AND linting.** `validate_structure`
  (`wiki_guides.py:102-162`) enforces `forbid_link_prefixes` as a **blocking** error →
  the article is quarantined, not saved. ✔ verified.
- **No "forbidden section" concept exists.** An un-migrated People article that still
  contains `## Health` *prose* (linking only `kb/Reference/Medicine`, not `kb/Health`)
  **keeps passing the lint** after decision #5 ships, and a lint failure only *no-ops* the
  maintain pass (`_apply_maintain`, `wiki_build.py:1176`) — existing content is never
  destroyed. So the prompt/spec change **does not mass-quarantine or lose data.** ✔ verified.
- **Routing.** `_TYPE_DOMAIN` / `_REF_SUB` / `create_article` live in `wiki_build.py:878-932`
  (not `pipeline.py`). Personal medical is **not its own entity type** — it's facts about a
  *person*, so it is steered by the outline/writer prompts, not by `_TYPE_DOMAIN`. ✔ verified.
- **Public read chokepoint.** `share.resolve_active_link` (`share.py:40`) serves exactly the
  token's note; only `kind='note'` links serve `content_md`; the public router takes no
  id/slug and does **not** traverse wikilinks — a `[[kb/People/...]]` link inside a shared
  Health body is inert text. Graph (`graph.py`) and search (`search.py`) are `CurrentUser`-
  gated. lab-share never touches KB prose. ✔ verified.
- **`upsert_note` revives a soft-deleted same-title/same-kind note** (`notes.py:305-316`) →
  rebuilds and re-runs are idempotent and preserve history. ✔ verified.

---

## 2. The must-fixes the red-team surfaced (do NOT ship without these)

> **[RT-1] CRITICAL — second mint path bypasses hardening.**
> `architect.py:_tool_create_share_link` (`~1769-1781`) is a live assistant tool
> (`"create_share_link"`, dispatched `~1994`) that calls `share_svc.create_link(...)`
> **directly**, not through the `mint` router. Hardening only the router leaves "share Jeff's
> health page" via chat minting a permanent, unbound PHI link.
> **Fix:** put the `kb/health/` bind+finite-TTL forcing **inside `share.create_link`** (the
> real chokepoint), so `mint`, the assistant tool, and any future caller inherit it.

> **[RT-2] CRITICAL — leaf collision needs three fixes, not one.**
> `entity_index._link_articles` (`entity_index.py:283`) does
> `SELECT title FROM notes WHERE kind='kb'...` with **no `ORDER BY`**, then first-wins
> `leaf_map.setdefault(normalize(leaf), title)`. Once `kb/Health/Jeff` coexists with
> `kb/People/Jeff` (both leaf `jeff`), the Person entity's `article_title` can bind
> nondeterministically to the Health page — corrupting browse links
> (`EntitiesPage.tsx`, `SearchPage.tsx`), disambiguation (`entity_index.py:416`), incremental
> routing (`_articles_for_note_entities`, `wiki_build.py:1305`), **and** `create_article`'s
> leaf-dedup (`wiki_build.py:902-904`) — which could *fold the two pages into one*.
> **Fix (all three):** exclude `is_health_title()` titles from (a) `_link_articles` leaf-map,
> (b) `create_article` leaf-dedup, (c) disambiguation/ambiguous-terms. Health pages are
> satellites of the People page, never an entity's canonical article.

> **[RT-3] HIGH — the outline/guide prompts must actually be rewritten.**
> The auto-wiring is real for the *plumbing*, but `wiki_outline` (`prompts.yaml:603-665`)
> hardcodes the 6 domains and routes personal medical → People `## Health`. Without rewriting
> it (+ the People and Reference guide prose), a full rebuild **re-injects `## Health` into
> People** and undoes the migration.

> **[RT-4] HIGH — full rebuild (`reset`) deletes Health pages and is unlocked.**
> `reset()` (`wiki_build.py:60`) keeps only `is_protected` titles; `kb/Health/Jeff` has no
> `_` segment, so it is soft-deleted by every rebuild. The rebuild recipe steps `_p_kb_reset`
> / `_p_wiki_write_batch` take **no KB lock**, so a manual Reorganize can interleave with the
> migration. **Fix:** the migration must refuse to run while a rebuild is in progress (and
> vice-versa); rely on `upsert_note` revival + the fixed prompts (RT-3) so a *post-fix*
> rebuild regenerates Health pages correctly.

> **[RT-5] HIGH — ship the `health_split:active` meta gate.**
> `wiki-update` is enabled daily at 02:00. Between deploying the prompt change and running the
> migration, the maintain/revise loop could try to relocate `## Health` ad hoc and race the
> migration, producing a split-brain KB. A one-line meta flag (builders read `## Health`→People
> until it flips, `kb/Health` after; the migration's first successful apply sets it) removes
> the entire half-state race. The footgun of forgetting to flip it is far cheaper than
> split-brain.

> **[RT-6] MEDIUM — migration must be conservative to avoid cutting non-PHI.**
> A broad heading set (`Conditions`, `Labs`, `Procedures`, `Encounters`, ...) is ambiguous
> (employment "Conditions", work "Procedures", social "Encounters"). The lint can't catch a
> wrong cut. **Fix:** match **top-level `##` only**; cut a section *only* if it carries a
> medical signal (a `[[kb/Reference/Medicine/...]]` link or a medical-term hit); route
> anything borderline (incl. medical keywords in the lead/`## Key facts`) to a **review card**,
> never an auto-cut.

> **[RT-7] MEDIUM — footnotes must be copied, never mis-moved.**
> A `[^id]` whose `[^id]:` def is shared between the cut section and surviving People text must
> be **copied** to Health and **kept** in People; only an exclusively-referenced def is removed.
> After the cut, verify every marker resolves on **both** resulting bodies (reuse
> `citation_issues`, `pipeline.py:503`) before committing, else abort that person to a review
> card. A dangling marker is a blocking lint error.

---

## 3. Implementation phases

Ship Phases A–D as one PR (go-forward correctness + firewall + share boundary). Ship Phase E
(migration) as a separate, run-on-demand PR. Exact ordering in §5.

### Phase A — Register the Health domain
- **`wiki_guides.py:22`** — `DOMAINS = ["Reference","People","Health","Groups","Places","Things","Activities"]`
  (Health after People, for index grouping).
- **`wiki_guides.py`** — add `def is_health_title(title) -> bool` returning
  `(title or "").lower().startswith("kb/health/")`. This is the one firewall predicate (#3).
- **`prompts.yaml`** — new `actions.wiki_guide.health` block. Prose: Health is the one PHI home
  per person; lead must link back `[[kb/People/<Name>]]`; link general
  `[[kb/Reference/Medicine/...]]` for background; keep person-specific values/doses/dates/stays
  here; cite `notes/medical/...` sources; never restate general facts. Spec:
  ```spec
  require_lead: true
  recommended_sections: [Conditions, Medications, Labs & vitals]
  recommend_link_prefixes: [kb/People, kb/Reference/Medicine]
  ```
  No `forbid_link_prefixes` (Health *must* link People + Reference). The back-link is
  **advisory, not required** — a person may legitimately have a Health page before a People
  page exists, and a required back-link would quarantine it.
- `kb/Health/_Guide` seeds automatically via `seed_guides` (it is `is_protected`).

### Phase B — Firewall / lint
- **`prompts.yaml` people spec** — add `forbid_link_prefixes: [kb/Health]`.
- **`prompts.yaml` reference spec** — `forbid_link_prefixes: [kb/People, kb/Groups, kb/Health]`.
- **`prompts.yaml` people prose** (`~529-536`) — delete the bullet naming `## Health` as the
  home of medical history and the "Health" sections entry; replace with: *a person's medical
  history lives in their own `kb/Health/<Name>` page, reached via the graph/backlinks, never
  linked or restated here.*
- No `validate_structure` code change is required — the spec edits drive the firewall, keeping
  it auditable in `prompts.yaml`.

### Phase C — Go-forward writing & routing
- **`prompts.yaml` wiki_outline** (`~603-665`) **[RT-3]** — add `Health` to the enumerated
  domains; add the routing rule: a specific person's personal medical history →
  `kb/Health/<their People leaf>`, never in People, never in Reference; one Health page per
  person; mirror the People leaf. Update the JSON example to include a `kb/Health/<Name>` entry.
- **`prompts.yaml` reference prose** (`~499-505`) **[RT-3]** — replace every "People/<Name>
  `## Health`" with "`kb/Health/<Name>`". Keep the general-stays-in-Reference rule.
- **`wiki_build.py`** — add `_health_title_for_person(people_title)` (`kb/People/.../<Leaf>` →
  `kb/Health/<Leaf>`) and `create_health_page(conn, person_title)` (mirrors `create_article`
  but forces `domain="Health"`, sources = the person's `notes/medical/...` notes, runs under
  the KB lock, points the writer at the Health guide). In `update_batch`/`maintain_batch`, when
  a changed source note is medical (under `notes/medical/...`), target the person's Health page
  (create-or-update) **instead of** their People page — *gated by the `health_split:active`
  flag* (RT-5).
- **`entity_index.py` `_link_articles`** **[RT-2]** — skip `is_health_title()` titles when
  building `leaf_map`.
- **`wiki_build.py` `create_article` dedup** (`902-904`) and disambiguation **[RT-2]** — skip
  `is_health_title()` titles so People and Health never fold together.
- Structured labs (`lab_results`) and the `notes/medical/...` captures are unchanged; only the
  synthesized narrative's destination changes.

### Phase D — Share boundary (PHI hardening)
- **`share.py` `create_link`** **[RT-1]** — the true chokepoint. If `is_health_title(title)`:
  force `bind=True` and clamp to a finite TTL (`ttl_days if ttl_days and ttl_days>0 else 14`),
  mirroring `create_labshare_link`. This covers `mint` **and** the assistant tool **and** any
  future caller. Optionally refuse `scope='edit'` on Health notes (public edit-proposal surface
  on PHI), or harden it identically.
- **`share.py`** — add `assert_health_share_policy()` boot invariant (sibling of
  `lab_share_scope.assert_recipient_tools_safe`): assert `create_link` cannot mint a
  `kb/health/` share that is unbound or non-expiring.
- **`research_scope.py` `filter_match_ids`** **[RT-2/M3]** — append
  `AND lower(n.title) NOT LIKE 'kb/health/%'` so Health pages are never surfaced as research
  candidates; add a belt-and-suspenders drop of `is_health_title()` ids in `scoped_search`.
  (Owner approval already default-denies; this is defense-in-depth.)
- **Audit (verified):** `resolve_active_link`/`share_read` serve only the token's note (Health
  shares are intentional and now hardened); `share_attachment` (`share.py:470`) serves a Health
  note's attachments only under an (now hardened) intentional share — fixing RT-1 closes the
  unintended-mint vector; graph/search are owner-gated; lab/guided back no KB note; the
  `/api/system/backup` export is full-PHI **by design**, owner-authenticated only.
- **Web (`web/`)** — for a `kb/Health/` note, the share dialog defaults bind ON, requires a TTL
  (hide the "never" option), hides/hardens "Can edit", and shows a "private health record"
  notice; the research scope picker doesn't list `kb/Health/` prefixes; the person page shows an
  **owner-only** "Health record" link (never serialized into shareable content). Server is
  authoritative; UI just reflects it.

### Phase E — Migration (deterministic, dry-run-first, undoable)
- **Files:** `actions/wiki_extract_health.yaml` (`type: wiki_extract_health`, category
  "Knowledge base", config `dry_run` default **true**, `limit`, `on_conflict` [skip|append|
  replace] default skip, `review` default true), and `workflows/wiki-extract-health.yaml`
  (`enabled: false`, year interval — run-only; manual "Run now" ignores the flag). A new
  `_p_extract_health` primitive in `pipeline.py` (acquires the KB lock, calls
  `wiki_build.extract_health`) plus an optional `wiki_extract_health_undo`.
- **Plan/apply split.** A **dry-run** produces the full report (what would move, collisions,
  citation aborts, borderline review items) with **no writes**; the apply pass mutates.
- **Per person (`kb/People/%` only):**
  1. **[RT-6]** Detect medical section(s) among **top-level `##`** headings only, matched
     against a medical heading set, **and** carrying a medical signal (a
     `[[kb/Reference/Medicine/...]]` link or medical-term hit). Multiple matching sections →
     all move into the one Health page. Borderline / lead / `## Key facts` medical keywords →
     **review card, never auto-cut.**
  2. **Health title** = `kb/Health/<leaf>`. **Collision** (two `Dr. Lee` under different
     practices, or any pre-existing different-owner page) → deterministically disambiguate
     (e.g. `kb/Health/<parent> <leaf>`), recorded in the ledger so re-runs reproduce it, and
     verify the disambiguated leaf does **not** re-collide with a People leaf (RT-2).
  3. **[RT-7]** Footnotes: copy defs referenced by both sides, move only exclusively-referenced
     defs; after the cut, `citation_issues` must be clean on **both** resulting bodies or the
     person is aborted to a review card.
  4. Build the Health body: lead `Personal health record for [[kb/People/<Name>]].` + the
     verbatim cut sections + assembled `## References`. `@t[...]` tokens and `[[wikilinks]]`
     move verbatim. `validate_structure` it; quarantine-to-review on error.
  5. Strip the cut sections (and now-unused defs) from the People note; **no** `[[kb/Health]]`
     link is left behind (would trip the firewall). `validate_structure` the trimmed People
     note; skip-to-review if it would break.
  6. Persist both via `upsert_note(..., source="extract", version_note=...)` (each a new,
     undoable version; the new Health note is its own row → share-safe).
- **Idempotency / resume / no-clobber:** a meta/`kb_structure_log`-style ledger keyed by
  People title; skip if already extracted and the Health page exists; if the Health page exists
  and was **owner-edited**, honor `on_conflict` (default **skip** + review card) rather than
  overwrite. Per-person commit + ledger makes crash-resume clean. `upsert_note` revival avoids
  duplicate rows.
- **[RT-4] Concurrency:** runs under `kb_lock_acquire`; additionally **refuse to run if a full
  rebuild is in progress** (and document not to Reorganize during the migration).
- **[RT-5]** First successful apply sets `health_split:active` so the daily builders switch to
  Health-targeting.
- **Review summary** card: extracted N, moved M sections, skipped K (already/owner-edited),
  C collisions disambiguated, P borderline (manual), Q citation aborts.
- **Undo / rollback:** per-note version restore (built-in) reverses one person; the optional
  `wiki_extract_health_undo` reverses every ledgered person; a pre-flight
  `sqlite3 brain.db ".backup ..."` snapshot is the nuclear option. Document all three.

---

## 4. Tests (`server/tests/`)

- **Taxonomy:** `domain_for_title("kb/Health/X")=="Health"`; `seed_guides` count +1 and
  `kb/Health/_Guide` present + protected; `spec_for("Health")` has no forbid list.
- **Firewall:** a People article linking `[[kb/Health/X]]` → blocking error; same for
  Reference → `kb/Health`; a Health page linking People + Reference/Medicine passes; a Health
  page with no People back-link → warning only (not error).
- **[RT-2] Collision:** `_link_articles` points the `Jeff` entity at `kb/People/Jeff`, never
  `kb/Health/Jeff`, regardless of row order; `create_article` does not fold People↔Health.
- **[RT-1] Share:** `create_link` on a `kb/Health/` title forces bind + finite TTL even with
  `ttl_days=None, bind=False` — asserted for BOTH the `mint` router and the architect tool;
  normal notes unchanged; `assert_health_share_policy` holds.
- **Research:** `filter_match_ids` never returns a `kb/Health/` id even under a matching prefix;
  `scoped_search` drops an injected Health id.
- **[RT-6/7] Migration:** literal and alternate headings move; multi-section union; medical
  signal required (a non-medical `## Conditions` is NOT cut); footnote shared-vs-exclusive
  copy/move with clean `citation_issues` on both pages; `@t[...]`/wikilinks verbatim; collision
  disambiguation deterministic + non-recolliding; idempotent re-run; owner-edited Health page
  not clobbered; dry-run writes nothing; pets/owner included; person-with-medical-notes-no-
  People-page → Health page with advisory-only warning.
- **Registration:** `wiki_extract_health` in `action_types()`; workflow ships `enabled:false`
  and is manually runnable.

---

## 5. Rollout order, back-compat, risks

**Ship order (one release for A–D, then run E):**
1. **A + the RT-2 collision fixes + D** (additive; zero Health pages exist yet, nothing is
   affected; shares/research hardened in advance).
2. **B + C** (firewall spec + outline/guide/routing prompts, builder behavior gated by
   `health_split:active`). Verified non-destructive: existing People `## Health` prose keeps
   passing the lint; a lint failure only no-ops a maintain.
3. **Run `wiki-extract-health` dry-run → review → apply** during a maintenance window, after a
   DB snapshot. First apply flips `health_split:active`; builders now route medical to Health.
4. Normal `wiki_update`/`wiki_maintain` resume, Health-aware.

**Back-compat:** the un-migrated state is stable and lossless. Fresh installs can start with
`health_split:active=1` (no legacy `## Health` to worry about). The flag makes the builder
switchover atomic.

**Residual risks (and mitigations):**
- Owner manually links a People page to a Health page → that page quarantines on next maintain;
  the firewall error message is explicit; documented in the Health guide.
- A **pre-existing share on a People page that still contains `## Health`** leaks until the
  migration strips it → the migration is the fix; optionally surface a review card listing
  active note-shares whose target still holds a medical section so the owner can revoke/re-mint
  before running the split.
- Pets: vet records are treated as PHI and firewalled like anyone else (confirm this is desired;
  if not, exclude `animal`-typed People pages from the migration).
- Citation integrity on cut is the highest-risk corruption path → the copy-not-move rule +
  dual-body `citation_issues` re-check + abort-to-review is the safeguard.

**Single riskiest part:** the leaf collision (RT-2) — silent, nondeterministic, and corrupts
routing/browse/dedup the instant the first Health page coexists with its People page. Its
three-site fix is a hard prerequisite, not optional.

---

## 6. Files touched (summary)

- `server/app/services/wiki_guides.py` — `DOMAINS`, `is_health_title`, (specs via prompts).
- `prompts.yaml` — new `wiki_guide.health` block; edit `people`/`reference`/`wiki_outline`.
- `server/app/services/wiki_build.py` — `_health_title_for_person`, `create_health_page`,
  medical routing in `update_batch`/`maintain_batch`, `create_article` dedup + disambiguation
  Health-awareness, `extract_health` migration.
- `server/app/services/entity_index.py` — exclude Health from `_link_articles` leaf-map.
- `server/app/services/share.py` — PHI-harden `create_link`; `assert_health_share_policy`.
- `server/app/routers/share_admin.py` / `server/app/services/architect.py` — both inherit the
  `create_link` hardening (no per-caller logic needed).
- `server/app/services/research_scope.py` — Health exclusion in `filter_match_ids` +
  `scoped_search`.
- `server/app/services/pipeline.py` — `_p_extract_health` (+ undo) primitive.
- `actions/wiki_extract_health.yaml`, `workflows/wiki-extract-health.yaml` — new.
- `web/` — share dialog + research scope + owner-only health link.
- `server/tests/` — the suite in §4.
