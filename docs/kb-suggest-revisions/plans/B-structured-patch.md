# Plan B — Structured patch / edit-ops protocol

**Stance:** the "Suggest revisions" loop does NOT re-emit the whole article each turn.
Each turn the model emits a small list of **structured edit operations**; the server
applies them deterministically to a carried-forward **working draft**, re-runs the
existing hardening backstops on the *patched* result, and streams the new draft (plus a
per-op applied/failed report) back to the panel. BASE = the current article, preserved
verbatim and only mutated by applied ops.

This document is a complete implementation plan for that stance, grounded in the five
research notes (`docs/kb-suggest-revisions/research/01..05`) and the real code they cite.
Citations are `path:line` against the tree on 2026-06-08.

---

## 0. Why B (one paragraph, honest)

Today's Guide turn is "N independent full re-drafts sharing one growing transcript"
(`research/01-rebuild-engine.md` §6): `run_guide` literally asks for *"the COMPLETE
revised article"* (`server/app/services/rebuild_engine.py:455-460`), `_generate` wipes
`run.draft=""` (`:295`) and re-streams the whole body. That is token-expensive, latent,
and—because every turn regenerates the whole article from a diluting transcript—**drifts**
the most on exactly the things the user is steering (dates, links, structure;
`research/02` §4, `research/03` root-cause #1). B fixes the cost/latency/drift at the
root: the model only describes *what changes*, the deterministic working draft is the
single source of truth, and BASE-preservation is a property of the apply algorithm (an
untouched span is byte-identical to BASE) rather than a hope about the model. The price
is real: an apply layer with match/failure/idempotency logic, more server code, and more
tests. The rest of this plan pays that price carefully and argues it is worth it.

---

## 1. Backend architecture

### 1.1 New & changed files

| File | Change |
|---|---|
| `server/app/services/edit_ops.py` | **NEW.** Pure module: the edit-op schema, the parser (fenced-JSON extraction + validation), and the deterministic `apply_ops(draft, ops)` algorithm. No DB, no LLM — `@pytest.mark.unit`. |
| `server/app/services/hardening.py` | **NEW.** Factor the deterministic backstop tail out of `_generate` into one reusable `harden_draft(conn, title, draft, *, known, source_titles, base_draft)` so draft / guide / the new patched-draft path can't drift (`research/02` rec #1). Adds the two NEW backstops: `enforce_date_tokens` (research/03 Option A+D) and the people rebind hook. |
| `server/app/services/rebuild_engine.py` | Add `run_suggest_start`, `run_suggest_turn`; refactor `_generate`'s tail to call `hardening.harden_draft`. Reuse `run_gather`. |
| `server/app/services/rebuild_runs.py` | Extend `RebuildRun`: `mode`, `base_draft`, `working_draft`, `backlinks`, `last_ops`, `entity_rebound`. |
| `server/app/routers/rebuild.py` | Add `POST /suggest/start/{slug}`, `POST /{run_id}/suggest`. Reuse `/accept`, `/reject`, `_sse`, `_kb_note`, `_live_run`. `/accept` gains the `promote_one` call. |
| `server/app/services/wiki_build.py` | Add `promote_one(conn, title)` (research/02 rec #4); call it from `finalize_rebuild` or from the accept handler. Add `backlinks_context(conn, title)` (read-only). Add `rebind_entities_cheap(conn)` (research/04 O1). |
| `prompts.yaml` | New `actions.suggest_edits` prompt (the edit-ops contract) + factored `{date_rules}` / `{crosslink_rules}` fragments (research/02 rec #3). |

### 1.2 The edit-op schema (the heart of B)

Ops are a JSON array. Each op has a discriminating `op` field. **Three op kinds**, chosen
to be (a) easy for the model to emit reliably, (b) deterministically locatable, (c) able
to express every real revision:

```jsonc
// 1) Replace an anchored span. The workhorse — corrections, rewrites, deletions.
{ "op": "replace", "find": "<exact text from the CURRENT working draft>",
  "with": "<new text>", "nth": 1 }            // nth optional, default 1 (first match)

// 2) Insert relative to an anchor (before|after). Additions without touching the anchor.
{ "op": "insert", "where": "after", "anchor": "<exact text>", "text": "<new text>" }

// 3) Replace a whole Markdown section by heading (## Heading … up to next same/higher ##).
{ "op": "section", "heading": "## Timeline", "with": "## Timeline\n\n<new body>" }
```

Design rationale:
- **No line/offset addressing.** Offsets are brittle the instant any earlier op shifts the
  text (the central failure of naive patch protocols). Anchors are *content*, resolved
  against the live working draft at apply time, so they survive surrounding churn.
- **`delete` is `replace` with `with:""`** — fewer op kinds, fewer model mistakes.
- **`section` is a coarse, very-reliable op.** The model uses it when restructuring a whole
  section (where a fine `find` would be long and error-prone); the server locates the
  heading deterministically. This is the "emit only changed sections" idea from
  `research/01` §10 *as one op kind*, not the whole protocol.
- **`nth`** disambiguates a `find` that legitimately occurs multiple times; default 1 plus
  the ambiguity rule below (1.4) keeps the model honest.

### 1.3 The apply algorithm — match strategy

`apply_ops(draft: str, ops: list[dict]) -> ApplyResult` where
`ApplyResult = {draft: str, applied: list[AppliedOp], failed: list[FailedOp]}`.

Match strategy, in strict precedence, per `replace`/`insert` op:
1. **Exact match (primary).** `draft.find(find)` / count occurrences. If exactly the
   requested `nth` occurrence exists → apply. Exact match is the contract we *teach* the
   model (§6) and the only path that auto-applies silently.
2. **Whitespace-normalized match (fallback, low-risk).** If no exact hit, retry matching a
   whitespace-collapsed (`\s+`→` `) version of both `find` and `draft`, mapping the hit
   back to the real span. Covers the model re-flowing a quoted anchor's internal spaces —
   common and safe. A whitespace-fallback hit is applied but **flagged** `fuzzy:true` in
   the report so the UI can mark the hunk.
3. **No further fuzzing.** We deliberately do **not** do edit-distance / token-overlap
   fuzzy matching: a "close" anchor that silently lands on the wrong span is worse than a
   clean rejection. A miss → the op **fails** (1.5).

`section` matching: locate `^## <Heading>\s*$` (case-insensitive, trimmed) and take the
span to the next `^#{1,2} ` or EOF (so a `##` section ends at the next `##` or `#`). Zero
or ≥2 heading matches → fail the op.

**Application is sequential and order-stable.** Ops are applied in array order against the
*evolving* string; each op's match is resolved against the result of all prior ops. This
makes overlapping/adjacent edits deterministic and lets the model emit a delete-then-insert
pair. To keep matches honest under sequential application we resolve each op's span on the
current buffer and splice by absolute offset, so a later op's `find` sees the already-patched
text (the model is told the ops are applied top-to-bottom — §6).

### 1.4 Overlap & ambiguity rules
- **Ambiguous anchor (count ≠ expected).** If `find` occurs N>1 times and the op gives no
  `nth`, the op **fails** with reason `ambiguous` (don't guess). If `nth` is given but
  out of range → `fail` `nth_out_of_range`.
- **Overlapping applied spans.** Because we apply sequentially against the evolving buffer,
  two ops can't physically overlap (the second matches post-first text). The only real
  hazard is op A deleting text that op B's anchor relied on → B then fails cleanly
  (`anchor_not_found`) rather than corrupting. That is the *desired* behavior.
- **Idempotency.** `replace` where `with == find` is a no-op (recorded `applied,
  noop:true`). A `replace` whose `with` already equals the surrounding text is still a
  textual replace but produces an identical buffer — harmless. We do **not** attempt
  cross-turn dedup; each turn's ops are independent against that turn's working draft.

### 1.5 Apply-failure handling (the key UX/robustness decision)

A failed op is **never** silently dropped and **never** corrupts the draft. Policy:

1. **Partial-apply is allowed.** Good ops apply; bad ops are collected into `failed[]`
   with `{op, reason, anchor}`. The patched draft (from the good ops) is hardened and
   streamed — the user still sees progress.
2. **One automatic model retry for the failed ops only.** If `failed` is non-empty after
   the first pass, the engine appends a terse system-style user turn: *"These ops did not
   apply against the current draft (anchor not found / ambiguous). Here is the current
   draft between markers. Re-emit ONLY corrected ops for these changes, or an empty array
   if no longer applicable."* plus the current working draft. We stream **one** retry turn,
   re-parse, re-apply against the (now possibly partly-patched) working draft. This mirrors
   the existing **one** auto-continue cap in `_generate` (`rebuild_engine.py:309`
   `for ... range(2)`).
3. **Still-failed after retry → surface, don't fail the turn.** Remaining failures ride
   out on the `ops` SSE event as `failed[]`; the panel shows a non-blocking banner ("2
   suggested changes couldn't be placed — rephrase or try again") and the conversation
   continues. The working draft is whatever the applied ops produced.
4. **Parse failure (no valid JSON block / schema-invalid).** Treated like an all-failed
   turn → one retry with an explicit "emit a fenced ```json block of ops" reminder, then a
   surfaced error. The working draft is unchanged from the prior turn (BASE-preservation
   holds trivially).

### 1.6 Transcript management (the research/01 §7 hazard, solved)

**Decision: ops are parsed from a fenced ```json block in the assistant's TEXT, NOT
tool-use.** This is the explicit recommendation of `research/01` §10 / §7 risk: tool_use +
thinking in one resumed transcript reintroduces the signed-thinking-block preservation
fragility the whole engine is built to avoid (`rebuild_engine.py:9-15`). By keeping ops as
text:
- The suggest transcript is **structurally identical to the Stage-2 draft transcript**:
  `tools=[]`, thinking allowed, assistant turns appended verbatim by `stream_turn`
  (`rebuild_engine.py:320`, §5 of research/01). "Has no tool_use blocks to preserve —
  trivially safe" (`rebuild_engine.py:9-15`) keeps holding.
- We reuse the existing append-one-user-turn-per-turn shape of `run_guide`
  (`rebuild_engine.py:455-461`) — only the *instruction* and the *parse-and-apply* differ.

**Transcript drift control.** Each turn we append (user = the steer + the CURRENT working
draft between explicit `<<<DRAFT` / `DRAFT>>>` markers; assistant = its ops block). The
embedded current-draft snapshot **re-grounds** the model every turn, so the conversation
can't drift from the actual server-side working draft even after many turns (the staleness
risk flagged in `research/01` §10). To bound transcript growth on long sessions we keep
only the **last K=3 turns** of prior steer/ops pairs plus a single rolling user turn that
carries the current working draft (older turns are summarized to one line each). The
working draft itself lives on the run, not reconstructed from the transcript — the
transcript is advisory context, the run's `working_draft` is canonical.

### 1.7 The engine generators

```text
run_suggest_start(run):        # entered after curate (reuse run_gather + curate UI)
  load curated sources (reuse _load_sources, build_write_prompt grounding)
  run.base_draft   = current article body (the preserved BASE)
  run.working_draft = run.base_draft
  run.backlinks    = wiki_build.backlinks_context(conn, run.title)   # §4
  run.entity_rebound = wiki_build.rebind_entities_cheap(conn)        # §5b, O1
  seed run.messages = [ system-equivalent user turn: actions.suggest_edits
                        filled with sources + backlinks + BASE draft ]
  emit  {type:"suggest_ready", draft: base_draft, backlinks:[titles]}
  # NOTE: no LLM call on start — start just stages BASE + context.

run_suggest_turn(run, instruction):
  append user turn = steer(instruction) + current working_draft snapshot
  for attempt in range(2):            # pass 0 = ops, pass 1 = retry failed ops
     stream one assistant turn (tools=[], thinking on) -> assistant text
       (stream thinking_delta as today; DO NOT stream the raw ops as content_delta)
     ops, parse_err = edit_ops.parse(assistant_text)
     result = edit_ops.apply_ops(run.working_draft, ops)
     if not result.failed and not parse_err: break
     # else build the "retry only failed ops" user turn and loop once
  patched = result.draft
  hardened, talk, lint = hardening.harden_draft(conn, run.title, patched,
                 known=run.known, source_titles=[s["title"] for s in run.sources],
                 base_draft=run.base_draft)
  run.working_draft = hardened ; run.draft = hardened ; run.talk += talk
  run.last_ops = {applied, failed}
  emit {type:"ops", applied, failed, draft: hardened}     # §2
  emit {type:"done", draft: hardened, lint, truncated:false}
```

The `done` event keeps the existing shape so the panel's `handleDraft` `done` branch is
reusable (`research/05` §1). The **new** `ops` event carries the diff metadata.

---

## 2. SSE protocol additions & state-machine deltas

### 2.1 New event types (added to `RebuildEvent` union, `web/src/api.ts:859-869`)

| `type` | Payload | Consumer |
|---|---|---|
| `suggest_ready` | `{draft, backlinks: string[], sources: string[]}` | seeds the working draft + BASE + context chips on entry to the loop |
| `ops` | `{applied: AppliedOp[], failed: FailedOp[], draft: string}` | highlight changed hunks, render the per-turn "what changed" summary, show failed-op banner |

`AppliedOp = {op, summary, fuzzy?: bool, noop?: bool, span?: [start,end]}` (span is in the
**post-patch** draft so the UI can highlight). `FailedOp = {op, reason, anchor}`.

We **reuse** `thinking_delta`, `lint`, `done`, `error` verbatim. We **do not** stream
`content_delta` in suggest mode (the body isn't typed token-by-token; it appears as a
patched whole on `ops`/`done`). `done.draft` remains the authoritative full body, so a
client that ignores `ops` still gets a correct draft.

Wire framing is unchanged: `_sse` already emits `event: {type}\ndata: {json}\n\n`
(`rebuild.py:99`); the client ignores the `event:` line and parses `data:`
(`api.ts:911-921`).

### 2.2 Run state machine deltas

Reuse the existing statuses (`rebuild_runs.py:45`). Add `mode ∈ {"rebuild","suggest"}` on
the run. Transitions for `suggest`:

```
create(mode="suggest") -> "streaming"
  run_gather ............. -> "sources_ready"     (reuse, unchanged)
  POST /suggest (first) .. seeds working_draft, status "ready"  (suggest_ready)
  POST /{id}/suggest ..... status "guiding" -> "streaming" (during) -> "ready" (done)
  POST /{id}/accept ...... "ready"/"guiding" gate already allows (rebuild.py:345)
  POST /{id}/reject ...... drop()    (unchanged)
```

`is_live` (`rebuild_runs.py:145-154`) already covers `streaming|ready|guiding` — no change.
Accept's `status in ("ready","guiding")` gate (`rebuild.py:345`) works unchanged because a
suggest turn lands in `ready`. `_sweep`'s never-reap-`accepting` rule (`rebuild_runs.py:71`)
is untouched and still protects the commit.

---

## 3. RebuildRun changes (`rebuild_runs.py:27-49`)

Add fields (defaults keep `rebuild` mode behavior byte-identical):

```python
mode: str = "rebuild"                  # "rebuild" | "suggest"
base_draft: str = ""                   # the preserved BASE article (suggest mode)
working_draft: str = ""                # the carried-forward, deterministically patched draft
backlinks: list[dict] = field(default_factory=list)   # [{title}] read-only CONTEXT (§4)
last_ops: dict = field(default_factory=dict)          # {"applied":[...], "failed":[...]} last turn
entity_rebound: bool = False           # set once per session by rebind_entities_cheap (§5b)
```

`run.draft` keeps its meaning (the staged body shown/accepted); in suggest mode it tracks
`working_draft`. `base_hash` (`:35`) is unchanged — staleness still keys off the *live page*
(`rebuild.py:371`), not the draft origin, so Accept needs zero change there (`research/01`
§10: "`base_hash` / staleness / Accept need no change").

---

## 4. Backlinks loading (read-only CONTEXT)

New `wiki_build.backlinks_context(conn, title) -> list[dict]` reusing the **exact** inbound
SQL at `architect.py:818-822`:

```sql
SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id = l.source_note_id
WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title
```

(resolve the article's `note_id` via `notes_svc.get_by_title`). Returns `[{title}]`,
capped (e.g. 20) and excluding the article itself. These titles are rendered into the
suggest prompt under a **clearly-labeled read-only "Backlinks (context only — do not cite,
do not rewrite)"** block.

**Critical grounding invariant (research/01 §10, research/02 rec):** backlinks must **NOT**
be injected as curated `run.sources`. `_repair_citation_titles` keys off `run.sources`
(`rebuild_engine.py:373`), and `harden_draft` will pass the same `source_titles`. If
backlinks leaked into `run.sources` they'd become citation-repair targets and shift
grounding. They live only on `run.backlinks`, are passed to the *prompt* as context, and are
never in `source_titles`. Cross-link discipline is still handled by `run.known` + the
`add_links_to_content` backstop, which already validate against the full known-title set —
so a legitimately-linkable backlink article still gets linked by the backstop, but is never
a *citation* source. This keeps `_repair_citation_titles` grounding exactly where it is.

---

## 5. Folded-in hardening — how the PATCHED draft flows through

All three backstops run inside `hardening.harden_draft`, called on the **patched** working
draft every suggest turn (and reused by classic draft/guide so the family can't drift —
`research/02` rec #1). Ordering matters; here it is, with the patch-offset hazards solved.

### 5a. Date-token enforcement (research/03)
Bundle **Option A (malformed-token error) + Option D (token-preservation guard) + Option B
(adjacency auto-rewrite with round-trip guard)**, factored into `clock.enforce_date_tokens`
so both batch and live share it (research/03 §6):

1. **Option D — preservation guard (loop-specific, highest value here).** Compute
   `set(_TOKEN_RE.findall(run.base_draft))` (regex at `clock.py:105`) and assert each token
   still present verbatim in the patched draft. A token that vanished across an edit (the
   model "tidied" `@t[age:1986-03-15]` into "40") is the *new* regression the loop can
   introduce. If a token disappeared and the surrounding fact is still present, re-insert
   the token (or, conservatively, emit a `lint` warning + a `failed`-style note). Near-zero
   false positives — fires only when a token genuinely left.
2. **Option A — malformed-token linter.** Flag any `@t\s*[\[{(]`-shaped substring not in
   `_TOKEN_RE.finditer` (or whose date arg fails `clock._to_dt`). Emit as a blocking-ish
   `lint` (surfaced, never auto-blocks Accept in this human-in-loop mode, per research/03
   §3 / research/02 rec #6). Zero false-positive risk.
3. **Option B — adjacency rewriter, round-trip-guarded.** A frozen "40 (born 1986-03-15)"
   adjacent to an ISO anchor is rewritten to `@t[age:1986-03-15]` **only when**
   `clock.expand_tokens("@t[age:1986-03-15]")` equals the literal the model wrote (the
   round-trip guard eliminates "replace 40 with a token that renders 41"). Otherwise →
   warning.

**Patch-offset safety:** date enforcement runs *after* `apply_ops` on the whole patched
body, by re-scanning text (regex/substring), never by absolute offsets carried from the
patch — so it cannot collide with op spans. Any change to expansion semantics must keep
`clock.expand_tokens` ↔ `web/src/time.ts:expandTimeTokens` byte-for-byte and update
`server/tests/fixtures/time_tokens.json` (research/03 §6) — but A/B/D are *production*
helpers, not expansion changes, so the twin/fixture stay untouched.

### 5b. People-link enforcement (research/04)
1. **Session-start rebind (O1, fixes H1).** `wiki_build.rebind_entities_cheap(conn)` — a
   no-embeddings refresh that runs `entity_index._link_articles` + the `reconcile_owner`
   alias fold (`research/04` O1; binds `entities.article_title`, `entity_index.py:529,340`)
   **without** the networked `_sync_embeddings`. Called once in `run_suggest_start`
   (`run.entity_rebound=True`). This is the structural fix for "rebuild doesn't link people"
   — the live engine never refreshed bindings (`research/04` §3). PII firewall preserved:
   `_link_articles` already excludes private titles (`entity_index.py:553`) and
   `alias_surface` keeps drop-rule (v) (research/04 O5).
2. **Per-turn backstop.** `harden_draft` calls `add_links_to_content(conn, run.title,
   patched_draft)` (`wiki_build.py:711`) on the patched body — exactly as `_generate` does
   at `rebuild_engine.py:387`. It self-guards the PII firewall (`wiki_build.py:733`).

   **The offset-collision concern, solved:** `add_links_to_content` *inserts* `[[...]]`
   wikilinks into the body, shifting offsets. This is safe because **we run it on the final
   patched string, after `apply_ops` has completed and returned a plain string** — there
   are no live op-offsets to invalidate (op spans were resolved and spliced during apply).
   The `span` we report in the `ops` event for highlighting is computed from a *diff*
   between pre-harden and post-harden text (§7), not from raw op offsets, so link insertion
   shifting the body doesn't corrupt the highlight. `add_links_to_content` itself re-masks
   the *current* body each call (`_mask_spans`, research/04 invariant #3), so it never
   nests links or links inside a citation the patch just added.
3. **O3 advisory (optional, recommended).** After the backstop, surface "unlinked known
   person still plain" as a `lint` warning so the user can steer ("link Allan everywhere")
   rather than the system auto-linking collision-prone first names (research/04 O3, respects
   drop-rule iv/H2). Cheap, advisory.

### 5c. Promotion parity on Accept (research/02 rec #4)
Add `wiki_build.promote_one(conn, title)` — the per-article subset of the build's
post-write promotion suite that single-article paths skip today (research/02 §6): the
already-existing `link_owner` (`wiki_build.py:1009`), `surface_aliases`
(`wiki_build.py:1177`), `normalize_link_labels`, `link_medications`, `link_places`,
`flag_ungrounded_reference` (`wiki_build.py:1428`). Wire it into the Accept commit so the
suggested article gets its "Also known as" line, MedlinePlus refs, place box, owner link,
and grounding audit — closing divergence #5 for the whole family (build/nightly/maintain
benefit too). Two placement options:
- **(a) inside `finalize_rebuild`** (`wiki_build.py:1681`) after `entity_index.rebuild` —
  cleanest, makes *every* finalize (nightly + suggest + classic rebuild) get parity.
- **(b) in the accept handler** after `finalize_rebuild`, suggest-only — narrower blast
  radius. **Recommend (a)** with a feature flag arg so the same PR fixes nightly rebuild's
  documented omission, but gate behind a test that nightly behavior doesn't regress.
`promote_one` runs *inside the KB write lock* the accept handler already holds
(`rebuild.py:360-378`) and before `conn.commit()`.

---

## 6. Prompt design (`actions.suggest_edits`)

A real structured prompt (not a bare steer — research/02 rec #3), assembled like
`build_write_prompt` (`wiki_build.py:784-818`) but for *edits*. Sections:

1. **Role & contract.** "You are revising an existing KB article by emitting a small list
   of EDIT OPERATIONS. You do NOT rewrite the whole article. Output ONLY a fenced ```json
   block containing a JSON array of ops, optionally preceded by one short sentence
   summarizing the change."
2. **The op grammar** (the three kinds from §1.2), with 2-3 worked examples, including a
   delete (`with:""`) and a `section` op.
3. **ANCHOR RELIABILITY rules (the make-or-break of B):**
   - "Copy each `find`/`anchor` **verbatim** from the CURRENT DRAFT shown below between
     `<<<DRAFT` and `DRAFT>>>`. Do not paraphrase the anchor."
   - "Make `find` long enough to be **unique** (include surrounding words). If a short
     phrase repeats, either lengthen it or set `nth`."
   - "Ops apply **top-to-bottom**; later ops see the result of earlier ones."
   - "Prefer one `section` op over many tiny `find`s when restructuring a whole section."
   - "Never invent text that isn't in the draft; never emit overlapping `find`s."
4. **The CURRENT DRAFT** between markers (re-sent each turn — the re-grounding from §1.6).
5. **Curated SOURCES** (reuse `_sources_text`, `wiki_build.py:465`) — the only citeable
   material.
6. **BACKLINKS (context only)** — read-only, do-not-cite (§4).
7. **Factored rule fragments** injected here AND back-ported into `wiki_write` /
   `wiki_revise` / `wiki_maintain` (research/02 rec #3): `{date_rules}` (the DATES & TIME
   block, `prompts.yaml:868-874` — "encode drifting values as `@t[...]`, PRESERVE any token
   verbatim"), `{crosslink_rules}` (CROSS-LINKS + KNOWN ALIASES, `prompts.yaml:855-860,
   901-904`), `{grounding_rules}`, `{talk_rules}`. This stops the research/02 §4 dilution
   where the guide steer carried none of the directive blocks.

The prompt makes deterministic-backstop existence explicit but does not rely on it: "Dates
and people-links are also enforced by the system after your edits — but still follow the
rules so your ops read correctly."

---

## 7. Frontend (`web/src/components/SuggestPanel.tsx`)

Copy `RebuildPanel.tsx` (research/05 §1/§4 — it's the closest sibling and already the
talk→edit primitive). Two shaping decisions:

### 7.1 Reuse vs. new
- **New panel `SuggestPanel.tsx`** (not a fork-flag inside RebuildPanel) — the loop is the
  *primary* surface, not a secondary tab, so the gather→curate→draft wizard `Stage` enum
  (`RebuildPanel.tsx:15`) collapses to `curate → loop`. Keep gather/curate (reuse
  `handleGather`, the candidate UI, `searchRebuildSources`) so the user still curates
  sources; after curate, enter the loop.
- Reuse wholesale: `Modal`, `MarkdownDiff`, the stable-`onClose` ref trick
  (`RebuildPanel.tsx:78-79`), the `thread` chat state (`:62`), the footer composer with
  Enter-to-send (`:283-289`), `streamSSE` (no hand-rolled reader — research/05 §2).

### 7.2 New API wrappers (`web/src/api.ts`, alongside `:931-959`)
```ts
export const suggestStart = (slug, onEvent) =>
  streamSSE(`/api/kb/rebuild/suggest/start/${encodeURIComponent(slug)}`, {}, onEvent);
export const suggestTurn  = (runId, text, onEvent) =>
  streamSSE(`/api/kb/rebuild/${runId}/suggest`, { text }, onEvent);
// accept/reject reuse acceptRebuild / rejectRebuild (api.ts:955-959) unchanged.
```
Extend the `RebuildEvent` union (or a parallel `SuggestEvent`) with `suggest_ready` and
`ops` (§2.1).

### 7.3 Loop UX
- `handleSuggest(e)` extends `handleDraft` (`RebuildPanel.tsx:117-134`): on `ops`, set the
  draft to `e.draft`, store `applied`/`failed`, push a **real per-turn AI summary bubble**
  ("Replaced the lead; added a Timeline entry") instead of the canned ack at
  `RebuildPanel.tsx:237` (research/05 §1/§4). On `done`, set lint/phase as today.
- **Optimistic user bubble on Send** (borrow Chat.tsx's pattern, `Chat.tsx:587`): push the
  user message immediately, then open `suggestTurn`.
- **Changed-hunk highlighting.** Compute the highlight by diffing the *previous* rendered
  draft against the new `e.draft` (the panel already imports `MarkdownDiff`,
  `RebuildPanel.tsx:12`) rather than trusting server `span` offsets — robust to the
  link-insertion shift (§5b). The server `applied[].span` is a hint for a faster scroll-to,
  not the source of truth. Keep the existing `showDiff` toggle vs. `note.content_md` (the
  preserved BASE — `RebuildPanel.tsx:460`).
- **Failed ops banner.** If `failed.length`, render a non-blocking inline notice ("2
  suggested changes couldn't be placed — try rephrasing") — no auto-fail.
- **Accept/Reject** footer reused unchanged (`acceptRebuild`/`rejectRebuild`).

### 7.4 Entry point (`web/src/pages/NotePage.tsx`)
Add a second KB-only `NoteActionsMenu` item **"Suggest revisions"** right next to "Rebuild
page now" (`NotePage.tsx:265-267`), same `rebuildNow`-style `llm.ready` pre-flight
(`:119-124`), mounting `<SuggestPanel slug note={{title, content_md}} .../>` near
`:379-383`. Backlinks are already on the page (`note.backlinks`, research/05 §3) but the
authoritative CONTEXT comes from the server `suggest_ready` event so the panel stays
correct even if the prop is stale.

---

## 8. Test plan (per CLAUDE.md Definition of Done)

### 8.1 Backend — `apply_ops` unit tests (`server/tests/test_edit_ops.py`, `@pytest.mark.unit`)
The richest new surface; no DB/LLM, pure functions:
- exact `replace` (single & `nth`); `replace` with `with:""` (delete); `insert`
  before/after; `section` replace; sequential ops where op2's anchor is post-op1 text.
- whitespace-normalized fallback applies + sets `fuzzy:true`.
- **failure paths:** `anchor_not_found`; `ambiguous` (N>1, no `nth`); `nth_out_of_range`;
  `section` heading missing / duplicated; parse failure (no json block, malformed json,
  schema-invalid op).
- idempotency: `with==find` → `noop:true`; double-apply of same op list is stable.
- token/link integrity at the apply layer: an op whose `find` straddles an `@t[...]` token
  or a `[[wikilink]]` and replaces only part of it is reported (so §5 can re-assert).

### 8.2 Backend — engine integration (`server/tests/test_suggest_engine.py`, `@pytest.mark.integration`)
Copy `server/tests/test_rebuild_engine.py` wholesale (research/05 §5b): `_drain(agen)`
(`test_rebuild_engine.py:36-47`), `FakeProvider` scripting ops as `TextDelta` turns
(`:50-80`), `llm`-seam monkeypatch (`_install_provider`, `:122-128`), real SQLite +
embeddings no-op (`conn` fixture `:83-107`), `_mk` seeds (`:110-114`),
`rebuild_runs.create(mode="suggest")` (`:117-119`). Cases:
- **happy turn:** script a `replace` op → assert `ops` event has `applied` of length 1,
  `failed` empty, `done.draft` equals the patched+hardened body, BASE spans byte-identical
  outside the op.
- **failed-op retry:** turn 1 scripts a bad anchor; FakeProvider's second turn scripts a
  corrected anchor → assert the retry path applied it (mirrors `range(2)` cap).
- **still-failed:** both turns bad → `done` still emitted, `failed` non-empty, working draft
  = prior.
- **date preservation (Option D):** BASE has `@t[age:1986-03-15]`; script an op that drops
  it → assert it's restored/warned (re-uses the research/04 test idiom of asserting a
  deterministic backstop ran).
- **people relink (O2):** script an op adding a bare known-person mention → after rebind +
  `add_links_to_content`, assert it's linked (test the rebind-then-link path that
  `test_alias_linking.py`/`test_owner_alias_backfill.py` model — research/04 §6).
- **no-credentials / `fail_on_turn` / `run.cancelled`** paths (research/05 §5b).
- **grounding invariant:** assert a backlink title is in the prompt context but NOT in
  `run.sources` (so `_repair_citation_titles` can't target it — §4).

### 8.3 Backend — Accept promotion parity (`server/tests/test_rebuild_refs_links.py` sibling)
Assert `promote_one`/`finalize_rebuild` runs `surface_aliases` etc. on suggest Accept;
assert nightly `rebuild_article` behavior is unchanged (guard against the placement-(a)
regression).

### 8.4 Frontend (`web/src/components/SuggestPanel.test.tsx`, vitest + MSW)
Copy `RebuildPanel.test.tsx` recipe (research/05 §5a) — mock only stream helpers
(`suggestStart`/`suggestTurn`) with `fakeStream` (`:29-36`); JSON accept/reject stay on
MSW; `renderWithProviders` + `__reset()` health + `vi.stubGlobal` confirm/alert. **The guide
loop is currently UNtested** (research/05 §5a gap), so these are net-new:
- a turn emits `ops`+`done` → user bubble appears optimistically, draft re-renders, AI
  summary bubble appears, changed hunk highlights.
- a second turn works (carry-forward working draft).
- `failed` ops → non-blocking banner shown, draft still updates.
- Accept routes to `acceptRebuild` (assert via recorded `posted[]`).

### 8.5 e2e (warranted — user-facing flow + new API contract)
Add one Playwright spec in `e2e/` driving Suggest revisions end-to-end with the faked LLM
(`e2e/fake_llm.py`) scripted to return an ops block: open panel → curate → one suggest turn
→ see patched draft → Accept → page updated. CLAUDE.md requires `./jt e2e` when a
user-facing flow / API contract changes; this qualifies.

### 8.6 Coverage floor
New `edit_ops.py` and `hardening.py` are heavily unit-covered, so per-domain real coverage
should rise; **ratchet the `fail_under` in `server/pyproject.toml` and the `web` thresholds
up** in the same PR once green (CLAUDE.md DoD #3). Never lower a floor.

---

## 9. Risks & edge cases (honest)

1. **Bad anchors are B's defining failure mode.** A model that paraphrases its anchor →
   apply miss. Mitigated by (a) re-sending the verbatim draft each turn, (b) whitespace
   fallback, (c) one auto-retry, (d) non-blocking surfacing. Residual risk: a turn where
   *everything* fails feels inert to the user. Acceptance: this is strictly safer than B's
   alternative (a corrupting fuzzy match) and no worse than a full-rewrite turn that
   ignores the instruction — and it's *cheaper*.
2. **Anchor ambiguity** on repeated phrases — handled by `nth` + `ambiguous` rejection
   (§1.4); never silently guesses.
3. **Partial-apply coherence.** A half-applied op set can leave the draft in a state the
   model didn't intend (op A applied, op B failed). Surfaced explicitly; the user re-steers.
   The working draft is never *corrupt*, only *partially edited*.
4. **Concurrent token/link rewrites vs. patch offsets.** Solved by running all deterministic
   rewriters (`enforce_date_tokens`, `add_links_to_content`) on the **final patched string**
   after `apply_ops` returns, never on live op-offsets; highlight via diff, not raw spans
   (§5, §7.3).
5. **Staleness on long conversations.** Bounded by re-grounding the model with the current
   working draft every turn and keeping only K=3 prior turns (§1.6); the run's
   `working_draft` is canonical, the transcript advisory.
6. **More server logic + tests** than the full-article approach — the genuine cost of B
   (research/01 §10). Concentrated in `edit_ops.py`, which is pure and exhaustively
   unit-testable, so the risk is *quantity* of code, not *fragility* of it.
7. **`section` boundary edge** (a `##` followed by a `#` H1, or fenced ```code containing a
   `##`) — `section` matching must mask fenced code (reuse `_mask_spans`,
   `wiki_build.py:1817`) so a `##` inside a code fence isn't treated as a heading.
8. **Stub reclassification** (research/03 §4): an op that shrinks an article below
   `stub_max_chars` flips lint rules — surfaced via the existing `validate_structure` lint
   in `done`, not hidden.

---

## 10. Sequencing / effort (PR breakdown)

| PR | Scope | Risk | Est. |
|---|---|---|---|
| **PR1** | `edit_ops.py` (schema + parser + `apply_ops`) + full unit suite (§8.1). Pure, no wiring. | Low | M |
| **PR2** | `hardening.py`: factor `_generate`'s tail out; add `enforce_date_tokens` (A+D+B) and `rebind_entities_cheap`; route classic draft/guide through it (no behavior change) + tests. Closes research/03 + research/04 H1 for *existing* rebuild too. | Med | M-L |
| **PR3** | `RebuildRun` fields + `backlinks_context` + `run_suggest_start`/`run_suggest_turn` + router `/suggest/*` endpoints + new SSE events + engine integration tests (§8.2). | Med | L |
| **PR4** | `actions.suggest_edits` prompt + factored `{date_rules}`/`{crosslink_rules}` fragments back-ported into `wiki_write`/`wiki_revise`/`wiki_maintain`; flows-tier validation. | Low | M |
| **PR5** | `promote_one` + Accept promotion parity (§5c) + tests (§8.3). Independent; can land early. | Med | M |
| **PR6** | `SuggestPanel.tsx` + api wrappers + NotePage entry + vitest/MSW tests (§8.4). | Med | L |
| **PR7** | e2e spec + coverage-floor ratchet (§8.5–8.6). | Low | S |

PR1, PR2, PR5 are independent and parallelizable. PR3 depends on PR1+PR2; PR6 depends on
PR3. Total ≈ 2–3 focused weeks. The single highest-leverage early land is **PR2**, which
fixes the user's *current* "wrong dates / doesn't link people" complaints on the existing
rebuild path before the new mode even ships.

---

## Appendix — key citations
- Engine/transcript: `rebuild_engine.py:9-15` (separate-transcript invariant), `:271-398`
  (`_generate` + hardening tail), `:295` (draft wipe), `:309` (`range(2)` cap), `:387`
  (`add_links_to_content`), `:440-463` (`run_guide` steer), `:466-503` (`run_redraft`).
- Run/registry: `rebuild_runs.py:27-49` (schema), `:71` (no-reap accepting), `:145-154`
  (`is_live`).
- Router: `rebuild.py:57-111` (`_sse`), `:137-166` (start), `:289-319` (guide), `:322-383`
  (accept + staleness + lock), `:345` (accept gate).
- Hardening reuse: `wiki_build.py:711-781` (`add_links_to_content`, `:733` PII guard),
  `:784-818` (`build_write_prompt`), `:1009` (`link_owner`), `:1177` (`surface_aliases`),
  `:1428` (`flag_ungrounded_reference`), `:1681-1721` (`finalize_rebuild`).
- Backlinks SQL: `architect.py:818-822`.
- Dates: `clock.py:105` (`_TOKEN_RE`), research/03 Options A/B/D.
- People: `entity_index.py:340,529,553` (rebind/firewall), research/04 O1/O2/O5.
- Frontend: `RebuildPanel.tsx:15-16,62,78-79,117-134,237,283-289,460`; `api.ts:855-959`;
  `NotePage.tsx:119-124,265-267,379-383`; `RebuildPanel.test.tsx` recipe;
  `test_rebuild_engine.py:36-128`.
