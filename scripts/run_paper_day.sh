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

# Make EVERY git op fail-fast, never hang. Incident 2026-07-07: the EOD `git push` wedged on
# the network/credentials, the runner never exited, and a hung runner would block the next
# day's launchd start. Never prompt (fail if creds missing); abort a stalled transfer.
export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=20

# Weekends: nothing to do (launchd calendar already skips them; belt-and-braces).
dow=$(date +%u)
if [ "$dow" -ge 6 ]; then
  echo "Weekend; exiting."
  exit 0
fi

# Sync to origin/main exactly (self-healing mirror). NOT `pull --ff-only`: the EOD dashboard
# commit below can leave a local commit that diverges from origin after the nightly retrain,
# and a plain ff-only pull then wedges — the bot silently stops updating. This host is a pure
# deploy mirror (trades.db + logs/ are gitignored and untouched by reset), so mirroring
# origin/main is safe. The pytest gate below still reverts to PRE_PULL if the synced code fails.
PRE_PULL=$(git rev-parse HEAD)
# Hard-cap the fetch: the osxkeychain credential helper can BLOCK in launchd's no-GUI context
# (it hung the EOD push on 2026-07-07). A stuck fetch here would freeze startup and block the
# whole day. Kill the git tree after 25s and fall back to the last-deployed local code.
git fetch --quiet origin main & FPID=$!
( sleep 25; kill -TERM "$FPID" 2>/dev/null
  pkill -9 -f "git-credential-osxkeychain" 2>/dev/null
  pkill -9 -f "git-remote-https.*odte-spy-bot" 2>/dev/null ) & KPID=$!
if wait "$FPID" 2>/dev/null; then
  kill "$KPID" 2>/dev/null
  git reset --hard origin/main || echo "WARN: reset failed; running local version."
else
  kill "$KPID" 2>/dev/null
  echo "WARN: git fetch timed out/failed — running the last-deployed local code."
fi

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

# Wait until IB Gateway is up AND authenticated, retrying with backoff. Do NOT exit-and-let-
# launchd-hot-restart on failure — that thrashed 100+ relaunches on 2FA-login mornings. This
# keeps ONE process alive, retrying every 2 min, and gives up cleanly (exit 0, no relaunch)
# once past the no-new-trades cutoff so nothing is left spinning after the window closes.
NO_NEW="1530"   # HHMM; matches config session.no_new_trades_after
echo "$(date +%H:%M) Waiting for an authenticated IB Gateway (retry every 2 min until $NO_NEW ET)..."
until nc -z 127.0.0.1 4002 2>/dev/null && \
      caffeinate -i "$REPO/venv/bin/python" -m src.main --healthcheck --mode paper; do
  if [ "$(date +%H%M)" -ge "$NO_NEW" ]; then
    echo "$(date +%H:%M) Past $NO_NEW ET with no authenticated Gateway (weekly 2FA not done?) — nothing to trade today. Clean exit."
    exit 0
  fi
  echo "$(date +%H:%M) Gateway down or logged out; retrying in 120s (log into IB Gateway to start immediately)."
  caffeinate -i sleep 120
done
echo "$(date +%H:%M) Gateway authenticated — starting the session."

# Live local dashboard: a SEPARATE read-only process (own IBKR client id) that auto-refreshes
# so you can watch the session live at http://127.0.0.1:8080. Cannot affect trading; killed
# at session end. Failure to start never affects the session.
"$REPO/venv/bin/python" -m src.livedash --port 8080 >>"$LOG" 2>&1 &
LIVEDASH_PID=$!
echo "$(date +%H:%M) Live dashboard at http://127.0.0.1:8080 (pid $LIVEDASH_PID)"

# caffeinate -i: keep the Mac from idle-sleeping while the session runs.
caffeinate -i "$REPO/venv/bin/python" -m src.main --mode paper --daily
rc=$?
kill "$LIVEDASH_PID" 2>/dev/null || true

# End-of-day evidence summary: every session closes with the TradeLog report.
echo "=== $(date) TradeLog report ==="
"$REPO/venv/bin/python" -m src.utils.trade_log --db "$REPO/trades.db" || true

# Plain-English bottom line for the human: is it working, what to do.
echo "=== $(date) OPERATOR BRIEFING ==="
"$REPO/venv/bin/python" -m src.briefing --db "$REPO/trades.db" || true

# H10 shadow cost-meta-labeler: retrain locally from trades.db (no-ops until >=100 fills;
# trains here, NOT in the cloud retrain, because trades.db lives only on this host).
echo "=== $(date) cost-meta-labeler retrain ==="
"$REPO/venv/bin/python" -m src.signals.cost_meta_labeler --train --db "$REPO/trades.db" || true

# Early-warning: strategy death-spiral monitor (exits non-zero on KILL-WATCH/RETIRE; logged).
echo "=== $(date) death-spiral monitor ==="
"$REPO/venv/bin/python" -m src.monitor --db "$REPO/trades.db" || true

# Dashboard: regenerate LOCALLY from trades.db. Deliberately NO git commit/push: this host is
# a pull-only deploy mirror. Pushing from here caused (a) local commits that diverged from
# origin and wedged the morning pull, and (b) an EOD hang on the osxkeychain credential helper
# in launchd's no-GUI context (2026-07-07). The operator's live view is the hosted artifact +
# the local live dashboard; the GitHub copy isn't needed and isn't worth the hang risk.
echo "=== $(date) dashboard (local regen; no push) ==="
# Save today's SPY intraday from IBKR (Gateway is still up post-session) so the dashboard
# can plot the session tape with the day's events. No-ops if Gateway is already down.
"$REPO/venv/bin/python" -m src.session_chart --pull-spy || true
"$REPO/venv/bin/python" -m src.dashboard_html --db "$REPO/trades.db" \
  --out "$REPO/docs/dashboard/status.html" || true
"$REPO/venv/bin/python" -m src.dashboard --db "$REPO/trades.db" --out "$REPO/docs/dashboard" || true

exit $rc
