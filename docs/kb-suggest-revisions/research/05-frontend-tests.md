# R5 — Frontend UX surface, API client, conversational precedents, test patterns

Research for the live "Suggest revisions" mode. All file:line refs are to the
current tree. Research only — no code changed.

---

## 1. RebuildPanel state machine — events → UI transitions

`web/src/components/RebuildPanel.tsx` is the closest sibling and the model to copy.
It is a single `<Modal>` driving a 3-stage / multi-phase state machine over SSE.

### State shape (RebuildPanel.tsx:15-69)
- `type Stage = "gather" | "curate" | "draft"` (:15) — the top-level wizard stage,
  rendered as progress pips (:306-313).
- `type Phase = "streaming" | "review" | "guiding" | "guiding-streaming" | "stale" | "truncated" | "empty" | "error"` (:16)
  — sub-state of the `draft` stage. **This Phase union is the part to fork for the new mode.**
- Key refs: `runId` (:71), `sse` (current SSEHandle, :72), `sawContent` (first content_delta
  flips activity panel shut, :73), `closeRef`/`stableClose` (:78-79 — a stable `onClose` so
  Modal's focus effect does not steal focus from the textarea on every keystroke; **reuse this
  exact trick for the new chat composer**).

### SSE event → UI transition map
Stage 1 gather handler `handleGather` (:82-107), opened on mount (:109-114):
| event | transition | line |
|---|---|---|
| `run_started` | store `run_id` | :84 |
| `tool_use` | set status, append a running `Step` | :85-88 |
| `tool_result` | mark the matching running step done + attach summary/items | :89-98 |
| `sources_proposed` | set candidates+skipped, `setStage("curate")` | :99-104 |
| `error` | set `gatherErr` (renders inline ⚠️) | :105 |

Stage 2/3 draft handler `handleDraft` (:117-134), shared by draft/redraft/guide streams:
| event | transition | line |
|---|---|---|
| `thinking_delta` | status "Thinking…", append to `thoughts` | :119 |
| `content_delta` | first one closes activity + status "Drafting…"; append to `draft` | :120-123 |
| `lint` | set `warn` banner | :124 |
| `done` | set final `draft`, `busy=false`; branch to `empty`/`truncated`/`review`/`guiding` phase | :125-131 |
| `error` | `busy=false`, phase `error` | :132 |

Note the `done` branch picks `guiding` vs `review` based on whether we were in
`guiding-streaming` (:130) — i.e. the same handler serves both first draft and a guided
revision. **The new mode's "talk → targeted edit → talk" loop is exactly this guide path,
promoted from a secondary tab to the primary loop.**

### The conversational "Guide" affordance (already present, the seed of the new feature)
- `thread` state: `{ role: "user" | "ai"; text }[]` (:62) — a minimal message list.
- `guideInput` textarea state (:63).
- `openGuide()` (:227) flips to phase `guiding`, tab `guide`.
- `sendGuide()` (:228-239): trims input, **optimistically** pushes the user msg to `thread`
  (:232), clears the draft + resets `sawContent`, sets phase `guiding-streaming`, opens
  `guideStream(runId, text, handleDraft)` (:235), and on `done` appends a canned AI ack
  ("Updated the draft — take a look.", :237). The AI "message" is a stub — the real output is
  the re-rendered draft, not a chat reply.
- Render: a draft/guide **segmented toggle** (:422-427); the guide tab renders `rb-thread`
  message bubbles `rb-msg user|ai` (:464-473) + a "Working from:" source chip row; the
  composer textarea+send-button lives in the **footer** (:283-289) with Enter-to-send
  (Shift+Enter newline). Accept/Reject/Redraft/Guide footer buttons are computed in `footer`
  (:252-297) per phase.
- Diff view: `MarkdownDiff before={note.content_md} after={draft}` toggled by `showDiff`
  (:431-435, :458-460). The BASE article is `note.content_md`, passed in as a prop.

