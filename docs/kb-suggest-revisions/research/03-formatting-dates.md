# R3 — Formatting, structure validation, and the date-token bug

Scope: the deterministic date-token system (`@t[...]`), how it flows through
authoring → storage → display, the root cause of the "rebuild uses the wrong
date format" complaint, a menu of deterministic enforcement options, and the
`validate_structure` rules + formatting conventions the new live "Suggest
revisions" mode must keep satisfying.

> Note on the brief: `web/src/timeTokens.ts` does **not** exist. The byte-for-byte
> twin expander lives in **`web/src/time.ts`** (`expandTimeTokens`, line 94). The
> shared pin is `server/tests/fixtures/time_tokens.json`. All references below use
> the real path.

---

## 1. How date tokens are SUPPOSED to work, end-to-end

### 1a. The token grammar (single source of truth: `clock.py`)

`server/app/services/clock.py:105`
```python
_TOKEN_RE = re.compile(r"@t\[(age|until|since):([^\]]+)\]")
```
Three kinds, single-bracket (never `[[wikilink]]`), no braces (won't collide with
the recipe Jinja engine or the legacy `{date}` token), JSON-string-safe:

- `@t[age:YYYY-MM-DD]` → integer years since a birthdate (`clock.py:178-184`).
- `@t[until:ISO]` → countdown ("in 3 days" / "3 days ago" / "now") (`clock.py:186-188`).
- `@t[since:ISO]` → elapsed ("1 month ago" / "just now") (`clock.py:189-192`).

`expand_tokens(text, *, snapshot=False, now=None)` (`clock.py:149`) replaces every
match with its live computed value. Two contracts that matter for the new mode:

- **Never raises, never blanks.** A malformed/parse-failed token is returned
  verbatim (`clock.py:152-153` docstring; `clock.py:176-177`, `185` early-returns
  `m.group(0)`). So a targeted edit that mangles a token degrades to showing the
  raw token, not a blank — visible, recoverable.
- **`snapshot=True`** renders a dated, self-explaining value for evergreen/exported
  contexts, e.g. `40 (as of 2026-06-01; born 1986-03-01)` for age
  (`clock.py:183-184`) and `… (as of <today>)` for until/since (`clock.py:192`).
  Snapshot is **not** used for the live UI body — it's used by the AI-context and
  share/export builders (see 1d).

The timezone resolver is `app_tz()` (`clock.py:37`), order: meta `app_tz` → env
`TZ` → UTC (`clock.py:22-34`). One resolver so cron, buckets, the agent, and the UI
never disagree.

### 1b. Authoring — the writer is told (prose only) to emit tokens

The `actions.wiki_write` prompt's **DATES & TIME** block, `prompts.yaml:868-874`:

```
DATES & TIME — this article is evergreen, so a value that DRIFTS must be written as a
live token, never a frozen number: @t[age:YYYY-MM-DD] … @t[since:…] … @t[until:…].
Use a literal ISO date for fixed historical facts ("diagnosed 2026-06-01").
PRESERVE verbatim any @t[...] token you see in a source; and when a source states a
drifting value AND its anchor (e.g. a birthdate), encode it as a token rather than
copying the stale number.
```

