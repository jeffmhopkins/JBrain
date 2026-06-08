# Plan D — Shared writer-core refactor FIRST, thin edit-loop on top

**Stance:** The folded-in hardening (deterministic date tokens, people-link fix, promotion/formatting
PARITY) is the **centerpiece**, not an afterthought. Research 02 found **four** divergent writer paths
(build / nightly-rebuild / live-rebuild-guide / legacy-synthesize) with asymmetric people-linking, **no**
shared date-token enforcement, and a large **promotion asymmetry**. Plan D fixes those bugs *for all
paths at once* by extracting a shared **writer core**, then builds the new conversational "Suggest
revisions" loop as a **thin consumer** of that core, inheriting correctness by construction.

This is honestly the riskiest sequencing (it front-loads a multi-call-site refactor before the user sees
the feature), but it is the only stance where the rebuild date/people/promotion bugs get fixed once
rather than re-implemented per path. I argue for it candidly, with regression-safety as a first-class
deliverable (§7 characterization tests, §8 incremental PRs).

---

## 0. The four paths and what each currently does (the problem, in citations)

| Path | Entry | Date enforce | `add_links_to_content` | Promotion suite |
|---|---|---|---|---|
| Full build | `write_one` `wiki_build.py:821` | prompt-only + advisory warn | YES `wiki_build.py:937` | FULL via `actions/wiki_build.yaml:79-110` (steps 6b–6e) |
| Nightly rebuild | `rebuild_article` `wiki_build.py:1723` → `finalize_rebuild` `:1681` | prompt-only | YES (calls `write_one`) | NONE beyond `finalize_rebuild` (`entity_index.rebuild`, disambig, dead-links) `wiki_build.py:1718-1720` |
| Live rebuild/guide | `rebuild_engine._generate` `:271`; Accept `rebuild.py:374` | prompt-only, **no revise loop** | YES `rebuild_engine.py:387` | NONE beyond `finalize_rebuild` `rebuild.py:374` |
| Maintain | `maintain_one` `wiki_build.py:1256` | prompt-only + revise-on-errors | **NO** (missing — Research 02 §1) | PARTIAL via `actions/wiki_maintain.yaml` |

Three concrete defects Plan D closes for the whole family:
1. **No deterministic `@t[...]` enforcement anywhere** (Research 03 §2): only an advisory warning at
   `wiki_guides.py:318-320` via `_REL_TIME_RE` `:47`; live path never even revises on it.
2. **People under-link on rebuild** because the entity index is **stale** at session start — nothing
   calls `entity_index.rebuild` in `rebuild_engine.py` (Research 04 H1). `maintain_one` additionally
   skips `add_links_to_content` entirely.
3. **Promotion asymmetry**: `link_owner`/`surface_aliases`/`link_medications`/`link_places`/
   `normalize_link_labels`/`flag_ungrounded_reference` run on full build only
   (`actions/wiki_build.yaml:82-108`); live Accept and nightly rebuild run none (Research 02 §6).

---

## 1. The shared writer-core module(s): exact API, what moves, which call sites change

### 1a. New module `server/app/services/writer_core.py`

A thin, dependency-light module that the existing writer modules import. It owns three concerns the
four paths currently re-implement or skip. Everything here is **pure / no LLM** except where noted; it
reuses existing helpers in `wiki_build.py` rather than re-coding them (to avoid behavior drift).

```python
# writer_core.py  — Google-style docstrings on every symbol per CLAUDE.md.

def harden_draft(conn, title, draft, *, known, source_titles, talk=None,
                 base=None) -> HardenResult:
    """Run the deterministic post-draft hardening pipeline on one article body.

    Single source of truth for the tail every writer path runs after the LLM produces
    a body: date-token enforcement, citation-typo repair, dead-link neutralize, the
    add_links_to_content people-link backstop, and (loop only) the BASE token-preservation
    guard. Returns the hardened body plus any talk notes and lint findings.
    """
    # returns HardenResult(body, talk, lint)  — lint = {ok,errors,warnings,stub,date_errors}

def enforce_date_tokens(conn, body, *, source_titles=None, base=None) -> DateResult:
    """Deterministically repair/flag @t[...] date tokens (Research 03 options A+B+D)."""

def promote_one(conn, title) -> dict:
    """Run the per-article promotion suite on a freshly-saved article (idempotent)."""

def rebind_entities(conn) -> dict:
    """Cheap, no-embeddings entity→article rebind so people-links use fresh bindings (Research 04 O1)."""
```

`HardenResult`/`DateResult` are small dataclasses (one-line class docstrings + `Attributes:` per CLAUDE.md).

