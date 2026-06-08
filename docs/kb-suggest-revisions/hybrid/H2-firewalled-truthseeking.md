# H2 — Firewalled truth-seeking with deterministic apply (RT2-intent, RT1-hardened)

**Stance.** Deliver the owner's *literal* intent — active truth-seeking, "the AI goes
and finds a salient fact mid-conversation" (RT2 T2) — while closing the one finding RT1
calls disqualifying: there is **no inbound PII firewall** (`hybrid_notes` has no privacy
filter, `search.py:36-76`; gather strips only `kb/`, `rebuild_engine.py:178`). H2 keeps
truth-seeking but routes every fact through (a) a **privacy-filtered search seam**, and
(b) a **user-approval gate** ("the AI found this in note X — include it?"), so a private
Health/Finance note's prose, facts, or title can never auto-weave into a public or
Reference article. Edits apply via **pure exact-match-only ops** (B's testable core with
the fuzzy fallback removed, per RT1) for clean diffs (RT2 T1). Tools live only in a
**disposable thinking-off sub-transcript** (C's verified-sound topology); `run.messages`
stays tool-free plain text. The fact-finding loop runs the **cheap** model (RT1 flagged C
used the synthesis model). The shared hardening core (D's `harden_draft` /
`enforce_date_tokens` / `promote_one` / session-start rebind) ships **first** so the
existing "Rebuild page now" benefits in week one.

**Honest framing.** H2 is the most engineering and the most test surface of any hybrid:
firewall + ops apply + a tool loop, each with its own failure branches. I de-risk by
sequencing a **complete tool-less increment first** (PR1 hardening, PR2 tool-less loop)
so a shippable product exists before any tool/firewall code lands, and the truth-seeking
layer (PR3) is purely additive — if it proves too costly, PR2 stands alone. Every
load-bearing claim below is verified against the tree on 2026-06-08.

---

## 1. Architecture: carry-forward generator, two-transcript topology, file changes

### 1.1 Why a sibling generator (not `_generate`)

`_generate` wipes the working draft every turn — `run.draft = ""` at
`rebuild_engine.py:295`, then streams a *full* body. RT1 ground-truth #1 is correct: any
carry-forward loop that routes through `_generate` loses BASE. H2 therefore writes its
own generators in a new module and reuses only the **hardening tail's helpers**
(`_extract_talk`, `_bad_links`, `_repair_citation_titles`, `_neutralize_links`,
`add_links_to_content`, `validate_structure`, `rebuild_engine.py:362-398`) via the shared
`writer_core.harden_draft` (D, §5). The run's `run.draft` is **canonical** and is mutated
only by the deterministic `apply_ops` (§3) — never re-derived from the transcript, which
sidesteps the staleness-drift class entirely (C §9's cleanest property).

### 1.2 New module `server/app/services/suggest_engine.py`

A sibling of `rebuild_engine.py`. Public generators:

- `async def run_suggest_start(run, source_ids) -> AsyncGenerator[dict, None]` — seeds the
  session. Loads curated/seed sources (reuse `wiki_build.rebuild_sources`,
  `wiki_build.py:1640`; `_load_sources`, `:412`), sets `run.base_draft = current article
  body` and `run.working_draft = run.base_draft`, captures `run.base_tokens =
  clock._TOKEN_RE.findall(base)`, loads read-only backlinks (§6), calls
  `entity_index.rebind(conn)` (§5), and emits a synthetic `session_ready` carrying the
  BASE draft (no LLM call — the unchanged article renders instantly). **No transcript seed
  with a fake assistant turn** — H2 stores `run.draft` directly and keeps `run.messages`
  empty until the first real turn, which avoids A's synthetic-seed/`run_redraft` bug
  (RT1 ground-truth #6 / A's MAJOR: `run_redraft` pops a trailing assistant turn,
  `rebuild_engine.py:491`; we never plant one).

- `async def run_suggest_turn(run, instruction) -> AsyncGenerator[dict, None]` — the edit
  turn. Two phases on two transcripts (§1.3). Carries forward `run.working_draft`, applies
  deterministic ops, runs `harden_draft` on the **final applied string**, appends one
  plain `user`/`assistant` pair to `run.messages`, and emits the new draft + diff
  metadata + any candidate facts.

### 1.3 The two-transcript topology (verified sound)

RT1 confirmed C's mechanism against `llm.py`: Anthropic appends `final.content` (signed
thinking + tool_use blocks) verbatim (`llm.py:404`); xAI appends an OpenAI dict with
`tool_calls` (`llm.py:676-678`) — non-interchangeable. H2 keeps the same split the gather
stage already proves safe (`rebuild_engine.py:9-15, 152, 193`):

1. **FACT-FINDING (tools, thinking=OFF, disposable).** A fresh local `ff` list runs the
   tool loop on the **cheap** model (`llm.model_for("cheap")`, mirroring gather at
   `rebuild_engine.py:149`). It carries `tool_use` blocks but **never** signed thinking,
   and is discarded after the turn — never assigned to `run.messages`. Tool calls stream
   to the UI (`tool_use`/`tool_result`, provider-neutral, `rebuild_engine.py:6-7`).

2. **APPLY + DISTILL (no tools, no thinking, persisted).** The terminal `apply_edits` tool
   carries structured ops + a plain summary + candidate facts. The server applies ops to
   `run.working_draft`, hardens, and records into `run.messages` only a plain `user`
   (instruction) + plain `assistant` (summary) pair — **zero** tool_use, **zero** thinking
   blocks. This is *stronger* than today's DRAFT transcript, which persists signed
   thinking (`llm.py:404`). A CI regression test asserts `run.messages` is all
   `{"role","content": str}` (§9, RT1 non-negotiable #1).

> Why thinking-off in fact-finding: the disposable transcript can then never carry a
> signed-thinking block to mis-resume cross-provider. The agent's judgment lives in the
> structured tool args, not a reasoning block we must preserve. Interstitial narration is
> streamed as provider-neutral plain `assistant_delta` text, not extended thinking.

### 1.4 Files

| File | Change |
|---|---|
| `server/app/services/suggest_engine.py` | **NEW** — `run_suggest_start`, `run_suggest_turn`, `_dispatch_tool`, `_EDIT_TOOLS`, `_editor_system`, `_turn_user_prompt`, `_summary_text`, `_TURN_MAX_ITER`/`_TURN_MAX_TOKENS`. |
| `server/app/services/edit_ops.py` | **NEW** — pure (`@pytest.mark.unit`): op schema, parser, `apply_ops(draft, ops) -> ApplyResult`. No DB/LLM (B's PR1). |
| `server/app/services/writer_core.py` | **NEW** — `harden_draft`, `enforce_date_tokens`, `promote_one`, `rebind_entities` (D's core, §5). |
| `server/app/services/note_privacy.py` | **NEW** — the inbound firewall seam: `note_sensitivity(conn, note_id)`, `allow_note_for_target(...)` (§2). |
| `server/app/services/rebuild_engine.py` | Refactor `_generate`'s tail to call `writer_core.harden_draft` (no behavior change for classic rebuild; characterization-pinned, §9). |
| `server/app/services/rebuild_runs.py` | Extend `RebuildRun`: `kind`, `base_draft`, `working_draft`, `base_tokens`, `backlinks`, `last_ops`, `candidate_facts`, `accepted_facts`, `entity_rebound`. |
| `server/app/routers/suggest.py` | **NEW** — `/api/kb/suggest/*` endpoints, reusing `_sse` (`rebuild.py:57-111`), accept/reject/staleness/lock. |
| `server/app/services/wiki_build.py` | `finalize_rebuild` gains `writer_core.promote_one`; add `backlink_titles`. |
| `prompts.yaml` | `actions.wiki_edit` + factored `{date_rules}`/`{crosslink_rules}` fragments; fold `{date_rules}` into `wiki_revise`. |
| `web/src/{api.ts, components/SuggestPanel.tsx, pages/NotePage.tsx}` | Frontend (§8). |

---

## 2. The INBOUND PII firewall (the central problem)

### 2.1 The exact hole

`hybrid_notes` returns `{id, title, slug}` with **no** privacy filter (`search.py:36-76`).
Gather strips only `kb/` hits (`rebuild_engine.py:178`). The existing firewall predicate
`wiki_guides.is_private_title` (`wiki_guides.py:148-161`) only matches **kb/Health/** and
**kb/Finance/** *titles* — it does nothing for **raw notes** (an `entry`/`daily` note has
no `kb/` prefix and no privacy column, verified: `notes.kb_ingest` is the only governance
flag, `db.py:920`). So a tool agent that searches notes and reads their prose can pull a
private fact into a public article, and the outbound link firewall (`add_links_to_content`
self-guard, `wiki_build.py:733`) protects *links/targets*, never *prose facts the agent
copied in*. This is RT1's CRITICAL and RT1 non-negotiable #4.

### 2.2 The seam: `note_privacy.py`

Because raw notes carry no privacy flag, H2 derives note sensitivity deterministically and
gates at **three** points (prose, facts, titles). `note_sensitivity(conn, note_id) -> str`
returns `"private"` if **any** of:

- The note is **filed under** a private domain — its title starts with `kb/Health/` or
  `kb/Finance/` (reuse `is_private_title`), OR a folder/notebook prefix the owner marks
  sensitive (extend `PRIVATE_DOMAINS` semantics to a note-title prefix list,
  `wiki_guides.py:144-145`).
- The note is **entity-linked to a private person/topic**: it appears in
  `entity_index.note_ids_for_name` for an entity whose `article_title` is private (the
  entity index already binds private satellites; `_link_articles` deliberately excludes
  private leaves at `entity_index.py:553`, so we query the inverse — notes whose resolved
  entity *is* private). This catches a raw daily-log entry that mentions a diagnosis even
  though its own title is innocuous.
- The note is **medically classified**: it has rows in the health tables
  (`visits`/`vitals`, `db.py:976,1017`) or its content matches the health-note heuristic
  the KB Health builder already trusts.

`allow_note_for_target(conn, *, note_id, target_title) -> bool`:

- **Public/Reference target** (`not is_private_title(target) and domain_for_title(target)
  != ... private`): a `"private"` note is **refused** — its prose, its facts, and its
  **title** are all withheld. The model never sees "HIV results 2024" as a search hit.
- **Private target, matching domain** (target `kb/Health/X` ↔ note classified Health):
  permitted. A `kb/Finance/*` target may not read Health notes and vice-versa (domain
  must match, not merely "both private").

### 2.3 What is filtered, at each tool

The firewall wraps the disposable tool dispatcher `_dispatch_tool` (§4), so it applies to
**every** lookup the agent makes, not a Risks-paragraph afterthought (RT1's exact
complaint about C):

- **`search_notes`** — after `hybrid_notes`, drop every hit where
  `not allow_note_for_target(...)` **before** building the `tool_result` body. Filtered at
  the **title** level: a refused note's title never reaches the model. (Also keeps the
  existing `kb/` strip and `require_kb_ingest`.)
- **`read_source`** — refuse a disallowed note: return a `ToolResult` "that note isn't
  available for this article" with **no** body text. Filtered at the **prose** level.
- **`read_backlink`** — refuse a backlink whose title `is_private_title` or
  `domain_for_title == "Reference"` when the target is public (reuse the same predicate
  the linker uses, `wiki_guides.py:733`). Filtered at the **prose** level.
- **Citation emission** — `apply_edits.facts[].source` and any `[^id]: [[Title]]` footnote
  the ops introduce are run through `allow_note_for_target` in `harden_draft`'s
  citation-repair step (after `_repair_citation_titles`, `rebuild_engine.py:372-374`): a
  footnote whose target resolves to a private/Reference note on a public page is
  neutralized exactly like a dead link (RT1's MAJOR on C — a real `[[kb/Finance/...]]`
  citation otherwise *resolves* and survives).

### 2.4 The user-approval gate (the human over the firewall)

Even a *permitted* fact (private target reading a matching-domain note) is surfaced as a
**candidate fact**, not auto-woven. `apply_edits.facts` become `candidate_facts` on the
run; the panel shows "the AI found this in *note X* — include it?" chips. Only on the
owner's **Include** does the engine re-issue the fact as a targeted op (a follow-up
`run_suggest_turn` with `accepted_facts` seeded). This honors RT2's "my input corrects AI
assumptions," adds a **second human gate** over the firewall, and means a
misclassification (a note the heuristic *should* have caught) still gets an owner's eyes
before it lands. For a *public* target the candidate chip is the only path facts from
notes enter at all — there is no auto-weave on a shareable article, ever.

### 2.5 Tests proving no leak (§9 lists them)

A private raw note (classified via 2.2) returned by a scripted `hybrid_notes` → on a
public target: `search_notes` result excludes its title; `read_source` refused; an
`apply_edits` that cites it gets the footnote neutralized; the draft body never contains
its prose. The same note on a matching-domain **private** target surfaces as a candidate
fact and only lands after an explicit Include turn.

---

## 3. Deterministic exact-match-only `edit_ops` apply

### 3.1 The ops (B's core, fuzzy fallback REMOVED per RT1)

`apply_ops(draft, ops) -> ApplyResult{draft, applied, failed}`. Op kinds:

```jsonc
{ "op":"replace", "find":"<exact text from current working draft>", "with":"<new>", "nth":1 }
{ "op":"insert",  "where":"after|before", "anchor":"<exact text>", "text":"<new>" }
{ "op":"section", "heading":"## Heading", "with":"## Heading\n\n<new body>" }
```
`delete` = `replace` with `with:""`.

**Match strategy — exact only.** `draft.find(find)`; apply iff the `nth` occurrence exists
**and is unambiguous**. RT1 showed B's whitespace-normalized fallback can silently
mis-apply ("the plan" unique exactly but doubled after collapse) — **H2 drops it
entirely**. A non-exact, ambiguous (`N>1` without `nth`), or out-of-range anchor → the op
**fails cleanly**; it never guesses, never lands fuzzy. This is the single safety property
that keeps targeted edits from corrupting BASE (C §1.3 / RT1's "keep the exact op, drop
the fuzzy fallback").

**`section` op fence-masking (RT1 ground-truth #4 / CRITICAL on E and B).** `_SECTION_RE`
matches `^##\s+` *inside* code fences (`wiki_guides.py:39`). The `section` op masks fenced
code (reuse `_mask_spans`, `wiki_build.py:1817`) before locating the heading, so a `## ` in
a shell example can't be mistaken for a boundary. Zero or ≥2 heading matches → fail.

**Atomic delete+insert (RT1's B MAJOR — a delete whose paired insert fails silently drops
a fact).** Ops may declare a `group` id; if any op in a group fails, the **whole group is
rolled back** (none applied) and the group is surfaced as failed. This prevents "deleted
the old date, failed to insert the corrected one."

Application is sequential against the evolving buffer (later ops see earlier results);
absolute-offset splice. `with == find` → `noop:true`.

### 3.2 Failure / retry / full-reemit fallback

1. **Partial-apply** of non-grouped good ops is allowed; failed ops collect into
   `failed[]` with `{op, reason, anchor}`. Never corrupts; the user still sees progress.
2. **One automatic retry** of *only* the failed ops, mirroring `_generate`'s single
   auto-continue cap (`for ... range(2)`, `rebuild_engine.py:309`): the engine re-prompts
   the agent with "these anchors weren't found in the current draft (between markers);
   re-emit corrected ops or an empty array," then re-applies.
3. **Full-article re-emit fallback** (the safety floor borrowed from A/D): if after retry
   the turn produced **zero** applied ops *and* the instruction clearly needed a change
   (parse failure, or all ops failed), the engine falls back to one tool-less full-article
   re-emit turn through the carry-forward generator — the model rewrites the COMPLETE
   article, hardened identically. This guarantees a conversational turn is **never inert**
   (RT2's headline B risk: "the owner says something and nothing happens"). The fallback is
   visible in the UI ("rewrote the section").

### 3.3 add_links offset-collision avoided

All deterministic rewriters run on the **final applied string** after `apply_ops` returns
a plain `str` — there are no live op-offsets to invalidate. `add_links_to_content` *inserts*
`[[...]]` and shifts offsets, but since it runs on the finished buffer this is safe; it
re-masks the *current* body each call (`_mask_spans`), so it never nests links or links
inside a just-added citation. Changed-hunk **highlighting** is computed by **diffing**
pre-harden vs post-harden text (the panel's `MarkdownDiff`), never from raw op spans (RT1
ground-truth #3/#5, B §5/§7.3). Ordering is enforce-dates → add-links (§5).

---

## 4. Truth-seeking tool set + candidate facts

### 4.1 `_EDIT_TOOLS` (cheap model, disposable transcript)

Schemas mirror gather's style (`llm.ToolDef`, `rebuild_engine.py:54-73`):

```python
_EDIT_TOOLS = [
  search_notes(query)        # firewalled hybrid_notes → titles+dates (§2.3)
  read_source(title)         # firewalled full-note read (§2.3)
  read_backlink(title)       # firewalled backlink read (§2.3)
  apply_edits(edits[], summary, facts[{claim, source}])   # TERMINAL
]
```

`apply_edits.edits` carry the §3 op shapes. The loop is the gather loop with `apply_edits`
as the terminal tool (analogue of `propose_sources`, `rebuild_engine.py:187-190`), capped
at `_TURN_MAX_ITER` (mirror `_GATHER_MAX_ITER=5`) and `_TURN_MAX_TOKENS=1500` (mirror
`_GATHER_MAX_TOKENS`, `rebuild_engine.py:50-51`). `search_notes` offloads ONNX inference
via `asyncio.to_thread` so the event loop isn't blocked (`rebuild_engine.py:176`).

### 4.2 When search vs edit

The prompt biases the agent: **search/read only when a salient fact is in play; otherwise
go straight to `apply_edits`.** "Make the intro shorter" → one `apply_edits`, no search,
no latency. "When did we buy the truck?" → `search_notes`→`read_source`→`apply_edits`. On
hitting `_TURN_MAX_ITER`, force-finish with whatever ops exist + a "ran out of lookups"
note (cost guard).

### 4.3 Found facts → approvable candidates

`apply_edits.facts[{claim, source}]` are **not** auto-applied. The engine:

- runs each `source` through `allow_note_for_target` (§2.3 — drops a disallowed source
  entirely, even from the candidate list, so a refused private note never surfaces even as
  a chip on a public target);
- for **permitted** facts, emits a `candidate_facts` event and stores them on the run.

The owner's **Include** on a chip seeds `run.accepted_facts` and triggers a follow-up
`run_suggest_turn` whose instruction is "weave this approved fact: {claim} [^{source}]" —
so the fact lands as a normal targeted op with a *grounded* citation. This is how H2
delivers visible truth-seeking (RT2 T2) without the auto-weave RT1 disqualified.

---

## 5. Shared hardening core (borrow D) + ordering

`writer_core.harden_draft(conn, title, draft, *, known, source_titles, base=None)` is the
single tail every writer path runs. Classic `_generate` is refactored to call it
(characterization-pinned, §9) so the existing rebuild benefits — this ships in **PR1**,
before the new mode (RT2's highest-weighted, nearly-plan-independent win).

**Order of operations** (RT1 non-negotiable #5 — dates before links):

1. **`enforce_date_tokens`** (research 03 A + B-with-round-trip + D). `_mask_spans` does
   **not** mask `@t[...]` (RT1 ground-truth #3, `wiki_build.py:1830-1831`), so tokens must
   be enforced **before** `add_links_to_content` to avoid a `[[link]]` inserted into a date
   arg. (A) flag any `@t`-shaped substring not matched by `clock._TOKEN_RE` (`clock.py:105`)
   or failing `_to_dt` (`:127`) → lint. (B) adjacency rewrite *only* when
   `clock.expand_tokens("@t[age:DATE]")` reproduces the literal (round-trip guard). (D)
   loop-only: assert every `run.base_tokens` survives; a vanished token (model "tidied"
   `@t[age:…]` → "40") → warn/re-insert. `enforce_date_tokens` only *produces* tokens via
   `clock` round-trip — it never touches expansion semantics, so the
   `clock.expand_tokens` ↔ `time.ts` byte-pin and `time_tokens.json` stay untouched
   (`test_api.py:1209-1211`).
2. `_bad_links` → `_repair_citation_titles` (keyed on `run.sources` **only**) →
   citation-firewall neutralize (§2.3) → `_neutralize_links`.
3. **`add_links_to_content`** people-link backstop (`wiki_build.py:711`, PII self-guard
   `:733`).
4. `validate_structure` — advisory.

**`rebind_entities(conn)`** (session start, RT1 non-negotiable #7). A cheap, **no-embeddings**
rebind: `entity_index._link_articles` (`:529`, already excludes private leaves `:553`) +
the `reconcile_owner` owner-alias fold, **without** the networked `_sync_embeddings`
(`entity_index.py:360`). Needed because the **draft-time** link offering reads bindings
*during* the conversation — and `finalize_rebuild` already runs the full
`entity_index.rebuild` on Accept (`wiki_build.py:1718`), so the Accept-time rebuild is too
late for the live loop (RT1 ground-truth #2). Run once in `run_suggest_start` and in
`run_draft` so classic rebuild benefits.

**`promote_one(conn, title)`** (RT1 non-negotiable #6). The per-article subset of the
build's promotion suite single-article paths skip (`link_owner`, `surface_aliases`,
`link_medications`, `link_places`, `normalize_link_labels`, `flag_ungrounded_reference`,
`actions/wiki_build.yaml:82-108`). Placed in `finalize_rebuild` **after**
`entity_index.rebuild` (`wiki_build.py:1718`) so live Accept **and** nightly inherit it;
`finalize_rebuild` already runs networked work in the lock (RT1 ground-truth #2), so the
latency worry is moot. Idempotent: `surface_aliases`/`_apply_aka_line` rebuild the AKA line
each call (`wiki_build.py:1137`); a second Accept after a 409 retry is safe.

---

## 6. SSE protocol + RebuildRun + backlinks

### 6.1 Events (reuse the `event: {type}\ndata: {json}\n\n` envelope, `rebuild.py:99`)

| `type` | Emitted by | Payload |
|---|---|---|
| `session_ready` | `run_suggest_start` | `run_id, slug, title, base_rev, base_draft, backlinks[], sources[]` |
| `tool_use` | `_dispatch_tool` | `tool, query?, title?` (reuse Stage-1 step UI, `RebuildPanel.tsx:85-88`) |
| `tool_result` | `_dispatch_tool` | `tool, summary, items[]` |
| `assistant_delta` | `run_suggest_turn` | `text` (plain interstitial narration) |
| `ops` | `run_suggest_turn` | `applied[], failed[], draft` (changed-hunk metadata) |
| `candidate_facts` | `run_suggest_turn` | `facts[{claim, source}]` (approval chips, §4.3) |
| `done` | `run_suggest_turn` | `draft, lint` (same shape as `_generate:396`, so the panel's `done` handler is reused) |
| `lint`, `error` | reused | — |

No `content_delta` full-body stream on an edit turn (the draft arrives whole on
`ops`/`done`); the full-reemit fallback (§3.2) *does* stream `content_delta`, which the
panel already handles.

### 6.2 State machine + RebuildRun

Reuse statuses; add `kind ∈ {"rebuild","suggest"}`. `create(kind="suggest")` →
`session_ready` (status `ready`, BASE staged, no LLM); turn → `guiding` →(tool loop)→
`ready`; Accept gated on `status in ("ready","guiding")` + non-empty draft
(`rebuild.py` 409 path, `:309/:338`) — **unchanged**; `is_live`/`_LIVE =
("streaming","ready","guiding")` (`rebuild_runs.py:24,154`) unchanged. **We do NOT widen
the Accept gate or `_LIVE`** (RT1's E MAJOR — E added `"suggesting"` and touched the shared
gate; H2 lands in `ready` like everyone else). `base_hash` still hashes the **live page**
(`rebuild_runs.py:35,100`); staleness/lock/TTL/one-per-slug unchanged.

New `RebuildRun` fields (all defaulted): `kind, base_draft, working_draft, base_tokens,
backlinks, last_ops, candidate_facts, accepted_facts, entity_rebound`.

### 6.3 Backlinks (read-only, NEVER in `run.sources`)

`wiki_build.backlink_titles(conn, note_id)` reuses the verified inbound SQL at
`architect.py:818-822` (`SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id =
l.source_note_id WHERE l.target_note_id = ? AND n.deleted_at IS NULL`). Titles go into the
prompt as a labeled **read-only "Linked from (do not cite)"** block and into
`run.backlinks` — **never** `run.sources` (RT1 non-negotiable #3: `run.sources` drives
`_repair_citation_titles`, `rebuild_engine.py:373`; a backlink leaking in becomes a
citation-repair target). The agent reads a backlink body lazily via the firewalled
`read_backlink` tool (§2.3).

---

## 7. Prompt / tool design

`actions.wiki_edit` (the editor system prompt, built by `_editor_system`) carries the
directive blocks the bare Guide steer lacks (RT2 §4 dilution; `run_guide`'s steer at
`rebuild_engine.py:455-460` carries none). Factor `{date_rules}` (the DATES & TIME block,
`prompts.yaml:868-872`) and `{crosslink_rules}` (`:855-860`) into reusable fragments;
reference them from `wiki_write`, `wiki_revise` (the date fold fixes the re-freeze,
research 02 §3), and `wiki_edit`. The contract:

- **Truth-seeking (AI's job):** on a salient fact, `search_notes`/`read_source` to GROUND
  it — do not guess; put each grounded fact in `apply_edits.facts` with its source.
- **Steering (owner's job):** structure / formatting / corrects assumptions. Make exactly
  the asked change; trust an owner correction over a note.
- **Edit discipline:** every `find`/`anchor` is a **unique verbatim** substring of the
  current draft; preserve every `@t[...]` token; never touch the `*Also known as:*` line
  (owned by `surface_aliases`); never target `is_protected` pages (`wiki_guides.py:87`);
  cite only source titles, never a backlink.
- **Firewall instruction:** "Some notes are withheld for privacy on a shareable article; if
  a lookup returns nothing, do not infer its content — ask the owner." (Belt-and-braces;
  the hard gate is server-side, §2.)

---

## 8. Frontend

New `SuggestPanel.tsx` reusing RebuildPanel primitives (the gather/curate wizard collapses
to a single loop surface — RT2 §4 / research 05):

- **Reuse:** `Modal`, `MarkdownDiff before={note.content_md} after={draft}` (BASE preserved,
  `RebuildPanel.tsx:431-435,460`, **diff default-on**), the stable-`onClose` ref trick
  (`:78-79`), `thread` chat state (`:62`), footer composer + Enter-to-send (`:283-289`),
  `streamSSE` (no hand-rolled reader, `api.ts:875-929`), Accept/Reject footer.
- **Tool activity:** reuse the exact Stage-1 `Step` rendering (`RebuildPanel.tsx:17,85-98`);
  extend `TOOL_LABEL` with read_source/read_backlink/apply_edits. Truth-seeking is
  *visible* — the C/RT2 selling point.
- **Candidate-facts UI:** `candidate_facts` chips ("found in *Truck log* — Include?"),
  Include → a follow-up turn (§4.3). This is the firewall's human gate, made tangible.
- **Diff view:** changed hunks highlighted by diffing prev vs new draft (§3.3), not server
  spans. Failed ops → a non-blocking "couldn't place that change — rephrasing…" note; the
  full-reemit fallback (§3.2) means turns are never silently inert (RT2 B-risk).
- **Optimistic dual-bubble** on Send (borrow `Chat.tsx:587`); replace the canned ack
  (`RebuildPanel.tsx:237`) with the real per-turn `summary`.

**`api.ts`:** a `SuggestEvent` union + thin `streamSSE` wrappers (`suggestStart`,
`suggestTurn`, `acceptSuggest`, `rejectSuggest`).

**UX coherence (RT2 §4 — the unaddressed product question).** Two entry points:
"Rebuild page now" (throw away the draft, re-synthesize) vs "Suggest revisions" (keep the
draft, talk to it). H2 differentiates with **intent-revealing copy** now ("Rebuild from
sources" / "Revise by talking") and **plans the convergence** the brief hints at: Suggest
should eventually **replace rebuild's Guide step** — one conversational editing surface
reachable both after a fresh rebuild ("now talk to it") and from an existing page. The
deterministic targeted diff + visible truth-seeking make Revise feel genuinely distinct
from Rebuild (RT2's strike against A's full-re-emit sameness). Entry: a second KB-only
`NoteActionsMenu` item with the `rebuildNow`-style `llm.ready` pre-flight
(`NotePage.tsx:119-124,265-267`), refusing `is_protected` pages in the router.

---

## 9. Test plan per tier (CLAUDE.md DoD) + realistic effort

Estimates below already absorb RT2's "discount 30-50%" warning — they are the *discounted*
numbers, with the failure-path + e2e + coverage-ratchet tax priced in.

### 9.1 Backend unit — `test_edit_ops.py` (`@pytest.mark.unit`)

`apply_ops` is pure and the richest surface: exact `replace`/`nth`/delete; `insert`
before/after; `section` (incl. **fence-masked** `## ` inside a code fence — the RT1
CRITICAL); sequential ops; **group atomicity** (a failed member rolls back its delete);
failure reasons (`anchor_not_found`, `ambiguous`, `nth_out_of_range`, dup/missing heading,
parse failure); `noop`. **No whitespace-fallback test exists because the fallback doesn't
exist** (RT1).

### 9.2 Backend integration — `test_suggest_engine.py` (`@pytest.mark.integration`)

Copy `test_rebuild_engine.py:36-128`: `_drain`, `FakeProvider` **scripting tool turns**
(it already yields `ToolCallEvent`; `append_tool_results` appends the provider shape), llm
**seam** monkeypatch (never the SDK), real SQLite with embeddings no-op'd.

- **Happy no-search:** turn yields `[apply_edits(replace), TurnEnd]` → draft changes at the
  anchor, BASE byte-identical elsewhere, `ops`/`done` emitted.
- **Truth-seeking:** `search_notes`→`read_source`→`apply_edits` → tool events stream in
  order; the edit reflects the read fact; the disposable transcript is discarded.
- **FIREWALL (the load-bearing suite, §2.5):** scripted `hybrid_notes` returns a private
  note (classified by title prefix / entity-private / health-table) → on a **public**
  target: `search_notes` result omits the title; `read_source` refused (no body);
  `read_backlink` on a private backlink refused; an `apply_edits` citing it has the
  footnote neutralized; the body never contains its prose. On a **matching-domain private**
  target: surfaces as a `candidate_facts` chip and only lands after an Include turn.
- **Transcript safety (RT1 #1):** after several turns assert `run.messages` is all
  `{"role","content": str}` — no list-content, no `tool_calls` key, no thinking block.
- **edit_ops failure → retry → full-reemit fallback:** all anchors bad twice → fallback
  full-article re-emit produces a non-inert turn.
- **Dates (RT1 #5):** BASE has `@t[age:1986-03-15]`; an op freezing it to "40" → Option-D
  warn/restore; malformed `@t{age:…}` → Option-A lint. Assert `time_tokens.json` untouched.
- **People rebind (RT1 #7):** create `kb/People/X` mid-session; without `rebind` a nickname
  stays plain (H1 repro), with `rebind` it links; a private/Reference target links nothing.
- **Grounding (RT1 #3):** a backlink title is in the prompt but NOT in `run.sources`; a
  near-miss `[[Backlink]]` is not repaired into a citation.
- **Accept parity (RT1 #6):** Accept (status `ready`) calls `finalize_rebuild` then
  `promote_one` in the lock; idempotent on a 409 retry; staleness 409 on hash mismatch.
- **No-creds / cancel / `fail_on_turn`** paths.

Plus `test_writer_core.py` (`enforce_date_tokens` round-trip/malformed; `rebind_entities`
binds, skips private, no embeddings call) and `test_note_privacy.py` (the three
classifiers + `allow_note_for_target` domain-match matrix).

### 9.3 Frontend — `SuggestPanel.test.tsx` (vitest + MSW)

Copy `RebuildPanel.test.tsx` recipe: mock only `suggestStart`/`suggestTurn` with
`fakeStream`; accept/reject on MSW. `session_ready` renders BASE + backlink chips; a turn
shows optimistic user bubble, tool steps, `ops` re-render + summary bubble; **candidate
facts chip → Include triggers a follow-up turn**; failed-op note; full-reemit fallback
streams; a second turn works; Accept routes to `acceptSuggest`; stale 409 renders stale.

### 9.4 e2e — one Playwright flow (LLM faked at `e2e/fake_llm.py`)

Open "Suggest revisions" → instruction → fake returns an `apply_edits` tool call → tool
step + applied diff → Accept → page updated. Exercises the real `_sse` bridge + Accept/lock
the integration tier bypasses. (CLAUDE.md DoD #2 — user-facing flow / API contract.)

### 9.5 Coverage

New modules land with focused tests above the domain floors (`fail_under`
`server/pyproject.toml`; `thresholds` `web/vitest.config.ts`); **ratchet up** in the same
PR once green (DoD #3, never lower). Google-style docstrings on every new symbol;
`ruff check app`.

### 9.6 Sequencing — tool-less-first increment

| PR | Scope | Risk | Est. |
|---|---|---|---|
| **PR1 — shared hardening (loop-agnostic).** `writer_core.harden_draft` (refactor `_generate`'s tail, characterization-pinned), `enforce_date_tokens`, `rebind_entities` (call from `run_draft` too), `promote_one` in `finalize_rebuild`, fold `{date_rules}` into `wiki_revise`. **Fixes the owner's date + people-link + promotion bugs on the EXISTING rebuild — ship even if the new mode slips.** | Low-Med | ~2.5d |
| **PR2 — `edit_ops.py` + tool-LESS suggest loop.** Pure ops module + full unit suite; `run_suggest_start`/`run_suggest_turn` asking for an `apply_edits`-shaped JSON in plain assistant text (no tools); carry-forward generator; `RebuildRun` fields; router; `SuggestPanel` + `api.ts`; backlinks read-only. **A complete, shippable Suggest mode.** | Med | ~3.5d |
| **PR3 — truth-seeking tool layer (additive).** `_EDIT_TOOLS` + disposable thinking-off transcript on the **cheap** model; `_dispatch_tool` + the **firewall** (`note_privacy.py`) + candidate-facts gate; `tool_use`/`tool_result`/`assistant_delta`/`candidate_facts` streaming + UI; the transcript-safety + firewall + citation-firewall suites. If too costly/latent, **PR2 stands alone**. | High | ~4d |
| **PR4 — e2e + coverage ratchet + UX-coherence copy.** Playwright; ratchets; entry-point copy + convergence note. | Low | ~1d |

Total ≈ **11 days** discounted (RT2 estimated C's comparable scope at ~10; H2's firewall +
ops + candidate-gate add ~1). The owner sees bug-fixes at PR1, a usable targeted-edit loop
at PR2, and the truth-seeking he literally asked for at PR3 — risk rising only as payoff
rises.

---

## 10. The SEVEN RT1 non-negotiables — enumerated and answered

1. **Transcript tool-free, or tools only in a disposable thinking-off transcript.** ✅
   Fact-finding runs `thinking=False` on a disposable `ff` list discarded each turn
   (`rebuild_engine.py:152,193` pattern); `run.messages` gets only plain `user`/`assistant`
   pairs. CI asserts no list-content / no `tool_calls` / no thinking block (§9.2). Verified
   against `llm.py:404,676`.
2. **Re-validate the FULL article every turn.** ✅ `harden_draft` runs on the whole final
   applied string (citations/markers↔defs, lead, AKA, **firewall**, dead-links, dates,
   people-links) — never on an isolated op span (§3.3, §5).
3. **Backlinks read-only, NEVER in `run.sources`.** ✅ `run.backlinks` is separate;
   `source_titles` derives only from `run.sources` (§6.3, `architect.py:818`). Tested.
4. **PII firewall on the way IN.** ✅ The whole of §2 — `note_privacy.py` gates
   `search_notes` (titles), `read_source`/`read_backlink` (prose), and citation emission
   (footnotes), keyed on the target page's domain, plus a user-approval candidate-fact
   gate. This is H2's central deliverable and directly fixes RT1's CRITICAL on C.
5. **Date-enforce BEFORE add-links; `enforce_date_tokens` only round-trips.** ✅ Order in
   §5 step 1→3; `_mask_spans` doesn't mask `@t[...]` (`wiki_build.py:1830`), so dates go
   first; expansion semantics + `time_tokens.json` untouched (`test_api.py:1209`).
6. **`promote_one` idempotent on the Accept path.** ✅ In `finalize_rebuild`
   (`wiki_build.py:1718`); `surface_aliases`/`_apply_aka_line` rebuild the AKA line each
   call (`:1137`); a 409-retry Accept is safe. Tested run-twice (§9.2).
7. **Cheap entity rebind at session start (no `_sync_embeddings`).** ✅ `rebind_entities`
   = `_link_articles` (already private-safe, `entity_index.py:553`) + owner-alias fold,
   skipping the networked `_sync_embeddings` (`:360`). Needed because the live loop reads
   bindings before Accept's full `entity_index.rebuild` (`wiki_build.py:1718`, RT1
   ground-truth #2).

Additional RT1 corrections folded: A's `run_redraft` seed bug avoided (no fake assistant
turn, §1.2); B's whitespace fuzzy fallback dropped (§3.1); B/E's fenced-`##` hazard masked
(§3.1); C's expensive-model misread fixed (cheap model, §4.1); E's Accept-gate widening
avoided (§6.2).

---

## Honest argument for H2

H2 is the only hybrid that ships the owner's **literal** intent — the AI *goes and finds*
a salient fact mid-conversation (RT2 T2) — with the one fatal hole RT1 found actually
closed: a real, tested, three-surface inbound firewall plus a human approval gate, not a
Risks-paragraph sentence. It pairs that with deterministic exact-match ops for clean diffs
(RT2 T1) and the shared hardening core that fixes the owner's existing rebuild complaints
in week one (RT2's highest-weighted win). It resolves RT1↔RT2 by taking **RT2's
C-ranked-first intent** and subjecting it to **RT1's seven non-negotiables** literally.

**Biggest strength:** it is the only design where truth-seeking and the privacy firewall
are co-designed — the firewall isn't bolted onto an agent that already leaks; the agent
*cannot* see a withheld note's title, prose, or cite it, and even permitted facts pass a
human gate. That is the trust the owner is implicitly asking for.

**Biggest risk:** it is the most code and the most test surface — firewall + ops + a tool
loop, three independent failure families. The firewall's note-classifier (§2.2) is a
heuristic over un-flagged raw notes; a false negative would be a leak. I mitigate with
**default-deny on a public target** (any classified-private note is refused; the
user-approval gate catches misclassifications before anything lands) and an exhaustive
firewall suite. The tool topology carries the transcript-resume risk C does, contained by
the CI assertion and the disposable thinking-off discipline.

**Worth it?** The extra complexity over a tool-less B/D loop is real and I won't pretend
otherwise. But the tool-less-first increment means we never *bet* the project on it: PR1
delivers the bug-fixes, PR2 delivers a complete clean-diff Suggest mode, and PR3's
truth-seeking is purely additive and independently abandonable. Given that the owner's
quote names truth-seeking as the *first* requirement, paying PR3's cost to deliver it —
behind a firewall that makes it safe — is the right call. If a reviewer judges the
firewall classifier too risky, PR2 ships the product without it; nothing is wasted.