The same instruction is duplicated in the house-style guide (`prompts.yaml:50-55`),
the People guide (`prompts.yaml:487-491`), the Finance guide (`prompts.yaml:716`),
and the maintain prompt (`prompts.yaml:1000-1001`, plus a "convert a frozen number
to a token once its anchor is known" rule at `prompts.yaml:985-986`). The maintain
prompt's analysis/gist handling also says "PRESERVE any `@t[...]` live token
verbatim" (`prompts.yaml:1137`).

**Every one of these is a soft, prose-only instruction to the model. There is no
code that produces, verifies, or repairs a token.**

### 1c. Tokens carry through the source pipeline RAW

`_load_sources` (`wiki_build.py:412`) deliberately passes **raw** note content to
the writer so live tokens survive into the evergreen article (`wiki_build.py:441-444`):

```python
# Pass RAW content (do NOT expand @t[...] tokens): the writer must see the live
# tokens so it can carry them through into the evergreen article — expanding
# here would freeze "Jeff is @t[age:1986-03-15]" into a literal that rots.
```

This is the **only** deterministic guarantee on the write path, and it's purely
*preservative*: if a token already exists in a source note, the model is shown it
verbatim and asked (in prose) to copy it. It does nothing to *create* a token from
a raw "40 years old" fact, and nothing stops the model from expanding/copying it as
a stale literal anyway.

### 1d. Storage = RAW token; display = live expansion

The article body is **stored** with the literal `@t[...]` token (it's just markdown
in `notes.content_md`). Expansion happens at read time:

- **Live PWA note view:** `web/src/pages/NotePage.tsx:465` runs
  `expandTimeTokensMarked(...)` over the body before `renderWikiLinks`.
  `expandTimeTokensMarked` (`time.ts:131`) reuses the byte-for-byte twin
  `expandTimeTokens` (`time.ts:94`) per token, then wraps each *dynamic* value in a
  `#dyn:` link carrier so the renderer marks it (dotted underline + "Live value…"
  tooltip, `time.ts:119-124`). A frozen literal stays plain; only true live values
  get marked — so the UI literally *shows* the user which numbers are live.
- **Shared/guest article view:** `web/src/pages/SharePage.tsx:198` runs plain
  `expandTimeTokens(..., data.app_tz)`.
- **AI-context / synthesis / digest reads (server):** `clock.expand_tokens(...)` is
  called in `notes.py:227,232`, `pipeline.py:710,1054,1061`, `workflows.py:260`,
  and across `architect.py` (575, 578, 640, 699, 704, 706, 732, 2792). Several use
  `snapshot=True` (`pipeline.py:710`, `workflows.py:260`) so the agent sees a dated,
  unambiguous value rather than a bare live number.

The Python `expand_tokens` and the TS `expandTimeTokens` are pinned to be identical
by `server/tests/fixtures/time_tokens.json` (10 live cases + 1 snapshot case),
asserted in `server/tests/test_api.py:1209-1211` and `web/src/time.test.ts`. **Any
change to expansion semantics must update this fixture and keep both twins byte-for-byte.**

So the intended contract is clean: **author a token → store the token → expand live
on every read.** The number you see is always computed from "today"; it can never go
stale *if the token is what got stored.*

---

## 2. ROOT CAUSE — why "rebuild page now" produces wrong/frozen dates

The user's report: during "rebuild page now" the model "doesn't use the proper date
format that auto-updates." The live rebuild path is `rebuild_engine.py` (Stage-2
draft → lint → human Accept). Walking that path:

**There is no deterministic enforcement that a drifting value was tokenized.**
Token usage rests *entirely* on a prose instruction the model may ignore, mis-apply,
or actively undo. Concretely, four failure modes, all consistent with the symptom:

1. **Prompt-only instruction, no check (primary cause).** The DATES & TIME block
   (`prompts.yaml:868`) is advice. The only post-draft lint touching time is a
   single **advisory warning** (`wiki_guides.py:318-320`):
   ```python
   frozen = _REL_TIME_RE.search(body)
   if frozen:
       warnings.append(f'"{frozen.group(0)}" looks frozen — use a live @t[...] token …')
   ```
   `_REL_TIME_RE` (`wiki_guides.py:47-48`) matches `"40 years old" / "aged 40" /
   "3 months ago"`. But it's a **warning, not an error** — `validate_structure`
   returns `ok` based on `errors` only (`wiki_guides.py:327`). In the live engine
   (`rebuild_engine.py:392-398`) the warning is surfaced to the human but **never
   blocks Accept and never rewrites anything.** The revise loop in the *batch*
   writer (`wiki_build.py:902-925`) feeds warnings back to the model, but (a) it's
   still the model deciding whether to tokenize, and (b) the live rebuild engine
   has **no equivalent automatic revise loop** — the human is the only gate. So a
   frozen "40 years old" sails through with at most a yellow note.