**Why a new module and not just more functions in `wiki_build.py`?** `wiki_build.py` is already ~3000
lines and is imported by `rebuild_engine` lazily (`rebuild_engine.py:290` `from . import wiki_build`).
A focused `writer_core` (a) gives the characterization tests a clear unit boundary, (b) lets
`rebuild_engine`, `wiki_build.write_one/maintain_one`, and `rebuild.py` Accept all import the *same*
tail without circular-import gymnastics, and (c) keeps the diff reviewable. `writer_core` imports
`wiki_build`'s existing primitives (`_bad_links`, `_repair_citation_titles`, `_neutralize_links`,
`add_links_to_content`) — it does not move them, so their existing tests stay green.

### 1b. `harden_draft` — exactly the tail `_generate` already runs, factored out

Today `rebuild_engine._generate:366-398` and `write_one:894-940` each open-code the same sequence in
slightly different order. `harden_draft` becomes the canonical implementation; both callers delete
their inline copy and call it. The sequence (order of ops in §2):

```
draft = enforce_date_tokens(conn, draft, source_titles, base).body      # NEW (Research 03)
bad = wiki_build._bad_links(conn, draft, known)
draft, bad = wiki_build._repair_citation_titles(draft, bad, source_titles)
draft, dropped = _neutralize_and_note(draft, bad)                       # wiki_build._neutralize_links + talk notes
draft, _ = wiki_build.add_links_to_content(conn, title, draft)         # people-link backstop, PII-self-guarded
lint = wiki_guides.validate_structure(title, draft)
lint["date_errors"] = enforce_date_tokens(...).errors                  # malformed-token errors surfaced
```

### 1c. Call-site changes (before/after for the live path)

**BEFORE — `rebuild_engine._generate` `:366-398`** (open-coded tail):
```python
draft, talk = wiki_build._extract_talk(wiki_build._clean_wrapper_fence(wiki_build._strip_fence(raw)))
allowed = set(run.known) | {run.title}
bad = wiki_build._bad_links(conn, draft, allowed)
if bad:
    source_titles = [s.get("title") for s in (run.sources or []) if s.get("title")]
    draft, bad = wiki_build._repair_citation_titles(draft, bad, source_titles)
if bad:
    draft = wiki_build._neutralize_links(draft, set(bad)); talk += [...]
    yield {"type": "lint", ...}
draft, _added = wiki_build.add_links_to_content(conn, run.title, draft)
v = wiki_guides.validate_structure(run.title, draft)
run.draft = draft; run.talk = talk; run.status = "ready"
yield {"type": "done", "draft": draft, "truncated": truncated, "lint": {...}}
```

**AFTER — `_generate` calls the shared core** (one call replaces the tail; new BASE-preservation guard
threaded for the loop):
```python
draft, talk = wiki_build._extract_talk(wiki_build._clean_wrapper_fence(wiki_build._strip_fence(raw)))
source_titles = [s.get("title") for s in (run.sources or []) if s.get("title")]
res = writer_core.harden_draft(conn, run.title, draft,
                               known=set(run.known) | {run.title},
                               source_titles=source_titles, talk=talk, base=run.base)  # run.base=None for classic rebuild
draft, talk, lint = res.body, res.talk, res.lint
for msg in res.lint_events:                       # dead-link/date cleanup notices → SSE lint events
    yield {"type": "lint", "ok": msg.ok, "message": msg.text}
if truncated:
    yield {"type": "lint", "ok": False, "message": "The draft was cut off ..."}
run.draft = draft; run.talk = talk; run.status = "ready"
yield {"type": "done", "draft": draft, "truncated": truncated,
       "lint": {"ok": lint["ok"] and not truncated, "errors": lint["errors"],
                "warnings": lint["warnings"], "stub": lint["stub"]}}
```

The **regression-safety contract**: for `base=None` (classic rebuild) and no malformed/frozen tokens,
`harden_draft` must produce a byte-identical body and the same lint dict as the current open-coded tail.
This is the first characterization test (§7).

**`write_one` `:894-940`** changes similarly: lines 894–937 (the citation-repair → neutralize →
`add_links_to_content` block, **but not** the `validate_structure` + bounded revise loop at `:897-925`,
which is path-specific and stays in `write_one`) are replaced by a `harden_draft` call. Subtlety:
`write_one`'s revise loop runs `validate_structure` *between* passes (`:919`), so `write_one` keeps that
loop and calls `harden_draft` **once at the end** for the link/date tail (matching today's `:927-937`
order where the link backstop runs after the loop). The revise loop's interior dead-link handling stays
as-is to avoid changing convergence behavior.

