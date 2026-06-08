# Plan C — "Suggest revisions" as a tool-using, truth-seeking edit agent

**Stance:** Lean fully into *truth-seeking for salient facts*. The conversational edit
loop is an **agent with tools** that can go *find* a fact mid-conversation
(re-search the owner's notes, pull a backlink or source in full) instead of being
boxed into an initially-curated source set. The owner steers structure / formatting /
corrects assumptions; the agent proactively grounds salient facts.

This plan is written against the real tree on 2026-06-08 and the five research notes
in `docs/kb-suggest-revisions/research/`. Every load-bearing claim carries a
`file:line` citation.

---

## 0. The central hazard and how C resolves it (read this first)

Research 01 states the single most important constraint: the rebuild design keeps
GATHER (tools, no thinking) and DRAFT (thinking, no tools) on **separate transcripts**
precisely so the draft/Guide resume "has no tool_use blocks to preserve — trivially
safe" (`server/app/services/rebuild_engine.py:9-15`, `:13-14`). Resuming a transcript
that mixes **signed thinking blocks** and **tool_use blocks** is the known
cross-provider fragility (research 01 §10 risk #1, lines 298-304). The Anthropic
adapter appends the model's turn *verbatim including signed thinking* to `messages`
(`server/app/services/llm.py:402-404`); the xAI adapter appends an OpenAI-shape turn
with `tool_calls` (`server/app/services/llm.py:675-678`). The two shapes are **not
interchangeable** — a transcript authored under one provider cannot be safely resumed
under the other, and a thinking+tool_use transcript is brittle even within one
provider.

**C's resolution: a two-transcript topology that mirrors the existing GATHER/DRAFT
split, applied per conversational turn.** Each edit turn runs in two phases on two
*different, disposable* transcripts:

1. **FACT-FINDING phase (tools, NO thinking, disposable transcript).** A fresh local
   `msgs` list — exactly like Stage-1 gather's local `msgs`
   (`rebuild_engine.py:152`, thrown away at `:193-195`) — runs the tool agent
   loop. Tools: `search_notes`, `read_source`, `read_backlink`, and the terminal
   `apply_edits`. Tool calls stream live to the UI (tool use is provider-neutral and
   already renders on Grok — `rebuild_engine.py:6-7`). This transcript **never becomes
   `run.messages`** and is discarded after the turn. It carries tool_use blocks but
   **never thinking** (`thinking=False`), so it is the *safe* combination the gather
   stage already proves works.

2. **DISTILLATION into the EDIT transcript (NO tools, NO thinking, persisted).** The
   agent's terminal `apply_edits` tool call carries a structured list of targeted
   edits (find→replace / section ops) **plus a short user-facing summary and the
   facts it discovered**. The server applies those edits deterministically to the
   working draft (`run.draft`), then records into the **persisted** `run.messages` a
   pair of *plain* turns: a `user` turn = the owner's instruction, and an `assistant`
   turn = the **plain-text** summary of what changed + facts found. `run.messages`
   thus stays a clean alternating plain-text transcript with **zero tool_use and zero
   thinking blocks** — identical in shape to today's Guide transcript
   (`rebuild_engine.py:455-461` appends one plain user turn; `_generate` appends one
   plain assistant turn). It is safe to resume across Anthropic↔xAI and across turns.

The persisted `run.messages` is the *conversational memory* (so a later turn knows
what was already changed and which facts were established); the disposable
fact-finding transcript is where the dangerous tool_use+provider-specific blocks live
and die. We never resume a transcript that contains them. **This is the gather pattern
applied to every turn**, and it is why C does not reintroduce the hazard research 01
warns about.

> Design note on "thinking": fact-finding runs thinking-OFF so the disposable
> transcript can never carry a signed thinking block. The agent's *judgment* about
> what to edit happens in the tool args (structured), not in a reasoning block we
> must preserve. If we want the owner to see reasoning, we surface a brief
> `assistant_delta` text stream from the agent's interstitial text between tool calls
> (provider-neutral plain text), **not** extended thinking. This keeps both
> transcripts free of signed-thinking blocks — strictly safer than today's DRAFT
> stage, which *does* persist signed thinking (`llm.py:404`) but gets away with it only
> because it has no tools.

---

## 1. Backend architecture

### 1.1 New module: `server/app/services/suggest_engine.py`

A sibling to `rebuild_engine.py`, reusing its primitives. New functions:

- `async def run_start(run) -> AsyncGenerator[dict, None]` — seeds the session:
  loads the BASE article, the deterministic seed sources (`rebuild_sources`,
  `wiki_build.py:1640`), and the read-only backlinks (§4). Seeds `run.draft` =
  **current article body** (BASE preserved — the key behavioral difference from
  `run_draft` which seeds an empty draft, `rebuild_engine.py:435`). Emits a
  `session_ready` event with the BASE draft + context summary. Runs the entity
  **rebind** (§5, research 04 O1) before the first turn so people-links are fresh.

- `async def run_turn(run, instruction: str) -> AsyncGenerator[dict, None]` — the
  agent edit turn. This is the heart of C. Pseudocode:

```
async def run_turn(run, instruction):
    conn = get_conn()
    run.status = "guiding"
    model = run.model                      # pinned synthesis model (reuse)
    provider = llm.get_provider(model)
    system = _editor_system(run)           # §6
    # DISPOSABLE fact-finding transcript — never touches run.messages.
    ff = [{"role": "user", "content": _turn_user_prompt(run, instruction)}]
    edits = None; summary = ""; facts = []
    for _ in range(_TURN_MAX_ITER):        # cap tool loop (cost guard, §9)
        if run.cancelled: return
        calls = []; text_parts = []
        async for ev in provider.stream_turn(ff, system=system, tools=_EDIT_TOOLS,
                                              model=model, max_tokens=_TURN_MAX_TOKENS,
                                              thinking=False):       # NO thinking → safe disposable transcript
            if run.cancelled: return
            if isinstance(ev, llm.TextDelta):
                text_parts.append(ev.text)
                yield {"type": "assistant_delta", "text": ev.text}  # provider-neutral plain text
            elif isinstance(ev, llm.ToolCallEvent):
                calls.append(ev.call)
        if not calls: break
        results = []
        for call in calls:
            async for ev, res in _dispatch_tool(run, conn, call):   # yields UI events + ToolResult
                if ev: yield ev
                if res is not None: results.append(res)
            if call.name == "apply_edits":
                edits = call.args.get("edits"); summary = call.args.get("summary","")
                facts = call.args.get("facts") or []
        provider.append_tool_results(ff, results)
        if edits is not None: break
    # APPLY edits deterministically to the working draft → new draft
    new_draft, applied, rejected = _apply_edits(run.draft, edits or [])
    # HARDENING TAIL (§5) runs on the WHOLE new draft, exactly like _generate:366-398
    new_draft, talk_adds, lint = _harden(conn, run, new_draft)
    run.draft = new_draft
    run.talk += talk_adds
    # PERSIST clean plain-text turns into the conversational memory transcript.
    run.messages.append({"role": "user", "content": instruction})
    run.messages.append({"role": "assistant",
                         "content": _summary_text(summary, applied, rejected, facts)})
    run.status = "ready"
    yield {"type": "edit_applied", "draft": new_draft, "summary": summary,
           "applied": applied, "rejected": rejected, "facts": facts}
    yield {"type": "lint", **lint}
    yield {"type": "done", "draft": new_draft, "lint": lint}
```

Key reuses: `_notes_meta` (`rebuild_engine.py:76`), `search.hybrid_notes` offloaded
via `asyncio.to_thread` to keep ONNX inference off the event loop
(`rebuild_engine.py:176-177`), `provider.append_tool_results`
(`llm.py:418,690` — provider-correct shape for the disposable transcript), the
cooperative cancel poll on `run.cancelled` (`rebuild_engine.py:157,162`).

### 1.2 The tool set (schemas — `llm.ToolDef`, `llm.py:68`)

All four are read-only against the owner's notes except `apply_edits` (which produces,
not persists, edits). They mirror the gather tools' style (`rebuild_engine.py:54-73`).

```python
_EDIT_TOOLS = [
  llm.ToolDef("search_notes",
    "Search the owner's personal notes for material relevant to a fact you need to "
    "verify or add. Returns matching note titles + dates. Use this to TRUTH-SEEK a "
    "salient fact before editing — do not invent.",
    {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}),
  llm.ToolDef("read_source",
    "Read the FULL text of one note by its EXACT title to ground a specific fact "
    "(dates, numbers, names). Prefer this over guessing.",
    {"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}),
  llm.ToolDef("read_backlink",
    "Read the FULL text of one KB article that links TO this page (a backlink), to "
    "see how it is described elsewhere. READ-ONLY context, never a citation source.",
    {"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}),
  llm.ToolDef("apply_edits",
    "Finish this turn: apply a set of TARGETED edits to the current article and stop. "
    "Each edit is a find→replace on a unique anchor, or a section op. Include a one-line "
    "summary for the owner and the salient facts you established (with their source).",
    {"type":"object","properties":{
      "edits":{"type":"array","items":{"type":"object","properties":{
        "op":{"type":"string","enum":["replace","insert_after","insert_section","delete"]},
        "anchor":{"type":"string"},          # unique substring locating the edit
        "find":{"type":"string"},            # exact text to replace (op=replace)
        "text":{"type":"string"}},           # replacement / inserted text
        "required":["op"]}},
      "summary":{"type":"string"},
      "facts":{"type":"array","items":{"type":"object","properties":{
        "claim":{"type":"string"},"source":{"type":"string"}}}}},
     "required":["edits","summary"]}),
]
```

### 1.3 `_apply_edits` — deterministic targeted-edit application

`_apply_edits(draft, edits) -> (new_draft, applied, rejected)`:

- **`replace`**: locate `find` in `draft`; if it occurs exactly once, replace it.
  If it occurs 0 or >1 times, **reject** that edit (don't guess) and report it back
  to the owner via `rejected`. Uniqueness is the safety property that keeps targeted
  edits from corrupting the BASE.
- **`insert_after`**: insert `text` after the unique `anchor` line.
- **`insert_section` / `delete`**: section-level ops keyed on a `## Heading` anchor.
- Returns `applied` (human-readable list) and `rejected` (so the agent/owner can
  retry). A rejected edit is *not* an error — it is surfaced and the loop can refine.

This is the "edit-application mechanism" the brief asks for. It is **deterministic and
in-memory** — no LLM re-emits the whole article (the central efficiency win over
today's full-rewrite Guide, `rebuild_engine.py:455-460` + research 01 §6). The agent
proposes structured edits; the server is the sole mutator of `run.draft`.

### 1.4 New/changed files & functions (summary)

| File | Change |
|---|---|
| `server/app/services/suggest_engine.py` | **NEW** — `run_start`, `run_turn`, `_dispatch_tool`, `_apply_edits`, `_harden`, `_editor_system`, `_turn_user_prompt`, `_summary_text`, `_EDIT_TOOLS`. |
| `server/app/routers/suggest.py` | **NEW** — endpoints (§2), reusing `_sse` bridge pattern from `rebuild.py:57-111`. |
| `server/app/services/rebuild_runs.py` | extend `RebuildRun` (§3): `kind`, `backlinks`, `base_draft`, `facts`. |
| `server/app/services/wiki_build.py` | **NEW** `enforce_date_tokens` (§5, research 03) + **NEW** `promote_one` (§5, research 02). Both shared with rebuild. |
| `server/app/services/entity_index.py` | **NEW** cheap `rebind(conn)` (§5, research 04 O1) — `_link_articles` + owner-alias fold, no embeddings. |
| `server/app/main.py` | register the `suggest` router. |
| `prompts.yaml` | **NEW** `actions.wiki_edit` editor system prompt + factored `{date_rules}`/`{crosslink_rules}` fragments (§6). |
| `web/src/api.ts`, `web/src/components/SuggestPanel.tsx`, `web/src/pages/NotePage.tsx` | frontend (§7). |

The `_sse` bridge, run registry, accept/staleness/lock, and `finalize_rebuild` are
reused **unchanged** from the rebuild path (research 01 §10 "Reuse (large)").

---

## 2. SSE protocol additions and run state machine

### 2.1 Event vocabulary

Reuse the existing envelope `event: {type}\ndata: {json}\n\n` with 15s keepalive
(`rebuild.py:90-99`). New/extended types:

| `type` | Emitted by | Payload | Consumer |
|---|---|---|---|
| `session_ready` | `run_start` | `run_id, slug, title, base_rev, base_draft, backlinks[], sources[]` | open panel with BASE draft visible |
| `tool_use` | `_dispatch_tool` | `tool, query?, title?` | **reuse Stage-1 tool_use rendering** (`RebuildPanel.tsx:85-88`) |
| `tool_result` | `_dispatch_tool` | `tool, summary, items[]` | resolve step (`RebuildPanel.tsx:89-98`) |
| `assistant_delta` | `run_turn` | `text` | stream the agent's interstitial reasoning text (plain) into the chat bubble |
| `edit_applied` | `run_turn` | `draft, summary, applied[], rejected[], facts[]` | re-render evolving draft + AI summary bubble + facts chips |
| `lint` | `_harden` | `ok, message`/`errors,warnings` | warn banner (`RebuildPanel.tsx:124`) |
| `done` | `run_turn` | `draft, lint` | turn complete; phase→`ready` |
| `error` | bridge / engine | `message` | error phase |

The big difference from the rebuild protocol: **no `content_delta` full-body stream**
on an edit turn. The draft changes via `edit_applied.draft` (a single re-render of the
targeted-edited body), and the *narration* streams via `assistant_delta`. `tool_use`/
`tool_result` reuse the exact Stage-1 shapes so the frontend renderer is shared
(research 05 §1, `RebuildPanel.tsx:85-98`).

### 2.2 Run state machine deltas

The session is **single-stage** (no gather→curate→draft wizard). States:

```
create(kind="suggest") → "session_ready"     (base draft staged, status "ready")
   POST /turn → run_turn: "guiding" → (tool loop) → "ready"   [repeatable]
   POST /accept (status in ready) → "accepting" → "accepted" → drop()
   POST /reject → drop()
   error → "error";  idle>TTL → _sweep() drops (skips "accepting")
```

Reuse `is_live`/`_LIVE = ("streaming","ready","guiding")` (`rebuild_runs.py:24,145`),
the TTL sweep that never reaps "accepting" (`rebuild_runs.py:71-72`), and the
one-run-per-slug enforcement (`rebuild_runs.py:53,91-93`). Accept stays gated on
`status in ("ready","guiding")` + non-empty draft (`rebuild.py:345-348`) — **unchanged**.

---

## 3. RebuildRun changes (`rebuild_runs.py:27-49`)

Add to the dataclass (keeps the registry, TTL, cancel, one-per-slug logic identical):

```python
kind: str = "rebuild"          # "rebuild" | "suggest" — discriminator
base_draft: str = ""           # BASE article body, preserved (seed for run.draft)
backlinks: list[dict] = field(default_factory=list)  # [{title, note_id}] read-only context
facts: list[dict] = field(default_factory=list)      # accumulated {claim, source} across turns
```

- `base_hash` still hashes the **live page** at start (`rebuild_runs.py:35,100`) — the
  staleness guard is unchanged because Accept keys off the live page, not the draft
  origin (research 01 §10 "base_hash / staleness / Accept need no change").
- `messages` continues to mean "the persisted, plain-text conversational transcript"
  (§0) — but for suggest it carries **zero** tool_use/thinking blocks by construction,
  which is *stronger* than the rebuild invariant (rebuild's `messages` carries signed
  thinking, `llm.py:404`).
- `sources` keeps its existing role: the curated/seed source titles used **only** by
  `_repair_citation_titles` grounding (`rebuild_engine.py:372-374`). Backlinks live in
  the **separate** `backlinks` field so they never become citation-repair targets (§4).
- `create()` gains a `kind` param (default `"rebuild"` — back-compat). `is_live`
  unchanged.

---

## 4. Backlinks loading (read-only) and the `read_backlink` tool

### 4.1 Loading

At `run_start`, load inbound links with the exact SQL pattern research 01 §7
identifies (`architect.py:818-822`):

```python
def _load_backlinks(conn, note_id) -> list[dict]:
    rows = conn.execute(
      "SELECT DISTINCT n.id, n.title FROM links l "
      "JOIN notes n ON n.id = l.source_note_id "
      "WHERE l.target_note_id = ? AND n.deleted_at IS NULL ORDER BY n.title",
      (note_id,)).fetchall()
    return [{"note_id": r["id"], "title": r["title"]} for r in rows]
```

Store on `run.backlinks`. The **titles** (not full bodies) go into the editor system
prompt as read-only context ("Other articles that link here: …"); the agent pulls a
full body on demand via `read_backlink`. This keeps the initial prompt small and lets
the agent truth-seek lazily (the C thesis).

### 4.2 Firewall on backlinks (critical)

Backlinks can include private/Reference pages. The `read_backlink` dispatcher MUST:

- refuse to read a backlink whose title `is_private_title` or
  `domain_for_title == "Reference"` **when the TARGET (the page being edited) is
  public** — never let a private note's content flow into a shareable article
  (research 04 invariant #2, `wiki_build.py:733`; research 03 §4 PII firewall). The
  same predicate the linker uses (`wiki_guides.is_private_title:148`,
  `domain_for_title:103`) gates the tool. See §9 firewall.

### 4.3 Not shifting `_repair_citation_titles` grounding (the trap)

Research 01 §10 (lines 311-313) and research 02 §4 are explicit: backlink-context
articles must **NOT** be injected as curated sources, or they become
`_repair_citation_titles` targets (`rebuild_engine.py:372-374`) and shift the grounding
set — a near-miss `[[Backlink Title]]` would get "repaired" into a citation. C keeps
`run.sources` = curated/seed *primary notes only* (set exactly as `run_draft` does,
`rebuild_engine.py:432`), and `run.backlinks` separate. The hardening tail's
`source_titles` (`rebuild_engine.py:373`) is derived **only** from `run.sources`. Tested
explicitly (§8).

---

## 5. Folded-in hardening — where each runs

The `_harden(conn, run, draft)` helper is the targeted-edit analogue of `_generate`'s
backstop tail (`rebuild_engine.py:362-398`) and runs **on the whole working draft after
every edit turn** (research 02 rec #1 — "call it on every draft shown"). Order:

1. **Date-token enforcement** (research 03; NEW `wiki_build.enforce_date_tokens`). Run
   **first**, before link work, on the post-edit draft. Bundle (research 03 §3
   recommended set):
   - **Option A — malformed-token linter (HIGH/LOW, recommended):** flag any `@t[`-shaped
     substring not matched by `clock._TOKEN_RE` (`clock.py:105`) or whose date fails
     `_to_dt` (`clock.py:127`). Surface as a blocking `lint` warning (research 03 §3A).
   - **Option D — token-preservation guard (loop-specific, recommended):** compute
     `_TOKEN_RE.findall(run.base_draft)` ∪ tokens from the *pre-edit* draft; assert each
     still present in the post-edit draft unless an edit deliberately removed that fact.
     A token silently expanded into a frozen number is the most likely *new* regression
     a targeted edit introduces (research 03 §3D, lines 222-229). If one vanished
     without a corresponding `delete` edit, re-insert it / warn.
   - **Option B-with-round-trip-guard (adjacency rewrite):** when a frozen literal sits
     next to an anchor date and `clock.expand_tokens(@t[age:DATE])` reproduces the
     literal exactly, rewrite to the token; otherwise warn only (research 03 §3B, the
     round-trip guard eliminates false positives).
   - Any expansion-semantics change MUST keep `clock.expand_tokens` ↔
     `time.ts:expandTimeTokens` byte-for-byte and update
     `server/tests/fixtures/time_tokens.json` (research 03 note + §6). C touches only
     *production* of tokens, not expansion, so the fixture is untouched.

2. **Dead-link + citation repair** — `_bad_links` → `_repair_citation_titles` (keyed on
   `run.sources` only, §4.3) → `_neutralize_links`, identical to
   `rebuild_engine.py:367-382`.

3. **People-link fix (research 04).** Two parts:
   - **Entity rebind at `run_start`** (research 04 O1, the H1 root cause). The rebuild
     path never refreshes the entity index (research 04 §3,
     `rebuild_engine.py` has no `entity_index.rebuild` call). Add a **cheap, no-embeddings**
     `entity_index.rebind(conn)` = `_link_articles` (`entity_index.py:529`) + the
     owner-alias fold (`reconcile_owner`, `wiki_build.py:1074-1081`) so a freshly
     created/renamed People page is bound *before* the first turn. This is binding-only
     (updates `entities.article_title` + `entity_aliases`), preserves the firewall
     (`_link_articles` already excludes private leaves, `entity_index.py:553`), and skips
     the networked `_sync_embeddings` (research 04 O1 "Preferred").
   - **`add_links_to_content` after every turn** (research 04 O2,
     `wiki_build.py:711`) — the deterministic backstop that links names the agent left
     plain, self-guarding the PII firewall (`wiki_build.py:733`). Same call site role as
     `rebuild_engine.py:387`. Because edits are *targeted* and re-masked against the
     **current** post-edit draft, invariant #3 (never nest links, research 04 line 246)
     holds.
   - Optionally **O3** "unlinked known person" advisory `lint` so the owner can steer
     ("link Allan everywhere") rather than auto-linking collision-prone first names
     (research 04 O3; respects drop-rule (iv), research 04 H2).

4. **Structure validation** — `wiki_guides.validate_structure` (`rebuild_engine.py:392`),
   advisory only (human-in-the-loop, research 02 rec #6).

5. **Promotion parity on Accept (research 02 #5; NEW `wiki_build.promote_one`).** The
   live Accept today runs only `finalize_rebuild` (`rebuild.py:374`,
   `wiki_build.py:1681-1721` → `entity_index.rebuild` + disambiguation + `flag_dead_links`)
   and **omits** `link_owner`, `surface_aliases`, `link_medications`, `link_places`,
   `normalize_link_labels`, `flag_ungrounded_reference` (research 02 §6, lines 109-119).
   `promote_one(conn, title)` **does not exist yet** (grep confirms — no
   `promote_one`/`def promote` in `server/app`). C factors it as a shared per-article
   promotion suite and calls it inside the Accept lock, after `finalize_rebuild`,
   **before commit** (`rebuild.py:374-376`). Shared by rebuild Accept, nightly rebuild,
   and suggest Accept so they can't drift (research 02 rec #4). `surface_aliases`
   (`wiki_build.py:1177`) owns the AKA line deterministically — the agent is forbidden to
   hand-edit it (research 03 §5, `_apply_aka_line:1137`).

**Why this order:** dates first (so a token the agent introduced is validated before the
linker masks spans), then links (which mask code/footnotes), then structure (read-only
check), then promotion at Accept. Each step takes the *whole* body, so targeted edits
flow through the same guarantees as a full rebuild (research 02 rec #1).

---

## 6. Prompt / tool design

### 6.1 Editor agent system prompt (`actions.wiki_edit`, NEW in `prompts.yaml`)

Built by `_editor_system(run)`. It must carry the directive blocks research 02 rec #3
says the bare Guide steer lacks (`rebuild_engine.py:455-460` carries none of DATES /
CROSS-LINKS / GROUNDING). C factors reusable fragments and injects them:

- **Factor** the `DATES & TIME` block (`prompts.yaml:868-874`) → `{date_rules}` and the
  `CROSS-LINKS` block (`prompts.yaml:855-860`) → `{crosslink_rules}`, then reference
  them from `wiki_write`, `wiki_revise`, **and** the new `wiki_edit` (research 02 rec #3,
  lines 198-204; research 03 §2 recommends folding DATES into `wiki_revise` too).
- The editor prompt's contract (the C-specific judgment rules):

```
You are revising the KB article "{title}" in a live conversation with its owner.
BASE = the current article (preserved). You make TARGETED edits, not rewrites.

TRUTH-SEEKING (your job): when the conversation turns on a SALIENT FACT (a date, a
number, a name, who-did-what), do NOT guess and do NOT rely only on memory. Use
search_notes / read_source to GROUND it from the owner's notes first, then edit.
Cite the source of each fact you add in apply_edits.facts.

STEERING (the owner's job): the owner directs STRUCTURE, FORMATTING, and CORRECTS
ASSUMPTIONS. When they ask for a structural/format change, make exactly that change —
do not also rewrite unrelated prose. When they correct a fact, trust them over a note.

EDIT DISCIPLINE: every edit's `find`/`anchor` must be a UNIQUE substring of the current
article. Touch only what the instruction (or a fact you verified) requires. Preserve
every @t[...] token verbatim; encode a drifting value as a token, never a frozen number.
{date_rules}
{crosslink_rules}
Other articles that link here (read-only context; never cite them): {backlinks}
You write from the owner's notes only — never from other kb/ articles.
Finish each turn with apply_edits (a summary + the facts you established).
```

### 6.2 Search vs edit decision

The agent decides via the tool loop, capped at `_TURN_MAX_ITER` (cost guard, §9). The
prompt biases it: *search/read when a salient fact is in play; otherwise go straight to
`apply_edits`.* A pure formatting instruction ("make the intro shorter") needs no
search → one `apply_edits` call. A fact instruction ("when did we actually buy the
truck?") triggers `search_notes`/`read_source` → then `apply_edits`. This is exactly
the gather-agent shape (`rebuild_engine.py:156-195`) with `apply_edits` as the terminal
tool (the analogue of `propose_sources`, `rebuild_engine.py:187-190`).

### 6.3 Guardrails

- **Edit-only-at-direction for structure/format; truth-seek facts proactively** — the
  prompt's two-paragraph contract above. Enforced structurally by `_apply_edits`
  rejecting non-unique anchors (the model can't smear edits across the doc).
- **Never invent links/citations** — `{crosslink_rules}` + the deterministic
  `_bad_links`/`_neutralize_links` backstop deletes any dead `[[link]]` and footnote to
  a non-source (research 04 invariant #6; research 03 §5). The agent is *told* and then
  *enforced*.
- **Never touch the AKA line / protected pages** — `surface_aliases` owns AKA; refuse to
  target `is_protected` pages (`wiki_guides.is_protected:87`, research 03 §5).

---

## 7. Frontend — `web/src/components/SuggestPanel.tsx`

Research 05 §4 is explicit: the RebuildPanel **Guide loop is the right primitive,
promoted to the only loop**; no gather/curate wizard. Structure:

- **Modal panel** copied from `RebuildPanel.tsx`, reusing: the stable-`onClose` ref
  trick so the composer keeps focus (`RebuildPanel.tsx:78-79`, research 05 §1), the
  `MarkdownDiff before={note.content_md} after={draft}` against preserved BASE
  (`RebuildPanel.tsx:431-435,460`), and Accept/Reject footer buttons
  (`RebuildPanel.tsx:252-297`).
- **Chat transcript** `thread: {role,text}[]` (`RebuildPanel.tsx:62`) with Chat.tsx's
  **optimistic user-bubble-on-Send** + a **real per-turn AI summary bubble** (replace
  the canned ack at `RebuildPanel.tsx:237` with `edit_applied.summary`; research 05 §4).
- **Tool activity** — **reuse the exact Stage-1 `Step` rendering** (`RebuildPanel.tsx:17,
  85-98, 321-334`): `tool_use` adds a running step, `tool_result` resolves it, chips
  show found titles. `TOOL_LABEL` extended with
  `read_source`/`read_backlink`/`apply_edits` labels (`RebuildPanel.tsx:26-29`). This
  shows the truth-seeking visibly — the C selling point — and it is provider-neutral
  (renders on Grok, `rebuild_engine.py:6-7`).
- **Evolving draft** — re-rendered from `edit_applied.draft` (a single set, not a
  token stream), with a subtle highlight of `applied` ranges; `rejected` edits surface
  as a small "couldn't apply automatically" note inviting a re-phrase.
- **Facts panel** — `facts[]` chips ("✓ bought 2024-03 — [[Truck log]]") so the owner
  sees what was grounded and from where (the C differentiator).
- **Composer** — footer textarea, Enter-to-send / Shift+Enter newline
  (`RebuildPanel.tsx:283-289`).

### 7.1 `api.ts` additions

A parallel `SuggestEvent` union + thin `streamSSE`-based wrappers (research 05 §2 — do
**not** hand-roll a reader; `streamSSE` already handles abort/stall/health/`\n\n`,
`api.ts:875-929`):

```ts
export type SuggestEvent =
  | { type:"session_ready"; run_id:string; slug:string; title:string; base_rev:string;
      base_draft:string; backlinks:{title:string}[]; sources:{title:string}[] }
  | { type:"tool_use"; tool:string; query?:string; title?:string }
  | { type:"tool_result"; tool:string; summary:string; items?:string[] }
  | { type:"assistant_delta"; text:string }
  | { type:"edit_applied"; draft:string; summary:string; applied:string[];
      rejected:string[]; facts:{claim:string;source:string}[] }
  | { type:"lint"; ok:boolean; message?:string; errors?:string[]; warnings?:string[] }
  | { type:"done"; draft:string }
  | { type:"error"; message:string };

export const suggestStart = (slug, onEvent) => streamSSE(`/api/kb/suggest/start/${enc(slug)}`, {}, onEvent);
export const suggestTurn  = (runId, text, onEvent) => streamSSE(`/api/kb/suggest/${runId}/turn`, { text }, onEvent);
export const acceptSuggest = (runId, renameTo?) => post(`/api/kb/suggest/${runId}/accept`, { rename_to: renameTo ?? null });
export const rejectSuggest = (runId) => post(`/api/kb/suggest/${runId}/reject`);
```

### 7.2 Launch

A second KB-only `NoteActionsMenu` item **"Suggest revisions"** beside "Rebuild page
now" (`NotePage.tsx:265-267`), same `rebuildNow`-style `llm.ready` pre-flight
(`NotePage.tsx:119-124`, research 05 §3). Mount `<SuggestPanel slug note={{title,
content_md}} .../>` like the rebuild mount (`NotePage.tsx:379-383`). Backlinks are
already on the page (`note.backlinks`, research 05 §3) for an instant initial render
while the server confirms.

---

## 8. Test plan (per CLAUDE.md Definition of Done)

### 8.1 Backend — `server/tests/test_suggest_engine.py` (copy `test_rebuild_engine.py`)

Reuse the harness wholesale (research 05 §5b): `_drain(agen)` fresh `asyncio.run`
(`test_rebuild_engine.py:36-47`), `FakeProvider` scripted turns
(`test_rebuild_engine.py:50-80`), `_install_provider` mocking the **llm seam**
(`get_provider`/`has_credentials`/`model_for`, `:122-128`) — never the SDK
(CLAUDE.md), real SQLite with embeddings no-op'd (`:83-107`), `_mk`/`_new_run`
(`:110-119`). Mark `@pytest.mark.integration`.

**`FakeProvider` must script tool turns** (it already yields `ToolCallEvent` —
`test_rebuild_engine.py:50-54`, and `append_tool_results` appends the provider-shape
turn, `:76-80`). Scripts for the disposable fact-finding transcript:

- **Happy path, no search:** turn 0 yields `[ToolCallEvent(apply_edits(edits=[replace
  ...], summary)), TurnEnd]`. Assert: `run.draft` changed exactly at the anchor; BASE
  preserved elsewhere; `run.messages` ends with a `user`+`assistant` **plain-text** pair
  with **no** tool_use/thinking blocks (the §0 invariant — assert no dict block in
  `run.messages` content); `edit_applied`/`done` emitted.
- **Truth-seeking path:** turn 0 yields `[ToolCallEvent(search_notes(query)), TurnEnd]`;
  turn 1 yields `[ToolCallEvent(read_source(title)), TurnEnd]`; turn 2 yields
  `[ToolCallEvent(apply_edits(...)), TurnEnd]`. Assert `tool_use`/`tool_result` events
  stream in order; the edit reflects the read fact; the disposable transcript is
  discarded (run.messages has only the two plain turns, not the 3 tool iterations).
- **Transcript-safety assertion (the core C claim):** after several turns, assert
  `run.messages` contains only `{"role","content": str}` turns — never a list-of-blocks
  content, never a `tool_calls` key, never a thinking block. This is the regression guard
  that C does not reintroduce research 01's hazard.
- **Reject-edit path:** `apply_edits` with a `find` that occurs twice → `_apply_edits`
  rejects it; assert `rejected` non-empty, draft unchanged for that edit, no crash.
- **Date hardening:** seed a source with `@t[age:1986-03-15]` in BASE; script an
  `apply_edits` that replaces the sentence with a frozen "40 years old"; assert
  `enforce_date_tokens` (Option D) re-flags/re-tokenizes and a `lint` warning fires.
  Malformed `@t{age:…}` (Option A) → blocking `lint`.
- **People-link backstop:** seed `kb/People/Jeffrey Hopkins`; BASE mentions "Jeff"
  plain; run `rebind` then a no-op edit turn; assert `add_links_to_content` linked it
  (research 04 O2 test). And: without `rebind`, a freshly-renamed People page stays
  plain (H1 reproduction), with `rebind` it links (research 04 §2 confirmation recipe).
- **Backlink firewall:** seed a private backlink; `read_backlink` on it when the target
  is public → refused (returns a "not available" ToolResult, no private text leaks).
- **Grounding not shifted:** assert `_repair_citation_titles` is called with
  `source_titles` from `run.sources` only, never `run.backlinks` (§4.3) — a near-miss
  `[[Backlink]]` is **not** repaired into a citation.
- **No-credentials / cancel / provider-fail paths:** `creds=False`
  (`test_rebuild_engine.py` no-creds pattern), `run.cancelled=True` mid-loop
  (`:169`), `FakeProvider(fail_on_turn=0)` (`:68`).
- **Accept parity:** mock the lock + `finalize_rebuild` + new `promote_one`; assert
  Accept (status `ready`) calls `finalize_rebuild` **then** `promote_one` inside the
  lock before commit; staleness 409 when `content_hash` differs (`rebuild.py:371-373`).

Separate unit tests for `wiki_build.enforce_date_tokens` (round-trip guard, malformed
detection — research 03 §3) and `entity_index.rebind` (binds article_title, skips
private, no embeddings call) as `@pytest.mark.unit` where pure.

### 8.2 Frontend — `web/src/components/SuggestPanel.test.tsx` (copy `RebuildPanel.test.tsx`)

Recipe from research 05 §5a: `vi.mock("../api")` swapping `suggestStart`/`suggestTurn`
for `fakeStream`-backed scriptable fakes (`RebuildPanel.test.tsx:38-53`), JSON
endpoints (accept/reject) on MSW, `renderWithProviders` + `server`, `vi.stubGlobal`
for `confirm`/`alert`, scope footer queries to `.modal-foot`
(`RebuildPanel.test.tsx:108-123`). Net-new (research 05 §5a "gap to fill" — the guide
loop is currently untested):

- `session_ready` renders the BASE draft + backlink chips.
- A turn: optimistic user bubble appears immediately; `tool_use`/`tool_result` render
  via the **reused Stage-1 step UI**; `edit_applied` re-renders the draft + AI summary
  bubble + facts chips; a second turn works (multi-turn memory).
- `MarkdownDiff` toggles BASE↔draft.
- `rejected` edit shows the "couldn't apply" affordance.
- Accept → `acceptSuggest` posted; Reject → `rejectSuggest`.

### 8.3 e2e (`e2e/`, LLM faked at the boundary, `e2e/fake_llm.py`)

Warranted — this is a user-facing flow behind the API contract (CLAUDE.md DoD #2).
One Playwright spec: open "Suggest revisions" on a KB page, send one instruction, see a
tool step + an applied edit + the diff, Accept, confirm the page changed. The
`fake_llm` script returns a scripted `apply_edits` tool call (tools are faked at the
boundary, never a real key — CLAUDE.md DoD #5).

### 8.4 Coverage floors

Add new modules above their domain floor (`fail_under` in `server/pyproject.toml`,
`thresholds` in `web/vitest.config.ts`); when real coverage lands comfortably above,
**ratchet the floor up in the same PR** (CLAUDE.md DoD #3 — never lower a floor).
Google-style docstrings on every new function/route (CLAUDE.md docstring policy);
`cd server && ruff check app`.

---

## 9. Risks / edge cases (candid)

- **Transcript fragility (the headline risk).** C's *whole* defense is the two-transcript
  topology (§0): tool_use lives only in a disposable thinking-OFF transcript; the
  persisted `run.messages` is plain text. If a future change ever appends the
  fact-finding transcript to `run.messages` (e.g. to "show reasoning"), it reintroduces
  exactly research 01's hazard (`rebuild_engine.py:9-15`). **Mitigation:** the §8.1
  transcript-safety assertion is a hard regression guard, and a code comment at the
  `run.messages.append` site documents the invariant. This is the most important thing to
  get right and the easiest to regress — be candid: C carries more topology risk than a
  tool-less stance.
- **Tool-call loops / cost.** An agent can loop search→read→search. Cap with
  `_TURN_MAX_ITER` (mirror `_GATHER_MAX_ITER=5`, `rebuild_engine.py:50`) and
  `_TURN_MAX_TOKENS` (mirror `_GATHER_MAX_TOKENS=1500`, `:51`); on hitting the cap,
  force-finish with whatever edits exist + a "ran out of lookups" note. Each turn is two
  transcripts (fact-finding + the cheap distillation), so a chatty session costs more than
  a tool-less stance — though *less* per turn than today's full-rewrite Guide
  (`rebuild_engine.py:455-460` regenerates the entire body every turn, research 01 §6).
- **Latency.** Tool round-trips add wall-clock vs. a single edit turn. `search_notes`
  runs ONNX inference (CPU-bound) — already offloaded via `asyncio.to_thread`
  (`rebuild_engine.py:176`); reuse it so the event loop isn't blocked. Stream
  `tool_use`/`assistant_delta` so the UI shows progress (keepalive every 15s,
  `rebuild.py:90-99`).
- **Firewall (must not leak private notes into a public article).** `search_notes`
  already excludes `kb/` hits (`rebuild_engine.py:178`); `_dispatch_tool` must
  additionally refuse `read_source`/`read_backlink` on private/Reference notes when the
  **target page is public** (§4.2), reusing `is_private_title`/`domain_for_title`
  (`wiki_guides.py:148,103`). `add_links_to_content` self-guards the target
  (`wiki_build.py:733`). This is a hard invariant (research 04 O5/§5, research 03 §4) —
  tested in §8.1.
- **Staleness on long conversations.** Over many turns the working draft drifts from
  BASE and the persisted summaries grow. The persisted transcript is *summaries*, not
  full drafts, so it grows slowly (cheaper than rebuild's growing full-draft transcript,
  research 01 §6). The actual `run.draft` is always the source of truth that edits apply
  to, so there's no "transcript's notion of the draft" drift (research 01 §10 risk) — the
  server re-derives nothing from the transcript. Accept still guards on the **live page**
  hash (`rebuild.py:371`), unaffected by draft drift.
- **Edit anchor non-uniqueness.** A `find` that matches 0/>1 places is **rejected, not
  guessed** (§1.3) — surfaced to the owner. This is a feature (safety) but can frustrate
  on repetitive text; the agent can re-phrase with a longer anchor.
- **One run per slug** (`rebuild_runs.py:53,91-93`): suggest and classic rebuild can't be
  live simultaneously on a page. UI must reflect it (disable the other entry while one is
  open).

---

## 10. Sequencing / effort & whether to ship a non-tool fallback first

**Yes — ship a non-tool fallback first.** The cleanest de-risking is to land the loop,
the BASE-preservation, the deterministic `_apply_edits`, and the full hardening tail
**without tools**, then add the tool agent on top. This sequences the *transcript-safety*
work (which is identical with or without tools — plain `run.messages`) ahead of the
riskier tool topology, and gives a shippable product at PR 2.

| PR | Scope | Tier(s) touched |
|---|---|---|
| **PR 1 — shared hardening (no UI).** `wiki_build.enforce_date_tokens` (research 03 A+D+B), `entity_index.rebind` (research 04 O1), `wiki_build.promote_one` (research 02 #4) factored from `wiki_build.yaml` steps; wire `promote_one` into the **existing** rebuild Accept (`rebuild.py:374`) for immediate parity win. Factor `{date_rules}`/`{crosslink_rules}` prompt fragments. | back, unit; flows (prompt frags) |
| **PR 2 — suggest engine, tool-LESS.** `suggest_engine.run_start`/`run_turn` with **no** `_EDIT_TOOLS` — the turn asks the model (one plain turn, thinking-off) for an `apply_edits`-shaped JSON via a single `complete`-style call; `_apply_edits` + `_harden`; `RebuildRun.kind/base_draft/backlinks`; router; `SuggestPanel` + `api.ts`; backlinks loaded read-only. Full backend+frontend tests. **Shippable.** | back, front |
| **PR 3 — tool agent.** Add `_EDIT_TOOLS` + the disposable fact-finding transcript loop (§0/§1), `_dispatch_tool`, firewall gates, `tool_use`/`tool_result`/`assistant_delta` streaming, the frontend tool-step reuse + facts panel. The transcript-safety assertion suite. | back, front |
| **PR 4 — e2e + ratchet.** Playwright spec (§8.3), coverage-floor ratchets, docstring/ruff pass. | e2e |

**Effort:** PR1 ~1.5d (the three helpers + rebuild parity wiring + tests). PR2 ~2.5d
(new engine + panel, but reuses ~80% of rebuild plumbing). PR3 ~2.5d (the tool loop is
mechanically the gather loop, but firewall + transcript-safety tests are exacting).
PR4 ~1d. **~7.5 days.** If the tool stance proves too costly/latent in PR3 review, PR2
stands alone as a complete tool-less "Suggest revisions" mode — so C degrades gracefully
to a tool-less product without rework.

---

## Honest argument for C

C is the **only** stance that delivers the owner's stated intent literally — *"truth
seeking for salient facts; my input is for structure/formatting/correcting
assumptions."* A tool-less stance can only re-arrange the initially-curated sources; it
cannot go *find* a fact the owner brings up mid-conversation, which is precisely the
moment grounding matters most. C makes the truth-seeking **visible** (reused tool-step
UI + a facts panel showing claim→source), which builds the owner's trust that the
article is grounded, not confabulated. And because edits are *targeted and
deterministic* (`_apply_edits`), C is **cheaper per turn** than today's full-rewrite
Guide while being *more* grounded.

The honest cost: C carries the most moving parts and the most transcript-topology risk.
Its safety rests entirely on the two-transcript discipline (§0) — a discipline the
codebase already proves works for gather, but one that a careless future change could
break. The PR sequencing (§10) mitigates this by landing a complete tool-less product
first, so the tool agent is an *additive* layer, not a precondition for shipping. C is
the highest-ceiling stance with a clean fallback floor.
