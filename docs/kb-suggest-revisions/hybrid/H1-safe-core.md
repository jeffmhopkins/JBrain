# Hybrid H1 — "Safe core, layered capability" (RT1-leaning, ship-safe)

**Stance.** Ship the live "Suggest revisions" mode as **A's full-article re-emit
conversational loop** sitting on **D's shared `harden_draft` core**. This combination has
**zero anchor / splice / fence / transcript-resume corruption surface** (RT1's safest
mechanism, RT1 "Guidance for the hybrid round") and it delivers the existing rebuild's
**date + people-linking + promotion bug-fixes first** (PR1), which RT2 weights as "the
single most valuable deliverable in the entire bake-off" (RT2 §1).

Truth-seeking in v1 is **conservative and firewall-safe**: the AI reasons over the curated
sources + read-only backlinks already in context. "Go find more notes" is the **existing
user-gated regather affordance** (reuse `run_gather`, `rebuild_engine.py:119-209`) — no
autonomous private search, so **no inbound-firewall hole** is opened in v1. Clean-diff
presentation is solved in the **UI** (diff-against-BASE, default on), accepting that the
model re-emits the full article.

**Honest position on intent (answered in full in §6 and the closing self-assessment).**
v1 **under-delivers on active truth-seeking** versus C: it cannot *go find* a fact the
owner raises mid-conversation without the user invoking regather. I argue this is the right
v1 because (a) the owner's two *literal* complaints (wrong dates, broken people-links) are
fixed in week one with the least blast radius; (b) the autonomous-search path is exactly
the one carrying RT1's single CRITICAL finding (PII leak into shareable content), so
shipping it later — behind a designed-in firewall — is strictly safer; (c) the safe loop
is a real, shippable product, and the richer capability bolts on **behind the same panel**
with no rework (§8).

Throughout: the new flow is `kind="suggest"` on the existing `RebuildRun`.

---

## 1. Architecture & file changes; the loop per turn; the `_generate` draft-wipe & `run_redraft` seed bugs

### 1.1 Decision: extend the engine, do not fork; do not refactor four paths

We add two engine entry points and route **every turn through `_generate`**
(`rebuild_engine.py:271-398`) — the full-re-emit loop. This is the **only** mechanism that
can reuse `_generate` verbatim (RT1 §0.1: `_generate` wipes `run.draft=""` at `:295` and
re-streams a full body, so B/C/E cannot reuse it; A/full-re-emit can). We borrow **only
D's `harden_draft` extraction** for the shared tail — **not** D's four-path refactor of
`write_one`/`maintain_one`. RT1 (D verdict) and RT2 (§1, D row) both judge the four-path
refactor's regression blast radius (the `maintain_one` link-backstop "free" behavior change
that moves thousands of golden snapshots) as paying maximum risk for parity the owner never
asked for. **H1 scopes `harden_draft` to exactly the two live-loop call sites** —
`_generate` and the new suggest start/turn — leaving `write_one`/`maintain_one` untouched.
(If a later cleanup wants nightly/maintain parity, that's an independent follow-up, not a
prerequisite — RT2 §1 verdict.)

| File | Change |
|---|---|
| `server/app/services/writer_core.py` | **NEW.** Pure-ish module: `harden_draft`, `enforce_date_tokens`, `promote_one`, `rebind_entities`. Imports `wiki_build` primitives; does **not** move them (their tests stay green). (Borrows D §1a, scoped to the live path.) |
| `server/app/services/rebuild_engine.py` | `_generate`'s tail (`:366-398`) calls `writer_core.harden_draft`. Add `run_suggest_start`, `run_suggest_turn`. Add the **seed guard** for `run_redraft` (§1.4). |
| `server/app/services/rebuild_runs.py` | `RebuildRun` gains `kind`, `base_body`, `backlinks`, `base_tokens` (§3). `create()` gains optional `kind`/`base_body`. |
| `server/app/routers/rebuild.py` | New `POST /api/kb/suggest/start/{slug}`, `/{run_id}/turn`; **reuse** `_sse` (`rebuild.py:57`), accept/reject, the staleness gate (`:345,:371`). |
| `server/app/services/wiki_build.py` | `backlink_titles(conn, note_id)` (read-only, §4); `finalize_rebuild` (`:1681`) calls `writer_core.promote_one`. |
| `prompts.yaml` | New `wiki_suggest_seed` + `wiki_suggest_turn`; fold the DATES block (`prompts.yaml:868-874`) into `wiki_revise` (`:908`). |
| `web/src/api.ts`, `SuggestPanel.tsx`, `NotePage.tsx` | Frontend (§6). |

### 1.2 How a turn works (full-re-emit; mirrors Guide, so the transcript stays tool-free)

