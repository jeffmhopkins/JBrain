#!/usr/bin/env bash
# Boot the REAL JBrain API for end-to-end tests: serve the built PWA from static/
# (single origin, like production) and point the LLM at the local fake (XAI_BASE_URL).
# Used as a Playwright `webServer` command. Assumes web/dist is already built.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Serve the built PWA from STATIC_DIR (== server/static, per server/app/main.py:
# Path(__file__).parent.parent / "static"). The mount only registers if it exists
# at import time, so copy BEFORE booting uvicorn.
rm -rf "$ROOT/server/static"
cp -r "$ROOT/web/dist" "$ROOT/server/static"

export DB_PATH="${DB_PATH:-$(mktemp -d)/e2e.db}"
export JBRAIN_ACCESS_KEY="${JBRAIN_ACCESS_KEY:-e2e-access-key-0123456789abcdef}"
export BRAIN_NAME="${BRAIN_NAME:-E2E Brain}"
export JBRAIN_DOMAIN="${JBRAIN_DOMAIN:-localhost}"
# LLM mocked at the boundary: OpenAI-compatible xAI adapter -> the local fake server.
export LLM_PROVIDER=xai
export XAI_API_KEY=fake-e2e-key
export XAI_BASE_URL="${XAI_BASE_URL:-http://127.0.0.1:8919/v1}"
# Local LLM (Ollama) enabled but pointed at the SAME fake server, which also serves a
# minimal Ollama admin API (GET /api/tags, POST /api/pull, DELETE /api/delete). This
# exercises the local-models settings UI end-to-end with no real Ollama.
export LLM_LOCAL_ENABLE="${LLM_LOCAL_ENABLE:-true}"
export LLM_LOCAL_ADMIN_URL="${LLM_LOCAL_ADMIN_URL:-http://127.0.0.1:8919}"
export LLM_LOCAL_BASE_URL="${LLM_LOCAL_BASE_URL:-http://127.0.0.1:8919/v1}"
export LLM_LOCAL_MODEL="${LLM_LOCAL_MODEL:-qwen2.5:7b}"
export LLM_TIMEOUT_SECONDS="${LLM_TIMEOUT_SECONDS:-600}"
# Keep boot fast/offline: no model warmup network egress beyond the already-cached embed model.

cd "$ROOT/server"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${JBRAIN_PORT:-8918}"
