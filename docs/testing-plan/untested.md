# Untested-code map

A visibility doc (not a gate): where the suite does **not** reach, so gaps are a
deliberate choice. Coverage = "a test executed this", which is weaker than "this is
verified" — even covered code usually asserts the main path, not every edge.

Regenerate: `./jt cov` then
`python - <<'PY'` parsing `web/coverage/coverage-final.json` (frontend per-function)
and `server/coverage.json` (backend per-file) — see the commands in this PR's history.

## Headline (last measured)
| | Covered | Untested |
|---|---|---|
| Frontend functions | **75.1%** (942/1254) | 312 functions never called by a test |
| Frontend lines | 89.6% | — |
| Backend lines | 79.7% | 2,709 statements |

## Frontend — top files by untested functions
| Untested/total | File | Examples |
|---|---|---|
| 31/90 | `pages/CalendarPage.tsx` | `addMonths`, `browserTz`, `doUndo` |
| 28/164 | `api.ts` | `chatGuestUploadFile`, `calUndismiss`, `attachmentObjectUrl` |
| 22/32 | `components/RebuildPanel.tsx` | `addSource`, `doRegather` + handlers |
| 19/51 | `pages/Chat.tsx` | touch/`onTouchEnd`, `addDest`, `cycle` |
| 15/38 | `pages/NotePage.tsx` | mostly inline handlers |
| 14/34 | `pages/MapPage.tsx` | `haversineM`, `perpDist`, `notePopup` |
| 8/20 | `pages/GraphPage.tsx` | `nodeCanvasObject`, `nodeColor` (canvas draw) |
| 8/15 | `components/EncryptedChat.tsx` | `sendFile`, `loadFile`, `openFile` |

**Read this honestly:** the untested 25% is **dominated by inline UI event handlers**
(`onClick`/`onChange`/`onClose`/`onFocus`/touch) and **device/canvas-only code**
(`nodeCanvasObject`, map geometry draw, file pickers) — not untested business logic.
Most of those handlers are one-line setters; the canvas/file paths need a real browser.
Worth targeting: the genuine logic helpers (`addMonths`/`browserTz` in Calendar,
`haversineM`/`perpDist` in Map, the `api.ts` guest-file methods) — extract & unit-test.
Not worth chasing in jsdom: canvas draw, touch gestures, file-picker plumbing (covered,
if anywhere, by e2e).

## Backend — top modules by uncovered lines
(coverage.py reports lines/branches, not functions; uncovered lines proxy untested
functions/branches.)

| Uncovered | % | Module |
|---|---|---|
| 436 | 68% | `services/architect.py` (the big agent loop — many leaf tool handlers) |
| 168 | 80% | `services/pipeline.py` |
| 129 | 64% | `routers/share.py` |
| 121 | 87% | `services/wiki_build.py` |
| 121 | 70% | `routers/share_admin.py` |
| 119 | 63% | `routers/staging.py` |
| 115 | 21% | `routers/rebuild.py` (thin HTTP shell over the tested engine) |
| 110 | 76% | `services/calendar.py` |
| 107 | 26% | `main.py` (ASGI wiring/lifespan; exercised indirectly) |

Highest-value backend targets: the architect leaf tool-handlers and the `share*`/
`staging` router branches (security/data-mutation surface). `main.py`/`rebuild.py`
routers are thin shells whose logic is tested in the services they call.

## Why we don't gate on function coverage
Function coverage is a weak, gameable proxy (you can "cover" a function with a test that
asserts nothing). The enforcement we use instead is **informational diff coverage** on
PRs (how well a PR's *new* lines are tested) — it targets "new features ship with tests"
without rewarding global-number gaming or forcing backfill of device-only code.
