# Source-of-Truth Corrections (Talk → Truth Layer) — Plan

**Status:** Phase 1 implemented on `claude/tokbox-source-truth-analysis-ZjoNq` (Phase 2 pending)
**Author:** Knowledge Architect (AI), best-of-N synthesis + adversarial review
**Example article:** `kb/.../TokBox` (the feature is general; TokBox is the motivating case)
**Schema version:** 44 → 45

> **Implementation status (Phase 1 — shipped on this branch):** the deterministic
> **"Correction" kind**, promotion to a dated truth note, the `is_correction`/`source_note_id`
> columns (migration 45), `maintain_batch` routing via `source_note_id`, the `wiki_maintain`
> "authoritative correction" prompt rules, rename/merge talk rekey, TalkPanel badge + kind, and
> tests (`server/tests/test_corrections.py`, 5 passing). Open items C/D/E took their recommended
> defaults: **C** entity healing → Phase 2; **D** full-rebuild routing → eventually-consistent;
> **E** → dedup identical open corrections. **Phase 2 (durable entity healing) is not yet built.**

---

## 1. The idea, in your words

> On a wiki article, the AI keeps track of things that need addressing. When I reply with
> a to-do / comment / question, it can *sometimes* be a **source of truth** (e.g. "the name
> is spelled X"). Next maintenance, the article should be rewritten with that truth. But I'm
> not sure **where** that source of truth gets stored. I think we should keep the paradigm
> that **notes are truth**, make a **new daily note** with the talk, and have the **talking
> point link that note** to the reply.

That instinct is correct, and this plan implements exactly it — with the sharp edges found
during review designed out.

---

## 2. Feasibility analysis

### 2.1 What already exists (you're not starting from zero)

- **`article_talk` table** (`server/app/schema.sql:164`) + **TalkPanel UI**
  (`web/src/components/TalkPanel.tsx`, rendered for `kind='kb'` at `NotePage.tsx:165`).
  You can already attach a **note / directive / question / todo** to any article. Stored
  *beside* the article, keyed by `article_title` — **not** in the article body, **not** a note.
- **Maintenance already consumes talk.** `wiki-maintain` (cron 3am) selects only articles that
  got a **new** talk item since a watermark (`maintain_batch`, `wiki_build.py:1207`) and applies
  the open items (`directive`=apply, `conflict`=reconcile, …) via `maintain_one`
  (`wiki_build.py:485`), guided by the `actions.wiki_maintain` prompt
  (`prompts.yaml:~787`, including its **"SUPERSEDE, don't accumulate"** rule).
- **The truth/derived split is real and enforced.** `wiki_build.py:16` — *"Raw source notes
  stay the ground truth."* KB articles and `note_analysis` are derived; nothing feeds back
  into raw notes. This is the invariant the whole system rests on.

So today, when you "submit a talking point," it lands in `article_talk` — **a metadata side
table outside the truth layer.** That is precisely the gap you intuited.

### 2.2 Why storing a correction only in `article_talk` is fragile

1. **Title-keyed, so renames orphan it.** `article_talk.article_title` is a string, not a FK; a
   rename / merge / split silently disconnects the correction.
2. **The underlying entries still say the wrong thing.** Search, entities, and **every other
   article** derived from those entries stay wrong.
3. **It's a standing nag, not a fact.** The maintenance AI must re-apply the directive forever
   instead of the truth simply *being* true; a full rebuild (which reads from notes, not talk)
   would lose it.

Promoting a correction into a real **note** (your "new daily note") and linking the talk item
to it fixes all three — it's the architecturally consistent move.

### 2.3 Verdict

**Feasible and well-aligned**, reusing existing machinery (dated-note capture, the maintenance
pass, the talk panel). Three things that look like one-liners are **not**, and were verified
against the code during review (§6): routing, entity healing, and dedup. The plan below is
designed around those realities.

---

## 3. Your four locked decisions (and how this plan honors them)

| # | Your decision | How it's honored | Caveat surfaced |
|---|---------------|------------------|-----------------|
| 1 | **Always automatic** (no manual button) | Auto-promotes on submit via a dedicated **"Correction" kind** — no separate "promote" step | **Resolved (owner):** only the "Correction" kind promotes; todos/questions/notes never become truth notes. |
| 2 | **New dated daily note** | A `kind='entry'` note under `notes/YYYY/MM/DD/NN` via `next_dated_title` + `upsert_note` | None — implemented as-is. |
| 3 | **Also correct source entries / entities** | Entities heal durably; the new note supersedes old entries by recency | **Resolved (owner): source entries are never modified.** Propagation is recency-supersede only; raw captures stay immutable ground truth. |
| 4 | **Keep flat** (reuse `article_talk`) | No threading; two new columns on the existing table | None. |

---

## 4. How the design was produced (method)

1. **Mapped the system** (schema, the wiki build/update/maintain pipelines, the talk service,
   note creation, the entity index).
2. **Best-of-N:** three independent specs were drafted from an identical grounded brief.
3. **Adversarial review:** a red-team pass attacked all three against the real code. It found
   **three blockers shared by all candidates** (§6). I then re-verified each blocker in source
   myself before adopting the fixes.

The result is the synthesis below — not any single candidate.

---

## 5. The design

### 5.1 Trigger — deterministic "Correction" kind (owner-confirmed)

Add **"Correction"** to the Talk submit affordance (`ADD_KINDS`, `TalkPanel.tsx:9`). When you
pick it and submit, the item is promoted **unconditionally** — no LLM on the request path, no
false negatives that silently drop a real correction, no added latency. **Only** the "Correction"
kind promotes; `note` / `directive` / `question` / `todo` behave exactly as today. Tagging "this
is a correction" is also a *more* reliable source-of-truth signal than an AI guess, which matters
because we are writing to the truth layer and healing entities.

### 5.2 Data model — migration 44 → 45

Two columns on `article_talk` (no new tables):

```sql
ALTER TABLE article_talk ADD COLUMN is_correction  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE article_talk ADD COLUMN source_note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_article_talk_source_note
  ON article_talk(source_note_id) WHERE source_note_id IS NOT NULL;
```

- `is_correction=1` marks a promoted correction; `source_note_id` is the FK to the truth note.
- `ON DELETE SET NULL`: deleting the note keeps the talk item as a historical record.
- Migration block goes at the end of `_run_migrations()` after the `current < 44` block
  (`db.py:~633`), using the existing `_add_column` helper; bump `SCHEMA_VERSION` to 45
  (`db.py:107`); mirror the columns into `schema.sql:164` so fresh DBs match. *(Migration shape
  verified correct against `db.py`.)*

### 5.3 Promotion flow

New module `server/app/services/corrections.py`, called from the existing talk endpoint
(`routers/notes.py:206`, which is owner-authenticated via `dependencies=[CurrentUser]` — verified):

1. Owner submits `POST /api/notes/{slug}/talk` with `kind="correction"`.
2. `article_talk.add(...)` inserts the talk row (author `user`).
3. **Dedup guard (required — see §6, F9):** skip promotion if an *open* `is_correction=1` row
   with the same normalized body already exists. `add()` does **not** dedup (only `record()`
   does, and the router calls `add()`), so without this guard each re-submit spawns a duplicate
   truth note.
4. Create the dated note:
   `title = next_dated_title(conn, clock.today_local())`,
   body = `"CORRECTION (source of truth): {body}\n\nApplies to [[{article_title}]]\n"`,
   `upsert_note(conn, title, body, source="user", kind="entry", fire_events=False)`.
5. `UPDATE article_talk SET is_correction=1, source_note_id=? WHERE id=?`.
6. `conn.commit()`, **then `notes_svc.flush_entry_events(conn)`** so analysis/auto-tag fire
   (fixes F14 — all candidates forgot this).

The `[[article]]` wikilink in the body is kept for **backlinks/display only** — it is **not**
the routing mechanism (see §5.4).

### 5.4 Routing — the central fix (do not trust the wikilink)

**The naïve approach is broken** (§6, F1, verified): `_articles_citing(note_id)`
(`wiki_build.py:1283`) returns articles that link *to* the note. A correction note links
*from* itself *to* the article, so it returns ∅; the note lands in `orphans`, never in
`maintain_one`.

**The fix is deterministic and needs no request-path LLM.** The article is *already* re-selected
by the nightly `wiki-maintain` pass because a new talk item was added (watermark query,
`wiki_build.py:1233`). Change `maintain_batch` so that for each article it works, it gathers the
`source_note_id`s of that article's open `is_correction=1` talk items and passes them as
`extra_source_ids` to `maintain_one` (which already accepts that parameter — `wiki_build.py:486`,
used by `update_batch` at `:1378`). The correction note is then loaded as a NEW/CHANGED source
(`new_srcs = _load_sources(...)`, `wiki_build.py:523`).

This is strictly better than the review's own suggestion of calling `maintain_one` synchronously
at promotion time, which would put a heavyweight LLM call back on the POST path — the very thing
we removed by dropping the classifier.

**Full-rebuild durability (residual):** `wiki-build` (full reorg) routes notes via the entity
roster + `note_ids_for_name(subject)`. Because the correction note names the subject, it will be
assigned to the article once analyzed — *eventually consistent*. Flagged for validation (§7,
Decision D); the day-to-day path (update/maintain) is exact.

### 5.5 Applying the correction to the article — prompt change

Add one paragraph to `actions.wiki_maintain` in `prompts.yaml`, right after the SUPERSEDE block
(`~:819`):

```
CORRECTION NOTES: a source note whose first line begins "CORRECTION (source of truth):" is an
owner-verified fact. Treat it as AUTHORITATIVE — apply it and let it override any conflicting
claim in the article or in other sources. Replace the wrong value everywhere; never leave the
old and new values both standing. Cite the correction note where the corrected fact appears.
```

Also surface the flag to the model: `article_talk.open_for` should return `is_correction`, and
`maintain_one`'s item rendering should tag those rows. No `wiki_write` change. The existing
**SUPERSEDE-by-recency** rule already makes a newer correction outrank older sources — including
two competing corrections (the later-dated one wins; a genuine standoff is recorded as a
`conflict` item for you).

### 5.6 Truth-layer propagation — entities heal, raw entries are NOT mutated

**Raw entries are never modified (owner-confirmed)** — this honors `wiki_build.py:16`.
Propagation to the article is via recency-supersede (§5.5). Propagation to *other* articles
happens because the correction note is a normal entry the pipeline already routes by entity.
This satisfies decision #3's intent without editing historical captures. There is no
source-entry annotation mode, by default or behind a flag.

**Entity healing is real work, not free** (§6, F3/F4/F5, verified):

- `rebuild()` recomputes an entity's display name from `raws.most_common` (`entity_index.py:179`),
  so one new note **cannot** outvote many old mentions — A's "free healing" is false.
- `normalize()` strips non-`[a-z0-9]` (`entity_index.py:22`), so **"Bjørn" → `bj rn`** but
  **"Bjorn" → `bjorn`** — *different keys, never merged.* The diacritic example is exactly the
  case the index cannot represent today, and any direct `UPDATE entities SET canonical_name` is
  reverted on the next nightly rebuild.

**Durable fix:** add an owner-override that `rebuild()` consults at display time — e.g. an
`entities.canonical_override TEXT` column (or a small `entity_overrides` table) that wins over
the frequency-derived display, plus registering the wrong form as an alias so search still
resolves it. This is a schema + `rebuild()` change, scoped to **Phase 2** (§8). MVP ships article
correctness; entity healing follows.

### 5.7 Rename / merge / split

Take the verified A/C fix: add `UPDATE article_talk SET article_title=? WHERE article_title=?` to
`recategorize_article` (`wiki_build.py:962`) and `merge_articles` (`wiki_build.py:989`). The
note-body `[[link]]` is already rewritten by `_rename_inbound_links` (`notes.py:66`). Split:
talk stays with the parent (documented limitation).

### 5.8 UI

- `article_talk.list_for` (`article_talk.py:124`) gains a `LEFT JOIN notes n ON n.id =
  t.source_note_id` to return `is_correction` and `source_note_slug`.
- `TalkPanel.tsx` `Talk` interface gains `is_correction?` and `source_note_slug?`; promoted
  items render a **"✓ truth note"** badge linking to the note (or "note deleted" if the FK is
  NULL). Add **"Correction"** to `ADD_KINDS`.

### 5.9 Idempotency, undo, dedup

- **Dedup:** the §5.3 guard prevents duplicate truth notes on re-submit (F9 fix).
- **Undo:** soft-delete the note → `ON DELETE SET NULL` keeps the talk record; the correction
  stops superseding on the next maintain. Because no raw entry was mutated, there is nothing
  else to roll back. (Entity overrides, Phase 2, get a matching revert.)
- **Idempotency:** promotion runs once at submit; no retry loop.

### 5.10 Security

No change needed — `/talk` is on the owner-only router (`dependencies=[CurrentUser]`,
`notes.py:12`); the looser `/entry` auth is a different router (verified).

---

## 6. Adversarial review — blockers found (and designed out)

All three naïve specs shared these; each was re-verified in source before adoption.

| # | Flaw (verified) | Severity | Resolution in this plan |
|---|-----------------|----------|--------------------------|
| F1 | `[[wikilink]]` does **not** route the note — `_articles_citing` is reverse-direction (`wiki_build.py:1283`) | **blocker** | Route via `maintain_batch` passing `source_note_id` as `extra_source_ids` (§5.4) |
| F2 | Entity-based routing needs `note_analysis` first; fresh note → `orphans` | **blocker** | Same as F1; `flush_entry_events` so analysis runs (§5.3) |
| F3 | "Free entity healing by frequency" is false (`entity_index.py:179`) | **blocker** | Sticky override in `rebuild()`, Phase 2 (§5.6) |
| F4 | Diacritic/spelling fixes **fork** the entity key (`entity_index.py:22`) | **blocker** | Alias registration + override; Phase 2 (§5.6) |
| F5 | Direct `UPDATE entities` reverted by nightly rebuild | major | Override consulted at display time (§5.6) |
| F6 | Mutating raw entry `content_md` violates `wiki_build.py:16` | **blocker (design)** | Recency-supersede; **source entries never modified** (§5.6) |
| F7 | LLM classifier contradicts "always automatic" and can silently drop a correction | major | Deterministic "Correction" kind (§5.1) |
| F8 | Gating on `kind=='directive'` misses corrections filed as note/question | major | Dedicated kind, not kind-sniffing (§5.1) |
| F9 | `add()` does **not** dedup (only `record()` does); re-submit → duplicate notes | major | Explicit dedup guard (§5.3, §5.9) |
| F14 | `fire_events=False` with no `flush_entry_events` → analysis never fires | minor | Flush after commit (§5.3) |
| — | Auth (`notes.py:12`) and migration 44→45 shape — **correct as proposed** | n/a | Adopted unchanged |

---

## 7. Open decisions needing your sign-off

**Decision A — Trigger mechanism. ✅ Resolved (owner): one-tap "Correction" kind only.**
Automatic on submit, deterministic, zero request-path LLM, no silently-dropped corrections.
Other talk kinds never promote.

**Decision B — Source-entry handling. ✅ Resolved (owner): never modify source entries.**
Propagation is recency-supersede only; raw captures stay immutable ground truth. No annotation
mode is built (not by default, not behind a flag).

**Decision C — Entity healing scope.** Durable healing needs a `rebuild()` change (sticky
override + alias). Ship it in **Phase 2** (recommended), or pull into MVP at higher cost.

**Decision D — Full-rebuild routing.** Day-to-day (update/maintain) routing is exact; full
*reorg* routing is eventually-consistent via entity assignment. Accept as-is, or add an explicit
article↔correction-note association for rebuild (small extra work).

**Decision E — Dedup behavior.** Recommended: dedup identical open corrections (one truth note).
Confirm you don't instead want every re-submit to log a fresh note.

---

## 8. Phased plan & effort

**Phase 1 — MVP (≈3–4 days): article correctness end-to-end.**
Migration (2 cols + index); `corrections.py` (promote + dedup guard); `add_talk` wiring +
`flush_entry_events`; `maintain_batch` routing fix; `wiki_maintain` prompt paragraph +
`open_for`/item rendering; `list_for` JOIN; TalkPanel badge + "Correction" kind; rename/merge
talk rekey; tests.

**Phase 2 — durable entity healing (≈3–5 days):** `entities` override column (or
`entity_overrides`); `rebuild()` honors it at display time; alias registration of the wrong
form; revert path; tests for diacritic/spelling forks.

**Phase 3 — optional polish:** instant promotion feedback in the panel; competing-correction
surfacing; explicit full-rebuild association.

---

## 9. Testing

New `server/tests/test_corrections.py`:
- promote creates a dated `kind='entry'` note with the `CORRECTION` header + `[[article]]`;
- talk row gets `is_correction=1`, `source_note_id`;
- **dedup guard**: re-submitting the same correction does **not** create a second note (tests the
  guard, *not* a false belief that `add()` dedups);
- `maintain_batch` passes the correction note as `extra_source_ids` and the article adopts the
  corrected value (LLM mocked / smoke-tested);
- `list_for` returns `is_correction` + `source_note_slug`;
- rename rekeys `article_talk.article_title`;
- soft-deleting the note NULLs the FK, talk record survives.
Phase 2 adds: entity override survives a `rebuild()`; wrong form resolves via alias.

---

## 10. Rollout

- Migration is two `ADD COLUMN`s with safe defaults — zero downtime, idempotent via `_add_column`.
- Feature flag (meta key or env, e.g. `corrections:enabled`, default on) to disable promotion.
- No backfill; applies to talk items created after deploy. `prompts.yaml` hot-reloads, so the
  prompt paragraph takes effect with no restart.

---

## 11. File-change map

| File | Change |
|------|--------|
| `server/app/schema.sql:164` | add `is_correction`, `source_note_id` to `article_talk` (+ index) |
| `server/app/db.py:107,~633` | `SCHEMA_VERSION=45`; `current < 45` migration block |
| `server/app/services/corrections.py` | **new** — promote + dedup guard |
| `server/app/routers/notes.py:206` | `add_talk` calls `corrections.maybe_promote`, flush events |
| `server/app/services/article_talk.py:124,141` | `list_for` JOIN; `open_for` returns `is_correction` |
| `server/app/services/wiki_build.py:1207,485` | `maintain_batch` passes correction `extra_source_ids`; item rendering tags corrections |
| `server/app/services/wiki_build.py:962,989` | rekey `article_talk.article_title` on recategorize/merge |
| `server/app/services/entity_index.py:164` | **Phase 2** — sticky override in `rebuild()` |
| `prompts.yaml:~819` | `wiki_maintain` CORRECTION NOTES paragraph |
| `web/src/components/TalkPanel.tsx:4,9,~45` | interface + "Correction" kind + badge/link |
| `server/tests/test_corrections.py` | **new** |

---

## 12. Appendix — candidate comparison

| Dimension | A | B | C | Synthesis (this plan) |
|-----------|---|---|---|------------------------|
| Trigger | classifier, directives only | classifier, any kind | classifier, directives only | **deterministic "Correction" kind** |
| Routing | wikilink (broken) | wikilink (broken) | wikilink (broken) | **`maintain_batch` extra_source_ids** |
| Entity heal | "free" (false) | det. patch (reverted) | det. patch (reverted) | **sticky override in rebuild (Ph2)** |
| Source entries | version-annotate | flag, off by default | **mutate by default** | **no raw mutation; recency-supersede** |
| Dedup | wrong (`add` myth) | wrong | wrong | **explicit guard** |
| Columns | 2 | 2 | 3 | **2** |
| Rename/merge | ✓ both | deferred | ✓ both | **✓ both** |
| MVP estimate | ~5d | ~1.5d | ~8–12d | **~3–4d (+3–5d Ph2)** |
