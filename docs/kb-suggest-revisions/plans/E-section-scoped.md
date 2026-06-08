# Plan E — Section-scoped editing for live "Suggest revisions"

**Stance E.** The conversational loop edits the article **one heading-delimited section at
a time**. Each turn the AI (or the server) decides which `## Section` a turn concerns,
the AI emits only that section's revised body, and the server **splices** it back into the
preserved full article and re-lints the *whole* document. Heading boundaries are the
natural, stable anchors that `validate_structure` already parses (`wiki_guides.py:39-40,
247`), so splicing is deterministic — a middle ground between full-article re-drafts
(token-heavy, noisy diffs: research 01 §6, `rebuild_engine.py:455-461`) and arbitrary
patch/anchor ops (fragile string-matching).

This plan is deliberately a *strong, honest* argument for E. Its core bet: **the section
boundary is a real, already-enforced invariant in this codebase**, so we get localized
edits and bounded tokens for free, without inventing a new patch grammar. Its core risk:
edits that legitimately span sections, the **article-global References footnotes**, and
malformed/section-less articles — all addressed explicitly in §1, §5, §9.

---

## 0. Design decisions up front (so the rest is concrete)

- **BASE preserved.** The current article (`note.content_md`) is the working draft from
  turn 0; we never re-draft it from sources. The loop mutates a server-held
  `run.draft` in place, section by section. (Research 01 §10 "working draft" extension.)
- **Output contract: a FENCED block, NOT tool-use.** The model emits exactly one fenced
  block per turn:

  ````
  ```jbrain-edit
  ## <Exact Heading>
  <full revised body of that section, markdown>
  ```
  ````

  This is decisive and follows research 01 §10's explicit warning
  (`rebuild_engine.py:9-15`): the Stage-2 transcript must carry **no tool_use blocks** so
  resuming a thinking-enabled conversation is "trivially safe." Tool-use + signed thinking
  blocks in one transcript is exactly the fragility the whole engine is architected to
  avoid. A fenced sentinel block keeps the transcript tool-free, parses with one regex
  server-side, and matches how the engine already strips wrapper fences
  (`wiki_build._strip_fence:477`, `_clean_wrapper_fence:493`) and extracts `\`\`\`talk`
  (`_extract_talk:190`). We reuse that machinery instead of inventing JSON tool plumbing.
- **The model addresses a section by its EXACT heading text.** The set of legal headings is
  injected each turn (the server parses them from `run.draft`), so the model can only name a
  heading that exists — or one of two sentinels: `LEAD` (prose before the first `##`) and
  `NEW: <Heading>` (append a new section). Splicing keys off `_SECTION_RE` /
  `_FIRST_SECTION_RE` (`wiki_guides.py:39-40`), the *same* regexes the linter uses, so the
  parser and validator cannot drift.
