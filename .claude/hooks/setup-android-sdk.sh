#!/bin/bash
# Installs the Android SDK packages the android/ build needs (also embeds the Wear OS
# watch app), points the project at them via local.properties, and — when run from the
# SessionStart hook — persists the SDK location for the session. Idempotent and safe to
# run directly at any time:  .claude/hooks/setup-android-sdk.sh
set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SDK_ROOT="$HOME/android-sdk"
CMDLINE_TOOLS="$SDK_ROOT/cmdline-tools/latest/bin"
CMDLINE_ZIP_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
# Versions the build pins (compileSdk 35 / build-tools 35.0.0 in app/build.gradle.kts).
PKGS=("platform-tools" "platforms;android-35" "build-tools;35.0.0")
LOG="$(mktemp)"

# 1. Command-line tools.
if [ ! -x "$CMDLINE_TOOLS/sdkmanager" ]; then
  echo "Installing Android command-line tools..." >&2
  mkdir -p "$SDK_ROOT/cmdline-tools"
  tmp_zip="$(mktemp --suffix=.zip)"
  curl -fsSL -o "$tmp_zip" "$CMDLINE_ZIP_URL"
  rm -rf "$SDK_ROOT/cmdline-tools/latest" "$SDK_ROOT/cmdline-tools/cmdline-tools"
  unzip -q "$tmp_zip" -d "$SDK_ROOT/cmdline-tools"
  mv "$SDK_ROOT/cmdline-tools/cmdline-tools" "$SDK_ROOT/cmdline-tools/latest"
  rm -f "$tmp_zip"
fi

export ANDROID_SDK_ROOT="$SDK_ROOT"
export ANDROID_HOME="$SDK_ROOT"

# 2. Licenses + packages (sdkmanager is idempotent).
yes | "$CMDLINE_TOOLS/sdkmanager" --licenses >"$LOG" 2>&1 || true
if ! "$CMDLINE_TOOLS/sdkmanager" "${PKGS[@]}" >>"$LOG" 2>&1; then
  echo "Android SDK package install failed; see log:" >&2
  tail -20 "$LOG" >&2
  rm -f "$LOG"
  exit 1
fi
rm -f "$LOG"

# 3. Point the Gradle build at the SDK (local.properties is gitignored / per-machine).
printf 'sdk.dir=%s\n' "$SDK_ROOT" > "$PROJECT_DIR/android/local.properties"

# 4. Persist the SDK location for the session (only meaningful inside the hook).
if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -q 'ANDROID_SDK_ROOT=' "$CLAUDE_ENV_FILE" 2>/dev/null; then
  {
    echo "export ANDROID_SDK_ROOT=\"$SDK_ROOT\""
    echo "export ANDROID_HOME=\"$SDK_ROOT\""
  } >> "$CLAUDE_ENV_FILE"
fi

echo "Android SDK ready at $SDK_ROOT — build with: cd android && ./gradlew :app:assembleRelease"
