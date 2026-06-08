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

detect_lan_ip() { # this machine's primary LAN IP (the src for the default route)
  local ip=""
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [[ -z "$ip" ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "$ip"
}

render_caddyfile() { # render_caddyfile TEMPLATE HOST_OR_DOMAIN EMAIL ACCESS_KEY
  # Inject the site address, ACME email, and a bcrypt of the access key so the live
  # deploy console at /deploy-status is gated. Hashing is best-effort: if it can't run
  # (offline / image unavailable) the placeholder is left as-is, exactly like before —
  # update.sh fills it in on the next update, and Caddy starts fine either way.
  local tmpl="$1" host="$2" email="$3" key="$4" hash=""
  hash="$($DOCKER run --rm caddy:2 caddy hash-password --plaintext "$key" 2>/dev/null | tr -d '\r' || true)"
  if [[ -n "$hash" ]]; then
    sed -e "s|{{DOMAIN}}|$host|g" -e "s|{{HOST}}|$host|g" \
        -e "s|{{ACME_EMAIL}}|$email|g" -e "s|{{LOG_AUTH_HASH}}|$hash|g" \
        "$tmpl" > Caddyfile
  else
    sed -e "s|{{DOMAIN}}|$host|g" -e "s|{{HOST}}|$host|g" \
        -e "s|{{ACME_EMAIL}}|$email|g" \
        "$tmpl" > Caddyfile
  fi
}

# --- Collect config ---------------------------------------------------------
info "Answer a few questions. Press Enter to accept the [default]."
echo
bold "Deployment mode"
echo "  1) Local network only — reach JBrain at this machine's LAN IP over HTTPS"
echo "     (Caddy issues its own self-signed cert; no domain or DNS needed). You can"
echo "     switch to a public domain anytime later with ./setupdns.sh."
echo "  2) Public domain — automatic Let's Encrypt HTTPS (needs a DNS A record"
echo "     pointing at this VM and ports 80/443 open to the internet)."
read -r -p "Choose [1/2] (1): " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-1}"
echo

if [[ "$MODE_CHOICE" == "2" ]]; then
  JBRAIN_MODE="public"
  ask JBRAIN_HOST  "Public domain (A record must point at this VM)" "brain.example.com"
  ask ACME_EMAIL   "Email for Let's Encrypt cert notices"
else
  JBRAIN_MODE="local"
  ask JBRAIN_HOST  "LAN IP (or hostname) to reach this machine on" "$(detect_lan_ip)"
  ask ACME_EMAIL   "Contact email (for push now + the HTTPS cert later — optional)" ""
fi
# JBRAIN_HOST is the Caddy site address either way; JBRAIN_DOMAIN feeds the app
# (share-link URLs, CORS context) and equals the host until ./setupdns.sh sets a domain.
JBRAIN_DOMAIN="$JBRAIN_HOST"

ask        BRAIN_NAME     "Name for your brain"                            "My Brain"
echo

# LLM provider — pick one; it sets LLM_PROVIDER and the default model id.
echo "Which LLM provider?"
echo "  1) Anthropic (Claude)   [default]"
echo "  2) xAI (Grok)"
read -r -p "Choose [1]: " prov_choice
case "${prov_choice:-1}" in
  2) LLM_PROVIDER="xai";       LLM_MODEL_DEFAULT="grok-4.3" ;;
  *) LLM_PROVIDER="anthropic"; LLM_MODEL_DEFAULT="claude-sonnet-4-6" ;;
esac
ask        LLM_MODEL      "LLM model"                                      "$LLM_MODEL_DEFAULT"
ask_secret LLM_API_KEY    "LLM API key (hidden)"
echo

# Timezone — pick a common IANA zone or type your own. Defaults to Florida (Eastern).
echo "Timezone (IANA name). Common US zones:"
echo "  1) America/New_York     (Eastern — Florida)   [default]"
echo "  2) America/Chicago      (Central)"
echo "  3) America/Denver       (Mountain)"
echo "  4) America/Phoenix      (Mountain, no DST)"
echo "  5) America/Los_Angeles  (Pacific)"
echo "  6) America/Anchorage    (Alaska)"
echo "  7) Pacific/Honolulu     (Hawaii)"
echo "  8) UTC"
echo "  9) Other (type a custom IANA name)"
read -r -p "Choose [1]: " tz_choice
case "${tz_choice:-1}" in
  2) TZ="America/Chicago" ;;
  3) TZ="America/Denver" ;;
  4) TZ="America/Phoenix" ;;
  5) TZ="America/Los_Angeles" ;;
  6) TZ="America/Anchorage" ;;
  7) TZ="Pacific/Honolulu" ;;
  8) TZ="UTC" ;;
  9) ask TZ "Enter IANA timezone (e.g. Europe/London)" "America/New_York" ;;
  *) TZ="America/New_York" ;;
