#!/bin/bash
# Remote access to the VIEW-ONLY dashboard over a free Cloudflare tunnel. Idempotent:
#
#     bash dashboard/setup_remote.sh
#
# What it does, in order:
#   1. Installs cloudflared (brew; free, no account needed for the binary).
#   2. Has YOU set the dashboard password — it is hashed (sha256) into
#      ~/.config/odte/dash_auth, OUTSIDE the repo. Claude/scripts never see or store the
#      plaintext; the repo contains no secret. The server enforces this on every request
#      (no localhost bypass — the tunnel arrives as localhost).
#   3. Installs + starts the tunnel launchd agent (com.pcnslt.dashboard-tunnel):
#        - QUICK mode (default, zero-account): free trycloudflare.com URL, printed below
#          and appended to logs/tunnel.log. The URL ROTATES if the tunnel restarts.
#        - NAMED mode (optional, stable URL + Cloudflare Access email-OTP): requires a
#          free Cloudflare account AND a domain on it. Run `cloudflared tunnel login`,
#          create a tunnel + ~/.cloudflared/config.yml per dashboard/README.md; this
#          script then auto-uses it (config detected -> named mode).
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AUTH="$HOME/.config/odte/dash_auth"
LA="$HOME/Library/LaunchAgents"

# 1. cloudflared
if ! command -v cloudflared >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/cloudflared ]; then
  echo ">> installing cloudflared (free)..."
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared || { echo "brew install failed"; exit 1; }
  else
    echo "Homebrew missing. Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
  fi
fi

# 2. owner-set password (hash only; plaintext never touches disk or the repo)
if [ ! -s "$AUTH" ]; then
  mkdir -p "$(dirname "$AUTH")"
  printf "Choose a dashboard username: "; read -r DU
  printf "Choose a dashboard password (input hidden): "; read -rs DP; echo
  printf "%s:%s\n" "$DU" "$(printf "%s" "$DP" | shasum -a 256 | cut -d' ' -f1)" > "$AUTH"
  chmod 600 "$AUTH"; unset DP
  echo ">> credentials hash written to $AUTH (outside git; chmod 600)"
else
  echo ">> auth file already present: $AUTH"
fi

# 3. tunnel agent
cp "$REPO/deploy/com.pcnslt.dashboard-tunnel.plist" "$LA/"
launchctl unload "$LA/com.pcnslt.dashboard-tunnel.plist" 2>/dev/null
launchctl load "$LA/com.pcnslt.dashboard-tunnel.plist"
echo ">> tunnel agent loaded; waiting for the URL..."
sleep 8
URL=$(grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" "$REPO/logs/tunnel.log" 2>/dev/null | tail -1)
echo
echo "==================== PHONE URL ===================="
if [ -n "${URL:-}" ]; then
  echo "  $URL   (Basic-Auth login: the username/password YOU just set)"
  echo "  NOTE: quick-tunnel URLs rotate when the tunnel restarts;"
  echo "        current URL is always the last one in logs/tunnel.log"
else
  echo "  URL not up yet — in ~30s run:  grep trycloudflare $REPO/logs/tunnel.log | tail -1"
fi
echo "  Stable URL + email-OTP login instead: see 'Named mode' in dashboard/README.md"
echo "==================================================="
