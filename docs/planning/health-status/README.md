# Planning: Server/API Health Indication + Pre-flight Capability Gating

**Goal:** Real-time server *and* API health in the PWA, and any service that won't
actually work should warn the user *before* they try to use it.

**➡️ The final, implementation-ready plan is [`70-hybrid-v2.md`](./70-hybrid-v2.md)** — GO-approved by the round-4 red team.

## How this plan was produced (red-team workflow)

Research → 4 competing plans → red team → iterate → red team → hybrid → red team → iterate → red team → present.

| Stage | Artifact(s) |
|---|---|
| Research grounding | [`00-research.md`](./00-research.md) |
| 4 competing plans | [`10-plan-A-minimal.md`](./10-plan-A-minimal.md) · [`11-plan-B-status-endpoint.md`](./11-plan-B-status-endpoint.md) · [`12-plan-C-capability-gating.md`](./12-plan-C-capability-gating.md) · [`13-plan-D-realtime-stream.md`](./13-plan-D-realtime-stream.md) |
| Red team round 1 (per plan) | [`20-redteam1-A.md`](./20-redteam1-A.md) · [`20-redteam1-B.md`](./20-redteam1-B.md) · [`20-redteam1-C.md`](./20-redteam1-C.md) · [`20-redteam1-D.md`](./20-redteam1-D.md) |
| Iterate (v2 of each) | [`30-plan-A-v2.md`](./30-plan-A-v2.md) · [`30-plan-B-v2.md`](./30-plan-B-v2.md) · [`30-plan-C-v2.md`](./30-plan-C-v2.md) · [`30-plan-D-v2.md`](./30-plan-D-v2.md) |
| Red team round 2 (comparative) | [`40-redteam2-backend.md`](./40-redteam2-backend.md) · [`40-redteam2-frontend.md`](./40-redteam2-frontend.md) |
| Hybrid synthesis | [`50-hybrid-v1.md`](./50-hybrid-v1.md) |
| Red team round 3 | [`60-redteam3-hybrid.md`](./60-redteam3-hybrid.md) |
| Iterate hybrid | [`70-hybrid-v2.md`](./70-hybrid-v2.md) ← **FINAL** |
| Red team round 4 (go/no-go) | [`80-redteam4-final.md`](./80-redteam4-final.md) → **GO** |

## One-paragraph shape of the final plan

A new soft-auth `GET /api/system/status` (two builders: a tiny public `{ok,brain,ts}`
skeleton vs. a full authed capabilities doc), with the same capabilities object also
folded into `/api/auth/verify` for a free boot snapshot. Per-subsystem readiness
(llm/embeddings/transcription/push/geocoder/db) backed by lock-guarded state machines
in the embeddings and audio services. A client `useSyncExternalStore` singleton merges
adaptive polling (5s warming / 20s steady) with **observed health from real traffic**
(instrumented `api()`/`streamChat`/`streamSSE`, zero token cost) to distinguish
browser-offline vs server-unreachable vs subsystem-degraded. A status dot + detail
panel replaces the `navigator.onLine`-only banner. Exhaustive pre-flight gating
(single-sourced copy + shared primitives) disables/explains every unrunnable feature,
including a fix to a real pre-existing `search.py` bug and server-driven gating for the
public share route. LLM "usable" is one predicate everywhere (`llm.has_credentials()`);
validity surfaces from observed traffic. No new heavy deps; SSE push left as an
explicit future multi-user enhancement.
