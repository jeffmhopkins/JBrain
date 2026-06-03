#!/bin/sh
# JBrain auto-updater sidecar. Watches the shared data volume for an update
# request written by the API (POST /api/system/update) and applies it:
# pulls the latest release, rebuilds the api image, and restarts it.
#
# Non-destructive: the DB + Caddy certs (named volumes) and .env are untouched;
# migrations run on the api's next boot. Only the `api` service is rebuilt, so
# this updater and Caddy keep running throughout.
set -eu

# docker:cli is Alpine; ensure git + the compose plugin are present.
apk add --no-cache git docker-cli-compose >/dev/null 2>&1 || true
git config --global --add safe.directory /repo >/dev/null 2>&1 || true

MARKER=/data/update-requested.json
DEPLOY_DIR=/repo/deploy-status   # shared with update.sh, the API and Caddy (live console)
mkdir -p "$DEPLOY_DIR" 2>/dev/null || true
echo "[updater] watching for update requests…"

while true; do
  if [ -f "$MARKER" ]; then
    printf '{"state":"running","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_DIR/status.json" 2>/dev/null || true
    # Tee the whole update to the shared log so the PWA can stream it; record status.
    {
      echo "[updater] update requested at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      cd /repo
      if git fetch --tags --prune origin 2>/dev/null; then
        TARGET="$(git tag --sort=-v:refname | head -n1 || true)"
        [ -z "$TARGET" ] && TARGET="origin/main"
        echo "[updater] checking out $TARGET"
        git checkout "$TARGET" 2>/dev/null || git pull --ff-only 2>/dev/null || true
      else
        echo "[updater] git fetch failed (offline?); skipping this cycle"
      fi
      echo "[updater] rebuilding api…"
      export GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
      if docker compose build api && docker compose up -d api; then
        echo "[updater] update applied -> ${TARGET:-unknown}"
        printf '{"state":"ok","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_DIR/status.json" 2>/dev/null || true
        # Reclaim the now-dangling previous image + build cache (volumes untouched).
        docker image prune -f >/dev/null 2>&1 || true
        docker builder prune -f >/dev/null 2>&1 || true
      else
        echo "[updater] rebuild failed; leaving current version running. Recent API logs:"
        docker compose logs --tail=120 api 2>&1 || true   # capture the traceback for the console
        printf '{"state":"failed","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_DIR/status.json" 2>/dev/null || true
      fi
    } 2>&1 | tee "$DEPLOY_DIR/update.log"
    rm -f "$MARKER" 2>/dev/null || true
  fi
  sleep 30
done
