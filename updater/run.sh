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
# This container runs ONE long-lived shell holding the run.sh it started with. A deploy
# git-checks-out a new run.sh on disk, but we'd keep running the stale in-memory copy —
# which is exactly how the live-console capture silently stops working. Remember our own
# hash so we can re-exec the new script after an update that changes it.
SELF=/repo/updater/run.sh
_selfhash() { md5sum "$SELF" 2>/dev/null | cut -d' ' -f1; }
SELF_HASH="$(_selfhash)"
export BUILDKIT_PROGRESS=plain   # line-buffered build output so the console streams live
# status.json carries a coarse PHASE so the PWA indicator follows progress.
_ustatus() { printf '{"state":"%s","phase":"%s","at":"%s"}\n' "$1" "${2:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_DIR/status.json" 2>/dev/null || true; }
echo "[updater] watching for update requests…"

while true; do
  if [ -f "$MARKER" ]; then
    _ustatus running starting
    # Tee the whole update to the shared log so the PWA can stream it; record status.
    {
      echo "[updater] update requested at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      cd /repo
      _ustatus running fetching
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
      _ustatus running building
      if docker compose build api && { _ustatus running restarting; docker compose up -d api; }; then
        _ustatus running health-checking
        # Don't call it OK just because `up` returned — wait for the new container to
        # actually pass health (≈90s, covers the embedding warmup), else mark failed.
        healthy=0; i=0
        while [ "$i" -lt 45 ]; do
          if docker compose exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health',timeout=3).status==200 else 1)" >/dev/null 2>&1; then
            healthy=1; break
          fi
          i=$((i + 1)); sleep 2
        done
        if [ "$healthy" = 1 ]; then
          echo "[updater] update applied -> ${TARGET:-unknown}"
          _ustatus ok
          # Reclaim the now-dangling previous image + build cache (volumes untouched).
          docker image prune -f >/dev/null 2>&1 || true
          docker builder prune -f >/dev/null 2>&1 || true
        else
          echo "[updater] new container did not become healthy. Recent API logs:"
          docker compose logs --tail=120 api 2>&1 || true   # capture the traceback for the console
          _ustatus failed
        fi
      else
        echo "[updater] rebuild failed; leaving current version running. Recent API logs:"
        docker compose logs --tail=120 api 2>&1 || true   # capture the traceback for the console
        _ustatus failed
      fi
    } 2>&1 | tee "$DEPLOY_DIR/update.log"
    rm -f "$MARKER" 2>/dev/null || true
    # If that deploy shipped a new run.sh, re-exec it so the updater never keeps
    # running stale logic (the very bug that breaks the live console). Safe: only
    # re-execs when the file actually changed, so it can't loop.
    if [ "$(_selfhash)" != "$SELF_HASH" ]; then
      echo "[updater] run.sh changed in this update — relaunching with the new version"
      exec sh "$SELF"
    fi
  fi
  sleep 5
done
