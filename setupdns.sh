#!/usr/bin/env bash
# JBrain: switch a LOCAL-mode install over to a public domain with automatic
# Let's Encrypt HTTPS. Re-renders ./Caddyfile from Caddyfile.template, updates
# .env, and reloads Caddy so it begins the ACME certificate flow. Safe to re-run
# (and handy for changing the domain later). Your database, certs, and keys are
# untouched — only the Caddy site address and a couple of .env values change.
set -euo pipefail
cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
err()  { printf '\033[31m%s\033[0m\n' "$1" >&2; }

[[ -f .env ]] || { err "No .env found. Run ./install.sh first."; exit 1; }
[[ -f Caddyfile.template ]] || { err "Caddyfile.template missing — are you in the JBrain repo?"; exit 1; }

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

_envval() { grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2-; }

_setenv() { # _setenv KEY VALUE — replace in place (preserving order/perms) or append
  local k="$1" v="$2" tmp found=0
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$k="* ]]; then printf '%s=%s\n' "$k" "$v"; found=1
    else printf '%s\n' "$line"; fi
  done < .env > "$tmp"
  [[ "$found" == 1 ]] || printf '%s=%s\n' "$k" "$v" >> "$tmp"
  cat "$tmp" > .env && rm -f "$tmp"   # cat (not mv) keeps .env's 600 perms
}

CUR_DOMAIN="$(_envval JBRAIN_DOMAIN)"
CUR_EMAIL="$(_envval ACME_EMAIL)"
KEY="$(_envval JBRAIN_ACCESS_KEY)"

bold "=== JBrain: enable public HTTPS (Let's Encrypt) ==="
echo
# In local mode JBRAIN_DOMAIN holds the LAN IP — don't offer that as the domain default.
DOMAIN_DEFAULT="$CUR_DOMAIN"
[[ "$(_envval JBRAIN_MODE)" == "local" ]] && DOMAIN_DEFAULT="brain.example.com"
ask DOMAIN "Public domain (its DNS A record must point at this VM)" "$DOMAIN_DEFAULT"
ask EMAIL  "Email for Let's Encrypt cert notices"                   "${CUR_EMAIL:-}"
[[ -n "$EMAIL" ]] || { err "An email is required for Let's Encrypt."; exit 1; }
echo

# --- Best-effort DNS sanity check ------------------------------------------
# Not authoritative (DNS caches, split-horizon, proxies) — just a friendly heads-up.
pubip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)"
resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
[[ -z "$resolved" ]] && resolved="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
if [[ -n "$resolved" && -n "$pubip" && "$resolved" == "$pubip" ]]; then
  info "DNS OK: $DOMAIN → $resolved (matches this VM's public IP)."
elif [[ -z "$resolved" ]]; then
  warn "Could not resolve $DOMAIN yet — DNS may not have propagated."
  warn "You can proceed; Caddy keeps retrying until it resolves to this VM."
  read -r -p "Continue anyway? [y/N] " c; [[ "${c,,}" == "y" ]] || { info "Aborted — no changes made."; exit 0; }
elif [[ -n "$pubip" && "$resolved" != "$pubip" ]]; then
  warn "Heads up: $DOMAIN resolves to $resolved, but this VM's public IP looks like $pubip."
  warn "If that's not right yet, the cert won't issue until DNS points here."
  read -r -p "Continue anyway? [y/N] " c; [[ "${c,,}" == "y" ]] || { info "Aborted — no changes made."; exit 0; }
fi
echo

# --- Re-render the public Caddyfile ----------------------------------------
# Inject a bcrypt of the access key to gate the live deploy console (best-effort,
# mirroring update.sh); if hashing can't run, leave the placeholder for ./update.sh.
HASH="$(docker run --rm caddy:2 caddy hash-password --plaintext "$KEY" 2>/dev/null | tr -d '\r' || true)"
if [[ -n "$HASH" ]]; then
  sed -e "s|{{DOMAIN}}|$DOMAIN|g" -e "s|{{ACME_EMAIL}}|$EMAIL|g" -e "s|{{LOG_AUTH_HASH}}|$HASH|g" \
    Caddyfile.template > Caddyfile
else
  sed -e "s|{{DOMAIN}}|$DOMAIN|g" -e "s|{{ACME_EMAIL}}|$EMAIL|g" \
    Caddyfile.template > Caddyfile
fi
info "Wrote Caddyfile for $DOMAIN (Let's Encrypt HTTPS)."

# --- Update .env ------------------------------------------------------------
_setenv JBRAIN_MODE   public
_setenv JBRAIN_HOST   "$DOMAIN"
_setenv JBRAIN_DOMAIN "$DOMAIN"
_setenv ACME_EMAIL    "$EMAIL"
_setenv VAPID_SUBJECT "mailto:$EMAIL"
info "Updated .env (mode=public, domain=$DOMAIN)."

# --- Reload Caddy -----------------------------------------------------------
# `caddy reload` is atomic: a bad config is rejected and the running one stays up,
# so a typo can't take the site down. Start the stack first if it isn't up.
DC="docker compose"; $DC version >/dev/null 2>&1 || DC="docker-compose"
echo
info "Reloading Caddy to pick up the new domain…"
$DC up -d caddy >/dev/null 2>&1 || true
if $DC exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  info "Caddy reloaded."
else
  warn "Live reload didn't take (Caddy may be starting); restarting the caddy service…"
  $DC restart caddy >/dev/null 2>&1 || $DC up -d caddy >/dev/null 2>&1 || true
fi

echo
bold "Done. Caddy is now requesting a Let's Encrypt certificate for $DOMAIN."
echo "Make sure:  • DNS A record → this VM   • TCP 80 and 443 open to the internet."
echo "Watch it issue the cert (usually a few seconds once DNS/ports are right):"
echo "    docker compose logs -f caddy"
echo
echo "Then open:  https://$DOMAIN  (your access key is unchanged)."