**Reuse for the new mode:** the guide loop is already the right primitive. The new mode
should make it the *only* loop (no gather/curate wizard front-end), keep `thread` as the
chat transcript, render the evolving draft beside/under it, keep `showDiff`/`MarkdownDiff`
against the preserved BASE, and keep Accept/Reject. The AI ack should become a real
per-turn summary message (what it changed) rather than the canned string at :237.

---

## 2. api.ts SSE consumption pattern (exact shape to copy)

The canonical helper is `streamSSE(path, body, onEvent)` (`web/src/api.ts:875-929`):
- Opens a **POST** `fetch` with `authHeaders()` + an `AbortController` (:876, :880-883).
- Reads `res.body.getReader()` + `TextDecoder`, buffers, splits on `"\n\n"`, finds the
  `data: ` line, `JSON.parse`s it as a `RebuildEvent`, dispatches to `onEvent` (:890-922).
- Health side-effects: `report({kind:"llm-fail"})` on `error`, `"llm-ok"` on `done` (:918-919).
- 90s **stall watchdog** (`arm()`, :896-897, :907) that aborts a silent stream.
- Returns `SSEHandle = { done: Promise<void>; abort: () => void }` (:871, :928).

The thin per-endpoint wrappers (:931-948) are one-liners over `streamSSE`:
```ts
export const guideStream = (runId, text, onEvent): SSEHandle =>
  streamSSE(`/api/kb/rebuild/${runId}/guide`, { text }, onEvent);
```
Plain JSON siblings: `searchRebuildSources` via `get<...>` (:951-953); `acceptRebuild`
(:955-956) / `rejectRebuild` (:958-959) via `post<...>`.

`RebuildEvent` discriminated union lives at :859-869; `SSEHandle` at :871.

**For the new mode:** add a parallel `SuggestEvent` union + one `streamSSE`-based wrapper per
endpoint (e.g. `suggestStart`, `suggestTurn`, `suggestAccept`/`Reject`). Do **not** hand-roll
a reader — `streamSSE` already handles abort, stall, health reporting, and the `\n\n` framing.
(Two other readers exist — `openChatStream` at :281-310 for the blind-relay encrypted chat
and the inline reader inside `streamChat` at :763-849 — but `streamSSE` is the right one to
copy: it is the rebuild-shaped POST+abort+keepalive helper.)

The server framing to match: `rebuild.py:_sse` (`server/app/routers/rebuild.py:57-111`) emits
`event: <type>\ndata: <json>\n\n` with `: keepalive\n\n` every 15s (:90-99). The client
ignores the `event:` line and parses only `data:`.

---

## 3. Launch / entry point in NotePage + owner-gating

`web/src/pages/NotePage.tsx`:
- Import (:20), open-state `rebuilding` (:74).
- Entry: a `NoteActionsMenu` item **"Rebuild page now"**, KB-only, `accent: true`, gated by
  `llm.ready` for its hint, `onClick: rebuildNow` (:265-267).
