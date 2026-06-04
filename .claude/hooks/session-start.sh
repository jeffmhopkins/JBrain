#!/bin/bash
# SessionStart hook: make Claude Code on the web able to build the Android app
# (android/, which also embeds the Wear OS watch app). It installs the Android SDK
# packages the build needs, points the project at them via local.properties, and
# exports the SDK location for the session. Idempotent — re-running is a no-op once
# the SDK is in place, and the container state is cached after the first run.
set -euo pipefail

# Only run in the remote (Claude Code on the web) environment; locally you have your
# own SDK and Android Studio.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

SDK_ROOT="$HOME/android-sdk"
CMDLINE_TOOLS="$SDK_ROOT/cmdline-tools/latest/bin"
CMDLINE_ZIP_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
LOG="$(mktemp)"

# Versions the build pins (compileSdk 35 / build-tools 35.0.0 in app/build.gradle.kts).
PKGS=("platform-tools" "platforms;android-35" "build-tools;35.0.0")

# 1. Install the command-line tools if missing.
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

# 2. Accept licenses + install/update the required packages (sdkmanager is idempotent).
yes | "$CMDLINE_TOOLS/sdkmanager" --licenses >"$LOG" 2>&1 || true
if ! "$CMDLINE_TOOLS/sdkmanager" "${PKGS[@]}" >>"$LOG" 2>&1; then
  echo "Android SDK package install failed; see log:" >&2
  tail -20 "$LOG" >&2
  exit 1
fi

# 3. Point the Gradle build at the SDK (local.properties is gitignored / per-machine).
printf 'sdk.dir=%s\n' "$SDK_ROOT" > "$CLAUDE_PROJECT_DIR/android/local.properties"

# 4. Persist the SDK location for the rest of the session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export ANDROID_SDK_ROOT=\"$SDK_ROOT\""
    echo "export ANDROID_HOME=\"$SDK_ROOT\""
  } >> "$CLAUDE_ENV_FILE"
fi

rm -f "$LOG"
echo "Android SDK ready at $SDK_ROOT — build with: cd android && ./gradlew :app:assembleRelease"
