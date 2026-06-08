# Article Talk — Conversational Redesign (Build Spec)

Status: **APPROVED for Phase 1+2.** Synthesized from four independent design
passes (threading data-model, dedicated GUI, agentic round-trip, minimalist) each
red-teamed against the other. Grounding citations are `file:line` against the tree
at writing.

## 0. Goal & position

The "talk box" (`web/src/components/TalkPanel.tsx`, mounted on kb/ articles at
`web/src/pages/NotePage.tsx:205`) is the Wikipedia-Talk-style ledger the KB
maintenance loop reads and writes (`server/app/services/article_talk.py`,
consumed by `wiki_build.py:maintain_one`/`maintain_batch`). Today it is a one-way
drop-box: the owner can `add()` items (`notes.py:223`) but cannot **respond** to
an entry, and there is intentionally no user-side resolve (`notes.py:225-227`).

Make the talk box **conversational** without breaking the loop's invariants:

1. **Reply** — the owner can respond to a specific entry; the AI reads replies as
   additional steering on the next pass over that article.
2. **Lifecycle agency** — the owner can **dismiss** an item that's wrong/obsolete/
   already-handled, so a stale item stops nagging the maintenance pass forever.
3. **Immediacy** — a one-click **"Resolve with AI now"** runs the existing
   *surgical* maintenance pass on that one article on demand, so a reply doesn't
   have to wait for the 03:00 batch. This is what makes the exchange feel two-way.

**Honest limit (the one compromise):** the maintenance loop is a *scheduled
batch* (`workflows/wiki-maintain.yaml`, `maintain_batch`, watermark-gated). This is
**not** synchronous chat. Replies left without clicking "Resolve with AI now" are
folded in the next time that article is maintained (a new directive triggers the
batch, or the owner clicks the button). The UI states this plainly rather than
faking responsiveness.

### Design decisions settled by the red-team
- **Child table, not `parent_id`.** Replies live in a new `article_talk_reply`
  table, NOT a self-referential `article_talk.parent_id`. The threading-lens plan's
  own red team found `parent_id` forces `AND parent_id IS NULL` guards onto *five*
  separate mutation sites (`record` dedup, `_cap_notes`, `demote_stub_notes`,
  `open_for`, `add`) — missing any one is silent corruption. A child table leaves
  every existing ledger query untouched.
- **No watermark/`consumed_at` surgery.** Replies enrich `maintain_one`'s prompt
  *whenever that article is maintained* (by the on-demand button or by the batch
  when a new item exists). A reply alone does not re-qualify an old item for the
  batch — the on-demand button is the explicit "act on my reply now" trigger. This
  sidesteps the watermark-rewind and auto-conversation-livelock hazards both the
  threading and round-trip plans flagged.
- **`/dismiss`, not `/resolve`.** Owner dismissal sets the same terminal state as a
  maintenance resolution (`resolved_at` + `resolution`) but labels it "dismissed by
  owner" so the audit trail stays honest. The name also preserves the existing
  `test_api.py:1726` assertion that `…/talk/{id}/resolve` does not exist.
- **`correction` is protected.** Dismissing a `correction` is **refused** (409): a
  correction's promoted truth-layer note (`article_talk.source_note_id`,
  `schema.sql:216`) would still rewrite the article next pass, so hiding the row
  would mislead. To undo a correction, delete its truth note.
- **No new free-form Q&A endpoint (deferred).** A persisted, ungrounded AI "answer"
  in a maintenance ledger is worse than in chat. The on-demand button reuses the
  grounded, versioned `maintain_one` path; a separate read-only `wiki_talk_answer`
  with a *code-level* grounding firewall is Phase 3, gated on need.
- **No dedicated console (deferred).** Talk items do NOT individually create Review
  cards (only "Reorganize"/"New subject" do — `wiki_build.py:1820-1824,1854-1861`),
  so there is no competing-inbox problem to solve and no cross-KB aggregator needed
  yet. The inline per-article panel stays the home base.

---

## 1. Data model