1. **Seed** (`run_suggest_start`, the structural twin of `run_draft`,
   `rebuild_engine.py:401-437`). Load curated seed sources deterministically via
   `rebuild_sources` + `_load_sources` (`wiki_build.py:1640`/`:412`) — **no** interactive
   curate wizard (the owner curates conversationally / via regather). Load backlinks (§4).
   Run `writer_core.rebind_entities(conn)` (§2). Set `run.base_body` = current article body,
   `run.base_tokens = clock._TOKEN_RE.findall(base)` (§2). Seed the transcript and stage the
   draft **without an LLM call** — see §1.3 for the *correct* way to do this.
2. **User talks** (`run_suggest_turn`). Append **one** user turn (the steer) to
   `run.messages` — exactly `run_guide`'s `run.messages.append(...)`
   (`rebuild_engine.py:461`).
3. **Generate.** `_generate` streams thinking + content with `tools=[]`, `thinking=True`
   (`:320-321`). The model emits the **FULL revised article**, steered to change only what
   was asked (§5). `stream_turn` appends the assistant turn verbatim (signed thinking
   included) — resume-safe because there is **no tool_use** in the transcript (the invariant
   at `rebuild_engine.py:9-15`).
4. **Hardening tail** = `writer_core.harden_draft` (§2), run on the **whole** new body:
   `enforce_date_tokens` → `_bad_links`/`_repair_citation_titles`/`_neutralize_links` →
   `add_links_to_content` → `validate_structure`. The current open-coded tail
   (`rebuild_engine.py:366-398`) is the byte-identical baseline (§7 characterization).
5. **Stage + emit.** `run.draft` = hardened body, `run.status="ready"`, `done` + a new
   `turn_summary` event (§4).
6. **Loop** from step 2. Transcript grows `[user steer, assistant full draft]` per turn —
   the same shape Guide grows today, so auto-continue and redraft work unchanged.

### 1.3 EXACTLY how we avoid the `_generate` draft-wipe problem

There is **no carry-draft-forward mechanism to break.** Because every turn re-emits the
full article, `_generate`'s `run.draft=""` wipe (`:295`) followed by a full re-stream is
**exactly what we want** — A is the only stance for which the wipe is harmless (RT1 §0.1).
We do **not** extend `_generate` with a seed and we do **not** write a sibling generator;
the turn body is re-generated wholesale every time. BASE preservation is achieved by
**transcript priming + prompt discipline**, not by reusing a prior draft string:

- The transcript is primed so the model treats BASE as the working document. The seed
  appends `[user=SUGGEST_PROMPT(base,…), assistant=BASE_BODY]` to `run.messages`.
- The seed itself runs **no LLM call**: we set `run.draft = run.base_body` and emit a
  synthetic `seeded` event so the panel shows the unchanged article instantly (zero cost).

### 1.4 Fixing the `run_redraft` seed bug (RT1's "A pops the hand-seeded BASE turn")

RT1 (A, MAJOR) and the ground-truth list flag this precisely: `run_redraft`
(`rebuild_engine.py:491-492`) unconditionally pops a trailing `assistant` turn so the model
re-answers the original prompt. If the user's **first** action is "Re-draft with more room"
*before any turn*, `run_redraft` pops the **hand-seeded BASE assistant turn** — silently
discarding BASE and re-asking with no working document. Fix, in `run_redraft`:

```python
# A hand-seeded BASE turn (suggest mode, no LLM generation behind it) must NOT be unwound:
# it is the working document, not a truncated generation. Guard on a per-turn marker.
if run.messages and run.messages[-1].get("role") == "assistant" \
        and not run.messages[-1].get("_seed"):
    run.messages.pop()
```

We tag the seeded assistant turn with a private `_seed: True` key (stripped before it goes
to a provider in `stream_turn`, or simply ignored — providers read only `role`/`content`).
`run_redraft` (and the auto-continue unwind at `:496-501`) skip a `_seed` turn. A
characterization test asserts: seed → immediate redraft does **not** lose BASE (this is the
fail-before/pass-after bug-fix test required by CLAUDE.md DoD). Equivalent guard: only ever
call `run_redraft` after `_generate` has produced ≥1 real turn — but the `_seed` flag is
robust against ordering and is what we ship.

---

## 2. The shared `harden_draft` / `enforce_date_tokens` / `promote_one` core + session-start rebind

Borrowed from **D §1b/§2/§3**, scoped to the live path. Canonical order of ops in
`harden_draft` (RT1 non-negotiable #5: **date-enforce BEFORE add-links**, because
`_mask_spans` does NOT mask `@t[...]` — verified `wiki_build.py:1830-1831`):

```
1. enforce_date_tokens(conn, body, base_tokens=run.base_tokens)   # FIRST — before any link masking
2. _bad_links → _repair_citation_titles(source_titles) → _neutralize_links   # citation/dead-link tail
3. add_links_to_content(conn, title, body)                        # people-link backstop (PII self-guarded)
4. validate_structure(title, body)                                # last — lint reflects what ships
```