- **Whole-article re-validation after every splice.** Because References, the lead, AKA,
  citation integrity, and the PII firewall are *document-global* invariants
  (`validate_structure`, `wiki_guides.py:277-325`), the hardening tail (links, dead-link
  neutralize, date tokens, lint) runs on the **full spliced article**, never on the section
  alone (research 03 §4, research 02 rec #1). The section scoping bounds what the *model*
  rewrites; it does **not** narrow what the *server* validates.

---

## 1. Backend architecture

### 1a. The section model / parser — `kb_sections.py` (NEW, small, pure)

Create `server/app/services/kb_sections.py` — a pure, DB-free, well-tested splitter that is
the single source of truth for "what is a section." It **reuses the linter's regexes** so
the model of an article is identical to what `validate_structure` enforces:

```python
# imports _H1_RE, _SECTION_RE, _FIRST_SECTION_RE from wiki_guides (or re-declare-and-pin
# them in a shared module both import, to guarantee one definition).

@dataclass(frozen=True)
class Section:
    key: str          # "LEAD" | exact heading text (e.g. "References", "Early life")
    heading: str | None  # None for LEAD; the raw "## Heading" line otherwise
    start: int        # char offset in the full body where this section begins
    end: int          # char offset where it ends (exclusive)
    body: str         # the section's content (for LEAD: H1+AKA+lead prose; else: heading line + content)

def split_article(content_md: str) -> list[Section]: ...
def section_keys(content_md: str) -> list[str]:       # legal headings + "LEAD"
def get_section(content_md: str, key: str) -> Section | None: ...
def splice_section(content_md: str, key: str, new_body: str) -> str: ...
def append_section(content_md: str, heading: str, new_body: str) -> str: ...
```

**`split_article`** walks `_FIRST_SECTION_RE`/`_SECTION_RE` matches: everything before the
first `##` is the `LEAD` segment (it carries the `# H1` and any `*Also known as:*` line —
see 1c); each subsequent `## Heading … (until next ## or EOF)` is one `Section` keyed by its
exact heading text. Duplicate headings (rare, malformed) are keyed `Heading#2` etc. and the
model is never offered a duplicate to target (it must rename/merge via a NEW section instead).

**`splice_section`** replaces `[start:end)` with the normalized new body, re-joining with
exactly one blank line between sections (the canonical layout the writer emits). It is a
**pure string substitution against the current `run.draft`** — the deterministic, low-risk
core of stance E. No anchor fuzzy-matching, no diff application: the heading is the anchor
and it is exact.

### 1b. The target-and-splice algorithm (per turn)

In the engine (`rebuild_engine.py`, new `run_suggest` generator — see below):

1. **Parse** the current `run.draft` into sections; compute `legal = section_keys(draft)`.
2. **Prompt** the model with: the *full* current article (BASE, for context), the legal
   heading list, the sources + read-only backlinks, the hardening rules (§6), and the user's
   turn. Ask for **one** `\`\`\`jbrain-edit` block: a heading line that is one of `legal`
   (or `LEAD`, or `NEW: <Heading>`) followed by the section's full revised body.
3. **Stream** content deltas to the client (same `content_delta` events as today so the
   panel can show progress, `rebuild_engine.py:332`).
4. **Extract** the fenced block from the joined raw text (reuse `_strip_fence`/regex). Parse
   the first line → `key`; the remainder → `new_body`.
5. **Splice**: `new = splice_section(draft, key, new_body)` (or `append_section` for `NEW:`).
   If the heading isn't legal (model hallucinated one), **fall back**: treat the whole block
   as a `LEAD`/whole-section mismatch → emit a `section_edit` event with `applied:false` and
   a one-line note, and DON'T mutate `run.draft` (the user re-steers). This is the safe
   failure mode — a bad target is a no-op, not a corruption.
6. **Harden the full article** (§5): dead-link repair/neutralize
   (`rebuild_engine.py:368-382`), `add_links_to_content` (`:387`), date-token enforcement
   (NEW, §5a), then `validate_structure` (`:392`) on `new`.
7. **Commit to draft**: `run.draft = hardened`; append any new talk; emit
   `section_edit{heading, applied, draft, lint}` then `done`.

### 1c. Lead / References / AKA special handling

These three are exactly where naïve section editing breaks, so E handles each explicitly:

- **LEAD** is a first-class section key (prose before the first `##`, minus H1, exactly as
  `validate_structure` computes it at `wiki_guides.py:266-268`). The model targets `LEAD`
  to edit the opening paragraph. The H1 line itself is **never** part of an editable body —
  `splice_section("LEAD", …)` preserves the existing `# H1` line and re-attaches it (renames
  go through Accept's `rename_to`, not a section edit).
- **AKA line.** The `*Also known as: …*` line lives in the LEAD segment but is **owned
  deterministically** by `_apply_aka_line` (`wiki_build.py:1137-1171`, research 03 §5).
  Rule: the model is told **never to emit or hand-edit an AKA line** (prompt, §6); if the
  model's LEAD body contains one, the splice strips it (reuse `_AKA_LINE_RE`,
  `wiki_build.py:1119`) and we let `_apply_aka_line` re-assert it during the promotion pass on
  Accept (§5c). This prevents AKA churn/duplication across many turns — the single biggest
  LEAD hazard.
- **References.** The `## References` section holds the footnote *definitions*
  (`[^s1]: [[Title]] — DATE`, `prompts.yaml:838-839`), but the *markers* `[^s1]` are scattered
  across every other section's prose. This is the **cross-section coupling** that makes
  stance E genuinely harder than it looks, and we address it head-on:
  - When the model edits a non-References section that **adds or removes** a citation marker,
    the References section must change too. We do **not** ask the model to also emit a
    References edit in the same turn (that violates one-section-per-turn). Instead:
    1. After splicing the prose section, the server computes the marker set of the *full*
       article and the def set under `## References`.
    2. **Orphan markers** (marker with no def) → the engine emits a `lint` warning AND, on the
       *same* turn, the prompt's contract requires that any new `[^id]` the model introduces be
       accompanied by a def block appended inside the **same fenced edit** under a sentinel
       `--- references` divider (a second mini-block the server folds into `## References`).
       This keeps "one prose section + its own new footnotes" atomic without a second turn.
    3. **Orphan defs** (def with no marker, e.g. the edit deleted the last citing sentence) →
       the server deletes the orphaned def from `## References` deterministically (it's safe:
       a def no marker points at is dead weight; mirrors `citation_issues`,
       `pipeline.citation_issues` via `wiki_guides.py:293`).
  - Editing `## References` *directly* is allowed (the model targets the `References` key) for
    fixing a malformed def, but the model is told not to invent defs for non-source titles —
    `_repair_citation_titles`/`_neutralize_links` (`rebuild_engine.py:372-380`) will drop a def
    that points at a non-source.
  - **Net:** citation integrity is re-checked on the FULL article every splice (§5), so even if
    the per-turn footnote-folding is imperfect, the document never ships with a broken
    marker↔def graph — at worst the user sees a lint warning and re-steers.

### 1d. New / changed files & functions

| File | Change |
|---|---|
| `server/app/services/kb_sections.py` | **NEW** — `Section`, `split_article`, `section_keys`, `get_section`, `splice_section`, `append_section`, footnote-fold helpers. Pure, no DB. |
| `server/app/services/rebuild_engine.py` | **NEW** `run_suggest(run, user_text, max_tokens)` generator (sibling of `run_guide`, `:440`). **NEW** `_suggest_system`/`_suggest_prompt` builders. Refactor `_generate`'s hardening tail (`:366-398`) into a reusable `_harden_full_article(conn, run, draft) -> (draft, talk, lint)` so both `_generate` and `run_suggest` share the dead-link/link/date/structure tail (research 02 rec #1). |
| `server/app/services/wiki_build.py` | **NEW** `enforce_date_tokens(...)` (§5a, research 03 Option A+D); **NEW** `promote_one(conn, title)` (§5c, research 02 rec #4) — does not exist today (grep confirmed). |
| `server/app/routers/rebuild.py` | **NEW** `POST /{run_id}/suggest` (mirror `/guide`, `:289-319`); **NEW** `POST /start-suggest/{slug}` seeding BASE + backlinks (mirror `/start`, `:137`). Accept path (`:322-383`) gains the `promote_one` call inside the lock. |
| `server/app/services/rebuild_runs.py` | **NEW** fields on `RebuildRun` (§3). |
| `server/app/services/architect.py` | No change — its backlinks SQL (`:818-822`) is *copied*, read-only (§4). |

### 1e. Transcript management (the crux, decided by §0)

The fenced-block contract keeps the transcript **tool-free**, so `run.messages` threading is
identical to today's safe Guide path (`rebuild_engine.py:299-308, 455-461`):

- Turn 0 (start-suggest) sets `run.messages = [{"role":"user","content": _suggest_prompt(BASE,
  sources, backlinks, legal_headings, rules)}]`. The assistant's first reply may be a
  no-op acknowledgement (no edit) or the first section edit.
- Each user turn appends **one** user message (the user's words + the *current* legal-heading
  list + a reminder of the rules). `_generate`-style streaming appends the assistant turn
  verbatim (thinking included) — safe because there are no tool_use blocks.
- **Drift control (research 01 §10 risk).** Because we mutate `run.draft` server-side but the
  transcript only *implicitly* tracks it, after every splice the next user turn carries the
  **full current section list** and, when the conversation grows long
  (e.g. > 6 turns or transcript > N tokens), a **resync turn**: a system-style user message
  "Current article is below; future edits target these sections" re-anchoring the model to the
  true `run.draft`. This is cheaper than re-drafting and prevents the model editing a stale
  mental copy. Per-turn cost stays bounded by *section* size, not article size, which is E's
  headline win over full re-draft (research 01 §6: today's loop is "N independent full
  re-drafts").

---

## 2. SSE protocol additions & run state machine

### 2a. New event type: `section_edit`

Add to the event vocabulary (research 01 §4 table; the existing set is
`content_delta`/`thinking_delta`/`lint`/`done`/`error`):

| `type` | Payload | Consumer handling |
|---|---|---|
| `section_edit` | `{heading, key, applied: bool, draft, note?}` | client highlights the target section, swaps in the new full `draft`, shows per-section diff |

Emission order per suggest turn: `thinking_delta*` → `content_delta*` (streamed section body)
→ optional `lint` (warnings, footnote folding, date-token notes) → **`section_edit`** (carries
the full spliced `draft`) → `done` (carries `{draft, lint{ok,errors,warnings,stub}}`, same
shape as `:396-398`). Keeping `done` identical means the panel's existing `done` handler
(`RebuildPanel.tsx:125-131`) still works; `section_edit` is the only net-new branch.

`run_started` for suggest carries `{run_id, slug, title, base_rev, sections}` so the client can
render the section list immediately (sections from `section_keys(BASE)`).

### 2b. State machine deltas

Reuse the existing machine (research 01 §3) with one renamed live state:

- New entry `POST /start-suggest` → status `"suggesting"` (analogue of `"ready"` after the BASE
  is seeded — there's no gather/draft for suggest; the article is ready to edit from turn 0).
- `POST /{run_id}/suggest` → `_generate`-style streaming sets `"streaming"`, returns to
  `"suggesting"` on `done`.
- Accept gate widens: `run.status in ("ready","guiding","suggesting")`
  (`rebuild.py:345`); `is_live` adds `"suggesting"` (`rebuild_runs.py:24`).
- Reject/staleness/lock/TTL/one-per-slug: **unchanged** (research 01 §8 — they key off the live
  page hash, not the draft origin). The suggest run is the same `RebuildRun`, so the registry,
  `_sweep`, and the `_sse` keepalive bridge (`rebuild.py:57-111`) are reused verbatim.

---

## 3. RebuildRun changes

Add to the dataclass (`rebuild_runs.py:27-49`):

```python
kind: str = "rebuild"            # "rebuild" | "suggest" — branches Accept's promotion + UI
base_article: str = ""          # the preserved BASE (== run.draft at turn 0); for the resync turn + diff origin
backlinks: list[dict] = field(default_factory=list)  # [{title, excerpt}] read-only CONTEXT (§4)
turns: int = 0                  # suggest-turn counter, drives the periodic resync
```

`run.draft` is **seeded to `base_article`** at start-suggest (not `""`), and is mutated by
splice each turn rather than wiped (`_generate` wipes it at `:295`; `run_suggest` must NOT).
`run.sources` is set at start-suggest from the curated/seed sources so
`_repair_citation_titles` (`:373`) keeps its grounding set. `base_hash` is still the live-page
hash for the Accept staleness guard (`rebuild_runs.py:35`, `rebuild.py:371`).

---

## 4. Backlinks loading (read-only CONTEXT)

Copy the **inbound-link** SQL from `architect.py:818-822` (verified read-only, `SELECT … FROM
links l JOIN notes n ON n.id = l.source_note_id WHERE l.target_note_id = ?`) into a tiny helper
called once at start-suggest:

```python
def _load_backlinks(conn, title: str, limit: int = 12) -> list[dict]:
    row = notes_svc.get_by_title(conn, title)
    if not row: return []
    rows = conn.execute(
        "SELECT DISTINCT n.title, n.content_md FROM links l JOIN notes n ON n.id = l.source_note_id "
        "WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title LIMIT ?",
        (row["id"], limit)).fetchall()
    return [{"title": r["title"], "excerpt": (r["content_md"] or "")[:400]} for r in rows]
```

**Critical invariant (research 01 §10, research 02 rec #1):** backlinks are injected into the
prompt as **read-only CONTEXT only** — they are **NEVER** added to `run.sources`. `run.sources`
is the curated grounding set that `_repair_citation_titles` (`rebuild_engine.py:373`) and
citation-repair key off; polluting it with backlink titles would let the model cite a
backlinking article as a source and would shift `_repair_citation_titles` grounding. The
prompt labels backlinks explicitly: "These articles link TO this page — context for tone and
cross-linking; do NOT cite them as sources." The PII firewall still applies: a
Health/Finance/Reference target refuses People links regardless of what a backlink says
(research 04 §5, `add_links_to_content` self-guard at `wiki_build.py:733`).

---

## 5. Folded-in hardening (after every section splice, on the FULL article)

The whole point of "re-validate the document, not the section" (§0). After each splice,
`_harden_full_article` runs on the spliced full `run.draft`:

### 5a. Date-token enforcement (research 03)

NEW `wiki_build.enforce_date_tokens(body, sources)` implementing research 03's recommended
bundle, run in the hardening tail:
- **Option A (malformed-token linter, HIGH/LOW):** flag any `@t\s*[\[{(]`-shaped substring not
  matched by `clock._TOKEN_RE` (`clock.py:105`) → a **blocking `lint` error** message
  (a broken token is unambiguously a defect, research 03 §3A). Surfaced, not auto-rewritten.
- **Option D (token-preservation guard, the LOOP-specific win):** compute the `@t[...]` set in
  `run.base_article` and assert each survives in the spliced draft unless the edit deliberately
  removed that fact's section; a dropped/expanded token → `lint` warning "a live date token was
  lost in this edit" (research 03 §3D — the most likely *new* regression a conversational loop
  introduces). Section scoping makes this precise: we only compare tokens *within the edited
  section's prior body* vs its new body, so an untouched section's tokens can't false-trigger.
- **Option B (adjacency rewriter, with round-trip guard):** when a frozen "40 (born
  1986-03-15)" literal sits next to its anchor AND `expand_tokens(@t[age:1986-03-15])` equals the
  literal, auto-rewrite to the token; else warn (research 03 §3B). Conservative; off by default
  if round-trip fails.

Any expansion-semantics touch must keep `clock.expand_tokens` ↔ `time.ts:expandTimeTokens`
byte-for-byte (pinned `time_tokens.json`, `test_api.py:1209-1211`, research 03 §6) — but
`enforce_date_tokens` is *production-side rewriting of stored tokens*, not expansion, so it
doesn't touch the fixture.

### 5b. People-link fix (research 04)

- **Entity rebind at session start (O1, fixes H1).** Factor a cheap, **no-embeddings** rebind
  entry point from `entity_index._link_articles` (`:529`) + the owner-alias fold
  (`wiki_build.py:1080-1081`), and call it once in `start-suggest` so a freshly-created/renamed
  People page's `entities.article_title` is bound before the first edit. The rebuild session
  never refreshes the index today (research 04 §2 H1, §3) — this is THE structural fix. No
  article writes, PII firewall preserved (`_link_articles` excludes private leaves,
  `entity_index.py:553`).
- **`add_links_to_content` on every spliced FULL article (O2).** Already in the shared tail
  (`rebuild_engine.py:387`); `_harden_full_article` runs it on the whole spliced document, so a
  name the model left plain in the edited section links to its kb page, and re-masking
  (`_mask_spans`) runs against the *current* full draft (research 04 §5 invariant 3). It self-
  guards the firewall and links once per target across the whole article (so editing one
  section won't double-link a person already linked in another).

### 5c. Promotion parity on Accept (research 02)

NEW `wiki_build.promote_one(conn, title)` (does not exist today — grep confirmed) factoring the
per-article subset of `wiki_build.yaml`'s post-write suite that single-article paths skip
(research 02 §6, rec #4): `link_owner` (People only), `surface_aliases` (rebuilds the AKA line
via `_apply_aka_line`, `:1137`), `link_medications`, `link_places`, `normalize_link_labels`,
`flag_ungrounded_reference`. Called inside the Accept lock (`rebuild.py:374`) right after
`finalize_rebuild` for `kind == "suggest"` (and reused by classic rebuild + nightly to close
divergence #5 for the whole family). This is where the AKA line E deliberately stripped per-turn
(§1c) gets re-asserted authoritatively.

### 5d. References integrity is article-global (the interaction the brief flags)

Because footnote **defs** live only in `## References` but **markers** are scattered, citation
integrity MUST be validated on the FULL article after each splice — never on the edited section
alone. `_harden_full_article` therefore always runs `validate_structure` (which calls
`citation_issues`, `wiki_guides.py:293`) + `_bad_links`/`_repair_citation_titles`/`_neutralize`
(`rebuild_engine.py:368-380`) on the whole spliced body, and the orphan-marker/orphan-def
reconciliation (§1c) runs there. This is non-negotiable in stance E and is its sharpest
coupling.

---

## 6. Prompt design

NEW `prompts.yaml` key `actions.wiki_suggest` (and reusable fragments, research 02 rec #3).
The suggest prompt — unlike `run_guide`'s bare steer (`rebuild_engine.py:455-460`, which carries
none of the DATES/CROSSLINK rules, research 02 §1, §4) — is a **structured** prompt:

```
You are revising the knowledge-base article "{title}" by editing ONE SECTION per reply.
The CURRENT ARTICLE (your working copy — preserve everything you are not changing):
{current_article}

You may target exactly one of these section headings: {legal_headings}
plus LEAD (the opening paragraph above the first ##) or NEW: <Heading> to add a section.

Reply with EXACTLY ONE fenced block and nothing else:
```jbrain-edit
## <Exact Heading from the list, or LEAD, or NEW: Heading>
<the full revised body of that one section>
```
- Edit only the section the user's message concerns. Leave every other section byte-for-byte.
- DATES & TIME: {date_rules}            # the @t[...] block, prompts.yaml:868-874
- CROSS-LINKS: {crosslink_rules}        # prompts.yaml:855-860 — link existing-article mentions
- Do NOT write or touch the "*Also known as:*" line — the system owns it.
- Citations: a fact from a source uses a [^id] marker. If you ADD a marker, append its
  definition after a "--- references" divider INSIDE this same block; the system folds it
  into ## References. Cite ONLY the source titles listed below — never a backlink.
SOURCES (cite these): {sources}
LINKS-IN (read-only context, do NOT cite): {backlinks}
USER: {user_text}
```

`{date_rules}`/`{crosslink_rules}`/`{author_rules}`/`{grounding_rules}` are factored fragments
(research 02 rec #3) injected here AND retro-fitted into `wiki_write`/`wiki_revise` so revise
turns stop re-freezing dates (research 02 §3). Cross-section / new-section handling lives in
the contract above (`NEW:` + the `--- references` divider). Server-side targeting (when the
*server* decides the section for a fact, e.g. the user says "add that he plays guitar") is the
fallback: if the user's turn names no section and the model's chosen heading is illegal, the
engine emits `section_edit{applied:false}` and asks the user to pick (no silent guess).

---

## 7. Frontend

Reuse `RebuildPanel.tsx` as the template (research 05 §1, §4), as a **new `SuggestPanel.tsx`**
(or a `kind` branch) whose **primary surface is the chat loop**, not the gather/curate wizard:

- **Article + highlighted target.** Render the full `draft` (markdown) with the section named in
  the last `section_edit.heading` visually highlighted (a left border / pulse). Parse sections
  client-side with the same `## ` boundary rule for the highlight only (display, not authority).
- **Conversational input.** Reuse `thread` state + footer composer + the stable-`onClose` ref
  trick (`RebuildPanel.tsx:62-63, 78-79, 283-289`), Enter-to-send. Adopt Chat's optimistic
  user-bubble-on-send (research 05 §4, `Chat.tsx:587`) and replace the canned ack
  (`RebuildPanel.tsx:237`) with a real per-turn summary derived from `section_edit` ("Edited
  **Early life**.").
- **Per-section diff.** Reuse `MarkdownDiff before={note.content_md} after={draft}`
  (`RebuildPanel.tsx:431-435, 458-460`) but default the diff scope to the edited section
  (compute the section slice of BASE vs draft) — this is E's UX payoff: small, focused diffs.
  A toggle expands to the whole-article diff.
- **Accept/Reject.** Reuse the footer + `acceptRebuild`/`rejectRebuild` wrappers (research 05
  §2, `api.ts:955-959`) unchanged.
- **api.ts.** Add a `SuggestEvent` union (`SuggestEvent = RebuildEvent | {type:"section_edit",
  …}`) and `streamSSE`-based wrappers `suggestStart`, `suggestTurn` (research 05 §2,
  `api.ts:875-948`) — do NOT hand-roll a reader.
- **Launch.** A second KB-only `NoteActionsMenu` item "Suggest revisions" next to "Rebuild page
  now" (`NotePage.tsx:265-267`) with the same `llm.ready` pre-flight (`:119-124`); mount
  `SuggestPanel` like `RebuildPanel` (`:379-383`); backlinks come from the existing `note` prop
  (research 05 §3).

---

## 8. Test plan (per CLAUDE.md Definition of Done, per tier)

### 8a. Backend unit — `server/tests/test_kb_sections.py` (NEW, `@pytest.mark.unit`)

Pure, no DB/LLM. The split/splice/re-lint core is where E's correctness lives, so test it hard:
- `split_article` round-trips: `"".join(s.body for s …) reconstructs the input` for: normal
  multi-section article; LEAD-only (stub, no `##`); article with `## References`; H1+AKA+lead;
  duplicate headings; CRLF; trailing-whitespace headings (matching `_SECTION_RE` `\s*$`).
- `splice_section` replaces only the target, leaves siblings byte-for-byte (the central E
  invariant), normalizes blank-line joins, and `validate_structure(title, spliced).ok` is
  preserved when the section was valid.
- `append_section` (NEW) adds before `## References` if present (so References stays last) else
  at EOF.
- Footnote fold: marker added in a prose section + `--- references` def block → def lands under
  `## References`; orphan-def deletion when a marker is removed.
- Malformed/section-less article → `section_keys` returns `["LEAD"]` only; an edit targets LEAD;
  the loop degrades gracefully (no crash, no corruption) — the explicit weak-spot from §9.

### 8b. Backend integration — `server/tests/test_suggest_engine.py` (NEW)

**Copy `test_rebuild_engine.py`** wholesale (research 05 §5b): `_drain(agen)`, `FakeProvider`
scripted at the `llm` seam (`_install_provider`), real temp SQLite with `embeddings.*` no-op'd,
`_mk` seeding, `rebuild_runs.create`. Marked `@pytest.mark.integration`. Cases:
- Happy path: seed BASE article, script a `\`\`\`jbrain-edit` block targeting one section →
  assert `section_edit.applied`, only that section changed, `done.draft` lints `ok`, transcript
  has **no tool_use block** (asserts the §0 safety bet).
- **Cross-section / References-coupling** (the brief's required case): edit adds a `[^s2]` +
  `--- references` def → assert the def folded into `## References` and `citation_issues` is
  clean on the full article; edit removes the last citing sentence → orphan def deleted.
- Illegal-heading hallucination → `section_edit{applied:false}`, `run.draft` unchanged.
- Date hardening: a section whose new body freezes a token that was live in BASE → `lint`
  warning (Option D); a malformed `@t{age:…}` → blocking `lint` error (Option A).
- People-link: a name left plain in the edited section is linked by `add_links_to_content` in the
  full draft; a Health/Finance target adds zero People links (firewall, research 04 §5).
- Entity rebind at start binds a just-created People page (research 04 H1 repro,
  cf. `test_owner_alias_backfill.py`).
- No-credentials path (`creds=False`), `fail_on_turn` error, cancellation (`run.cancelled=True`).
- Promotion parity: a `promote_one` unit test asserting AKA/owner-link/etc. fire on Accept.

### 8c. Frontend — `web/src/components/SuggestPanel.test.tsx` (NEW)

**Copy `RebuildPanel.test.tsx`** (research 05 §5a): mock only `suggestStart`/`suggestTurn` with
`fakeStream` scripts; JSON accept/reject on MSW; `renderWithProviders`; `vi.stubGlobal` for
`confirm`/`alert`; scope footer queries to `.modal-foot`. The existing test never exercises the
guide loop (research 05 §5a "gap to fill"), so these are net-new:
- Script `suggestTurn` to emit `content_delta` + `section_edit` + `done`; assert the optimistic
  user bubble appears, the targeted section highlights, the draft re-renders, and a **second**
  turn works.
- Per-section diff renders the edited slice; Accept posts and `onAccepted` fires.
- **Explicitly UNtested per the brief:** the *guide* loop on the classic `RebuildPanel` stays
  untested (not in scope); we test the *suggest* loop only.

### 8d. e2e — warranted, minimal

The brief: run `./jt e2e` if a user-facing flow / API contract changes — it does (new panel +
endpoint). Add one Playwright spec in `e2e/` driving open → type a turn → see the section edit →
Accept, with the LLM faked at the boundary (`e2e/fake_llm.py`) emitting one `jbrain-edit` block.
Keep it to the happy path; the deterministic hardening is covered at the integration tier.

### 8e. Coverage floor

New code (`kb_sections.py`, `run_suggest`, `enforce_date_tokens`, `promote_one`) is small and
heavily unit-tested, so backend coverage should land **comfortably above** the `fail_under` in
`server/pyproject.toml` — **ratchet the floor up** in the same PR per CLAUDE.md §3 (never lower
it). Frontend `thresholds` in `web/vitest.config.ts`: the new panel test must keep the bar; if
it pushes coverage up, ratchet. CI's four jobs (`back/front/e2e/android`) each enforce their own
floor.

---

## 9. Risks / edge cases (candid, including E's real weak spots)

1. **Edits that legitimately span sections (E's biggest weakness).** "Rename X to Y everywhere"
   or "this fact changes the lead AND the timeline" don't fit one-section-per-turn. Mitigation:
   the loop is conversational — the model makes the changes across **successive turns** (one
   section each), and the panel shows progress; the user confirms. It's more turns than a
   full-rewrite would need for a sweeping change. Honest assessment: for broad cross-cutting
   edits, full-article stance (other agents) is more ergonomic; E wins for the common case of
   targeted factual/structural fixes and loses for sweeping ones. We accept that trade.
2. **Reference/footnote coupling across sections (E's sharpest coupling).** Handled by §1c +
   §5d (atomic per-turn footnote folding + full-article citation re-validation), but the per-turn
   fold is the most intricate part of E and the likeliest place for a subtle bug. The safety net:
   citation integrity is *always* re-checked on the full article, so the worst case is a lint
   warning, never a shipped broken graph. Still, this is real added complexity full-article
   stances avoid.
3. **Add / remove / reorder sections.** Add → `NEW:` sentinel + `append_section` (before
   References). Remove → the model emits an empty body for a non-required section; we drop it
   (required sections refuse — `validate_structure` would error, surfaced as lint). Reorder →
   **not supported per-turn** (a heading is an anchor; moving it is two ops). The user reorders
   via successive NEW/delete or falls back to classic rebuild. Honest weak spot.
4. **No-clear-sections / malformed article.** `section_keys` returns `["LEAD"]` only; the loop
   becomes effectively whole-article editing of the LEAD blob — functional but loses E's
   localization benefit (and large LEAD edits are token-heavy, eroding the headline win).
   Mitigation: when an article is section-less, the prompt invites the model to *introduce*
   sections via `NEW:` so subsequent turns regain localization. Tested in §8a.
5. **Staleness on long conversations (drift).** Server `run.draft` is authoritative, but the
   transcript's mental copy can drift (research 01 §10). Mitigated by the periodic resync turn
   (§1e) and by always sending the current legal-heading list. The Accept staleness guard
   (`rebuild.py:371`) independently protects against a concurrent live-page edit.
6. **Model emits a whole-article rewrite anyway.** The prompt forbids it, but if the model
   returns a full article in the `jbrain-edit` block, the splice fails the legal-heading check →
   `applied:false` + re-steer. No corruption; just a wasted turn.
7. **Splice never corrupts.** Pure string substitution on exact heading offsets — no fuzzy
   anchors — so the deterministic core cannot silently mis-place an edit (vs. the patch-op stance's
   anchor-drift risk). This is E's deterministic-safety argument and it holds.

---

## 10. Sequencing / effort (PR breakdown)

- **PR1 — `kb_sections.py` + unit tests (§1a, §8a).** Pure, no deps, fast to land; the parser/
  splicer is E's foundation. Ratchet backend floor. *(~0.5 day)*
- **PR2 — shared hardening + new helpers.** Refactor `_harden_full_article` out of `_generate`
  (§1d); add `enforce_date_tokens` (§5a) and `promote_one` (§5c) with unit tests; wire
  `promote_one` into the existing Accept path for classic rebuild too (closes research 02 #5
  repo-wide). *(~1 day)*
- **PR3 — entity rebind seam (§5b/O1).** Factor the no-embeddings `_link_articles`+owner-fold
  rebind; test the H1 under-link repro. *(~0.5 day)*
- **PR4 — `run_suggest` engine + `actions.wiki_suggest` prompt + backlinks loader (§1b, §4, §6)
  + integration tests (§8b).** The core. *(~1.5 day)*
- **PR5 — router endpoints + RebuildRun fields + SSE `section_edit` (§2, §3, `rebuild.py`).**
  *(~0.5 day)*
- **PR6 — `SuggestPanel.tsx` + api.ts wrappers + NotePage launch + frontend tests (§7, §8c).**
  Ratchet frontend floor. *(~1.5 day)*
- **PR7 — e2e spec (§8d) + docs.** *(~0.5 day)*

Total ≈ 6 engineer-days. PRs 1–3 are independently useful (they also harden classic rebuild),
de-risking the feature: even if the suggest UX changes, the deterministic hardening and the
section library ship value.

---

### Why E is the right bet (one paragraph)

The section boundary is **already a load-bearing invariant** in this codebase —
`validate_structure` parses and enforces `## Section` structure today
(`wiki_guides.py:39-40, 247-327`). Stance E spends that existing invariant as a free, exact,
deterministic anchor: edits are localized, token cost is bounded by section size (fixing the
"N full re-drafts" cost of today's Guide loop, research 01 §6), diffs are small, and the splice
is pure string substitution that **cannot mis-place** (no anchor fuzz, unlike patch ops; no
whole-article noise, unlike full rewrites). It keeps the transcript tool-free (the engine's
central safety design, `rebuild_engine.py:9-15`) by using a fenced sentinel block, and it routes
every spliced draft through the *full-article* hardening tail so the document-global invariants
(References, lead, AKA, PII firewall, dates, people-links) are honored regardless of how narrow
the edit was. Its honest cost is the References-footnote coupling (§1c/§5d) and sweeping
cross-section edits (§9.1) — real, bounded, and never able to ship a corrupted article.