### 1.1 New table `article_talk_reply`
Added to `server/app/schema.sql` (after the `article_talk` block, ~`:220`) and
created for existing DBs by a migration (§1.2):
```sql
CREATE TABLE IF NOT EXISTS article_talk_reply (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  talk_id    INTEGER NOT NULL REFERENCES article_talk(id) ON DELETE CASCADE,
  author     TEXT NOT NULL DEFAULT 'user',   -- user | ai
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_article_talk_reply_talk ON article_talk_reply(talk_id);
```
- A reply is depth-1: it always points at a root `article_talk` row, never another
  reply. The maintenance dialogue is owner↔AI ping-pong about one item, not a forum.
- `ON DELETE CASCADE` is safe here: `article_talk` rows are only ever hard-deleted by
  `_cap_notes` (`article_talk.py:88`), which targets `kind='note' AND author='ai'`
  rows. Such an AI-note root could in principle carry an owner reply, so `_cap_notes`
  gains an `AND NOT EXISTS(reply)` guard (§2) — after which cascade never destroys
  owner words.

### 1.2 Migration
`server/app/db.py`: bump `SCHEMA_VERSION` 54→55 (`:107`) and add:
```python
if current < 55:
    # Threaded replies on article-talk items (owner↔AI maintenance conversation).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS article_talk_reply (...);
        CREATE INDEX IF NOT EXISTS idx_article_talk_reply_talk ON article_talk_reply(talk_id);
    """)
```
New self-contained table → index is safe inline (no late-column hazard). `schema.sql`
carries the identical block for fresh DBs (runner no-ops, schema.sql lands it).

---

## 2. Service layer (`article_talk.py`)

- **`reply(conn, talk_id, body, author="user") -> int | None`** — validate the root
  exists; insert a reply row. Replies bypass `record()` dedup entirely (a reply is a
  deliberate utterance). Returns the new id.
- **`replies_for(conn, talk_ids) -> dict[int, list[dict]]`** — batch-fetch replies
  for a set of item ids, oldest-first, grouped by `talk_id` (one query; used by both
  `list_for` and `maintain_one`).
- **`list_for` (`:127`)** — attach `replies: list[dict]` to each returned item via
  `replies_for`. Additive: old clients ignore the field.
- **`dismiss(conn, talk_id, reason=None)`** — owner terminal close. Reuses the
  `resolved_at`/`resolution` columns (like `resolve_with`, `:141`) with
  `resolution = "dismissed by owner" + (": " + reason if reason)`. Guarded by
  `resolved_at IS NULL` so it's a harmless no-op against a concurrent maintenance
  resolve.
- **`_cap_notes` (`:88`)** — add `AND NOT EXISTS (SELECT 1 FROM article_talk_reply r
  WHERE r.talk_id = article_talk.id)` to the delete candidate query so an AI-note
  root that has owner replies is never capped away (protects cascade, §1.1).

Everything else in `article_talk.py` is unchanged — replies live in a separate
table, so `record`/dedup/`open_for`/`demote_stub_notes` need no guards.

---

## 3. Maintenance-loop integration (`wiki_build.py`)

### 3.1 Fold replies into the writer prompt (`maintain_one`, `:723-726`)
After building `open_items`, fetch their replies and append each as an indented
sub-line under its item in `items_text`:
```
[12] (directive, by user) Use British spelling.
    ↳ reply (user): I meant only in the prose — keep code samples as-is.
```
The owner's replies thus become part of the existing `{items}` placeholder
(`prompts.yaml:1036`) — no prompt-contract change. The `wiki_maintain` prompt gains
one line under "Work each OPEN ITEM" noting that an item may carry the owner's
follow-up replies (`↳ reply`), which are the **latest, authoritative steer** and
must be honored.

### 3.2 On-demand single-article pass (`maintain_now`, new, near `rebuild_article` `:1017`)
```python
def maintain_now(conn, title) -> dict:
    """Owner-triggered surgical maintenance of ONE article right now — the on-demand
    twin of the maintain_batch loop body. Folds in the owner's talk replies, runs
    under the KB write lock so it can't interleave with the batch. NOT a full rebuild."""
```
- KB article check → `kb_lock_acquire` (`:919`); return `{ok:False, reason:"KB is
  busy…"}` if held (mirrors `rebuild_article:1030`).
