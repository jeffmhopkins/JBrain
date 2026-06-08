# Plan A — "Minimal extension of the existing engine"

**Stance:** Ship the live "Suggest revisions" mode as a thin extension of the existing
rebuild machinery. Reuse `RebuildRun`, `_sse`, `_generate`, the curate/gather stages, and
the Accept/lock/staleness path **as-is**. The edit turn is essentially `run_guide` with
three changes: (a) the working draft is **seeded with the current article body**, (b) the
model is told to make MINIMAL, TARGETED changes and **emit the FULL revised article each
turn** (no new patch format), (c) **backlinks** are injected as read-only context.
"Targeted" is enforced by prompt discipline + a client-side diff against BASE, **not** by
structured patch ops. Hardening (date tokens, people-link rebind, promotion parity) is
folded in as deterministic post-turn backstops + an Accept-time promotion call that the
classic rebuild *also* inherits.

This stance optimizes for **ship-fast, low-risk**: it reuses the single most important
design invariant of the current engine — the Stage-2 transcript carries **no tool_use
blocks, so resuming it is "trivially safe"** (`rebuild_engine.py:9-15`, R1 §1, §7) — and
adds the least new infrastructure. Its honest weakness vs. a surgical-patch approach is
**token cost per turn** (each turn re-streams the whole article) and **large-diff noise**
(a full re-emit can perturb untouched prose), discussed in §9.

Throughout, the new flow is referred to as **"suggest"** and the run-kind as `"suggest"`.

---

## 1. Backend architecture

### 1.1 Decision: extend, don't fork the engine

We add **one new top-level engine entry per phase** but route every turn through the
existing `_generate` (`rebuild_engine.py:271-398`). We do **not** add a new transcript
spine, a patch parser, or a tool. Concretely:

- **`run_suggest_start(run, conn)`** — the seed turn. Loads context (curated sources +
  backlinks), builds the suggest prompt, sets `run.messages` to a single user turn, then
  calls `_generate`. This is the structural twin of `run_draft` (`rebuild_engine.py:401-437`)
  except the prompt **preserves BASE** and the curate UI is collapsed (see §1.3 for whether
  gather/curate runs).
- **`run_suggest_turn(run, conn, instruction)`** — the edit turn. The structural twin of
  `run_guide` (`rebuild_engine.py:440-463`): append ONE user turn with the steer
  instruction, re-run `_generate`. The ONLY behavioral differences from `run_guide` are
  the steer text (targeted-edit framing, §6) and the fact that `run.messages` already
  contains BASE as the seed assistant turn.

Both live in `rebuild_engine.py` (same module — they share `_generate`, `_clamp_tokens`,
the lint tail, and the `llm` seam). No new service module; this keeps the "one engine"
mental model and means the hardening we add (§5) is written once and serves both modes.

### 1.2 How the edit turn works, step by step

The edit turn deliberately mirrors today's Guide path (R1 §5, R4 §1c) so the transcript
hazard is avoided:

1. **Seed (first turn only, `run_suggest_start`).** Build `run.messages` as:
   ```
   [ {role:user,    content: SUGGEST_PROMPT(BASE, sources, backlinks, known)},
     {role:assistant, content: BASE_BODY} ]
   ```
   i.e. we **prime the transcript with the current article as the assistant's first
   "draft"** so the model treats it as the working document to edit, not a blank page.
   This is the key BASE-preservation trick and it costs no new infrastructure — it is just
   two dict entries appended to `run.messages` exactly like `run_draft` does at
   `rebuild_engine.py:435`. We then **do not** stream a generation on the seed turn; we
   stage `run.draft = BASE_BODY` directly and emit a synthetic `done` so the panel shows
   the unchanged article immediately (no LLM call, no cost — the first model call happens
   only when the user sends their first message).
2. **User talks.** The router appends a user turn (the instruction) to `run.messages` —
   exactly `run_guide`'s `run.messages.append({"role":"user","content":steer})`
   (`rebuild_engine.py:461`).
3. **Generate.** `_generate` streams thinking + content deltas, with `tools=[]` and
   `thinking=True` (`rebuild_engine.py:320-321`). The model emits the FULL revised article
   (steered to change only what was asked). `stream_turn` appends the assistant turn
   verbatim — signed thinking included — to `run.messages` (`rebuild_engine.py:299-308`),
   so the next turn resumes safely.