- `rebuildNow()` (:119-124): a **pre-flight** — if `!llm.ready`, `showToast(llm.reason)` and
  bail (don't open a doomed run); else `setRebuilding(true)`. `llm = useCapability("llm")` (:63).
- Mounts the panel near the end (:379-383): `<RebuildPanel slug note={{title, content_md}}
  onClose={() => setRebuilding(false)} onAccepted={(s) => { close; navigate or reload }} />`.
  `onAccepted` navigates if the slug changed (rename), else `reload()` (:382).

**Owner-gating:** there is **no per-feature owner check in NotePage** — the entire PWA is the
owner's authenticated app (recipients use the separate `SharePage`/share routes). The only
gating is `note.kind === "kb"` (:266) and the `llm` capability pre-flight. **The "Suggest
revisions" entry belongs as a second KB-only item right next to "Rebuild page now" (:266-267),
with the same `rebuildNow`-style pre-flight.** TalkPanel is mounted unconditionally for KB
pages that aren't `/_` system pages (:229) — backlinks/CONTEXT are already on the page
(`note.backlinks`, :231-239), so the new panel can read them from the same `note` prop.

---

## 4. Best conversational-loop precedent

Two candidates; **the RebuildPanel "Guide" loop itself (§1) is the primary model** — it is
already the talk→edit primitive over the same SSE rails. For the chat-transcript rendering and
optimistic-input ergonomics, borrow from the two precedents below:

### TalkPanel (`web/src/components/TalkPanel.tsx`) — NOT a streaming chat
It is a **poll/refresh** panel (`load()` GETs `/api/notes/:slug/talk`, :51-52; mutate then
`load()`). Message-list rendering: `Replies`/`ItemActions`/`ReplyBox` (:101-138), reply
composer is an `<input>` with Enter-to-send (:132-135). Useful as the *persistent memory*
surface (directives/corrections feed the maintenance pass) but it does **not** stream and has
no optimistic assistant bubble. Reuse its compact item/reply CSS idioms, not its data flow.

### Chat.tsx (`web/src/pages/Chat.tsx`) — the full streaming conversational precedent
This is the richest streaming chat: `messages: Msg[]` (:153), and the **optimistic
dual-bubble** pattern — on Send it pushes BOTH a user bubble and an empty assistant bubble
tagged by `id` (`asstId`/`userId`, :587), then `streamChat(..., onEvent)` targets the
assistant bubble *by identity* as tokens arrive (:636-671). Tokens accumulate in a ref and a
typewriter loop reveals them (:641-647); `error` swaps the bubble content by id (:670);
rollback drops the optimistic rows by id on a pre-token throw (:601). `ChatEvent` types:
`token | tool | staging | applied | external_proposal | chart | replace_text | error` (:637-664).

**Recommendation for "Suggest revisions":** structure it as a **RebuildPanel-shaped Modal/panel**
whose primary surface is the guide loop, not a wizard:
- Keep RebuildPanel's `thread` + footer composer (RebuildPanel.tsx:62-63, :283-289) and the
  stable-`onClose` ref trick (:78-79).
- Adopt Chat's **optimistic user-bubble-on-Send** + **per-turn AI summary bubble** so each
  turn reads as a real exchange (replace the canned ack at RebuildPanel.tsx:237 with the
  streamed change-summary).
- The "evolving draft" is the single streamed `draft` target re-rendered each turn (handleDraft,
  :117-134) with `MarkdownDiff` against the preserved BASE (`note.content_md`, :460).
- Reuse Chat's typewriter/reveal only if desired; RebuildPanel's plain append (:122) is simpler
  and already tested.

---

## 5. Test harness recipes

### 5a. New conversational panel (vitest + MSW) — copy RebuildPanel.test.tsx
`web/src/components/RebuildPanel.test.tsx` is the exact template (read its header comment
:1-11 for the rationale). The recipe:

1. **Mock only the stream helpers, keep everything else real** (:38-53):
   ```ts
   vi.mock("../api", async (importOriginal) => {
     const actual = await importOriginal<typeof import("../api")>();
     return { ...actual, guideStream: vi.fn((runId, text, onEvent) =>
       fakeStream("guide", onEvent, [runId, text])) };
   });
   ```
   SSE is awkward over MSW, so stream fns are swapped for **scriptable fakes**; the plain JSON
   endpoints (accept/reject/search) stay on MSW.
2. `fakeStream(kind, onEvent, args)` (:29-36): records the call, runs a per-kind `Script` on a
   microtask (`Promise.resolve().then(...)`, :34 — lets the component commit before events
   arrive, mirroring a round-trip), returns a real-shaped `{ done, abort: abortSpy }`.
3. A `scripts: Record<string, Script>` registry (:26); a test sets `scripts.guide = (onEvent)
   => { onEvent({type:"content_delta",...}); onEvent({type:"done", draft}); }`.
4. MSW recording handlers for JSON endpoints via `server.use(...)` per test (:85-100, applied in
   `beforeEach` :124); assert routing by inspecting a recorded `posted[]` array.
5. `renderWithProviders` from `web/src/test/render.tsx` (wraps `MemoryRouter`, returns a
   `user` from `userEvent.setup()`); `server` from `web/src/test/server.ts`.
6. Gotchas already solved here to copy: `vi.stubGlobal("confirm"/"alert", ...)` because jsdom
   lacks them (:122-123); `__reset()` of the health module in `beforeEach` (:116, :57);
   scoping footer-button queries to `.modal-foot` (`footBtn`, :108-113) so they don't collide
   with Modal's header Close button.
7. **Gap to fill:** the existing test does NOT exercise the guide/conversational loop (only
   gather→curate→draft→accept/reject; grep shows `guideStream` is mocked but never scripted in a
   test). The new mode's loop tests are net-new — write a `scripts.<turn>` that emits
   `content_delta`+`done`, assert the user bubble appears optimistically, the draft re-renders,
   and a second turn works. Use the `gotoReview()`/`gotoDraft()` helper pattern (:215-222, :274-282).

### 5b. New SSE backend endpoint (pytest) — copy test_rebuild_engine.py
`server/tests/test_rebuild_engine.py` (header :1-22). Pattern, marked
`pytestmark = pytest.mark.integration` (:31):

1. **Drive the engine's async generators directly** with `_drain(agen)` — a fresh
   `asyncio.run` per call collecting yielded event dicts (:36-47). It does **not** go through
   the FastAPI route/`_sse` bridge; it tests the generator that the route wraps. (The route's
   `_sse` keepalive bridge in `rebuild.py:57-111` is thin and not unit-tested separately.)
2. **Mock at the `llm` seam, never the SDK** (`_install_provider`, :122-128): monkeypatch
   `llm.get_provider`→a `FakeProvider`, `llm.has_credentials`→bool, `llm.model_for`→pinned.
3. `FakeProvider` (:50-80): a `script` = list of turns; each turn a list of neutral events
   (`TextDelta`/`ThinkingDelta`/`ToolCallEvent`/`TurnEnd`) its `stream_turn` yields; it appends
   a placeholder assistant turn to `messages` like the real adapters (:73-74) so
   continuation/redraft bookkeeping is realistic. `fail_on_turn` injects a provider error (:68).
4. **Real SQLite**, embeddings no-op'd: the `conn` fixture (:83-107) sets env, clears
   `get_settings` cache, monkeypatches every `embeddings.*` to a no-op / `[]` (:95-101), and
   `db.init_db()` against a temp DB. Seed notes with `_mk(conn, title, body, kind)` (:110-114);
   make a run with `rebuild_runs.create(...)` (:117-119).
5. Registry-lifecycle tests clear `rebuild_runs._RUNS`/`_BY_SLUG` and monkeypatch
   `rebuild_runs.time.monotonic` for deterministic TTL/sweep (:144-145, :155, :191-211).

**For a new SSE endpoint:** add the engine generator + run it through `_drain` with a scripted
`FakeProvider` for the happy path, a no-credentials path (`creds=False`), a `fail_on_turn`
error path, and a cancellation path (`run.cancelled = True`, cf. :169). If you want HTTP-level
coverage of the route + `_sse` framing, the repo has no precedent for streaming the route via
TestClient — keep that out of scope and test the generator directly as above.

---

## Key file references
- `web/src/components/RebuildPanel.tsx` — the sibling UX + guide loop (states/phases/SSE handlers).
- `web/src/api.ts:875-959` — `streamSSE` + rebuild endpoint wrappers + `RebuildEvent`/`SSEHandle`.
- `web/src/pages/NotePage.tsx:119-124, 265-267, 379-383` — launch + pre-flight + mount.
- `web/src/components/TalkPanel.tsx` — persistent talk memory (poll-based, not streaming).
- `web/src/pages/Chat.tsx:587, 636-671` — optimistic dual-bubble streaming chat precedent.
- `web/src/components/RebuildPanel.test.tsx` — vitest+MSW SSE-fake recipe.
- `web/src/test/render.tsx`, `web/src/test/server.ts` — `renderWithProviders` + MSW server.
- `server/app/routers/rebuild.py:57-111` — `_sse` keepalive bridge (server framing to match).
- `server/tests/test_rebuild_engine.py:36-128` — `_drain` + `FakeProvider` + `llm`-seam pattern.