- Gather promoted-correction note ids exactly as the batch does
  (`wiki_build.py:1620-1622`).
- `out = maintain_one(conn, title, _known_titles(conn), extra_source_ids=corr_ids or None)`.
- On `out["ok"]`: `_apply_maintain(conn, out, "maintenance (on-demand)")`, commit,
  return `{ok, changed, resolved, examined, kept_open}`.
- On failure: return `{ok:False, reason}`. `finally: kb_lock_release`.

### 3.3 Lock the batch (`maintain_batch`, `:1566`)
`maintain_batch` today takes **no** lock, so an on-demand `maintain_now` and the
nightly batch could mutate the same article concurrently. Wrap the batch body in
`kb_lock_acquire`/`kb_lock_release`; if the lock is held, return early
`{… "skipped":"KB busy"}` without advancing the watermark (retried next tick). This
is the one necessary non-trivial change and closes a pre-existing concurrency gap.

---

## 4. API (`server/app/routers/notes.py`, beside `:214-242`)

- **`POST /{slug}/talk/{talk_id}/reply`** `{body}` → verify the row's
  `article_title == _note_title(slug)`; `article_talk.reply(conn, talk_id, body,
  author="user")`. Returns the new reply row.
- **`POST /{slug}/talk/{talk_id}/dismiss`** `{reason?}` → verify scope; **refuse
  `kind='correction'` with 409**; else `article_talk.dismiss(...)`. Returns `{ok}`.
- **`POST /{slug}/talk/maintain-now`** → `wiki_build.maintain_now(conn,
  _note_title(slug))`. Returns the result dict (incl. `ok:false, reason` when busy).

---

## 5. Frontend (`web/src/components/TalkPanel.tsx`, `api.ts`, `styles.css`)

- **`api.ts`**: `talkReply(slug,id,body)`, `talkDismiss(slug,id,reason?)`,
  `talkMaintainNow(slug)`.
- **`TalkPanel.tsx`**: extend `Talk` with `replies?: {id;author;body;created_at}[]`.
  Per open primary item: render its replies indented (reuse the chat `.msg`
  idiom/`makeLinkRenderer`), a **Reply** affordance (inline composer), and a
  **Dismiss** button (hidden for `correction`). Above the add box: a **"Resolve with
  AI now"** button calling `maintain-now`, with a small note that replies are
  otherwise read by the next scheduled maintenance pass. Reuse `.composer-box` from
  `ResearchChat`/`GuidedChat` to replace the cramped `.talk-add` single line.
- **`styles.css`**: `.talk-replies` (indent), `.talk-reply` rows, reuse existing
  chat classes.

---

## 6. Testing (`server/tests/test_api.py`, mirroring `test_article_talk`)
- `reply` adds a row that appears in `list_for`'s `replies[]`; replies survive
  `_cap_notes` (an AI-note root with a reply is not capped).
- `dismiss` sets `resolved_at`+`resolution` (item leaves `open_for`); **refused on a
  `correction` (409)**; no-op against an already-resolved row.
- `maintain_one` includes an item's owner reply text in the LLM prompt (monkeypatch
  `llm.complete` to capture the prompt).
- `maintain_now` runs under the lock (returns busy when the lock is held) and applies
  a revision via the existing `_apply_maintain` path.
- The existing `…/talk/{id}/resolve` 404/405 assertion (`:1726`) still holds.

---

## 7. Phasing
- **Phase 1** (reply + lifecycle): §1, §2, §3.1, the `/reply` + `/dismiss`
  endpoints, the `TalkPanel` rework. Near-zero model cost, lowest risk.
- **Phase 2** (immediacy): §3.2 `maintain_now` + §3.3 batch lock + `/maintain-now`
  endpoint + the "Resolve with AI now" button.

## 8. Explicitly NOT built (deferred / gated on usage)
- `parent_id` self-referential threading; deep nested threads (depth-1 only).
- Per-reply auto-AI loops / a reply-driven batch re-qualification (`consumed_at`).
- Free-form read-only Q&A (`wiki_talk_answer`) — needs a code-level grounding
  firewall first.
- A cross-KB Maintenance Console route.
- User-resolve/dismiss of `correction` items.
