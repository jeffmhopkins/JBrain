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

# --- Live console capture --------------------------------------------------
# Tee the whole run to a shared file (bind-mounted at ./deploy-status) so the API
# (and, when it's down, Caddy) can serve it to the PWA's live update console, and
# record a status the UI can show. Best-effort: never let this break the update.
DEPLOY_DIR="$(pwd)/deploy-status"
mkdir -p "$DEPLOY_DIR" 2>/dev/null || true
_dstatus() { printf '{"state":"%s","at":"%s"}\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_DIR/status.json" 2>/dev/null || true; }
exec > >(tee "$DEPLOY_DIR/update.log") 2>&1   # mirror all stdout+stderr to the log
trap '_dstatus "$([ $? -eq 0 ] && echo ok || echo failed)"' EXIT
_dstatus running
# ---------------------------------------------------------------------------

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

# Re-render the Caddyfile from the template so Caddy config changes (e.g. the live
# deploy-console route) reach existing installs, injecting a bcrypt of the access key
# to gate it. VALIDATED before swapping — a bad render never replaces a working file.
if [[ -f Caddyfile.template ]]; then
  _envval() { grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2-; }
  CDOMAIN="$(_envval JBRAIN_DOMAIN)"; CEMAIL="$(_envval ACME_EMAIL)"; CKEY="$(_envval JBRAIN_ACCESS_KEY)"
  if [[ -n "$CDOMAIN" && -n "$CKEY" ]]; then
    CHASH="$(docker run --rm caddy:2 caddy hash-password --plaintext "$CKEY" 2>/dev/null | tr -d '\r' || true)"
    if [[ -n "$CHASH" ]]; then
      sed -e "s|{{DOMAIN}}|$CDOMAIN|g" -e "s|{{ACME_EMAIL}}|$CEMAIL|g" -e "s|{{LOG_AUTH_HASH}}|$CHASH|g" \
        Caddyfile.template > Caddyfile.new
      if docker run --rm -v "$(pwd)/Caddyfile.new:/c:ro" caddy:2 caddy validate --config /c --adapter caddyfile >/dev/null 2>&1; then
        mv Caddyfile.new Caddyfile; echo "    Caddyfile re-rendered (live deploy console enabled)."
      else
        rm -f Caddyfile.new; echo "    WARN: rendered Caddyfile failed validation — keeping the existing one." >&2
      fi
    fi
  fi
fi

$DC up -d || { echo "FAIL: compose up failed." >&2; exit 1; }
$DC exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || true

echo "==> Waiting for the API to come up…"
HEALTHY=0
for _ in $(seq 1 45); do   # up to ~90s (covers the embedding-model warmup)
  if $DC exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health',timeout=3).status==200 else 1)" >/dev/null 2>&1; then
    HEALTHY=1; break
  fi
  sleep 2
done
if [[ "$HEALTHY" != 1 ]]; then
  echo "FAIL: API did not become healthy. Recent API logs (the startup error is below):" >&2
  # Capture the container's own logs INTO this run's output so the live update
  # console shows the actual traceback, not just a hint to go look for it.
  $DC logs --tail=120 api 2>&1 || true
  exit 1
fi
echo "    OK — API is healthy."

# Reclaim disk: each rebuild leaves the previous image dangling and grows the
# build cache. Safe to prune now that the new image is up and healthy — named
# volumes (DB, certs, model cache) are never touched by image/builder prune.
echo "==> Reclaiming disk (dangling images + build cache)…"
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f >/dev/null 2>&1 || true

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
