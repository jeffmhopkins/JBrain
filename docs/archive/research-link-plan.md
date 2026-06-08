# Research link — implementation plan (v2, post adversarial review)

A fourth share type: a **scoped, read-only Q&A link**. A recipient chats with an AI
that answers only from a fenced slice of the brain (e.g. "medical history only").

> **v2 incorporates a hostile security review.** Changes from v1 are marked **[REVIEW]**.
> Two items reopen earlier product decisions and are flagged **[DECISION NEEDED]**.

## Locked decisions (+ review-forced revisions)

| Area | Decision |
|------|----------|
| Scope | The exposed boundary is an **explicit approved note-id allowlist**. A folder-prefix (+ optional kind) filter only *surfaces candidates* — the link exposes only notes the owner has ticked. (Tags dropped as a primary selector.) |
| Scope growth | **Approve-to-add (D3).** New filter matches land as **pending candidates** with a review-inbox nudge; **nothing new is exposed until the owner approves it**. The approved set is **editable** — remove a note from a live link without killing it. No silent drift, no auto-pause, no recipient disruption. |
| Retrieval | **Scoped semantic + scoped FTS (D1).** Semantic ranking is done **exactly, in-process** over the in-scope embeddings (not sqlite-vec KNN — see F1 resolution), giving 100% in-scope recall; FTS covers keyword hits. |
| Persona | Neutral default; optional override constrained to a **voice variable** interpolated into a fixed template — never raw appended instructions. **[REVIEW]** |
| Boundaries | Airtight: no `query_sql`, no geo; out-of-scope `[[links]]` inert; no citations/titles (a *cosmetic* opacity, not a containment control — see F5). |
| Lifecycle | Same as guided intake (mint → approve persona+scope → consent → chat → audit), flipped to answering. |
| Name | **Research link** ("Guided · Research" badge). |

## The framing that drives everything