esac
info "Timezone: $TZ"
echo
echo "Automatic updates run an 'updater' sidecar that applies updates you trigger"
echo "from the app — it mounts the Docker socket and the project directory."
read -r -p "Enable automatic updates? [y/N] " autoupd
COMPOSE_PROFILES=""
[[ "${autoupd,,}" == "y" ]] && COMPOSE_PROFILES="autoupdate"

# --- Optional local LLM (Ollama) -------------------------------------------
# Runs routine jobs (tags, summaries, filing) on a local model — no API key, nothing
# leaves the box. The cloud provider above still handles the chat agent. CPU inference
# is RAM-bandwidth bound, so model size is gated by how much RAM the box has.
echo
echo "Optional: run a LOCAL LLM on this box (Ollama) for routine jobs — no API key,"
echo "nothing leaves the machine. The cloud provider above still handles the chat agent."
read -r -p "Enable a local LLM (Ollama)? [y/N] " localllm_ans
LLM_LOCAL_ENABLE="false"
LLM_LOCAL_MODEL=""
OLLAMA_MEM_LIMIT="8g"
LLM_TIMEOUT_SECONDS="120"
if [[ "${localllm_ans,,}" == "y" ]]; then
  LLM_LOCAL_ENABLE="true"
  LLM_TIMEOUT_SECONDS="600"        # CPU inference can be slow — give requests room
  if [[ -n "$COMPOSE_PROFILES" ]]; then COMPOSE_PROFILES="$COMPOSE_PROFILES,localllm"; else COMPOSE_PROFILES="localllm"; fi
  echo
  echo "Pick a model to pull on first boot. The RAM figure is the resident footprint —"
  echo "your box MUST have at least that much free, or the model won't load."
  echo "  Small — fine on a 32 GB / CPU box (the recommended cheap-tier sweet spot):"
  echo "    1) qwen2.5:7b    ~6 GB RAM   routine jobs (recommended)   [default]"
  echo "    2) llama3.1:8b   ~6 GB RAM   alternative routine model"
  echo "    3) llava:7b      ~6 GB RAM   vision (image analysis)"
  echo "  Large — need a HIGH-MEMORY box (e.g. a 128 GB Strix Halo); too big for 32 GB:"
  echo "    4) qwen2.5:14b   ~12 GB RAM  stronger agent"
  echo "    5) qwen2.5:32b   ~26 GB RAM  high-quality agent"
  echo "    6) llama3.3:70b  ~56 GB RAM  top quality"
  echo "    7) gpt-oss:120b  ~85 GB RAM  largest (MoE)"
  echo "    8) Other (type an Ollama model tag)"
  read -r -p "Choose [1]: " m_choice
  case "${m_choice:-1}" in
    2) LLM_LOCAL_MODEL="llama3.1:8b" ;;
    3) LLM_LOCAL_MODEL="llava:7b" ;;
    4) LLM_LOCAL_MODEL="qwen2.5:14b";  OLLAMA_MEM_LIMIT="16g" ;;
    5) LLM_LOCAL_MODEL="qwen2.5:32b";  OLLAMA_MEM_LIMIT="28g" ;;
    6) LLM_LOCAL_MODEL="llama3.3:70b"; OLLAMA_MEM_LIMIT="60g" ;;
    7) LLM_LOCAL_MODEL="gpt-oss:120b"; OLLAMA_MEM_LIMIT="96g" ;;
    8) ask LLM_LOCAL_MODEL "Ollama model tag (e.g. mistral:7b)" "qwen2.5:7b" ;;
    *) LLM_LOCAL_MODEL="qwen2.5:7b" ;;
  esac
  info "Local model: $LLM_LOCAL_MODEL — pulls on first boot; routine jobs route to it,"
  info "and you can pull/assign more later in System → Local models."
  warn "Make sure this box has the RAM for $LLM_LOCAL_MODEL (see the note above), or it won't load."
