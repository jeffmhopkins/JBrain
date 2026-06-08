#!/usr/bin/env bash
# JBrain one-command installer. Interactively collects config, renders .env and
# Caddyfile, and (optionally) starts the stack. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
err()  { printf '\033[31m%s\033[0m\n' "$1" >&2; }

bold "=== JBrain installer ==="
echo

# --- Prerequisites ----------------------------------------------------------
# Install Docker Engine + Compose v2 via Docker's official convenience script.
# Works on most Linux distros (Ubuntu/Debian/Fedora/CentOS/etc.). The script
# also installs the Compose v2 plugin, so this covers both checks below.
install_docker() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    err "Automatic Docker install is only supported on Linux."
    err "Install Docker Desktop manually: https://docs.docker.com/get-docker/"
    exit 1
  fi

  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo="sudo"
    else
      err "Need root to install Docker, but 'sudo' is not available. Re-run as root."
      exit 1
    fi
  fi

  info "Downloading Docker's official install script (https://get.docker.com)…"
  local script
  script="$(mktemp)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com -o "$script"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$script" https://get.docker.com
  else
    rm -f "$script"
    err "Neither 'curl' nor 'wget' is available to download the installer."
    exit 1
  fi

  info "Installing Docker (this may take a minute and will ask for your password)…"
  $sudo sh "$script"
  rm -f "$script"

  # Enable and start the daemon (no-op if systemd isn't the init system).
  if command -v systemctl >/dev/null 2>&1; then
    $sudo systemctl enable --now docker >/dev/null 2>&1 || true
  fi

  # Let the current non-root user talk to the daemon without sudo.
  if [[ "$(id -u)" -ne 0 ]]; then
    $sudo usermod -aG docker "$USER" >/dev/null 2>&1 || true
    warn "Added '$USER' to the 'docker' group. You may need to log out and back in"
    warn "for that to take effect. This run will fall back to 'sudo docker' if needed."
  fi
}

# Resolve how we invoke docker for this run (handles the group-not-yet-applied case).
DOCKER="docker"

if ! command -v docker >/dev/null 2>&1; then
  warn "Docker is not installed."
  read -r -p "Install Docker Engine + Compose now? [Y/n] " do_install
  if [[ "${do_install,,}" == "n" ]]; then
    err "Docker is required. Install it manually: https://docs.docker.com/engine/install/"
    exit 1
  fi
  install_docker
fi

# Verify the daemon is reachable; fall back to sudo if the group change is pending.
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    err "Docker is installed but the daemon isn't reachable."
    err "Start it (e.g. 'sudo systemctl start docker') and re-run this installer."
    exit 1
  fi
fi

if ! $DOCKER compose version >/dev/null 2>&1; then
  err "Docker Compose v2 is not available ('docker compose'). Install the Compose plugin:"
  err "  https://docs.docker.com/compose/install/linux/"
  exit 1
fi

# --- Don't clobber an existing config without consent -----------------------
if [[ -f .env ]]; then
  warn "An .env already exists."
  warn "Overwriting generates a NEW access key — every device you've connected will need to re-paste it."
  read -r -p "Overwrite it and reconfigure? [y/N] " ans
  [[ "${ans,,}" == "y" ]] || { info "Keeping existing .env. Run 'docker compose up -d --build' to (re)start."; exit 0; }
fi

# --- Helpers ----------------------------------------------------------------
ask() { # ask VAR "Prompt" "default"
  local __var="$1" __prompt="$2" __default="${3:-}" __input
  if [[ -n "$__default" ]]; then
    read -r -p "$__prompt [$__default]: " __input
    __input="${__input:-$__default}"
  else
    read -r -p "$__prompt: " __input
  fi
  printf -v "$__var" '%s' "$__input"
}

ask_secret() { # ask_secret VAR "Prompt"
  local __var="$1" __prompt="$2" __input
  read -r -s -p "$__prompt: " __input; echo
  printf -v "$__var" '%s' "$__input"
}

gen_secret() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# --- Collect config ---------------------------------------------------------
info "Answer a few questions. Press Enter to accept the [default]."
echo
ask        JBRAIN_DOMAIN  "Public domain (A record must point at this VM)" "brain.example.com"
ask        ACME_EMAIL     "Email for Let's Encrypt cert notices"
ask        BRAIN_NAME     "Name for your brain"                            "My Brain"
ask        LLM_MODEL      "LLM model"                                      "claude-sonnet-4-6"
ask_secret LLM_API_KEY    "LLM API key (hidden)"
echo
ask        TZ             "Timezone"                                       "UTC"
echo
echo "Automatic updates run an 'updater' sidecar that applies updates you trigger"
echo "from the app — it mounts the Docker socket and the project directory."
read -r -p "Enable automatic updates? [y/N] " autoupd
COMPOSE_PROFILES=""
[[ "${autoupd,,}" == "y" ]] && COMPOSE_PROFILES="autoupdate"

# The pasteable access key (the "cert"). Generated here; you paste it into the
# app (and watch) on first run. Treat it like a password.
JBRAIN_ACCESS_KEY="$(gen_secret)"
EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"

# --- Render .env ------------------------------------------------------------
umask 077
cat > .env <<EOF
JBRAIN_DOMAIN=$JBRAIN_DOMAIN
ACME_EMAIL=$ACME_EMAIL
LLM_PROVIDER=anthropic
LLM_API_KEY=$LLM_API_KEY
LLM_MODEL=$LLM_MODEL
BRAIN_NAME=$BRAIN_NAME
JBRAIN_ACCESS_KEY=$JBRAIN_ACCESS_KEY
EMBEDDING_MODEL=$EMBEDDING_MODEL
DB_PATH=/data/brain.db
TZ=$TZ
VAPID_SUBJECT=mailto:$ACME_EMAIL
COMPOSE_PROJECT_NAME=jbrain
COMPOSE_PROFILES=$COMPOSE_PROFILES
EOF
info "Wrote .env (permissions 600)."

# --- Render Caddyfile -------------------------------------------------------
sed -e "s|{{DOMAIN}}|$JBRAIN_DOMAIN|g" \
    -e "s|{{ACME_EMAIL}}|$ACME_EMAIL|g" \
    Caddyfile.template > Caddyfile
info "Wrote Caddyfile for $JBRAIN_DOMAIN."

echo
bold "Before HTTPS can work, make sure:"
echo "  1. DNS: an A record for $JBRAIN_DOMAIN points at this VM's public IP."
echo "  2. Firewall: TCP ports 80 and 443 are open to the internet."
echo

bold "Your access key (paste this into the app on first run):"
printf '\033[1;32m    %s\033[0m\n' "$JBRAIN_ACCESS_KEY"
echo "Keep it safe — it's the only credential. It's also stored in .env."
echo

read -r -p "Build and start JBrain now? [Y/n] " go
if [[ "${go,,}" != "n" ]]; then
  info "Building and starting (first run downloads the embedding model)…"
  export GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
  $DOCKER compose up -d --build
  echo
  bold "JBrain is starting. In a minute, open: https://$JBRAIN_DOMAIN"
  echo "Paste the access key above, then use your browser's 'Install app' / 'Add to Home Screen'."
  echo "Logs:  docker compose logs -f"
else
  info "Skipped startup. When ready run:  docker compose up -d --build"
fi
