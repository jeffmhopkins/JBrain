#!/bin/bash
# SessionStart hook (Claude Code on the web): provision the Android SDK, but ONLY for
# sessions that look like native-app (android/) work — so server, PWA, and prompt
# sessions don't pay the ~150 MB / few-minute SDK download.
#
# A session counts as native-app work if any of these hold:
#   - the branch name mentions android / wear / watch
#   - there are uncommitted/staged changes under android/
#   - the branch has committed changes under android/ vs the default branch
#
# If a session that didn't trigger turns out to need the SDK, just run the installer
# directly:  .claude/hooks/setup-android-sdk.sh
set -euo pipefail

# Web-only; locally you have your own SDK + Android Studio.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}"

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
