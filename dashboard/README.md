# Status dashboard — the ONE dashboard, view-only

**URL: http://127.0.0.1:8090** — always on (launchd `com.pcnslt.dashboard`), auto-refreshes
every 15s. It serves the single dashboard module `src/dashboard_html.py` (the same page the
EOD artifact uses), rendered live from files only: `trades.db`, `logs/netliq.jsonl`,
`logs/risk_state.json`, the daily log, and `logs/quotes/`. **No broker connection, no
controls** — nothing on this page can alter the bot (owner order, 2026-07-20; sealed by
`tests/test_dashboard_live.py::test_serve_is_view_only`). Bot control is terminal-only:
`python -m src.main --flatten --mode paper`, config, or unloading the launchd agents.

## Page contents (top to bottom)
- **Architecture — live module map**: CSS-only box-flow (phone-readable) of Polygon / IB
  Gateway → live loop + quote logger → risk state + trades.db/ledger → this dashboard →
  gates; each node colored by its live health signal.
- **Readiness**: heartbeat, quote logger, morning test gate, **sessions-missed counter**
  (with latest reason), **2FA status** ("last OK <date> · next required SUNDAY EVENING"),
  **G2-FWD progress** (sessions/60 · structure trades/200 · basis fills/40 · VRP snap days),
  **G2-FWD earliest-verdict date** (computed; assumptions shown), next milestones.
- Account tiles, NetLiq curve, SPY session tape, daily history, trade log, activity —
  the original `dashboard_html.py` content, unchanged.

## History (why there is exactly one dashboard)
- 07-09: localhost livedash deleted on owner order ("one dashboard").
- 07-20 AM: war room built on owner order, then made view-only on owner order.
- 07-20 PM: owner ordered the war room replaced by THIS — `dashboard_html.py` served live.
  `dashboard/warroom.py`, the Tailscale setup (`setup_tunnel.sh`, `REMOTE.md`), and the
  `com.pcnslt.warroom` agent were deleted in the same commit. Data freshness choice (owner):
  live log-tail intraday, EOD fallback before the session starts. Live-broker NetLiq tiles
  require a Gateway connection and are intentionally NOT part of the always-on server; the
  EOD regeneration (runner, `--live`) still stamps broker-truth numbers each close.

## Install / operate

    cp deploy/com.pcnslt.dashboard.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.pcnslt.dashboard.plist    # start (auto on boot)
    launchctl unload ~/Library/LaunchAgents/com.pcnslt.dashboard.plist  # stop

## Sunday 2FA ritual (the #1 cause of missed sessions)
3 of 5 sessions the week of 07-13 were lost solely because the Gateway sat unauthenticated.
**Every Sunday evening (or Monday before 09:25): open IB Gateway, log in to paper, approve
the phone push.** The runner waits and trades the moment auth appears (retries to 15:30).
A launchd agent (`com.pcnslt.2fa-reminder`) pops a local dialog every **Sunday 18:30** as a
reminder (auto-dismisses after 10 min). IBC is NOT installed — it cannot bypass 2FA anyway.