fi

# The pasteable access key (the "cert"). Generated here; you paste it into the
# app (and watch) on first run. Treat it like a password.
JBRAIN_ACCESS_KEY="$(gen_secret)"
EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
VAPID_EMAIL="${ACME_EMAIL:-admin@localhost}"

# --- Render .env ------------------------------------------------------------
umask 077
cat > .env <<EOF
JBRAIN_MODE=$JBRAIN_MODE
JBRAIN_HOST=$JBRAIN_HOST
JBRAIN_DOMAIN=$JBRAIN_DOMAIN
ACME_EMAIL=$ACME_EMAIL
LLM_PROVIDER=$LLM_PROVIDER
LLM_API_KEY=$LLM_API_KEY
LLM_MODEL=$LLM_MODEL
LLM_LOCAL_ENABLE=$LLM_LOCAL_ENABLE
LLM_LOCAL_MODEL=$LLM_LOCAL_MODEL
LLM_TIMEOUT_SECONDS=$LLM_TIMEOUT_SECONDS
OLLAMA_MEM_LIMIT=$OLLAMA_MEM_LIMIT
BRAIN_NAME=$BRAIN_NAME
JBRAIN_ACCESS_KEY=$JBRAIN_ACCESS_KEY
EMBEDDING_MODEL=$EMBEDDING_MODEL
DB_PATH=/data/brain.db
TZ=$TZ
VAPID_SUBJECT=mailto:$VAPID_EMAIL
COMPOSE_PROJECT_NAME=jbrain
COMPOSE_PROFILES=$COMPOSE_PROFILES
EOF
info "Wrote .env (permissions 600)."

# --- Render Caddyfile -------------------------------------------------------
if [[ "$JBRAIN_MODE" == "public" ]]; then
  render_caddyfile Caddyfile.template "$JBRAIN_HOST" "$ACME_EMAIL" "$JBRAIN_ACCESS_KEY"
  info "Wrote Caddyfile for $JBRAIN_HOST (Let's Encrypt HTTPS)."
else
  render_caddyfile Caddyfile.local.template "$JBRAIN_HOST" "$ACME_EMAIL" "$JBRAIN_ACCESS_KEY"
  info "Wrote Caddyfile for https://$JBRAIN_HOST (local self-signed HTTPS)."
fi

echo
if [[ "$JBRAIN_MODE" == "public" ]]; then
  bold "Before HTTPS can work, make sure:"
  echo "  1. DNS: an A record for $JBRAIN_HOST points at this VM's public IP."
  echo "  2. Firewall: TCP ports 80 and 443 are open to the internet."
  ACCESS_URL="https://$JBRAIN_HOST"
else
  bold "Local-network mode:"
  echo "  • Reach JBrain at https://$JBRAIN_HOST from devices on the same network."
  echo "  • The cert is self-signed, so browsers show a one-time warning — click"
  echo "    through it (HTTPS is required for the PWA/offline features to work). To"
  echo "    silence it, install Caddy's root CA (from the 'caddy-data' volume) on"
  echo "    your devices."
  echo "  • Tip: give this machine a static IP / DHCP reservation so $JBRAIN_HOST"
  echo "    doesn't change. Ready to go public? Run ./setupdns.sh."
  ACCESS_URL="https://$JBRAIN_HOST"
fi
echo

bold "Your access key (paste this into the app on first run):"
printf '\033[1;32m    %s\033[0m\n' "$JBRAIN_ACCESS_KEY"
echo "Keep it safe — it's the only credential. It's also stored in .env."
echo

read -r -p "Build and start JBrain now? [Y/n] " go
if [[ "${go,,}" != "n" ]]; then
  info "Building and starting (first run downloads the embedding model)…"
  [[ "$LLM_LOCAL_ENABLE" == "true" ]] && \
    warn "Local LLM on: first boot also pulls $LLM_LOCAL_MODEL (several GB, in the background)." && \
    warn "Watch it with:  docker compose logs -f ollama-pull   — until it finishes, routine jobs use the cloud."
  export GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
  $DOCKER compose up -d --build
  echo
  bold "JBrain is starting. In a minute, open: $ACCESS_URL"
  echo "Paste the access key above, then use your browser's 'Install app' / 'Add to Home Screen'."
  echo "Logs:  docker compose logs -f"
else
  info "Skipped startup. When ready run:  docker compose up -d --build"
fi
