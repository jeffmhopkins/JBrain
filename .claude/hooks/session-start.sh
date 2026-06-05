#!/bin/bash
# SessionStart hook (Claude Code on the web). Two jobs:
#
#  1. ALWAYS (remote): provision the server's Python deps + the web deps so tests and
#     linters run out of the box, including the OPTIONAL lab image-intake stack
#     (Tesseract + pytesseract + pypdfium2) so the photo/scan lab path is testable.
#     NOTE: the vision EXTRACTION step also needs an LLM key (LLM_API_KEY / ANTHROPIC_API_KEY)
#     — a secret a hook can't provide; set it in the environment if you want live extraction.
#
#  2. Android SDK, but ONLY for sessions that look like native-app (android/) work — so
#     server/PWA/prompt sessions don't pay the ~150 MB / few-minute SDK download. A session
#     counts as native-app work if the branch mentions android/wear/watch, or there are
#     changes under android/ (working tree or vs the default branch). If a session that
#     didn't trigger needs the SDK, run .claude/hooks/setup-android-sdk.sh directly.
set -euo pipefail

# Web-only; locally you have your own toolchain.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}"

# --- Dependency setup (best-effort: a single failed step warns but never blocks the session) ---
step() { echo "[session-start] $*" >&2; }
try()  { "$@" || step "WARN: '$*' failed (continuing)"; }

# System: Tesseract OCR — the independent reader that gates the vision lab path.
if ! command -v tesseract >/dev/null 2>&1; then
  step "installing tesseract-ocr…"
  try apt-get install -y -qq tesseract-ocr
fi

# Python: server runtime + tests (-dev) + optional lab image intake (-vision).
# Idempotent: skip the (heavy) install once the key deps import. --ignore-installed sidesteps
# 'cannot uninstall' errors for debian-managed packages (PyYAML/cryptography ship without a
# RECORD file), installing the pinned versions over them rather than trying to remove them.
if [ -f server/requirements.txt ] && \
   ! python -c "import pydantic, fastembed, pdfplumber, pytesseract, pypdfium2" >/dev/null 2>&1; then
  step "installing server Python deps (runtime + dev + vision)…"
  try python -m pip install -q --root-user-action=ignore --ignore-installed \
      -r server/requirements.txt \
      -r server/requirements-dev.txt \
      -r server/requirements-vision.txt
fi

# Web: node_modules so `tsc`/vite (typecheck + build) work.
if [ -f web/package.json ]; then
  step "installing web deps (npm install)…"
  try bash -lc 'cd web && npm install --no-audit --no-fund --silent'
fi

step "dependency setup done (tesseract + server/web deps)."

# --- Android SDK, only for native-app sessions ---
native_session() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  printf '%s' "$branch" | grep -qiE 'android|wear|watch' && return 0

  [ -n "$(git status --porcelain -- android/ 2>/dev/null)" ] && return 0

  local ref base=""
  for ref in origin/main origin/master main master; do
    if git rev-parse --verify -q "$ref" >/dev/null 2>&1; then base="$ref"; break; fi
  done
  if [ -n "$base" ] && [ -n "$(git diff --name-only "$base"...HEAD -- android/ 2>/dev/null)" ]; then
    return 0
  fi
  return 1
}

if native_session; then
  exec "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/setup-android-sdk.sh"
fi

echo "Not a native-app session — skipping Android SDK setup. Run .claude/hooks/setup-android-sdk.sh if you need it."