**`maintain_one` `:1356-1404`** gains a `harden_draft` call (currently it only runs `_bad_links` +
`_neutralize_links` at `:1362-1364`, and **never** `add_links_to_content`). Inserting `harden_draft`
after the revise loop (after `:1393`, before the return `:1402`) fixes Research 02 §1 (maintain's missing
people-link backstop) **and** gives maintain date-token enforcement — both for free.

**`rebuild_engine.run_draft` `:435` / `run_guide` `:461`**: unchanged in mechanism; they already funnel
through `_generate`, so they inherit `harden_draft` automatically. `run_draft` additionally calls
`writer_core.rebind_entities(conn)` once at session start (§2c, Research 04 H1 fix) before
`build_write_prompt`.

**`finalize_rebuild` `:1681-1721`**: gains a `writer_core.promote_one(conn, new_title)` call after
`entity_index.rebuild` (`:1718`) — see §3. This single insertion gives **both** nightly rebuild and live
Accept the full promotion suite.

**`actions/wiki_build.yaml:79-110`**: the steps 6b2–6e (`link_owner`, `surface_aliases`,
`link_medications`, `link_places`, `normalize_link_labels`, `flag_ungrounded_reference`) become a single
new step `promote_articles` (a `_p_promote_articles` primitive wrapping `promote_one` over all live kb
titles) — **or**, to minimize churn and keep `flows`-tier validation simple, leave the yaml steps as-is
and have them and `promote_one` call the **same** underlying service functions (they already do — see
§3). I recommend the **leave-yaml-as-is** option: `promote_one` is defined as the per-article
composition of the *existing* primitives' service functions, so the build yaml and `promote_one` cannot
drift. No yaml change ships in the core PR; the yaml is only touched (optionally) in a later cleanup PR.

---

## 2. The deterministic hardening pipeline: design + order of operations

### 2a. `enforce_date_tokens` (Research 03 — bundle A + B-with-round-trip + D)

A single deterministic helper, living next to `clock.py`'s `_TOKEN_RE` (`clock.py:105`) and reusing
`wiki_guides._REL_TIME_RE` (`wiki_guides.py:47`). Three sub-checks, in this order:

- **A — Malformed-token error (HIGH value, ~zero false-positive).** Scan for the permissive shape
  `@t\s*[\[{(]` whose hits are **not** matched by `clock._TOKEN_RE`, or whose date arg fails
  `clock._to_dt` (`clock.py:127`). Examples caught: `@t{age:…}`, `@t[born:…]`, `@t[age:1986]`
  (no month/day). These are unambiguous defects → emit as a **lint error** (`date_errors`), surfaced in
  the SSE `lint`/`done` payload. *Auto-fix is not attempted* (we can't know intent); the user steers.
- **B — Adjacency auto-rewrite with round-trip guard (HIGH value, LOW risk with the guard).** Detect an
  age/elapsed **literal adjacent to an ISO date** ("40 (born 1986-03-15)", "born 1986-03-15 … 40 years
  old") and rewrite to `@t[age:1986-03-15]` **only when** `clock.expand_tokens("@t[age:DATE]")` reproduces
  the exact literal the model wrote (the round-trip guard eliminates "replace 40 with a token that renders
  41"). When the round-trip fails, **downgrade to a warning** (no rewrite). The anchor date can come from
  the same sentence or from `source_titles`' note bodies (already loaded by `_load_sources`).
- **D — BASE token-preservation guard (loop-only; `base` arg present).** Compute
  `set(clock._TOKEN_RE.findall(base))`; assert each token still appears verbatim in the edited draft.
  A token that vanished across an edit (the model "tidied" `@t[age:…]` into a number) is the most likely
  *new* regression the conversational loop introduces → emit a warning (and, if the dropped token's
  surrounding fact is still present, attempt re-insertion). For classic rebuild (`base=None`) this
  sub-check is skipped, preserving today's behavior.

The frozen-literal warning (`_REL_TIME_RE`, today `wiki_guides.py:318-320`) is **kept as a warning** in
`validate_structure` (unchanged) **and** additionally drives B's adjacency search. Do **not** promote it
to an error globally (false positives on genuine historical literals — "scored 40 points"). Research 03
Option C (a revise loop on the warning) is **not** added to the live/loop path — the human is the
reviewer there; B's deterministic rewrite + the warning are sufficient.

**Twin invariant:** `enforce_date_tokens` only ever *produces* `@t[...]` tokens via `clock` round-trip;
it never changes expansion semantics, so `server/tests/fixtures/time_tokens.json` and the
`clock.expand_tokens` ↔ `time.ts:expandTimeTokens` byte-for-byte pin (`test_api.py:1209-1211`) are
untouched (Research 03 §1d / §6).

### 2b. `add_links_to_content` unification (incl. fixing `maintain_one`)

`harden_draft` always calls `wiki_build.add_links_to_content(conn, title, draft)` (`wiki_build.py:711`,
PII-self-guarded at `:733`). Because `maintain_one` now routes through `harden_draft`, it gains the
backstop it lacked (Research 02 §1). No change to `add_links_to_content` itself — only its **call sites**
unify. All seven invariants of Research 04 §5 (never link inside/ to private targets; no nesting; one
link per target; etc.) are preserved because the *same* function runs everywhere.

### 2c. Entity-rebind fix (Research 04 H1 / O1) — `rebind_entities`

A **cheap, no-embeddings** rebind that materializes `entities.article_title` and the owner-alias fold,
without the networked `_sync_embeddings` cost. Implementation: factor the binding part of
`entity_index.rebuild` (`entity_index.py:263`) — specifically `_link_articles` (`:529`) plus a
`reconcile_owner` fold (`wiki_build.py:1074`, whose seeded aliases materialize "one rebuild later" per its
docstring `:1080-1081`) — into a `rebind_entities(conn)` entry point that runs **only** those steps.
Called by `run_draft` (and the new `suggest` start) at session start, and after any in-session
promote/rename. Risk LOW: it only writes `entities.article_title` + `entity_aliases`, no article writes;
the PII firewall is intact because `_link_articles` already excludes private titles (`entity_index.py:553`)
and `alias_surface` drop-rule (v) still applies (Research 04 O5).

### 2d. Order of operations (canonical, in `harden_draft`)

1. `enforce_date_tokens` (A error-flag, B adjacency-rewrite, D base-preserve) — **first**, so date
   rewrites happen before link masking sees the body.
2. `_bad_links` → `_repair_citation_titles` → `_neutralize_links` (+ talk notes) — citation/dead-link
   tail, exactly today's `rebuild_engine.py:368-382`.
3. `add_links_to_content` — people-link backstop **after** dead-link neutralize (matches today's
   `:387` and `write_one:937`).
4. `validate_structure` — last, on the fully hardened body (so lint reflects what ships).

`rebind_entities` runs **before** drafting (session start), not inside `harden_draft`, because it mutates
DB bindings that `add_links_to_content` reads.

---

## 3. `promote_one`: bundled steps, idempotent, parity with build 6b–6e

`promote_one(conn, title)` is the per-article composition of the build's post-write promotion suite. Each
underlying function is **already deterministic and idempotent** (they were designed to run KB-wide,
repeatedly, in the nightly pipeline). `promote_one` simply runs the article-scoped subset:

| Build step (`actions/wiki_build.yaml`) | Service fn | Scope in `promote_one` | Idempotency note |
|---|---|---|---|
| 6b2 `link_owner` (`:82`) | `wiki_build.link_owner` `:1009` | run only if `title` is a People page or the owner page | UPDATEs `people.note_slug`; safe to repeat |
| 6b2a `surface_aliases` (`:85`) | `wiki_build.surface_aliases` `:1177` | the AKA line for `title` (factor a `surface_aliases_one(conn, title)`) | `_apply_aka_line` `:1137` rebuilds layout each call — idempotent by design |
| 6b3 `link_medications` (`:88`) | `medref.link_medications` | only if `domain_for_title(title)` is medication-ish | link-only, cached |
| 6b4 `link_places` (`:91`) | `places.link_places` | only if `kb/Places/` title | link-only, back-link de-dupes |
| 6d3 `normalize_link_labels` (`:105`) | `entity_index.normalize_link_labels` | the `title` body | label-only cleanup, idempotent |
| 6e `flag_ungrounded_reference` (`:108`) | `wiki_build.flag_ungrounded_reference` `:1428` | only Reference titles | flags a talk todo; de-dupes open items |

**Scoping the KB-wide functions to one article:** several (`surface_aliases`, `link_medications`,
`link_places`, `normalize_link_labels`) currently iterate all entities/articles. Two safe options:
(a) add a `title`/`entity_id` filter param to each (small, well-tested change), or (b) call them KB-wide
inside `promote_one` — correct but does more work. **Recommend (a)** for the People-page-heavy hot path
(`surface_aliases_one`, `link_medications` already resolves per-article), and (b) for the cheap label/
grounding sweeps. Either way the *logic* is the existing tested function, so build/Accept can't drift.

**Where `promote_one` is called:** inside `finalize_rebuild` (`wiki_build.py:1718`, right after
`entity_index.rebuild`). This single insertion gives **live Accept AND nightly rebuild** the full
promotion suite — closing Research 02 §6's "big one" for the whole rebuild family, not just the new mode.
It runs **under the KB write lock** (Accept already holds it, `rebuild.py:360`; nightly rebuild holds it
too) and before `conn.commit()` (`rebuild.py:376`). It must be **fully deterministic / no network on the
request path**; `link_medications`/`link_places` already cache and are link-only, but to keep Accept
snappy we run their network-touching parts only when a cache miss occurs, and on a miss fall back to a
talk-todo (their existing behavior) rather than blocking. (If even that is too slow for the request path,
gate medref/places behind a post-commit background task — but the deterministic owner/alias/label/
grounding steps stay inline.)

---

## 4. The thin suggest-revisions loop on top

Because the core does the heavy lifting, the new mode is a **small** addition. It reuses the entire
RebuildRun lifecycle, `_sse` bridge, accept/staleness/lock, and `finalize_rebuild` (Research 01 §10).

### 4a. RebuildRun changes (`rebuild_runs.py:27-49`)
Add two fields:
- `kind: str = "rebuild"` — `"rebuild"` (classic) or `"suggest"` (new). One field, no new registry.
- `base: str | None = None` — the **preserved BASE article body** for the loop (seed = current article),
  threaded into `harden_draft(..., base=run.base)` for the D-guard (§2a). For classic rebuild it stays
  `None`, so classic behavior is unchanged.

`base_hash`, staleness, Accept, one-run-per-slug, TTL all key off the live page and need **no** change
(Research 01 §8, §10).

### 4b. Backlinks loading (read-only CONTEXT)
Reuse the exact inbound-link SQL at `architect.py:818-822`
(`SELECT DISTINCT n.title FROM links l JOIN notes n ON n.id = l.source_note_id WHERE l.target_note_id = ?
AND n.deleted_at IS NULL ORDER BY n.title`). Add a tiny helper `wiki_build.backlink_titles(conn, title)`
returning the inbound article titles, loaded once at suggest-session start and injected into the suggest
prompt as **read-only context** ("ARTICLES THAT LINK HERE — context only, do not treat as sources").
Critical guardrail (Research 01 §10 risk): backlinks are **NOT** added to `run.sources`, so they never
become `_repair_citation_titles` targets or shift grounding.

### 4c. Engine changes (`rebuild_engine.py`) — minimal
- New `run_suggest_start(run, source_ids)` — like `run_draft` (`:401`) but: (1) seeds `run.base =
  current article body`; (2) calls `writer_core.rebind_entities(conn)`; (3) builds the **suggest prompt**
  (§5) instead of `build_write_prompt`; (4) seeds the transcript with the current article as the working
  draft. Reuses `_generate` unchanged (so it inherits `harden_draft`).
- New `run_suggest_turn(run, instruction)` — like `run_guide` (`:440`) but appends the **structured
  suggest steer** (§5 `{talk_rules}`-bearing, BASE-preserving "make TARGETED edits, output the COMPLETE
  revised article") rather than the bare steer at `:455-461`. **Decision (Research 01 §7 risk):** the
  loop uses the **simplest viable approach** — full-article output, no tools, thinking allowed — exactly
  like `run_guide` today. This keeps the transcript tool-less (the documented safe-resume invariant,
  `rebuild_engine.py:9-15`), avoids reintroducing the thinking+tool_use fragility, and means the only new
  divergence from `run_guide` is the *prompt text* and the BASE seed. We accept the cost/latency of
  full-article re-emit per turn (the core's hardening makes each emit correct), trading it for
  implementation simplicity and inherited safety. (A future patch can switch to emit-changed-sections; the
  D-guard already protects against drift.)

### 4d. SSE additions (`rebuild.py`)
- New endpoints `POST /api/kb/suggest/start`, `/{run_id}/turn`, `/{run_id}/accept`, `/{run_id}/reject` —
  thin wrappers over the engine generators, reusing `_sse` (`rebuild.py:57-111`) verbatim.
  Accept/Reject can literally reuse the classic `accept`/`reject` handlers (they're kind-agnostic; they
  call `finalize_rebuild` which now also promotes). The only genuinely new server code is the
  start/turn generators.
- **No new event types are required** for the simplest-viable loop: the existing
  `content_delta`/`thinking_delta`/`lint`/`done`/`error` vocabulary (Research 01 §4) covers full-article
  re-emit. (Research 01 §10's "patch/edit event" is only needed for the emit-changed-sections variant we
  deferred.) One optional addition: a `turn_summary` event carrying a one-line "what I changed" string so
  the chat reads as a real exchange (replaces the canned ack — Research 05 §1) — but this is cosmetic and
  can ship in the frontend PR.

---

## 5. Prompt refactor: shared fragments, recomposed without behavior change

### 5a. Extract five fragments into `prompts.yaml`
Today the directive blocks are **inlined** in `wiki_write` (`prompts.yaml:846-904`). Factor them into
named fragments and reference them by placeholder, **with byte-identical text** so existing paths don't
change:

| Fragment | Source today | Used by |
|---|---|---|
| `{grounding_rules}` | `wiki_write:846-853` | write, suggest |
| `{crosslink_rules}` | `wiki_write:855-860` + `:898-904` (EXISTING ARTICLES + KNOWN ALIASES) | write, revise, maintain, suggest |
| `{author_rules}` | `wiki_write:862-866` | write, maintain, suggest |
| `{date_rules}` | `wiki_write:868-874` | write, **revise (NEW — fixes Research 02 §3)**, maintain, suggest |
| `{talk_rules}` | `wiki_write:876-887` | write, suggest |

Mechanism: add a `_fragments` map to the prompt loader (or a tiny `prompts.compose()` that does the
`.replace()` of `{fragment}` placeholders *before* the per-call `.replace()`). `build_write_prompt`
(`wiki_build.py:784`) and the inline `write_one` assembly (`:867-873`) are recomposed to fill the new
placeholders — but because the substituted text is the *same bytes*, the rendered prompt is identical.

**Regression-safety for the recomposition:** a golden-prompt characterization test (§7) snapshots the
rendered `wiki_write`/`wiki_revise`/`wiki_maintain` for a fixed article/source set **before** the refactor
and asserts byte-equality **after** — for every existing path. The **only intended diff** is `wiki_revise`
gaining `{date_rules}` (the fix for Research 02 §3 — revise no longer re-freezes a tokenized date); that
one diff is asserted explicitly and the golden updated in the same PR with a comment.

### 5b. The new `suggest` prompt
Composed from `{grounding_rules}` + `{crosslink_rules}` + `{author_rules}` + `{date_rules}` +
`{talk_rules}` + the **BASE article** (preserved) + **read-only backlinks** + the curated sources. The
steer for `run_suggest_turn` carries the same fragments inline (so multi-turn dilution of the original
directives — Research 02 §4 — can't happen the way it does for `run_guide`'s bare steer). It also reuses
`scoped_known_titles` + `known_aliases_block` (`wiki_build.py:808-810`) for fresh cross-link candidates,
not a frozen `run.known` (Research 02 §5 / Research 04 O-notes).

---

## 6. Frontend: panel + entry point (lean — the core is the focus)

Keep the frontend deliberately thin (Research 05). Reuse, don't rebuild.

- **Entry point:** a second KB-only `NoteActionsMenu` item **"Suggest revisions"** beside "Rebuild page
  now" (`NotePage.tsx:265-267`), same `rebuildNow`-style `llm.ready` pre-flight (`:119-124`), mounting a
  panel near `:379-383`. No new owner-gating (the PWA is the owner's app — Research 05 §3).
- **Panel:** a `SuggestPanel.tsx` that is **RebuildPanel with the gather/curate wizard removed** and the
  guide loop promoted to the primary surface (Research 05 §1, §4). Reuse: the `thread` state
  (`RebuildPanel.tsx:62`), the footer composer + stable-`onClose` ref trick (`:78-79`, `:283-289`),
  `handleDraft` (`:117-134`) for the streamed draft, `MarkdownDiff before={note.content_md} after={draft}`
  (`:431-435`) against the preserved BASE. The AI bubble becomes a real per-turn summary (the optional
  `turn_summary` event, §4d) instead of the canned ack at `:237`; borrow Chat's optimistic user-bubble
  pattern (`Chat.tsx:587`).
- **API client:** add a `SuggestEvent` union + thin `streamSSE`-based wrappers (`suggestStart`,
  `suggestTurn`, `suggestAccept`, `suggestReject`) mirroring `guideStream` (`api.ts:931-948`); do not
  hand-roll a reader (Research 05 §2).

The frontend ships **last** (PR 6, §10) — the core's correctness is independent of and verifiable before
any UI exists.

---

## 7. Test plan per tier (CRUCIAL for a refactor — characterization first)

Per CLAUDE.md Definition of Done: tests exist for every change; `./jt` green; coverage doesn't regress
(ratchet where it climbs); no real LLM/network. The refactor's defining test obligation is
**characterization tests that prove no regression** before any factoring lands.

### 7a. Characterization (lock current behavior BEFORE refactoring) — PR 1
- `server/tests/test_writer_core_characterization.py` (**new**): for a seeded DB + fixed article/sources,
  capture the **current** output of `write_one`, `maintain_one`, and `rebuild_engine._generate` (driven
  via the `_drain` + `FakeProvider` recipe, `test_rebuild_engine.py:36-128`) — body + lint + talk — as
  golden snapshots. These run against the **pre-refactor** code and must stay green **after** each
  factoring PR. This is the regression net.
- `server/tests/test_prompt_golden.py` (**new**): snapshot the rendered `wiki_write`/`wiki_revise`/
  `wiki_maintain` strings (§5a). Asserts byte-equality post-fragment-extraction except the one intended
  `wiki_revise` date-rule diff.

### 7b. New shared-helper unit/integration tests — PRs 2–4
- `test_writer_core.py` (**new**): `enforce_date_tokens` — malformed-token error cases (`@t{age:…}`,
  `@t[age:1986]`), adjacency rewrite **with** round-trip guard (rewrite when token reproduces literal,
  warn when it doesn't), BASE token-preservation (token dropped across an edit → warning). `harden_draft`
  — order-of-ops, dead-link neutralize parity, `add_links_to_content` invoked. `rebind_entities` — the
  Research 04 H1 confirmation test: link a `kb/People/X`, call `add_links_to_content` with a nickname →
  under-links; call `rebind_entities` → links (mirrors `test_owner_alias_backfill.py`). Mark
  `@pytest.mark.unit` where no DB, `integration` otherwise.
- `test_rebuild_refs_links.py` (**extend**, existing): assert the live `_generate` path now enforces
  dates and that `maintain_one` now runs the people-link backstop (the previously-missing parity).
- `test_promote_one.py` (**new**): `promote_one` runs the six steps idempotently (run twice → identical
  body); People page gets AKA + owner link; Reference page gets grounding flag + no People links (PII
  firewall, Research 04 O5); a second call is a no-op. `test_corrections.py:130` already monkeypatches
  `flag_dead_links` — extend that pattern for the promotion fns where needed.
- `finalize_rebuild` test (extend existing rebuild tests): Accept now promotes — assert the AKA line /
  owner link appear after Accept (closes Research 02 §6 for live + nightly).

### 7c. New loop tests — PR 5
- `test_rebuild_engine.py` (**extend**): `run_suggest_start` + `run_suggest_turn` via `_drain` +
  `FakeProvider`: happy path (BASE seeded, turn edits, `harden_draft` re-links a dropped name),
  no-credentials path (`creds=False`), `fail_on_turn` error path, cancellation (`run.cancelled=True`,
  cf. `:169`), and the **BASE token-preservation** end-to-end (a turn that drops a `@t[...]` token →
  warning surfaced). Backlinks loaded read-only and **not** in `run.sources` (assert grounding unchanged).

### 7d. Frontend tests — PR 6
- `SuggestPanel.test.tsx` (**new**), copying `RebuildPanel.test.tsx`'s scriptable-fake recipe
  (Research 05 §5a): mock `suggestStart`/`suggestTurn`, keep accept/reject on MSW. Fill the **gap**
  Research 05 §5a flags (the existing test never scripts the guide loop): script a turn emitting
  `content_delta`+`done`, assert the optimistic user bubble, the re-rendered draft, the diff vs BASE, and
  a second turn. `renderWithProviders` + the stable-`onClose`/footer-scoping gotchas.

### 7e. e2e + coverage
- `./jt e2e`: extend an existing rebuild Playwright flow (LLM faked at `e2e/fake_llm.py`) with a
  suggest-revisions open → turn → diff → Accept happy path, since this is a user-facing flow / API
  contract (CLAUDE.md DoD #2).
- **Coverage ratchet:** the new `writer_core.py` arrives with high-coverage focused tests; once real
  coverage sits comfortably above the floor, **ratchet** `fail_under` in `server/pyproject.toml` (and the
  web `thresholds` in `web/vitest.config.ts` for the panel) up in the same PR (CLAUDE.md DoD #3). Never
  lower a floor.

---

## 8. Migration / rollout: land the refactor safely

Incremental, each PR independently green on `./jt` (and the characterization snapshots from PR 1 stay
green through every later PR — that's the safety mechanism):

1. **PR 1 — Characterization net (no behavior change).** Add 7a snapshots against current code. Merge
   first so every subsequent PR is measured against a frozen baseline.
2. **PR 2 — `writer_core.harden_draft` extraction.** Factor the existing tail; switch `_generate` and
   `write_one` to call it (`base=None`, no new date logic yet → snapshots unchanged). Pure refactor.
3. **PR 3 — `enforce_date_tokens` + `rebind_entities` + fragment extraction.** Add the date pipeline
   (A/B/D), the entity rebind, and the `{…_rules}` fragments (5a). Snapshots update only for the one
   intended `wiki_revise` date diff. Also wires `harden_draft` into `maintain_one` (people-link parity).
4. **PR 4 — `promote_one` + `finalize_rebuild` parity.** Promotion suite on every save path. Closes
   Research 02 §6. (Optional separate PR to collapse `wiki_build.yaml` steps 6b–6e onto `promote_one`.)
5. **PR 5 — suggest engine + endpoints, behind a feature flag.** `run_suggest_*`, the `/api/kb/suggest/*`
   routes, RebuildRun `kind`/`base`. **Flag:** a settings/meta key `feature_suggest_revisions` (default
   **off**); the endpoints 404 and the menu item is hidden when off. Backend tests run with it on.
6. **PR 6 — frontend `SuggestPanel` + entry point**, gated by the same flag (capability-style, like
   `llm.ready` at `NotePage.tsx:119-124`). e2e flow added. Flip the flag on once green.

The hardening/parity wins (PRs 2–4) deliver value to **existing** users (rebuild/maintain get correct
dates, people-links, promotion) **before** the new feature ships — so the front-loaded refactor isn't
dead weight; it's the bug-fix the user actually reported (Research 03 §2 is the user's "wrong date
format" complaint).

---

## 9. Risks (candid)

1. **Regressing existing passing paths.** This is the dominant risk and the reason for §7a/§8's
   characterization-first sequencing. The mitigation is structural: PR 2 is a *pure* refactor proven by
   byte-identical snapshots; behavior changes (PR 3–4) are isolated and each asserted by an explicit
   intended-diff test. If a snapshot moves unexpectedly, the PR doesn't merge.
2. **Scope creep.** "Factor a shared core" can metastasize (e.g. tempting to also unify the legacy
   `synthesize_wiki`/`wiki_plan` outlier, Research 02 §7). **Explicitly out of scope** for this effort —
   `promote_one`/`harden_draft` target the four main paths; the legacy engine is left alone (a noted
   follow-up). The PR breakdown caps each step.
3. **Prompt-recomposition drift.** Extracting fragments risks a subtle whitespace/ordering change that
   shifts model output. Mitigated by the golden-prompt test (§7a) asserting byte-equality; the fragments
   are substituted, not rewritten.
4. **`promote_one` on the request path** could make Accept slow (medref/places network) or, if a step is
   not perfectly idempotent, corrupt a body on repeat. Mitigated by: scoping to per-article, asserting
   idempotency in `test_promote_one.py` (run-twice = identical), and falling back to talk-todos on cache
   miss rather than blocking (deferring network to background if measured slow).
5. **Front-loaded effort before user-visible payoff.** Honest weakness of stance D. Countered by PRs 2–4
   shipping the actual bug-fixes to existing users first; the new UI (PRs 5–6) is the smaller increment.
6. **The simplest-viable loop re-emits the whole article per turn** (cost/latency, Research 01 §6/§10).
   Accepted trade for safety/simplicity; the BASE-preservation D-guard contains drift, and the
   emit-changed-sections optimization is a clean future patch the core already supports.

---

## 10. Sequencing / effort — core lands BEFORE the feature

| PR | Title | Touches | Effort | Ships value |
|---|---|---|---|---|
| 1 | Characterization net | new tests only | S | safety baseline |
| 2 | `harden_draft` extraction | `writer_core.py`, `_generate`, `write_one` | M | pure refactor |
| 3 | date tokens + rebind + fragments | `writer_core`, `clock`-adjacent, `prompts.yaml`, `maintain_one`, `wiki_revise` | L | **fixes user's date bug + people-link parity for rebuild/maintain** |
| 4 | `promote_one` parity | `writer_core`, `finalize_rebuild`, per-article scoping of promo fns | M | **promotion parity for live Accept + nightly** |
| 5 | suggest engine + routes (flagged off) | `rebuild_engine`, `rebuild.py`, `rebuild_runs`, `architect`-reuse backlinks | M | new mode backend |
| 6 | SuggestPanel + entry (flag on) | `SuggestPanel.tsx`, `api.ts`, `NotePage.tsx`, e2e | M | user-visible feature |

**Explicit ordering guarantee:** PRs 2–4 (the shared core + all hardening/parity) land and ship to
existing users **before** PRs 5–6 (the new feature). The new conversational mode is, by then, a thin
consumer that inherits correct dates, fresh people-links, and full promotion **by construction** — which
is the entire argument for stance D.
