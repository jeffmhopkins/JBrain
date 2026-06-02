# Research link — implementation plan (v2, post adversarial review)

A fourth share type: a **scoped, read-only Q&A link**. A recipient chats with an AI
that answers only from a fenced slice of the brain (e.g. "medical history only").

> **v2 incorporates a hostile security review.** Changes from v1 are marked **[REVIEW]**.
> Two items reopen earlier product decisions and are flagged **[DECISION NEEDED]**.

## Locked decisions (+ review-forced revisions)

| Area | Decision |
|------|----------|
| Scope | Filter by **folder-prefix** (+ optional kind). **[REVIEW]** drop free tag-based scope as a *primary* selector (tags are mass-applied by automations → silent exposure). Owner confirms the matched-note list at mint. |
| Scope growth | **Live + auto-pause (D2).** The set is re-evaluated each query, but the link **auto-pauses and re-prompts the owner** when its matched count grows by > N since last confirmation. |
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
`scope_json` `{ prefixes:[], kinds:[] }`, `last_confirmed_count` (for F9 auto-pause),
`persona_voice` (nullable → default), `intro`, `bind`, `single_use`, `max_turns`,
`max_total_replies`, `token_budget`, expiry.

**`research_sessions`** (mirror `guided_sessions`): `secret` cookie, `transcript_json`,
`retrieved_note_ids_json` (audit), `denied_count` (audit of out-of-scope attempts),
`turn_count`, status.

## Scope resolution — the security core (built & tested FIRST)

`scope.py` (new), pure + unit-tested:
```
def scoped_ids_subquery(scope) -> (sql, params)   # "(SELECT id FROM notes WHERE ...)"
def scoped_note_ids(conn, scope) -> set[int]
def matched_count(conn, scope) -> int             # for the F9 growth guard
```
Predicate: `deleted_at IS NULL AND kind IN (...) AND (title LIKE 'prefix/%' OR title = 'prefix' for each prefix)`. Excludes system/secret notes. **Reject empty/`/`/root prefixes (F10).**

- **FTS (works, ships v1):** `... WHERE notes_fts MATCH ? AND n.id IN <scoped subquery> ORDER BY rank LIMIT ?` — the scope filter composes **before** LIMIT (`search.py:33-40`), so it's sound.
- **Semantic (D1 — exact, in-process):** do NOT use vec0 KNN (it's global; `vec_notes`/`vec_chunks` have no partition key, `db.py:84-90`, so an outer `id IN (scope)` only deletes rows → a small scope yields zero results, F1). Instead: scoped `SELECT note_id, embedding FROM vec_notes WHERE note_id IN <scoped subquery>`, deserialize the stored float32 vectors (numpy), and compute cosine vs. the query embedding in-process; top-k. Exact (100% in-scope recall), read-only-safe, no schema change. **Cap scope size** (e.g. ≤ a few thousand vectors) and cache the scoped matrix per session keyed by a scope+watermark hash so it isn't refetched each turn; invalidate on the F9 growth tripwire.
- **`read_note` (scoped):** resolve title → id, then assert id ∈ `scoped_note_ids`; else deny + bump `denied_count`. Never auto-expand `[[links]]`.
- **Attachments (scoped):** only attachments whose `note_id ∈ scope`.
- **Preview endpoint:** returns matched titles + count for the owner to confirm at mint and any time after.

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
mint (draft) · preview scope (titles + count) · set persona/intro/options · activate ·
list research links + sessions · view session (transcript + retrieved + denied) · revoke.

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
5. Audit (session view, retrieved + denied) + revoke + global-cap meter + F9 auto-pause.
6. Docs.

## Adversarial test matrix (gate for steps 1–2)

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
- **D2 — scope growth:** **live + auto-pause.** Re-evaluate each query; store
  `last_confirmed_count`; when `matched_count` grows by > N, auto-pause the link and notify the
  owner to re-confirm. Folder-prefix scope (tags dropped as a primary selector).

## Deferred

Redaction within scope (framing makes scope the control); demo mode (none exists in code);
per-link spend dashboards beyond the global meter.
