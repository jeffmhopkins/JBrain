#!/usr/bin/env bash
# JBrain non-destructive updater. Pulls the latest main, rebuilds the api image,
# restarts it, and verifies the new container is healthy.
#
# What is preserved (NOT touched):
#   - the SQLite database and Caddy certs  -> Docker named volumes
#   - your access key / API keys / config  -> .env (a persistent file)
# Schema changes are applied automatically by the migration runner on boot.
#
# Usage:
#   ./update.sh            # update to the latest origin/main
#   ./update.sh v0.2.0     # update to a specific tag/branch instead
set -euo pipefail
cd "$(dirname "$0")"
[[ -f docker-compose.yml ]] || { echo "FAIL: run update.sh from the JBrain repo ($(pwd))." >&2; exit 1; }

DC="docker compose"; docker compose version >/dev/null 2>&1 || DC="docker-compose"

echo "==> Fetching latest from origin…"
git fetch --prune origin || { echo "FAIL: git fetch failed (offline?). Nothing changed." >&2; exit 1; }

TARGET="${1:-main}"
BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "==> Updating to: $TARGET (current $BEFORE)"
git checkout "$TARGET" >/dev/null 2>&1 || { echo "FAIL: could not checkout '$TARGET'." >&2; exit 1; }
if [[ "$TARGET" == "main" ]]; then
  git merge --ff-only origin/main \
    || { echo "FAIL: local history diverged from main; resolve it first (your data is untouched)." >&2; exit 1; }
fi
AFTER="$(git rev-parse --short HEAD)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  echo "    Already at $AFTER — rebuilding anyway to apply any .env/local changes."
else
  echo "    $BEFORE -> $AFTER:"
  git --no-pager log --oneline "$BEFORE..$AFTER" 2>/dev/null | sed 's/^/      /' || true
fi

echo "==> Rebuilding and restarting (volumes + .env preserved)…"
export GIT_SHA="$(git rev-parse HEAD)"
$DC build api || { echo "FAIL: image build failed — current version is still running (see output above)." >&2; exit 1; }
$DC up -d || { echo "FAIL: compose up failed." >&2; exit 1; }

echo "==> Waiting for the API to come up…"
HEALTHY=0
for _ in $(seq 1 45); do   # up to ~90s (covers the embedding-model warmup)
  if $DC exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health',timeout=3).status==200 else 1)" >/dev/null 2>&1; then
    HEALTHY=1; break
  fi
  sleep 2
done
if [[ "$HEALTHY" != 1 ]]; then
  echo "FAIL: API did not become healthy. Check: $DC logs --tail=50 api" >&2
  exit 1
fi
echo "    OK — API is healthy."

echo "==> Status:"
$DC exec -T api python - <<'PY' 2>/dev/null || echo "    (could not read the database for a summary)"
import sqlite3
c = sqlite3.connect("/data/brain.db")
g = lambda k: (c.execute("select value from meta where key=?", (k,)).fetchone() or [None])[0]
print("    schema version : %s" % g("schema_version"))
print("    VAPID key       : %s" % ("present" if g("vapid_public_key") else "MISSING"))
try:
    print("    push devices    : %s" % c.execute("select count(*) from push_subscriptions").fetchone()[0])
except Exception:
    print("    push devices    : (table missing — migration may not have run)")
PY
$DC ps --format 'table {{.Service}}\t{{.Status}}' 2>/dev/null | sed 's/^/    /' || $DC ps

rm -f data/update-requested.json 2>/dev/null || true
echo "==> Done — now at $AFTER. On your phone, fully close and reopen the app to pick up the new service worker."
