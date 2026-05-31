#!/usr/bin/env bash
# JBrain non-destructive updater. Pulls the latest release and rebuilds.
#
# What is preserved (NOT touched):
#   - the SQLite database and Caddy certs  -> Docker named volumes
#   - your access key / API keys / config  -> .env (a persistent file)
# Schema changes are applied automatically by the migration runner on boot.
#
# Usage:
#   ./update.sh            # update to the latest release tag
#   ./update.sh v0.2.0     # update to a specific tag/branch
#
# To let the PWA "Update" button trigger this automatically, point
# JBRAIN_UPDATE_CMD at it (it runs in the api container), or wire a host watcher
# to the /data/update-requested.json marker the server writes.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Fetching latest from origin…"
git fetch --tags --prune origin

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  TARGET="$(git tag --sort=-v:refname | head -n1 || true)"
  [[ -z "$TARGET" ]] && TARGET="origin/main"
fi
echo "==> Updating to: $TARGET"
git checkout "$TARGET"
git pull --ff-only origin "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null || true

echo "==> Rebuilding and restarting (volumes + .env preserved)…"
export GIT_SHA="$(git rev-parse HEAD)"
docker compose up -d --build

# Clear any pending update marker.
rm -f data/update-requested.json 2>/dev/null || true
echo "==> Done. Migrations run automatically on boot; check: docker compose logs -f"
