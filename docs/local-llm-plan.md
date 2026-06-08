# Hybrid Local-LLM — Implementation Plan

Status: **Phase 1 implemented** (backend plumbing + compose + config + tests). Phase 2
(UI) is designed below but not yet built.

## Goal

Run JBrain's high-volume, latency-tolerant **`cheap` tier** (tagging, titling,
summaries, date/place/event extraction — all plain `llm.complete()`, no tools) on a
**local model** (Ollama, OpenAI-compatible) while keeping the interactive agent,
synthesis, and vision on the cloud API. Embeddings (fastembed) and speech-to-text
(faster-whisper) are already local. Target hardware: a CPU-only mini PC (e.g. Beelink
SEi12, i7-12650H, 32 GB DDR4 → ~6–8 tok/s on a 7B). Allow ≤13B q4, recommend 7–8B,
never 70B on that box.

## Design decisions (reconciled from three planning passes)

1. **`LocalProvider(XAIProvider)`** — a thin subclass of the existing OpenAI-compatible
   adapter, pointed at `LLM_LOCAL_BASE_URL` with a placeholder key. Registered as
   `local`/`ollama` in `_REGISTRY`. Tool-use/streaming inherited but best-effort — the
   agent stays cloud.
2. **Model-id-based routing, enable-gated.** `_provider_for_model` recognises an Ollama
   id (contains `:`, or a `LLM_LOCAL_PREFIXES` entry) as `local` **only when
   `LLM_LOCAL_ENABLE` + a base URL are set**. This makes the hybrid automatic with zero
   call-site changes: set `models.cheap` to a local id and that tier alone goes local.
3. **Tier→model storage reuses the existing `/api/prompts` store** that `ModelPicker`
   already writes and `model_for()` already reads — so **no new `meta` override and no
   `tier-models` endpoints**. `model_for()` is unchanged.
4. **One readiness module: `services/local_models.py`** (Ollama admin: list/pull +
   readiness), mirroring `embeddings.readiness()`. Health key: `local_llm`
   (informational — does NOT gate the authoritative `llm` predicate).
5. **Configurable timeout** `LLM_TIMEOUT_SECONDS` (default 120; raise to ~600 for CPU).
6. **Graceful cloud fallback** — a failed local-tier `complete()`/`complete_with_meta()`
   retries on the cloud default when `LLM_LOCAL_FALLBACK` is on and a cloud key exists.
7. **Two run modes:** Mode A (turnkey: `COMPOSE_PROFILES=localllm` runs Ollama in a
   container, auto-pulls the model); Mode B (BYO host Ollama via
   `host.docker.internal`). `extra_hosts: host-gateway` added unconditionally on `api`.

## Phase 1 — implemented

- `server/app/config.py` — `LLM_LOCAL_*`, `LLM_TIMEOUT_SECONDS`, `LLM_LOCAL_FALLBACK`
  fields; `has_local` property; folded into `has_llm`.
- `server/app/services/llm.py` — `LocalProvider`, registry aliases, `_is_local_model_id`
  + `_provider_for_model` branch, `_timeout()` (replaces the hardcoded constant),
  `_cloud_fallback_provider()` + fallback in the `complete`/`complete_with_meta` facades.
- `server/app/services/local_models.py` (new) — stdlib-urllib Ollama admin client
  (`list_models`, `model_present`, `pull_model`, `warm`) + per-process readiness.
- `server/app/services/usage.py` — local ids cost `$0` (token counts still recorded).
- `server/app/services/system_status.py` — `local_llm` capability + `local` in the
  informational providers map.
- `server/app/main.py` — boot warm probe (`_warm_local_llm`), off the event loop.
- `docker-compose.yml` — `ollama` + `ollama-pull` services (`profiles: ["localllm"]`),
  `ollama-models` volume, `extra_hosts` on `api`, `COMPOSE_PROFILES` passthrough to the
  updater.
- `.env.example` — local-LLM stanza (Mode A/B, RAM/model guidance).
- Tests: `test_llm.py` (routing/provider/fallback), `test_local_models.py` (new),
  `test_usage.py` (new), `test_system_status.py` (new), `test_compose_localllm.py` (new).

### Enabling it (operator steps, no UI required)

Mode A (turnkey):
```
# .env
COMPOSE_PROFILES=localllm
LLM_LOCAL_ENABLE=true
LLM_LOCAL_BASE_URL=http://ollama:11434/v1
LLM_LOCAL_ADMIN_URL=http://ollama:11434
LLM_LOCAL_MODEL=qwen2.5:7b
LLM_TIMEOUT_SECONDS=600
```
```yaml
# prompts.yaml
models:
  cheap: "qwen2.5:7b"     # this tier now runs local; everything else stays cloud
```
`docker compose up -d` brings up Ollama, pulls the model in the background, and the
health dot shows `local_llm: pulling → ready`.

## Phase 2 — UI (designed, not built)

- `web/src/api.ts` — `getLocalModels`, `pullLocalModel` (reuse `streamSSE`),
  `deleteLocalModel`.
- New router endpoints: `GET /api/system/local-models` (with server-computed
  `fits`/`warn` per detected RAM), `POST .../pull` (SSE), `DELETE .../{name}`.
- `web/src/components/LocalModelsPanel.tsx` (new) — installed list, curated pull
  allowlist (Qwen2.5-7B, Llama 3.1 8B, a vision model), pull-with-progress,
  hardware-aware guardrails.
- `web/src/components/ModelPicker.tsx` — add a "Local (Ollama)" optgroup; skip the
  missing-key warning for local ids; disable won't-fit options; `cheap`-tier hint.
- Health wiring: `local_llm` slice in `health.ts`/`statusDerive.ts`/`capabilities.ts`
  (with `pulling` → warn); pull progress teed to the deploy console.
- `install.sh` + README — local-LLM prompt + curated model menu; env table, Mode A/B
  table, allowed-model table, RAM math.
- Tests: `LocalModelsPanel.test.tsx`, extended `ModelPicker.test.tsx`, MSW handlers,
  status-derive cases, and one Playwright e2e (Ollama + pull faked at the boundary).

## Guardrails / invariants

- A `:`-id must never route local while disabled (else it 404s against Anthropic) —
  tested both polarities.
- Only `cheap` carries a local id on the CPU box; never `default`/`synthesis`/`vision`.
- Fully reversible: drop `localllm` from `COMPOSE_PROFILES`; no DB/migration.
- No real network/Ollama in tests — mock at the urllib/SDK seam.
