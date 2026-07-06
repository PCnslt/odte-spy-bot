#!/bin/zsh
# Daily paper-trading session runner. Launched by launchd at 09:25 ET on weekdays
# (see deploy/com.pcnslt.odte-spy-bot.plist). Exits after the close via --daily.
set -u

# Repo root derived from this script's location — the runtime deployment MUST live outside
# iCloud Drive (launchd cannot read Mobile Documents; discovered the hard way, self-audit R6).
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

mkdir -p logs
LOG="logs/daily_$(date +%Y%m%d).log"
exec >> "$LOG" 2>&1

echo "=== $(date) run_paper_day starting ==="

# Weekends: nothing to do (launchd calendar already skips them; belt-and-braces).
dow=$(date +%u)
if [ "$dow" -ge 6 ]; then
  echo "Weekend; exiting."
  exit 0
fi

# Pull the latest code + nightly-retrained model (fast-forward only; never break local state).
PRE_PULL=$(git rev-parse HEAD)
git pull --ff-only origin main || echo "WARN: git pull failed; running with local version."

# Audit M3: never trade freshly pulled code that fails its own tests — revert and run the
# last-known-good commit instead (fail closed, but still trade the proven version).
if [ "$(git rev-parse HEAD)" != "$PRE_PULL" ]; then
  if ! "$REPO/venv/bin/python" -m pytest -q >/dev/null 2>&1; then
    echo "ERROR: tests FAILED on pulled code — reverting to pre-pull commit $PRE_PULL."
    git reset --hard "$PRE_PULL"
  else
    echo "Pulled $(git rev-parse --short HEAD); tests pass."
  fi
fi

# Sanity 1: is IB Gateway's paper API port up at all?
if ! nc -z 127.0.0.1 4002 2>/dev/null; then
  echo "ERROR: IB Gateway paper API (4002) not reachable. Log into IB Gateway (Paper) and retry."
  exit 1
fi

# Sanity 2: is the session AUTHENTICATED? (Port can listen while logged out after 2FA expiry.)
if ! "$REPO/venv/bin/python" -m src.main --healthcheck --mode paper; then
  echo "ERROR: Gateway reachable but NOT authenticated (weekly 2FA re-login needed?). Aborting."
  exit 1
fi

# caffeinate -i: keep the Mac from idle-sleeping while the session runs.
exec caffeinate -i "$REPO/venv/bin/python" -m src.main --mode paper --daily