Each runs in **`harden_draft`**, called by `_generate`'s tail (so classic rebuild + suggest
both inherit it). `harden_draft(base=None)` for classic rebuild must reproduce today's
open-coded tail **byte-for-byte** (§7 characterization is the gate).

### 2a. `enforce_date_tokens` (Research 03 Option A + Option D; defer B for v1)

- **Option A — malformed-token linter** (HIGH value, ~zero false-positive). Flag any
  `@t\s*[\[{(]`-shaped substring not matched by `clock._TOKEN_RE` (`clock.py:105`) or whose
  date arg fails `clock._to_dt` (`clock.py:127`): `@t{age:…}`, `@t[born:…]`,
  `@t[age:1986]`. Surface as a `lint` finding; in the loop the human fixes it next turn (no
  Accept-block — minimal guard surface, the human is the reviewer).
- **Option D — BASE token-preservation guard** (the loop-specific check). Assert every token
  in `run.base_tokens` still appears verbatim in the new draft. A vanished token (the model
  "tidied" `@t[age:…]` → "40") is the most likely **new** regression the loop introduces;
  emit a `lint` warning naming the lost token.
- **Defer Option B (adjacency auto-rewrite)** for v1 (MEDIUM-risk, needs the round-trip
  guard). The conversational loop lets the user just *say* "make that a live age", which the
  now-strengthened DATES fragment (§5) handles. Honest minimal-surface tradeoff; B is a
  clean follow-up inside the same helper.