4. **Hardening tail.** The existing tail runs unchanged on the whole new body:
   `_extract_talk` → `_bad_links`/`_repair_citation_titles`/`_neutralize_links` →
   `add_links_to_content` → `validate_structure` (`rebuild_engine.py:366-398`). We
   **insert** the new date-token + token-preservation enforcement here (§5a) and re-run the
   per-turn people-link backstop (already present at `:387`, §5b).
5. **Stage + emit.** `run.draft` = hardened body, `run.status="ready"`, emit `done`
   (`rebuild_engine.py:393-398`) plus a new `edit_summary` event (§2) carrying a one-line
   "what changed".
6. **Loop.** Repeat from step 2. The transcript grows by `[user steer, assistant full
   draft]` per turn — same growth pattern as Guide today (R1 §6).

### 1.3 Gather/curate: reuse, but make it optional

Two viable sub-choices; **Plan A picks (b)** for ship-fast:

- (a) Run the full Stage-1 gather + curate wizard first (reuse `run_gather` verbatim),
  then enter the suggest loop. Maximum reuse, but the brief's UX is "talk to the article",
  not "pick sources first".
- **(b) Skip the interactive curate screen; seed the curated set deterministically.** Reuse
  `rebuild_sources` (`wiki_build.py:1640-1678`) + `_load_sources` (`:412-462`) to assemble
  the same source set the gather agent would have proposed from (prior citations ∪ entity
  index), **without** the LLM gather agent or the curate UI. This is what `run_draft`
  already does internally after curate (`rebuild_engine.py:422-432`); we just call it at
  start. The user curates *conversationally* instead ("drop the old診 source", "use note
  X") — which is the whole point of the mode.

Choosing (b) means **zero new gather/curate frontend** and one fewer LLM stage. If a future
iteration wants explicit source curation, the gather path is still there to bolt on.

### 1.4 Transcript management — avoiding the tool_use / signed-thinking hazard

This is the load-bearing reason Plan A is low-risk. R1 §1 and §10 are explicit: the Stage-2
transcript is safe to resume **only because it has no tool_use blocks**
(`rebuild_engine.py:9-15`). Plan A preserves this invariant absolutely:

- The edit turn uses **no tools** (`tools=[]`, same as draft/guide) — we never introduce an
  `apply_edit` tool, so we never mix tool_use + signed thinking in one resumable transcript
  (the exact fragility called out in R1 §10 "Risks").
- The seed assistant turn (BASE body) is a **plain text** assistant block, not a
  provider-signed thinking/tool block, so priming the transcript with it is safe.
- Auto-continue (`CONTINUE_PROMPT`, `rebuild_engine.py:309-358`) and redraft unwind
  (`run_redraft`, `:466-503`) work unchanged because every suggest turn still ends with a
  **full regenerated draft** — exactly the shape redraft assumes (R1 §10 "Auto-continue").
  This is a direct benefit of "emit the full article" over a patch format: we reuse the
  truncation/redraft machinery verbatim.

---

## 2. SSE protocol additions & state-machine deltas

Plan A reuses the entire event vocabulary (R1 §4): `thinking_delta`, `content_delta`,
`lint`, `done`, `error`, `run_started`. The full-article-per-turn choice means **we need no
`patch`/`edit` event type** — the draft arrives as `content_delta`s + `done.draft` exactly
as today. Additions:

| New/changed event | Emitted by | Payload | Why |
|---|---|---|---|
| `run_started` (reused, +`kind`) | router `start_suggest` | `run_id, slug, title, base_rev, kind:"suggest", base_body, backlinks` | client seeds BASE + backlink chips |
| `edit_summary` (NEW) | engine, after `done` | `text` (≤1 sentence) | the real per-turn AI "what changed" bubble (replaces RebuildPanel's canned ack, R5 §1) |
| `seeded` (NEW, optional) | engine `run_suggest_start` | `draft` (= BASE body) | renders the unchanged article instantly with no LLM call |
| `lint` (reused, +date) | engine tail | `ok, message` | now also carries date-token/token-preservation findings (§5a) |

`edit_summary` is cheap: the model is asked (in the suggest prompt, §6) to end its output
with a fenced `summary` block (parsed and stripped server-side, mirroring how `_extract_talk`
strips the `talk` fence, `rebuild_engine.py:366`), OR we derive a trivial summary from the
diff size. Prefer the fenced-summary approach — deterministic to parse, no extra call.

**State machine deltas** (R1 §3): we add **no new statuses**. The suggest loop reuses
`streaming` → `ready` (per turn) and accepts from `ready`/`guiding`
(`rebuild.py:345`). The only nuance: the **seed** transitions straight to `ready` without a
`streaming` LLM pass (it's a synthetic `done`). `is_live` (`rebuild_runs.py:145-154`) and
`_LIVE` need no change. Accept/Reject/redraft transitions are untouched.

---

## 3. RebuildRun object changes

`RebuildRun` (`rebuild_runs.py:27-49`) gains a small number of additive fields (all
defaulted, so the classic rebuild is unaffected):

| New field | Type / default | Meaning |
|---|---|---|
| `kind` | `str = "rebuild"` | `"rebuild"` or `"suggest"`; gates suggest-only behavior + Accept promotion. |
| `base_body` | `str = ""` | The preserved BASE article body (== the live page at start). Seeds the transcript + the client diff baseline. (`base_hash` already hashes this; `base_body` keeps the text.) |
| `backlinks` | `list[dict] = []` | `[{title}]` of kb pages linking TO this page — READ-ONLY context (§4). |
| `base_tokens` | `list[str] = []` | The `@t[...]` tokens present in BASE, captured at start for the token-preservation guard (§5a, R3 Option D). |

`create()` (`rebuild_runs.py:76-103`) gains optional `kind`/`base_body` params (default
preserves today's behavior). `base_hash` continues to guard staleness against the **live
page** (R1 §8) — unchanged. No persistence, no share-payload exposure — the run stays
owner-only by construction (`rebuild_runs.py:1-9`).

---

## 4. Backlinks loading (read-only context)

Reuse the exact inbound-link SQL the research identifies at `architect.py:818-822` (R1 §7):

```sql
SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id = l.source_note_id
WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title
```

A new helper **`wiki_build.backlink_titles(conn, note_id) -> list[str]`** wraps this
(natural home — `wiki_build` already owns source loading and the link helpers). Called once
in `run_suggest_start` against the note's row id (fetched the same way `_kb_note` does,
`rebuild.py:128-131`).

**Critical invariant (R1 §7, R2 §1, R1 §10 "Hardening on partial edits"):** backlinks are
injected **into the prompt as a labeled read-only "Linked from" block**, and are **NEVER**
added to `run.sources`. `run.sources` is the curated grounding set that drives
`_repair_citation_titles` (`rebuild_engine.py:372-374`); polluting it with backlink titles
would make backlinks become citation-repair targets and shift grounding. Backlinks are also
**not** added to `run.known` (the cross-link allow-set) unless they already are — they're
context for *understanding how this page is referenced*, not new link targets the model may
invent. We render them as plain titles ("Linked from: A, B, C") so the model can keep
existing inbound references coherent (e.g. don't rename a section other pages cite) without
treating them as sources to cite or pages to link.

---

## 5. Folded-in hardening — what runs WHERE

All three harden the **shared** `_generate` tail / Accept path, so **both** classic rebuild
and suggest inherit them (the "benefits both" requirement).

### 5a. Date-token enforcement — R3 Option A + Option D (+ surface C)

**Where:** a new `clock.enforce_date_tokens(body, *, base_tokens)` helper called inside the
`_generate` tail, right after `add_links_to_content` and before `validate_structure`
(`rebuild_engine.py:387-392`). Living next to `clock.py` keeps both twins (Python +
`time.ts`) honest (R3 §6).

Plan A ships the **two zero-coverage, near-zero-false-positive checks** (R3 §3
recommendation) and defers the riskier adjacency rewriter:

1. **Option A — malformed-token linter (HIGH value, ~zero risk).** Detect any `@t`-shaped
   substring (`@t\s*[\[{(]`) that `clock._TOKEN_RE` (`clock.py:105`) does **not** match, or
   whose date arg fails `clock._to_dt` (`clock.py:127`) — e.g. `@t{age:…}`, `@t[born:…]`,
   `@t[age:1986]`. A broken token renders verbatim as ugly raw text (R3 §2 failure mode 4).
   Emit a `lint` event; in the suggest loop the human can fix it next turn. (We do **not**
   block Accept on it in Plan A — keep Accept's guard surface minimal; surface + let the
   conversation resolve. A red-team may argue for blocking; that's a one-line `ok=False`.)
2. **Option D — token-preservation guard (the loop-specific check, R3 §3 "Recommended for
   the targeted-edit LOOP").** Compute `base_tokens = _TOKEN_RE.findall(BASE)` at start
   (stored on the run, §3). After each edit turn, assert every BASE token still appears
   verbatim in the new draft. A token that vanished is the **most likely new regression the
   conversational loop introduces** (the model "tidies" `@t[age:…]` into a frozen number).
   If a token disappeared AND the same fact is still present (heuristic: the surrounding
   noun phrase survived), emit a `lint` warning naming the lost token. Cheap, deterministic.
3. **Surface (Option C-lite):** the existing `validate_structure` frozen-literal warning
   (`wiki_guides.py:318-320`, `_REL_TIME_RE` at `:47-48`) already flows through as advisory
   `lint` (`rebuild_engine.py:392`). Plan A keeps it advisory (the human is the reviewer,
   R2 §6) — we do **not** port the batch revise loop (that's the surgical/heavier stance's
   territory; keep Plan A minimal).

Plan A deliberately **defers Option B (adjacency auto-rewrite)**: it's MEDIUM-risk and
needs the round-trip guard; the conversational loop lets the user just *say* "make that age
a live value", which the model handles via the now-strengthened DATES prompt fragment (§6).
This is the honest minimal-surface tradeoff.

### 5b. People-link fix — entity-index rebind at session start + per-turn backstop

This is the R4 §H1 root cause fix and the single biggest correctness win.

1. **Rebind at session start (fixes H1, R4 O1).** Add a cheap, **embeddings-free**
   `entity_index.rebind(conn)` that runs only the binding half of `rebuild()`:
   `_link_articles(conn)` (`entity_index.py:529-564`) + `_apply_overrides` +
   `reconcile_owner` materialization — **without** `_sync_embeddings` (the networked part,
   `entity_index.py:360`, R4 O1 caveat). Call it once in `run_suggest_start` (and in
   `run_draft` for the classic path, so rebuild benefits too — R4 §3). This binds a
   freshly-promoted/renamed `kb/People/<X>` so its nickname surface is offered to both the
   `{known_aliases}` prompt block and the Pass-2 `add_links_to_content` backstop. PII
   firewall is preserved for free: `_link_articles` already excludes private titles
   (`entity_index.py:553`) and `alias_surface` keeps drop rule (v) (R4 O5).
   *Implementation note:* the owner-alias fold is eventual-consistent
   (`entity_index.py:343-358`); to get first-session correctness, `rebind` runs
   `reconcile_owner` then the alias materialization in the same call (R4 O1 caveat).
2. **Per-turn backstop (R4 O2 — already true, make it explicit + tested).** Every suggest
   turn re-runs `add_links_to_content` at `rebuild_engine.py:387` (it's in the shared tail).
   We add a test asserting a turn re-links a name the model dropped (R4 O2). Nothing new in
   code; the rebind step is what makes this backstop actually *have* the binding to use.
3. **(Optional, defer) R4 O3 surface "unlinked known person" as lint** — nice but not
   required for parity; defer to keep surface minimal.

The PII invariants (R4 §5) are untouched: we never bypass `add_links_to_content`/`_mask_spans`
because we still hand the **whole** body to the tail every turn (a direct benefit of full
re-emit over patching: no risk of patching around the masker on stale text — R4 §5 item 3).

### 5c. Promotion parity on Accept — shared `promote_one`

**Where:** a new **`wiki_build.promote_one(conn, title)`** called inside `finalize_rebuild`
(`wiki_build.py:1681-1721`) — so **both** classic Accept AND suggest Accept AND nightly
rebuild gain promotion parity (R2 §6, R2 rec 4 — "closes divergence #5 for the whole
family"). Placing it in `finalize_rebuild` (rather than only the new Accept route) is the
maximal-leverage, minimal-code choice.

`promote_one` runs the per-article subset of the build's post-write suite
(`actions/wiki_build.yaml:82-108`) that the single-article paths skip today (R2 §6):
`link_owner` (`wiki_build.py:1009`), `surface_aliases` (`:1177`), `link_medications`
(`medref.py:415`), `link_places` (`places.py:191`), `normalize_link_labels`, and
`flag_ungrounded_reference` (`wiki_build.py:1428`). These are **already deterministic,
idempotent, cached, and corpus-wide-but-cheap** (verified: `link_places`/`link_medications`
self-scope to the relevant `kb/Places/%` / medication entities; `surface_aliases` is
idempotent and AKA-owns the line per R3 §5). Plan A's `promote_one` simply **calls the
existing corpus-wide functions** post-Accept — no per-title refactor needed for v1 (they're
cheap and idempotent). If profiling later shows cost, factor title-scoped variants; not
required to ship.

Ordering inside `finalize_rebuild`: keep the current `entity_index.rebuild` →
`write_disambiguation_pages` → `flag_dead_links` (`:1718-1720`), then run `promote_one`
**after** `entity_index.rebuild` (so owner/alias bindings are fresh), then a final
`flag_dead_links` pass if `promote_one` added links. `surface_aliases` must run last among
body-mutators since it owns the AKA line (R3 §5).

**Net of §5:** a single `enforce_date_tokens` helper, a single `entity_index.rebind`, and a
single `promote_one` — three new functions, each called from a shared chokepoint, each
benefiting rebuild + suggest together.

---

## 6. Prompt changes / shared fragments

R2 §3 (rec 3) calls for factoring directive blocks into reusable fragments. Plan A does the
**minimal** version of this — enough to make the suggest turn carry the rules, without a
sweeping prompt refactor:

1. **New `actions.wiki_suggest_seed`** in `prompts.yaml` — the seed/system framing for the
   suggest mode. It states: BASE is the current article (below as the working draft);
   make **MINIMAL, TARGETED** edits per the user's guidance; **PRESERVE** everything not
   asked to change, including every `@t[...]` token verbatim; output the **COMPLETE** revised
   article in the same Markdown format; end with a fenced `summary` block (one sentence).
   It **embeds the DATES & TIME block** (copied from `wiki_write`, `prompts.yaml:868-874`)
   and the **CROSS-LINKS** block (`:855-860`) so the edit turn honors them even after many
   turns push the original directives back in context (R2 §4 — the dilution problem that
   makes guide revisions the weakest at honoring rules).
2. **New `actions.wiki_suggest_turn`** — the per-turn steer (replaces `run_guide`'s bare
   string at `rebuild_engine.py:455-460`). It re-states "targeted, preserve the rest,
   PRESERVE @t[...] tokens, output the complete article, summary block" + the user's
   `{instruction}`. Keeping the DATES/CROSS-LINK reminders **in the steer itself** is the
   cheap way to fight directive dilution without a structured prompt-fragment system.
3. **Fold the DATES directive into `wiki_revise`** (`prompts.yaml:908-932`) — R2 §3 / R3
   §2: `wiki_revise` currently omits DATES, so a self-critique pass can re-freeze a token.
   One-block addition; benefits the batch writer too.

Plan A explicitly does **not** build the full `{date_rules}/{crosslink_rules}/…` fragment
substitution system (R2 rec 3) — that's a larger refactor better owned by a heavier stance.
We copy the two blocks into the two new suggest prompts (small duplication, low risk). If a
red-team prefers the fragment system, it's a clean follow-up that doesn't change Plan A's
shape.

---

## 7. Frontend

Reuse and extend `RebuildPanel.tsx` (R5 §1, §4). Plan A's UX choice: a **separate
`SuggestPanel.tsx`** that shares the lower-level primitives, rather than overloading
`RebuildPanel`'s `Stage` wizard — because the suggest mode has **no gather/curate stage**
(§1.3) and making the conversational loop the *primary* surface inside the existing 3-stage
state machine would tangle the `Stage`/`Phase` unions (`RebuildPanel.tsx:15-16`). The new
panel is small because it borrows:

- **The guide loop primitives, promoted to primary** (R5 §1, §4): `thread` state
  (`RebuildPanel.tsx:62`), the footer composer textarea + Enter-to-send (`:283-289`), the
  **stable-`onClose` ref trick** (`:78-79`) so the Modal focus effect doesn't steal the
  composer's focus, `handleDraft`'s SSE handler (`:117-134`), and `sawContent` (`:73`).
- **Optimistic dual-bubble** from `Chat.tsx:587` (R5 §4): on Send, push the user bubble
  immediately, then replace the canned ack with the streamed `edit_summary` as the AI
  bubble (R5 §1 — "replace the canned string at :237").
- **Diff against BASE**: `MarkdownDiff before={note.content_md} after={draft}` toggled by
  `showDiff` (`RebuildPanel.tsx:431-435, :458-460`). For the suggest mode the diff is the
  PRIMARY view (it makes "targeted" legible despite full re-emit — directly mitigating the
  large-diff-noise weakness, §9). Default `showDiff = true`.
- **Accept/Reject** footer buttons reuse `acceptRebuild`/`rejectRebuild` (`api.ts:955-959`).

**api.ts additions** (R5 §2): a `SuggestEvent` union (reuses most of `RebuildEvent` +
`edit_summary`/`seeded`), and thin `streamSSE`-based wrappers — `suggestStart(slug,onEvent)`,
`suggestTurn(runId,text,onEvent)` — plus reuse of `acceptRebuild`/`rejectRebuild`. Do NOT
hand-roll a reader; `streamSSE` (`api.ts:875-929`) already handles abort/stall/health/`\n\n`
framing (R5 §2).

**Entry point** (R5 §3): a second `NoteActionsMenu` item **"Suggest revisions"** right next
to "Rebuild page now" (`NotePage.tsx:265-267`), KB-only, with the same `rebuildNow`-style
`llm.ready` pre-flight (`NotePage.tsx:119-124`). Mount `<SuggestPanel slug note={{title,
content_md}} onClose onAccepted />` near the existing RebuildPanel mount (`:379-383`);
`onAccepted` navigates on rename else reloads, identical to today. Backlinks are already on
the page (`note.backlinks`, R5 §3) — but Plan A loads them **server-side** (§4) so the
prompt grounding is authoritative; the client only needs them for display chips.

---

## 8. Test plan (per tier, honoring CLAUDE.md Definition of Done)

### 8.1 Backend (server/tests, `llm` seam mocked — copy `test_rebuild_engine.py`)

New `server/tests/test_suggest_engine.py`, `pytestmark = pytest.mark.integration`, copying
the harness from `test_rebuild_engine.py:36-128` (R5 §5b): `_drain(agen)` to run generators
directly, `FakeProvider` with a scripted turn list, `_install_provider` to monkeypatch the
`llm` seam (never the SDK), real SQLite with embeddings no-op'd, `_mk`/`rebuild_runs.create`
seeding. Cases:

- **Seed preserves BASE**: `run_suggest_start` stages `run.draft == BASE` with no LLM call;
  `base_tokens` captured; `run.sources` does NOT contain backlink titles (§4 invariant).
- **One edit turn**: scripted `FakeProvider` returns a full edited body; assert
  `add_links_to_content` re-linked a dropped name (R4 O2), `done` + `edit_summary` emitted,
  transcript ends with the assistant draft (resume-safe — no tool_use blocks).
- **Two turns**: second turn resumes the grown transcript; assert no exception (the
  transcript-hazard regression test — the core low-risk claim).
- **Date hardening (§5a)**: turn output containing `@t{age:…}` (malformed) → `lint`
  malformed-token warning; turn that drops a BASE `@t[age:…]` → token-preservation `lint`.
- **People rebind (§5b)**: create `kb/People/<X>` mid-session, call `entity_index.rebind`,
  assert a nickname surface now links (mirrors `test_owner_alias_backfill.py`, R4 §H1
  confirmation recipe). Assert PII firewall: a Reference/private TARGET links nothing.
- **No-credentials / error / cancellation** paths (R5 §5b): `creds=False`, `fail_on_turn`,
  `run.cancelled=True` mid-stream.
- **Backlinks SQL**: `backlink_titles` returns inbound titles, excludes deleted/self.

New `server/tests/test_promote_one.py` (or extend `test_rebuild_refs_links.py`):
`finalize_rebuild` now runs `promote_one` → assert `surface_aliases` AKA line present,
`link_owner` linked, idempotent on a second call. Staleness guard unchanged.

`server/tests/test_clock.py` (or extend): `enforce_date_tokens` malformed-detection +
token-preservation, plus **the byte-for-byte twin pin** — any expansion-semantics change
must update `server/tests/fixtures/time_tokens.json` and keep `clock.expand_tokens` ↔
`time.ts:expandTimeTokens` identical (R3 §1d, §6; `test_api.py:1209-1211`). Plan A's date
work is **additive (production of tokens), not expansion semantics**, so the fixture should
NOT need changes — a test asserting that is itself valuable.

### 8.2 Frontend (vitest + MSW — copy `RebuildPanel.test.tsx`)

New `web/src/components/SuggestPanel.test.tsx`, copying the recipe at
`RebuildPanel.test.tsx:1-11, 29-53` (R5 §5a): mock only the stream helpers
(`suggestStart`/`suggestTurn`) with scriptable `fakeStream`, keep accept/reject on MSW,
`renderWithProviders` + `server`, `vi.stubGlobal("confirm"/"alert")`, health `__reset()`,
footer-button scoping to `.modal-foot`. Cases:

- Seed renders BASE immediately, diff view default-on, no spurious LLM call.
- A turn: optimistic user bubble appears, draft re-renders from `content_delta`s, AI
  `edit_summary` bubble replaces the optimistic placeholder; a **second** turn works.
- `lint` warning banner shows on a date/token finding.
- Accept routes to `acceptRebuild`; stale 409 renders the stale state (reuse
  `RebuildPanel.tsx:220-222` handling); Reject calls `rejectRebuild`.

**Coverage-floor risk (named):** R5 §5a flags that the existing guide/conversational loop
is **currently untested** (`guideStream` is mocked but never scripted). Adding `SuggestPanel`
with a real loop test *raises* covered branches, but the new panel also adds uncovered
lines until tested — Plan A must land the panel and its tests **in the same PR** to avoid a
`thresholds` regression in `web/vitest.config.ts`. Likewise backend `fail_under` in
`server/pyproject.toml`: the new engine functions + `promote_one` + `enforce_date_tokens` +
`rebind` are several hundred lines; their tests must cover the happy + error + PII-firewall
branches in the same PR, and we **ratchet the floor up** if real coverage lands comfortably
above (CLAUDE.md DoD item 3).

### 8.3 e2e (Playwright, LLM faked at `e2e/fake_llm.py`)

**One e2e is warranted** because the suggest mode is a new **user-facing flow** behind the
API contract (CLAUDE.md DoD item 2). A minimal happy-path: open a KB page → "Suggest
revisions" → send one instruction → fake LLM returns an edited body → diff shows the change
→ Accept → page reloads with the new body. This exercises the real SSE bridge (`_sse`,
`rebuild.py:57-111`) + Accept/lock that the integration tests deliberately bypass (R5 §5b
notes there's no TestClient streaming precedent — e2e is the right place for the framing).
Keep it to one flow; the per-turn correctness lives in the unit/integration tiers.

---

## 9. Risks, edge cases, staleness interaction

- **Token cost per turn (the headline weakness).** Each turn re-streams the WHOLE article
  and appends it to `run.messages` (R1 §6 — "N independent full re-drafts sharing one
  growing transcript"). For a long article over many turns, both output cost and transcript
  size grow linearly. A surgical-patch approach would emit only changed spans. **Mitigations
  Plan A ships:** (i) the diff-first UI makes targeted edits legible despite full re-emit;
  (ii) `_clamp_tokens` already bounds per-turn output (`rebuild_engine.py:33-48`); (iii) the
  TTL/`_MAX_RUNS` caps bound memory (`rebuild_runs.py:20-21`). **Honest admission:** this is
  the cost Plan A pays for reusing `_generate` and avoiding the patch-parser + the tool_use
  hazard. We judge the trade worth it for v1; a patch format can be layered later behind the
  same panel without re-architecting Accept/lock/staleness.
- **Large-diff noise / drift.** A full re-emit can perturb untouched prose, and the draft can
  drift from BASE over many turns (R1 §10 "Cost/latency"). **Mitigations:** the targeted-edit
  prompt (§6) + the **token-preservation guard** (§5a, R3 Option D) catch the most damaging
  silent regression (dropped `@t[...]`); the diff view surfaces unexpected churn so the user
  catches drift; and the user can always Reject and restart (cheap, in-memory).
- **Staleness guard vs. a long conversation (R1 §8).** `base_hash` is the sha256 of the
  **live page at start** (`rebuild_runs.py:35,100`); Accept refuses if the live page changed
  since (`rebuild.py:369-373`). This is **orthogonal to conversation length** — a 20-turn
  suggest session is fine *as long as nobody else edited the live page*. The risk is a long
  session increasing the window for a concurrent edit (another device, the nightly maintain
  pass). Plan A keeps the existing behavior: on a stale 409 the panel shows the stale state
  (`RebuildPanel.tsx:220-222`) and the user re-opens. We do **not** auto-rebase the draft
  onto the new BASE (that's drift-prone); explicit restart is the safe minimal choice. Note
  the sliding TTL (`rebuild_runs.py:18-20`) means an *active* conversation never expires
  under the user, but an idle one reaps after 30 min — acceptable.
- **One run per slug** (`rebuild_runs.py:53,91-93`): a suggest run and a classic rebuild
  can't be live on the same page simultaneously; `create()` drops the prior. The UI must
  reflect this (opening one cancels the other) — same constraint as today, just now across
  two entry points. The shared `_BY_SLUG` registry handles it for free.
- **Seed turn with a stub article.** A stub (< `stub_max_chars`, R3 §4) seeded as BASE is
  fine, but an edit that grows it past the threshold suddenly imposes lead/section lint
  (R3 §4 boundary). Advisory only — surfaced as `lint`, the human decides.
- **Protected pages** (`is_protected`, `wiki_guides.py:87`, R3 §5): the suggest entry must
  refuse `kb/_*` pages, same as the writer never overwrites them. Gate in the router
  (reuse/extend `_kb_note`).
- **Backlinks shifting grounding** — already mitigated by the §4 invariant (never into
  `run.sources`). Tested in §8.1.

---

## 10. Sequencing / effort estimate (PR breakdown)

Plan A is structured so the **shared hardening lands first** (benefiting classic rebuild
immediately, de-risking the new mode), then the new mode is a thin top layer.

- **PR 1 — Shared hardening, no new mode (small/medium).** `entity_index.rebind` (call it
  from `run_draft` too), `clock.enforce_date_tokens` (Options A + D) wired into the
  `_generate` tail, `wiki_build.promote_one` wired into `finalize_rebuild`, fold DATES into
  `wiki_revise`. Tests: `test_clock` date checks, `test_promote_one`, a rebuild test that a
  freshly-created People page now links. **This PR alone fixes the date + people-link +
  promotion-parity bugs for the existing "Rebuild page now".** Lowest risk, highest standalone
  value — ship even if the new mode slips.
- **PR 2 — Backend suggest engine (medium).** `RebuildRun` new fields + `create()` params;
  `wiki_build.backlink_titles`; `run_suggest_start` / `run_suggest_turn`; router endpoints
  `/api/kb/suggest/start/{slug}`, `/{run_id}/turn` (reuse `_sse`, `_live_run`, accept/reject);
  `wiki_suggest_seed`/`wiki_suggest_turn` prompts. Tests: `test_suggest_engine.py` (full set).
- **PR 3 — Frontend + e2e (medium).** `SuggestEvent` + `suggestStart`/`suggestTurn` in
  `api.ts`; `SuggestPanel.tsx`; NotePage entry item + mount; `SuggestPanel.test.tsx`; one
  Playwright e2e. Ratchet coverage floors if comfortably above (DoD item 3).

Rough effort: PR1 ~1–1.5 days, PR2 ~2 days, PR3 ~2 days incl. tests. Total ~1 week, with PR1
deliverable independently. The sequencing means CI stays green per-domain at each step and
the user's two original complaints (dates, people links) are fixed in week-one PR1.

---

## Appendix — citations index

Research: R1 `01-rebuild-engine.md` (§1 transcript invariant, §3 state machine, §4 events,
§5 threading, §6 full-rewrite crux, §7 sources+backlinks, §8 Accept/lock, §9 hardening tail,
§10 risks). R2 `02-synthesis-vs-rewrite.md` (§3 dates, §4 links, §6 promotion asymmetry,
recs 1–6). R3 `03-formatting-dates.md` (§1 token grammar, §2 root cause, §3 Options A–E,
§4 validate_structure, §5 formatting). R4 `04-people-linking.md` (§1 pipeline, §2 H1 root
cause, §3 rebuild-vs-build gap, §4 O1–O5, §5 invariants). R5 `05-frontend-tests.md` (§1
RebuildPanel state, §2 streamSSE, §3 entry point, §4 conversational precedents, §5 test
recipes).

Code: `rebuild_engine.py:9-15,119-209,271-398,401-463,466-503`; `rebuild_runs.py:24-49,
76-103,145-154`; `rebuild.py:57-111,128-133,322-397`; `wiki_build.py:412-462,711-781,784-818,
1009,1177,1428,1640-1678,1681-1721`; `entity_index.py:263-362,529-564`; `places.py:191`;
`medref.py:415`; `architect.py:818-822`; `wiki_guides.py:47-48,87,318-320`; `clock.py:105,
127`; `prompts.yaml:855-874,908-932`; `actions/wiki_build.yaml:82-108`; `api.ts:858-959`;
`RebuildPanel.tsx:15-16,62-79,117-134,220-289,431-460`; `NotePage.tsx:119-124,265-267,
379-383`; `Chat.tsx:587`; `test_rebuild_engine.py:36-128`; `RebuildPanel.test.tsx:1-53`.
