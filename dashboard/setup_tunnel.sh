#!/bin/bash
# Remote war room over TAILSCALE - private to YOUR OWN devices (WireGuard, device-authenticated).
# No public exposure, no password to leak, nothing added to git. Run ONCE on the trading Mac:
#
#     bash dashboard/setup_tunnel.sh
#
# What it does:
#   1. Installs Tailscale (if missing) and logs THIS Mac into your tailnet.
#   2. Installs an always-on launchd agent so the war room (127.0.0.1:8090) stays up 24/7 -
#      independent of the trading session, so your phone can reach it any time.
#   3. Runs `tailscale serve` to proxy :8090 to your tailnet over HTTPS.
#      serve = PRIVATE (only your devices). We NEVER use funnel (funnel = public internet); this
#      dashboard has live trade controls, so it must never be reachable off your own tailnet.
# Then: install the Tailscale app on your phone, log into the SAME account, open the printed URL.
# Full guide: dashboard/REMOTE.md
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Find a Tailscale CLI that supports `serve` (standalone app / brew cask; NOT the App Store build).
TS="$(command -v tailscale || true)"
if [ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi

# 1. Install Tailscale if absent.
if [ -z "$TS" ]; then
  echo ">> Tailscale not found. Installing the standalone app (it ships the serve CLI)..."
  if command -v brew >/dev/null 2>&1; then
    brew install --cask tailscale || { echo "brew install failed"; exit 1; }
    TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  else
    echo "Homebrew not found. Install Tailscale manually: https://tailscale.com/download/mac"
    echo "Use the STANDALONE app, not the Mac App Store build (that build lacks serve)."
    exit 1
  fi
fi
echo ">> tailscale CLI: $TS"

# 2. Always-on war room via launchd (KeepAlive across crashes/reboots). Localhost-bound -
#    nothing is exposed until the serve step below.
PLIST="$HOME/Library/LaunchAgents/com.pcnslt.warroom.plist"
pkill -f "dashboard/warroom.py" 2>/dev/null || true   # drop any ad-hoc copy before launchd owns :8090
cp "$REPO/deploy/com.pcnslt.warroom.plist" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo ">> War room installed as an always-on service (com.pcnslt.warroom) on 127.0.0.1:8090."

# 3. Log THIS Mac into your tailnet (opens a browser; free, no credit card). Idempotent if up.
"$TS" up || { echo "tailscale up failed - run '$TS up' yourself, then re-run this script."; exit 1; }

# 4. Serve :8090 to your tailnet over HTTPS. PRIVATE. (Syntax has varied across versions - try a
#    few forms, then always print status so you get the real URL.)
"$TS" serve --bg 8090 2>/dev/null \
  || "$TS" serve --bg http://127.0.0.1:8090 2>/dev/null \
  || "$TS" serve https / http://127.0.0.1:8090 2>/dev/null \
  || echo "Could not auto-configure serve. Run it yourself:  $TS serve --bg 8090"

echo
echo "==================== YOUR PRIVATE WAR-ROOM URL ===================="
"$TS" serve status 2>/dev/null || echo "(run: $TS serve status)"
echo "=================================================================="
echo "Install the Tailscale app on your phone, log into the SAME account, then open the"
echo "https://<mac-name>.<your-tailnet>.ts.net URL above. Only your own devices can reach it."