**Twin invariant (RT1 #5).** `enforce_date_tokens` only ever *produces* tokens via a `clock`
round-trip — it never touches expansion semantics, so `server/tests/fixtures/time_tokens.json`
and the `clock.expand_tokens` ↔ `web/src/time.ts` byte-for-byte pin (`test_api.py:1209-1211`)
stay untouched. A test asserts the fixture does **not** change.

### 2b. `promote_one` on Accept (Research 02 §6 — fixes live + nightly in one insert)

`writer_core.promote_one(conn, title)` called inside `finalize_rebuild`
(`wiki_build.py:1681-1721`), **after** `entity_index.rebuild` (`:1718`, so owner/alias
bindings are fresh) and **before** the final `flag_dead_links`. It runs the per-article
subset of the build's promotion suite (`actions/wiki_build.yaml` 6b–6e): `link_owner`
(`wiki_build.py:1009`), `surface_aliases` (`:1177`), `link_medications`, `link_places`,
`normalize_link_labels`, `flag_ungrounded_reference` (`:1428`) — already deterministic,
idempotent, cached. `surface_aliases` runs **last** among body-mutators (it owns the AKA
line, Research 03 §5).

**Latency worry is moot (RT1 §0.2).** `finalize_rebuild` *already* runs
`entity_index.rebuild` → the networked `_sync_embeddings` inside the Accept lock today; the
lock already blocks on embeddings, so `promote_one`'s deterministic, cached steps add
negligible cost. **Idempotency (RT1 #6):** Accept can be retried after a 409
(`rebuild.py:371-373`); `surface_aliases`/`_apply_aka_line` rebuild the AKA line each call,
and the other steps are link-only/cached. `test_promote_one.py` asserts run-twice ==
identical body.

### 2c. Cheap session-start entity rebind (RT1 #7, R4 H1 — the biggest correctness win)

`writer_core.rebind_entities(conn)` runs **only the binding half** of `entity_index.rebuild`
— `_link_articles` (`entity_index.py:529-564`) + the `reconcile_owner` owner-alias fold —
**without** the networked `_sync_embeddings` (`entity_index.py:360`). Called once in
`run_suggest_start` (and in `run_draft`, so classic rebuild benefits too). This is needed
because the **live loop reads bindings during the conversation** (the `{known_aliases}`
prompt block + the Pass-2 `add_links_to_content` backstop), and Accept-time `rebuild` is too
late to help the live offering (RT1 §0.2: "Accept-time rebuild is too late to help the live
loop"). PII firewall preserved for free: `_link_articles:553` already excludes private
titles (verified). Confirmation test mirrors `test_owner_alias_backfill.py` (R4 H1 recipe).

---

## 3. The INBOUND PII firewall for v1

RT1 #4 and the ground-truth list are emphatic: **no inbound firewall exists today** —
`hybrid_notes` has no privacy filter (verified `search.py:36`), and the existing firewall
guards only *links/targets* (`add_links_to_content:733`, `_link_articles:553`). A
conversational edit can introduce private *prose/facts/citations* into a public article.

**H1's v1 design closes the hole by construction — there is no inbound private channel:**

1. **No autonomous private search in v1.** The AI reasons over only (a) the curated seed
   sources (already grounding-scoped by the existing rebuild's source selection) and (b)
   read-only **backlink titles** (KB pages only). It has **no tool** to pull arbitrary
   notes. The one way new private notes enter is the **user-gated regather**
   (`run_gather`), where the *owner* explicitly approves the curated set — the same trust
   boundary the classic rebuild already has.
2. **Target-domain output gate (defense in depth, even though no inbound channel exists).**
   When the edited page is **public** (not `is_private_title`, `wiki_guides.py:148`, and not
   `domain_for_title == "Reference"`, `:103`), `harden_draft` runs an **inbound firewall
   pass** over the staged body before `done`:
   - **Citation gate.** Any footnote `[^sN]: [[T]]` whose `T` is a private/Reference title
     is neutralized to plain text (extends `_repair_citation_titles`/`_neutralize_links`,
     `rebuild_engine.py:372-382`). RT1 (C, MAJOR) shows a *real* private link survives onto
     a public page because the dead-link neutralizer only drops *dead* links; a live private
     citation resolves. The gate drops it explicitly. (v1 almost never hits this since
     sources are pre-curated, but the gate is cheap and is the seam the LATER tool layer
     reuses.)
   - **Link gate** is already enforced (`add_links_to_content:733` refuses private targets).
3. **Backlinks are titles only, never bodies, never citeable** (§4). Even a private backlink
   title is **labeled context** ("Linked from"), never woven as a cited fact and never a
   `read` target in v1.

This makes the firewall a **first-class, tested gate keyed on the edited page's domain**
(RT1 #4) — not a Risks-section sentence — and it is the exact seam the LATER firewalled
tool-search bolts onto (§8). Tested in §7 (public target: a private-titled citation in the
model's output is neutralized; private/Reference target: `add_links_to_content` links
nothing).

---

## 4. SSE protocol + RebuildRun changes + backlinks loading

### 4.1 RebuildRun (`rebuild_runs.py:27-49`) — additive, defaulted (classic unaffected)

| Field | Type / default | Meaning |
|---|---|---|
| `kind` | `str = "rebuild"` | `"rebuild"`/`"suggest"`; gates seed + Accept promotion path. |
| `base_body` | `str = ""` | Preserved BASE body; seeds the transcript + the client diff baseline. |
| `backlinks` | `list[dict] = []` | `[{title}]` inbound KB links — READ-ONLY context. |
| `base_tokens` | `list[str] = []` | `@t[...]` tokens in BASE, for the Option-D guard. |

`base_hash` still hashes the **live page** at start (`rebuild_runs.py:35,100`); staleness
keys off the live page, **orthogonal to conversation length** — a 20-turn session is fine
as long as nobody else edits the live page (`rebuild.py:371-373`). `is_live`/`_LIVE` and the
one-run-per-slug registry need **no change**.

### 4.2 SSE — reuse the whole vocabulary; two small additions

Full-article-per-turn means **no `patch`/`edit`/`ops` event** is needed; the draft arrives
as `content_delta`s + `done.draft` exactly as today. Additions:

| Event | Emitted by | Payload | Why |
|---|---|---|---|
| `seeded` (NEW) | `run_suggest_start` | `draft (=BASE), backlinks` | render unchanged article instantly, no LLM call |
| `turn_summary` (NEW) | engine, after `done` | `text` (≤1 sentence) | replaces RebuildPanel's canned ack (`RebuildPanel.tsx:237`) |
| `lint` (reused) | tail | `ok, message` | now also carries date-token / preservation findings |

`turn_summary` is parsed from a fenced `summary` block the model is asked to emit, stripped
server-side exactly as `_extract_talk` strips the `talk` fence (`rebuild_engine.py:366`) —
the parser must be **as defensive** as `_extract_talk` (RT1 A, MINOR: a malformed summary
fence must not leak into the body). **State machine:** no new statuses; reuse
`streaming`→`ready` per turn; Accept from `ready`/`guiding` (`rebuild.py:345`) — we do **not**
widen the shared Accept gate (RT1 E, MAJOR: E's gate-widening is an unnecessary shared-handler
change). The seed transitions straight to `ready` via the synthetic event.

### 4.3 Backlinks loading (read-only)

`wiki_build.backlink_titles(conn, note_id)` wraps the **exact** inbound SQL at
`architect.py:818-822` (verified):

```sql
SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id = l.source_note_id
WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title
```

**Non-negotiable invariant (RT1 #3).** Backlinks are injected into the **prompt** as a
labeled "Linked from (read-only context — do not cite, do not treat as sources)" block, and
are **NEVER** added to `run.sources`. `_repair_citation_titles` keys off `run.sources`
(`rebuild_engine.py:373`); polluting it would retarget backlinks as citation-repair targets
and shift grounding. They are also not added to `run.known` (the cross-link allow-set).
Tested in §7.

---

## 5. Prompt design

Minimal fragment work (not D's sweeping five-fragment substitution system — RT2 D verdict:
the golden-prompt whitespace battles aren't worth it for v1):

1. **`wiki_suggest_seed`** (NEW). Frames BASE as the working document below; **MINIMAL,
   TARGETED** edits per the user's guidance; **PRESERVE** everything not asked to change,
   including **every `@t[...]` token verbatim**; output the **COMPLETE** revised article in
   the same Markdown; end with a fenced `summary` block. **Embeds the DATES & TIME block**
   (copied from `wiki_write`, `prompts.yaml:868-874`) and the **CROSS-LINKS** block
   (`:855-860`) so the rules survive transcript dilution over many turns (RT2 §4 / Research
   02 §4 — Guide revisions are the weakest at honoring rules because the steer carries none
   of the directive blocks). Includes the read-only "Linked from" block (§4).
2. **`wiki_suggest_turn`** (NEW). The per-turn steer (replaces `run_guide`'s bare string,
   `rebuild_engine.py:455-460`): re-states "targeted, preserve the rest, **PRESERVE @t[...]
   tokens**, output the complete article, end with a `summary`" + the user's `{instruction}`.
   Keeping DATES/CROSS-LINK reminders **in the steer itself** is the cheap anti-dilution fix.
3. **Fold the DATES block into `wiki_revise`** (`prompts.yaml:908-932`) — `wiki_revise`
   currently omits DATES, so a self-critique pass can re-freeze a token (Research 02 §3 /
   RT1 surviving-ideas from D). One-block addition; benefits the batch writer too.

We deliberately do **not** build the full `{date_rules}/{crosslink_rules}` substitution
engine — we copy the two blocks (small duplication, low risk). It's a clean follow-up that
doesn't change H1's shape.

---

## 6. Frontend panel + entry point + diff view; the rebuild-vs-revise mental model (RT2 §4)

A **separate `SuggestPanel.tsx`** sharing RebuildPanel's lower-level primitives (the suggest
mode has no gather/curate wizard, so overloading the `Stage`/`Phase` unions at
`RebuildPanel.tsx:15-16` would tangle the state machine). Borrowed primitives: `thread`
state (`:62`), the footer composer + Enter-to-send (`:283-289`), the **stable-`onClose` ref
trick** (`:78-79`) so the Modal focus effect doesn't steal composer focus, `handleDraft`'s
SSE handler (`:117-134`), `sawContent` (`:73`). Optimistic dual-bubble from `Chat.tsx:587`:
push the user bubble on Send, replace the canned ack (`RebuildPanel.tsx:237`) with the
streamed `turn_summary` (RT2 §4 wants this real per-turn summary).

**Diff is the PRIMARY view, default on.** `MarkdownDiff before={note.content_md}
after={draft}` (`RebuildPanel.tsx:431-435,460`). This is the load-bearing answer to T1
"showed me the draft" and to the full-re-emit drift weakness (RT2 §2): the owner reads
BASE-with-the-change, not a wall of re-rendered prose. **This is H1's honest weakness** —
the draft *is* a re-generation, so the diff view is doing the work a deterministic apply
would do for free in B/C/E (RT2 §2). We accept it for v1 and remove it in the LATER edit-ops
layer (§8).

**`api.ts`:** a `SuggestEvent` union (reuses most of `RebuildEvent` + `turn_summary`/
`seeded`) and thin `streamSSE`-based wrappers `suggestStart`/`suggestTurn` (do **not**
hand-roll a reader; `streamSSE`, `api.ts:875-929`, handles abort/stall/health/`\n\n`).
Accept/Reject reuse `acceptRebuild`/`rejectRebuild` (`api.ts:955-959`).

**Entry point:** a second KB-only `NoteActionsMenu` item next to "Rebuild page now"
(`NotePage.tsx:265-267`), same `llm.ready` pre-flight (`:119-124`), mounting `<SuggestPanel>`
near `:379-383`. Refuse protected `kb/_*` pages (`is_protected`, `wiki_guides.py:87`) in the
router.

### 6.1 Rebuild-vs-Revise mental model (RT2 §4, the shared blind spot)

RT2 §4 is right that two buttons whose mental models overlap (rebuild already has a Guide
talk→edit loop) risks confusing the owner. H1's position:

- **Differentiate the copy now.** "Rebuild page now" = *regenerate from sources*; "Suggest
  revisions" = *keep this article, talk to it*. The diff-first SuggestPanel makes "Revise"
  *feel* distinct from rebuild's full re-draft even though both re-emit — because the owner
  sees a quiet diff, not a fresh blank-to-body stream.
- **Plan the convergence, don't ship it in v1.** The endorsed end-state (RT2 §4) is that
  "Suggest revisions" eventually **replaces rebuild's Guide step** — one conversational
  editing surface reachable both after a fresh rebuild ("now talk to it") and from an
  existing page ("revise this"). H1 designs toward this by building `SuggestPanel` on the
  *same* primitives the Guide loop uses, so a later PR can route rebuild's post-draft Guide
  into `SuggestPanel` and delete the duplicate canned-ack loop. **v1 does not** replace the
  Guide step (that would widen the change surface and couple the two flows prematurely); it
  ships the second entry point with intent-revealing copy and the convergence noted as the
  documented next step.

---

## 7. Test plan (per tier, CLAUDE.md Definition of Done) + characterization + the untested guide loop

### 7.1 Characterization (the shared-core safety net) — lands with PR1

`server/tests/test_writer_core_characterization.py` (NEW). Drive `_generate` via the
`_drain` + `FakeProvider` recipe (`test_rebuild_engine.py:36-128`) on a seeded DB + fixed
article/sources; snapshot **current** body + lint + talk as golden, assert **byte-identical**
after `harden_draft` extraction with `base=None` (the only intended diff is the new
date-token *findings*, asserted explicitly). This is the regression gate for borrowing D's
core without D's blast radius — and because we scope to two call sites (not four), the
snapshot surface is small and stable.

### 7.2 Backend integration — `server/tests/test_suggest_engine.py` (`@pytest.mark.integration`)

Copy `test_rebuild_engine.py:36-128`: `_drain`, `FakeProvider`, `_install_provider`
monkeypatching the **`llm` seam** (never the SDK), real SQLite with embeddings no-op'd. Cases:

- **Seed preserves BASE:** `run_suggest_start` stages `run.draft == BASE`, no LLM call;
  `base_tokens` captured; `run.sources` does **not** contain backlink titles (§4 invariant).
- **`run_redraft` seed-guard (the bug-fix test, fails before / passes after):** seed → call
  `run_redraft` immediately → assert BASE is **not** discarded (the `_seed` guard, §1.4).
- **One edit turn:** scripted full edited body; assert `add_links_to_content` re-linked a
  dropped name (R4 O2), `done` + `turn_summary` emitted, transcript ends with the assistant
  draft, **no tool_use blocks** in `run.messages` (RT1 #1 regression test — assert no
  list-content / no `tool_calls` key).
- **Two turns:** second turn resumes the grown transcript without exception (the core
  low-risk claim).
- **Date hardening:** `@t{age:…}` → malformed-token `lint`; a dropped BASE `@t[age:…]` →
  preservation `lint`.
- **People rebind:** create `kb/People/<X>` mid-session → `rebind_entities` → a nickname now
  links; a Reference/private TARGET links nothing (PII firewall).
- **Inbound firewall (§3):** public target, model output contains `[^s1]: [[kb/Health/…]]`
  → neutralized; private/Reference target links nothing.
- **Error paths:** `creds=False`, `fail_on_turn`, `run.cancelled=True` mid-stream.
- **Backlinks SQL:** `backlink_titles` returns inbound titles, excludes deleted/self.

`server/tests/test_promote_one.py` (NEW): `finalize_rebuild` runs `promote_one` → AKA line
present, `link_owner` linked, **idempotent on a second call** (RT1 #6). `test_clock.py`
(extend): `enforce_date_tokens` malformed + preservation, plus assert the
`time_tokens.json` twin fixture is **unchanged** (RT1 #5).

### 7.3 Frontend — `web/src/components/SuggestPanel.test.tsx` (vitest + MSW)

Copy `RebuildPanel.test.tsx:1-53`: mock only `suggestStart`/`suggestTurn` with scriptable
`fakeStream`, accept/reject on MSW, `renderWithProviders` + `server`, `vi.stubGlobal`
confirm/alert, footer scoping to `.modal-foot`. **This fills the gap RT (Research 05 §5a)
flags — the existing conversational/guide loop is currently UNtested** (`guideStream` is
mocked but never scripted). Cases: seed renders BASE + diff-on; a turn streams the draft +
optimistic user bubble + `turn_summary` AI bubble; a **second** turn works; `lint` banner on
a date finding; Accept→`acceptRebuild`, stale 409 → stale state (`RebuildPanel.tsx:220-222`),
Reject→`rejectRebuild`.

### 7.4 e2e + coverage

**One Playwright flow** (`e2e/`, LLM faked at `e2e/fake_llm.py`) — warranted as a new
user-facing flow behind the API contract (DoD #2): open KB page → "Suggest revisions" → send
one instruction → faked edited body → diff shows the change → Accept → page reloads. Exercises
the real `_sse` bridge + Accept/lock the integration tests bypass.

**Coverage floors.** New `writer_core.py` + the engine functions + the panel arrive with
their tests in the **same PR** (no `fail_under`/`thresholds` regression — DoD #3); when real
coverage lands comfortably above, **ratchet the floor up** in the same PR. Never lower a
floor. Google-style docstrings on every new symbol; `ruff check app`.

---

## 8. The explicit LATER layers (how they bolt on without rework)

H1 is honest that v1 under-delivers on active truth-seeking. Two additive layers, sequenced
**after** v1 ships, each behind the **same panel** with **no re-architecture** of
Accept/lock/staleness/transcript:

### 8a. LATER-1 — B's PURE EXACT-MATCH-ONLY `edit_ops` (cost + clean-diff optimization)

Add `server/app/services/edit_ops.py` — a pure, exhaustively-unit-tested module (RT1's "keep
the exact op; drop the fuzzy fallback"). **Hard constraints from RT1:**

- **Exact + uniqueness-preserving match ONLY. NO whitespace/fuzzy fallback** (RT1 B, MAJOR:
  a `fuzzy:true` silent mis-apply is the exact corruption B claims to avoid; RT1 fatal-flaws:
  drop it).
- **`section` op masks code fences before splitting** (RT1 §0.4 / B+E CRITICAL: `_SECTION_RE`
  matches `## ` inside ```fences; reuse `_mask_spans`, `wiki_build.py:1817`, before any
  splice).
- **Delete+insert pairs are atomic or surfaced loudly** (RT1 B, MAJOR: a delete whose paired
  insert failed is semantic corruption the linter can't catch).

It bolts on cleanly because: (1) the model still emits ops in **assistant text** (a fenced
```json block), so `run.messages` stays tool-free — the **same transcript invariant** v1
relies on (RT1 #1); (2) ops apply to a carried-forward `working_draft` field, then run
through the **exact same `harden_draft`** v1 already ships (RT1 #2: re-validate on the full
article every turn); (3) **full re-emit (v1's `_generate` path) becomes the fallback** when
an op fails to place — so a failed turn is never inert (the answer to RT2's B "inert turn"
risk). The panel gains an `ops` event + diff-from-server-spans; everything else is reused.

### 8b. LATER-2 — firewalled tool-search (C's truth-seeking, made safe)

Add the agentic search **only** behind C's **two-transcript discipline** (RT1's surviving
idea from C): tool_use lives in a **disposable, thinking-OFF** sub-transcript that is
discarded; the persisted `run.messages` gets only plain `{role,content}` turns (RT1 #1). It
bolts on because v1 **already shipped the inbound firewall as a domain-keyed gate** (§3) —
LATER-2 simply extends the same gate to the new tools: `search_notes` filters private notes
(today it strips only `kb/`, `rebuild_engine.py:178`); `read_source`/`read_backlink` refuse
private targets when the edited page is public; citations to private targets are already
neutralized by the §3 citation gate. Run the tool loop on `llm.model_for("cheap")`, **not**
the synthesis model (RT1 C, MAJOR: C misread the gather precedent's cheap-model choice).
Ship the C-style `assert run.messages has no block-content/no tool_calls` regression suite.

Because v1 already built (a) the firewall seam, (b) the cheap session-start rebind, (c) the
shared `harden_draft`, and (d) the panel, **neither layer requires reworking v1** — they are
strictly additive, matching RT2's "highest ceiling, solid floor" sequencing and C's own
"tool-less core first, tools additive" pattern.

---

## 9. How H1 satisfies ALL SEVEN of RT1's non-negotiables

1. **Transcript stays tool-free** (or tools only in a disposable thinking-OFF sub-transcript).
   ✔ v1 uses `tools=[]` + thinking on, identical to Guide (`rebuild_engine.py:320-321`);
   `run.messages` carries only plain user/assistant turns. The seeded BASE turn is plain
   text. LATER-2's tools live in a disposable thinking-OFF transcript (§8b). The
   "no block-content / no `tool_calls` in `run.messages`" regression test ships in v1 (§7.2).
2. **Re-validate on the FULL article every turn.** ✔ `harden_draft` runs on the whole
   re-emitted body every turn (§2); citations/markers↔defs, lead, AKA, PII firewall,
   dead-links, dates, people-links all run on the full string — never a section/patch in
   isolation. (LATER-1's `working_draft` is also hardened whole, §8a.)
3. **Backlinks read-only, NEVER in `run.sources`.** ✔ §4.3 — injected as labeled prompt
   context only; `source_titles` derives solely from `run.sources`; tested (§7.2).
4. **PII firewall on the way IN.** ✔ §3 — v1 has **no autonomous inbound channel** (no tool
   search; new notes only via user-gated regather) **and** a domain-keyed inbound gate that
   neutralizes private/Reference citations on a public target. This is the seam LATER-2's
   tools extend.
5. **Date-enforce BEFORE add-links; tokens via `clock` round-trip only.** ✔ §2 order of ops
   step 1 precedes step 3; `_mask_spans` not masking `@t[...]` (`wiki_build.py:1830-1831`) is
   the reason. `enforce_date_tokens` only *produces* tokens; `time_tokens.json` twin pin
   untouched and asserted (§7.2).
6. **`promote_one` idempotent on the Accept path.** ✔ §2b — placed in `finalize_rebuild`
   (live + nightly inherit); `surface_aliases`/`_apply_aka_line` rebuild the AKA line each
   call; run-twice == identical body test (§7.2). Accept-retry-after-409 safe.
7. **Cheap entity rebind at session start (no `_sync_embeddings`).** ✔ §2c —
   `rebind_entities` = `_link_articles` + owner-alias fold only; reuses the private-safe
   `_link_articles:553`; called in `run_suggest_start` and `run_draft`.

Plus the three RT1 ground-truth corrections honored explicitly: `_generate` draft-wipe is
*harmless* for full re-emit (§1.3, no carry-forward); the `run_redraft` BASE-pop bug is fixed
with a `_seed` guard (§1.4); the date-token ordering is enforced before linking (§2).

---

## 10. Sequencing / effort (PR breakdown; estimates discounted per RT2 §5: "30–50% optimistic")

| PR | Scope | Risk | Honest est. (DoD-loaded) |
|---|---|---|---|
| **PR1 — Shared hardening, loop-agnostic.** `writer_core.harden_draft` extraction (2 call sites only) + characterization net; `enforce_date_tokens` (A+D); `rebind_entities` (called from `run_draft` too); `promote_one` in `finalize_rebuild`; fold DATES into `wiki_revise`. **Fixes the owner's date + people-link + promotion bugs on the EXISTING rebuild in week one.** | Low–Med | 2.5–3.5 d |
| **PR2 — Suggest engine + endpoints.** `RebuildRun` fields; `backlink_titles`; `run_suggest_start`/`run_suggest_turn`; `run_redraft` `_seed` guard; inbound firewall gate; `wiki_suggest_*` prompts; router + `_sse` reuse; full `test_suggest_engine.py`. | Med | 3–4 d |
| **PR3 — Frontend + e2e + ratchet.** `SuggestEvent` + `suggestStart`/`suggestTurn`; `SuggestPanel.tsx` (diff-first); NotePage entry; `SuggestPanel.test.tsx` (fills the untested-guide-loop gap); one Playwright flow; coverage ratchet. | Med | 3–4 d |

**Total ~1.5–2 weeks** for v1 (RT2 priced A at ~1 week and called it "optimistic on the e2e
+ coverage-ratchet PR3"; H1 adds the firewall gate + the characterization net, so the honest
number is higher). **LATER-1 (edit-ops)** ≈ +1 week; **LATER-2 (firewalled tools)** ≈ +1
week — each independently shippable, neither reworking v1.

**PR1 is the single highest-leverage land** and ships even if the new mode slips — exactly
RT2 §1's verdict that the bug-fix is the most valuable, nearly plan-independent deliverable,
and should NOT be gated behind a writer-core refactor (D) or a patch engine (B).

---

## Appendix — verified citations

Code verified on 2026-06-08: `rebuild_engine.py:9-15` (transcript invariant), `:271-398`
(`_generate` + tail), `:295` (draft wipe), `:320-321` (`tools=[]`, thinking), `:366-398`
(open-coded tail = `harden_draft` baseline), `:401-437` (`run_draft`), `:440-463`
(`run_guide`), `:466-503` (`run_redraft`; `:491-492` the BASE-pop bug); `wiki_build.py:711`
(`add_links_to_content`, `:733` PII self-guard), `:1640`/`:412` (source loading),
`:1681-1721` (`finalize_rebuild`, `:1718` `entity_index.rebuild`), `:1817-1833` (`_mask_spans`
— NO `@t[...]` masking), `:1009`/`:1177`/`:1428` (promotion fns); `entity_index.py:529-564`
(`_link_articles`, `:553` private exclusion), `:360` (`_sync_embeddings` networked);
`search.py:36` (`hybrid_notes` — no privacy filter); `architect.py:818-822` (backlinks SQL);
`clock.py:105` (`_TOKEN_RE`), `:127` (`_to_dt`); `wiki_guides.py:87` (`is_protected`), `:103`
(`domain_for_title`), `:148` (`is_private_title`); `rebuild.py:57` (`_sse`), `:345` (Accept
gate `ready`/`guiding`), `:371-373` (staleness 409); `prompts.yaml:855-860` (CROSS-LINKS),
`:868-874` (DATES & TIME), `:908-932` (`wiki_revise`); `RebuildPanel.tsx:15-16,62,78-79,
117-134,220-222,237,283-289,431-460`; `api.ts:875-929,955-959`; `NotePage.tsx:119-124,
265-267,379-383`; `Chat.tsx:587`; `test_rebuild_engine.py:36-128`; `RebuildPanel.test.tsx:1-53`;
`test_api.py:1209-1211` (twin pin).