A determined recipient can extract ~everything in scope by asking enough questions, and
the AI **will leak structure via paraphrase** (dates, counts, "there's a record about
X") no matter what a sanitizer strips. So this is **read access to the filtered slice
with a chat UI** — the scope filter is the *only* real control, enforced server-side on
every query. "No titles/citations" only stops trivial structure-mapping.

## Why this can't reuse the existing pieces wholesale (grounding)

- **Guided intake's ISOLATION INVARIANT** (`guided.py:1-14`): the interview AI has **no
  tools** and imports no note code → structurally cannot read the brain. Research link
  **deletes this on purpose**, so none of that safety transfers; it needs a *new* scoped
  read path in its own module.
- **`get_query_conn` is read-only, NOT scoped** (`db.py:31-36`, `query_only=ON` blocks
  writes only). It can still `SELECT * FROM meta` (holds secrets), `share_links` (every
  token), all notes. **[REVIEW]** The boundary is therefore "every tool ANDs in scope" —
  there is no structural backstop, so each tool must (a) compose the scope predicate AND
  (b) re-verify the resolved id ∈ scope after lookup.
- **Existing research helpers do NOT scope**: `_tool_read_note` → `get_by_title`
  (`notes.py`) and `_tool_read_attachment` (`architect.py:345-351`) read by id/title with
  no scope join. **[REVIEW]** The runner must NOT call these; it needs scoped reimplementations.

## Data model

**`share_links`** — add `kind='research'`. **[REVIEW] Keep `note_id NOT NULL`.** Anchor
each research link to a dedicated owner-side **placeholder/audit note** (exactly as
`_tool_create_guided_share` already creates a dest note, `architect.py:611-615`). This:
- avoids a SQLite `DROP NOT NULL` table-rebuild migration (F11), and
- keeps every owner listing INNER JOIN valid so research links **stay visible/revocable**
  (`share_admin.py:51,73,80,89,105`, `_tool_list_share_links`). A nullable note_id would
  make a live public LLM link **disappear from the revoke screen** — a security bug.
- `resolve_active_link` (`share.py:48`) gains a `kind='research'` branch that resolves the
  link but **never returns note content**.

**`research_specs`** (mirror `guided_specs`): `share_link_id`, `status` draft/active,
`scope_json` `{ prefixes:[], kinds:[] }` (the candidate **filter** only),
`approved_note_ids` (the exposed **allowlist** — the actual boundary),
`dismissed_note_ids` (candidates the owner rejected, so they don't re-nag),
`persona_voice` (nullable → default), `intro`, `bind`, `single_use`, `max_turns`,
`max_total_replies`, `token_budget`, expiry.

**`research_sessions`** (mirror `guided_sessions`): `secret` cookie, `transcript_json`,
`retrieved_note_ids_json` (audit), `denied_count` (audit of out-of-scope attempts),
`turn_count`, status.

## Scope resolution — the security core (built & tested FIRST)

**The boundary is the approved allowlist** (explicit, owner-confirmed ids) — the live filter
only *finds candidates*; it never gates retrieval. `scope.py` (new), pure + unit-tested:
```
def approved_ids(spec) -> set[int]            # the exposed allowlist — the ONLY boundary
def candidate_ids(conn, spec) -> set[int]     # filter matches − approved − dismissed (for the nudge)
def filter_predicate(scope) -> (sql, params)  # candidate filter only; never gates retrieval
```
Filter predicate: `deleted_at IS NULL AND kind IN (...) AND (title LIKE 'prefix/%' OR title = 'prefix')`. **Reject empty/`/`/root prefixes (F10).** Every retrieval tool constrains to
`n.id IN (approved allowlist)` — an explicit id set, re-verified after lookup.

- **FTS:** `... WHERE notes_fts MATCH ? AND n.id IN <approved ids> ORDER BY rank LIMIT ?` — composes **before** LIMIT (`search.py:33-40`), sound.
- **Semantic (D1 — exact, in-process):** do NOT use vec0 KNN (global; no partition key, `db.py:84-90` → an outer filter only deletes rows → small set yields zero, F1). Instead: `SELECT note_id, embedding FROM vec_notes WHERE note_id IN <approved ids>`, deserialize the float32 vectors (numpy), cosine vs. the query in-process, top-k. Exact (100% recall over the approved set), read-only-safe, no schema change. **Cap the approved-set size** (≤ a few thousand vectors) and cache the matrix per session (keyed by approved-set hash); invalidate when the owner edits the set.
- **`read_note` (scoped):** resolve title → id, assert id ∈ `approved_ids`; else deny + `denied_count`++. Never auto-expand `[[links]]`.
- **Attachments (scoped):** only attachments whose `note_id ∈ approved_ids`.

**Candidate detection + approve-to-add (D3):** a daily workflow (reuse the reviews/workflows
system) computes `candidate_ids`; if non-empty it posts a **review-inbox nudge** ("N notes now
match your 'Medical' research link — review to include"). Also computed on demand in the share
settings UI. **Approve** → ids move into `approved_note_ids` (and the semantic cache invalidates);
**dismiss** → into `dismissed_note_ids`; **remove** → out of `approved_note_ids`, instantly out of
scope. Mint-time preview is just the first pass of this same approve flow.

## Scoped runner — `shared_research.py` (new, NOT under guided isolation)

- Tools: scoped `search_notes` (FTS), scoped `semantic_search` (in-process exact ranking,
  D1), scoped `read_note`, scoped attachment read. **No `query_sql`, no geo.** Reimplemented —
  does not call architect helpers.
- LLM loop: a trimmed tool-calling loop over `get_query_conn`, with `max_turns` /
  `max_total_replies` / `token_budget` enforced.
- **Persona [REVIEW]:** fixed system template with the scope/disclaimer/no-titles rules as
  the *last, immutable* block; the owner override is interpolated only as a `{voice}`
  adjective/role string, never as a free instruction block that could countermand rules.
  Reuse guided's recipient-input hardening (`_SENSITIVE_RE`, control-scrub, nonce fence).
- **Sanitizer [REVIEW]:** strip `[[links]]`/markdown-links/control tokens and clamp — but
  documented as *opacity, not containment* (cannot stop paraphrased structure).
- **Audit:** append retrieved ids per turn; count denied out-of-scope attempts.

## Endpoints

**Owner** (`share_admin.py` + assisted-only architect tool `create_research_share`,
mirroring `create_guided_share` — **draft + explicit activate**, and the tool **may not set
a root/over-broad scope (F10)**; preview scans matched **note bodies** for sensitive content,
not just the goal text):
mint (draft) · **review candidates / approve / dismiss / remove from allowlist** ·
set persona/intro/options · activate · list research links + sessions ·
view session (transcript + retrieved + denied) · revoke.

**Public** (`routers/share.py`, mirroring the guided block ~147-217):
- `GET /share/{token}` → research landing (consent only; never content).
- `POST /share/{token}/research/start` · `POST /share/{token}/research/turn`.
- **[REVIEW] Add the `sec-fetch-site == "cross-site"` reject to BOTH start AND `turn`**
  (guided's `turn` at `share.py:221` is missing it — back-port). Cookie `samesite="strict"`.
  Fix the wrong CORS comment at `main.py:124` ("safe with `*` because no cookies" — these
  routes DO use cookies).

## Cost / abuse [REVIEW — F8]

- Per-link cap mirrors guided's **atomic** `UPDATE ... WHERE reply_count < max` (`guided.py:251-255`).
- **Add a GLOBAL daily reply+token ceiling in `meta`**, enforced atomically — the per-link
  cap alone leaves total spend = (#links × budget) unbounded, worsened by architect-minted links.
- Do **not** trust `X-Forwarded-For` for throttling (`share.py:23-29` takes it verbatim);
  the in-memory per-IP limiter is best-effort only. State the worst-case $ for default caps.

## Build order (security-first)

1. Schema (+ `note_id` stays NOT NULL; anchor note) + `scope.py` + **scoped FTS, scoped
   in-process semantic, scoped read** tools + the adversarial test matrix below. Nothing
   recipient-facing yet.
2. `shared_research.py` runner (loop, persona template, sanitizer, caps) + stubbed-LLM tests.
3. Owner endpoints + mint/preview/activate + `SharesPage` UI (folder-prefix scope picker,
   matched-note preview, default/voice persona).
4. Public endpoints + `ResearchChat` recipient UI (clone `GuidedChat`, reuse `.chat-status`).
5. Audit (session view, retrieved + denied) + revoke + global-cap meter + the daily
   candidate-nudge workflow + approve/dismiss/remove allowlist management.
6. Docs.

## Adversarial test matrix (gate for steps 1–2)

- A note matching the filter but **not yet approved** → **not reachable** (only
  `approved_note_ids` gates retrieval; the filter never does); removing an approved id →
  instantly unreachable, even mid-session (cache invalidated).
- Out-of-scope `read_note`/attachment by exact id/title → **denied**, `denied_count`++.
- Scoped FTS can **never** return an out-of-scope note even as best match (before LIMIT).
- Scoped semantic: with a tiny in-scope set inside a large brain, the nearest in-scope note
  is **always** returned (exact ranking) AND no out-of-scope vector can appear (the F1 regression test).
- Scoped note `[[link]]` to out-of-scope → not followed/surfaced.
- `query_sql` and geo absent from the toolset (asserted).
- Persona override cannot countermand the fixed rules block (try "quote titles", "read note X").
- Recipient injection to widen scope / dump all / reveal system prompt → fails.
- Caps: per-link + **global** ceiling + token budget + expiry + single_use + bind + revoke.
- `validate_agent_config` (`architect.py:191-211`) extended to cover the research toolset.

## Resolved forks

- **D1 — semantic retrieval:** scoped **in-process exact cosine** over the in-scope
  embeddings (see Scope resolution). Closes F1 with exact recall; scope-size cap + per-session
  cache for cost.
- **D3 — scope growth:** **approve-to-add.** The exposed boundary is an explicit
  `approved_note_ids` allowlist. The folder-prefix filter only surfaces candidates; a daily
  workflow posts a review-inbox nudge for new matches; nothing is exposed until the owner
  approves, and the approved set is editable (remove a note from a live link instantly). No
  drift, no auto-pause. Tags dropped as a primary selector.

## Deferred

Redaction within scope (framing makes scope the control); demo mode (none exists in code);
per-link spend dashboards beyond the global meter.