2. **The model copies the stale literal instead of the token.** Even though
   `_load_sources` shows the raw `@t[age:…]` token (`wiki_build.py:441`), the model
   frequently *expands it in its head* and writes the resolved number, or copies a
   plain "40 years old" sentence from a note that never had a token. Nothing
   re-tokenizes it. "PRESERVE verbatim any @t[...]" is unenforced.

3. **Continuation/seam mangling can corrupt a token across the cap.** A truncated
   live draft is stitched by `_join_continuation` (`wiki_build.py:161`). The seam
   glue is "" by default (`_seam_separator`, `wiki_build.py:127`), so a token split
   across the cap (`@t[age:` + `1986-03-15]`) should re-form — but `_trim_restated_overlap`
   / `_drop_duplicate_title` operate on the raw join and a restated line containing
   a token could be trimmed. This is a lower-probability contributor, but it means a
   token can be *damaged* (then shown verbatim/garbled per `clock.py:152-153`),
   reinforcing the "wrong format" perception.

4. **Wrong-format vs frozen-value confusion.** The user says "doesn't use the proper
   date format that auto-updates." Two distinct defects both present this way:
   (a) a frozen literal ("40") where a token belonged — value rots; (b) a near-miss
   token the regex can't expand (wrong kind, `{ }` instead of `[ ]`, a space in the
   date) — `_TOKEN_RE` doesn't match, so it renders verbatim as ugly raw text.
   There is **no validation that a token that IS present is well-formed**:
   `validate_structure` only flags *missing* tokens (frozen literals), never a
   *malformed* token. A model that writes `@t{age:1986}` or `@t[years:…]` ships an
   un-expanding token with zero warning.

**Bottom line:** the date-token system is *correct and deterministic on expansion*,
but *non-deterministic on production*. The entire burden of emitting a well-formed
token sits on an LLM following prose, with a single non-blocking warning as the only
safety net, and that net (a) only fires on a narrow set of frozen-literal phrasings,
(b) never fires on a malformed token, and (c) is purely advisory in the live rebuild
path the user is complaining about.

---

## 3. Menu of DETERMINISTIC enforcement options

Goal for the new mode: make tokenization (and token correctness) a *deterministic
post-draft step*, not a model hope. Options, ranked roughly by value/effort, with
false-positive risk assessed. All belong in the post-draft / pre-Accept stage of the
rebuild engine and the new revise loop, so they apply to BOTH batch and live paths.

### Option A — Malformed-token linter (HIGH value, LOW risk). **Recommended.**
Add a check that flags an `@t[...]`-shaped substring that `_TOKEN_RE` does **not**
match, or whose date arg fails `_to_dt` (`clock.py:127`). I.e. detect "the author
tried to write a token and got it wrong": `@t{...}`, `@t[born:…]`, `@t[age:1986]`
(no month/day → `_to_dt` returns None → renders verbatim today). Implementation:
a permissive shape regex `@t\s*[\[{(]` whose hits aren't in `_TOKEN_RE.finditer`.
False-positive risk: ~zero (prose almost never contains `@t[`). Could be an **error**
(block Accept) safely, because a broken token is unambiguously a defect. This is the
single biggest gap — there is currently *no* check for it.

