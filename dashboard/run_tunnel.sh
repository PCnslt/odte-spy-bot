#!/bin/bash
# Tunnel shim for the VIEW-ONLY dashboard. Called by launchd (com.pcnslt.dashboard-tunnel).
#
# SAFETY GATE: refuses to expose anything until the owner has created the Basic-Auth
# credentials file (~/.config/odte/dash_auth) — the dashboard shows account P&L and must
# never sit on a public URL unauthenticated. Exits 0 when preconditions are missing so
# launchd (KeepAlive SuccessfulExit=false) does not thrash.
#
# Modes (auto-detected):
#   named  — ~/.cloudflared/config.yml exists (owner ran the full setup with a Cloudflare
#            account + domain): `cloudflared tunnel run` -> stable URL, Cloudflare Access.
#   quick  — no config: free TryCloudflare quick tunnel -> random https URL, printed to
#            logs/tunnel.log. No account needed. URL changes if the tunnel restarts.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AUTH="$HOME/.config/odte/dash_auth"
CF="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"

if [ ! -x "$CF" ]; then
  echo "cloudflared not installed — run: bash dashboard/setup_remote.sh"; exit 0
fi
if [ ! -s "$AUTH" ]; then
  echo "REFUSING to open a public tunnel: no auth file at $AUTH."
  echo "Create it (never in git):  bash dashboard/setup_remote.sh"
  exit 0
fi

mkdir -p "$REPO/logs"
if [ -s "$HOME/.cloudflared/config.yml" ]; then
  echo "$(date) starting NAMED tunnel (stable URL; Cloudflare Access governs auth upstream)"
  exec "$CF" tunnel run 2>>"$REPO/logs/tunnel.log"
else
  echo "$(date) starting QUICK tunnel (URL below; rotates on restart)"
  # TryCloudflare prints the URL on stderr; tee it into tunnel.log so the owner can grep it.
  exec "$CF" tunnel --url "http://127.0.0.1:8090" 2>&1 | tee -a "$REPO/logs/tunnel.log"
fi
