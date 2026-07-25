#!/bin/bash
# Cold-start bootstrap: fresh clone -> runnable system. For any operator or agent taking
# over this repo (docs/HANDOVER.md is the map; read it first).
#
#     git clone https://github.com/PCnslt/odte-spy-bot ~/trading/odte-spy-bot
#     cd ~/trading/odte-spy-bot && bash scripts/bootstrap.sh
#
# NOTE the deployment path: launchd cannot read iCloud Drive paths — the runtime checkout
# MUST live at ~/trading/odte-spy-bot (all plists hardcode it).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo ">> 1/6 python venv + deps"
python3 -m venv venv 2>/dev/null || true
venv/bin/pip -q install --upgrade pip
venv/bin/pip -q install -r requirements.txt
[ -f requirements-extras.txt ] && venv/bin/pip -q install -r requirements-extras.txt

echo ">> 2/6 secrets (.env) — NEVER committed"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "   EDIT .env now: add POLYGON_API_KEY (and any keys listed in .env.example)."
fi

echo ">> 3/6 runtime dirs"
mkdir -p logs/quotes docs/dashboard/history

echo ">> 4/6 test suite (MUST be green before anything trades)"
venv/bin/python -m pytest -q || { echo "RED SUITE — stop here and fix."; exit 1; }

echo ">> 5/6 launchd agents (bot 09:25 weekdays, dashboard :8090, 2FA reminder Sun 18:30)"
LA="$HOME/Library/LaunchAgents"
for p in com.pcnslt.odte-spy-bot com.pcnslt.dashboard com.pcnslt.2fa-reminder; do
  cp "deploy/$p.plist" "$LA/"
  launchctl unload "$LA/$p.plist" 2>/dev/null || true
  launchctl load "$LA/$p.plist"
  echo "   loaded $p"
done
echo "   (optional remote tunnel: bash dashboard/setup_remote.sh)"

echo ">> 6/6 manual steps NO script can do (see docs/HANDOVER.md):"
echo "   - Install IB Gateway, log into the PAPER account (DU-prefixed), enable API on port 4002."
echo "   - Weekly Sunday-evening 2FA login (the reminder agent nags; missing it idles the week)."
echo "   - Keep the Mac awake and plugged in on trading days."
echo
echo "BOOTSTRAP COMPLETE. Verify: venv/bin/python -m src.main --healthcheck --mode paper"