### Option B — Adjacency rewriter: literal value next to a known anchor (HIGH value, MEDIUM risk).
A post-draft linter that detects an age/elapsed literal **adjacent to a date** and
rewrites it to a token, e.g. "40 (born 1986-03-15)" or "born 1986-03-15 … 40 years
old" → `@t[age:1986-03-15]`. The anchor can come from (i) the same sentence, (ii) a
People-article birthdate already in the KB, or (iii) the source notes
(`_load_sources` already has them). Deterministic rewrite is *safe to auto-apply only
when the computed `expand_tokens(@t[age:DATE])` equals the literal the model wrote*
— that round-trip check (does the token reproduce today's number the model used?)
eliminates the false positives (you never replace "40" with a token that renders
"41"). When it doesn't match, downgrade to a warning. Risk: medium without the
round-trip guard, low with it. This directly kills failure mode #2.

### Option C — Promote the frozen-literal warning to an enforced revise step (MEDIUM value, LOW risk).
Keep `_REL_TIME_RE` (`wiki_guides.py:47`) but, in the new mode's revise loop, treat
its hit as something the loop must *resolve* (model rewrites, then re-lint) rather
than a passive warning. Cheap because the regex and message already exist
(`wiki_guides.py:318-320`); the live rebuild engine just needs the batch writer's
bounded, non-regressing revise loop (`wiki_build.py:902-925`) ported in (it currently
has none — see root cause #1). Risk: low (the regex is conservative and won't match a
correctly-tokenized age, by design — see the comment at `wiki_guides.py:45-46`).
False positives possible on genuine historical literals ("scored 40 points"), which
is exactly why it must stay a *warning the model may dismiss with reason*, not a hard
error — combine with Option B's round-trip guard before any auto-rewrite.

### Option D — Token-preservation diff guard (MEDIUM value, LOW risk). **Recommended for the targeted-edit LOOP.**
The new mode does *targeted* edits to a preserved BASE. Compute the set of
`@t[...]` tokens in BASE (`_TOKEN_RE.findall`) and assert every one still present
(verbatim) in the edited draft unless the edit deliberately removed that fact. A
dropped/expanded token across an edit is the most likely *new* regression the
conversational loop introduces (the model "tidies" a token into a number). Cheap,
deterministic, near-zero false positives (only fires when a token genuinely
vanished). This is the loop-specific analogue of "PRESERVE verbatim".

### Option E — Widen `_REL_TIME_RE` coverage (LOW value, MEDIUM risk).
Add "tenure"/"for N years"/"since 20XX" patterns. Marginal: more phrasings caught
but higher false-positive rate (lots of legitimate historical "in 2019"). Defer
unless A–D prove insufficient.

**Recommended bundle for the new mode:** A (malformed-token error) + D (token-
preservation guard in the loop) + B-with-round-trip-guard (adjacency auto-rewrite,
warn on uncertainty) + C (port the bounded revise loop so warnings actually get
worked). A and D are the cheapest, safest, and close the two gaps that currently have
*zero* coverage (malformed tokens; tokens lost across an edit).

---

## 4. `validate_structure` rules the new mode must keep satisfying

`validate_structure(title, content_md)` (`wiki_guides.py:247`) returns
`{ok, errors, warnings, stub, domain}`; `ok = not errors` (`wiki_guides.py:327`).
The spec is layered `_DEFAULTS` ← general guide spec ← domain guide spec
(`spec_for`, `wiki_guides.py:199`); both the writing guide and the lint read the
same fenced ```spec block, so they can't drift (`wiki_guides.py:1-12`). A targeted
edit must not turn any of these from passing to failing:

**Blocking errors (`ok=False`):**
- **Missing lead.** Non-stub articles need ≥30 chars of prose before the first `##`
  (minus the H1) (`wiki_guides.py:277-278`). A targeted edit must not delete the lead.
- **Missing required section.** Per domain `required_sections`
  (`wiki_guides.py:280-282`). Don't remove a required `## Section`.
- **Citation integrity.** When the body has footnote markers `[^id]` OR definitions,
  `pipeline.citation_issues(body)` runs (`wiki_guides.py:287-294`) — every marker
  needs a definition, no duplicate ids, no orphan defs. An edit that touches a `[^id]`
  or `[[Source]] — DATE` line must keep markers↔defs balanced.
- **Markers without a `## References` section** when `require_references_when_cited`
  (`wiki_guides.py:295`). Don't add a marker without the References section.
- **PII firewall.** `forbid_link_prefixes` — a Reference/Health/Finance article must
  not link `[[kb/People/...]]` etc. (`wiki_guides.py:303-310`). A targeted edit must
  never introduce a forbidden cross-link. (Predicates: `is_private_title`
  `wiki_guides.py:148`, `is_health_title` `:127`, `domain_for_title` `:103`.)

**Advisory warnings (don't block, but the loop should resolve, not introduce):**
- Missing recommended section (`wiki_guides.py:283-285`); empty `## References`
  heading with no defs (`wiki_guides.py:300-301`); missing recommended link prefix
  (`wiki_guides.py:312-316`); **frozen relative-time literal** (`wiki_guides.py:318-320`
  — the date-token warning); flat Reference/Finance article that should be foldered
  (`wiki_guides.py:322-325`).

**Stub exemption:** an article < `stub_max_chars` (default 350) with **no** sections
is a stub (`wiki_guides.py:272`) and is exempt from lead/section/citation errors
(`wiki_guides.py:277,281,292`). A targeted edit that *shrinks* an article below the
threshold could silently reclassify it as a stub (hiding real errors); one that
*grows* a stub past it suddenly imposes lead/section requirements. The new mode
should be aware of this boundary when validating a small edit.

---

## 5. Formatting conventions a targeted edit must not break

- **H1 + AKA line layout.** The "Also known as" line is owned *deterministically* by
  `_apply_aka_line` (`wiki_build.py:1137`), which rebuilds the `# H1` / `*Also known
  as: …*` / blank layout each call (idempotent, fence-aware, frontmatter-aware,
  `wiki_build.py:1153-1164`). The conversational loop must **not** hand-edit the AKA
  line — let this function own it, or it'll accumulate blanks / duplicate. Pattern:
  `_AKA_LINE_RE` (`wiki_build.py:1119`); aliases escaped via `_md_escape` (`:1123`).
- **References footnote format.** `[^s1]` markers in prose; definitions under
  `## References` as `[^s1]: [[Exact Source Title]] — DATE` (`prompts.yaml:838-839`).
  Exact source titles so links resolve. Never emit an empty `## References` heading
  (`prompts.yaml:841-842`; advisory-flagged at `wiki_guides.py:300-301`). A dead
  citation link is auto-repaired (`_repair_citation_titles`, `wiki_build.py:605`)
  then dropped (`_neutralize_links`, `wiki_build.py:571`) — a targeted edit that
  introduces a citation to a non-source will have the footnote *deleted*, not kept.
- **Cross-link discipline.** `[[...]]` allowed only for cited source titles or an
  EXISTING ARTICLES title; mandatory when a mention matches an existing title; never
  invent (`prompts.yaml:855-860`, revise `:913-917`). The deterministic backstop
  `add_links_to_content` (`wiki_build.py:711`) re-links bare mentions in memory and
  self-guards the PII firewall (Reference/private targets, `wiki_build.py:733`) — the
  new loop should run it post-edit just like the rebuild engine does
  (`rebuild_engine.py:387`).
- **Evergreen voice.** No "today"/"yesterday"/changelog/"update:" lines — use dates
  or `@t[...]` (`prompts.yaml:995-1001`). The targeted-edit prose must stay encyclopedic.
- **Foldering.** Reference & Finance articles should sit at `kb/<Domain>/<Sub>/<Name>`
  (≥4 path segments), else a warning (`wiki_guides.py:322-325`). A targeted edit
  doesn't rename, so this is mostly a don't-introduce concern.
- **Protected pages.** `is_protected` (`wiki_guides.py:87`) — any `_`-prefixed path
  segment (`kb/_index`, `kb/_Style Guide`, `kb/People/_Guide`). The new mode must
  refuse to target these (they're never overwritten by the writer; `wiki_build.py:236`,
  `:387`).

---

## 6. Pointers for the lead / other agents

- Put any new date enforcement in a single shared helper (alongside `clock.py`) so
  both the batch writer (`wiki_build.write_one`) and the live engine
  (`rebuild_engine`) call it — the live engine currently lacks the batch revise loop
  (`wiki_build.py:902-925`) entirely, which is the structural reason the user's
  "rebuild" path under-enforces tokens.
- Any change to token expansion semantics MUST update `time_tokens.json` and keep
  `clock.expand_tokens` ↔ `time.ts:expandTimeTokens` byte-for-byte (pinned at
  `test_api.py:1209-1211`).
- The malformed-token check (Option A) and the token-preservation guard (Option D)
  are the two zero-coverage gaps and the cheapest high-value wins.
