# War-room dashboard

**Launch (one command, on the trading Mac):**

    bash ~/trading/odte-spy-bot/dashboard/run_warroom.sh

then open **http://127.0.0.1:8090**. It also starts automatically with every trading session
(the 09:25 runner) and stays up after the close. Auto-refreshes every 15s.

## Panels
- **Account** — NetLiq / day P&L / total P&L vs the $1M deposit; LIVE from IB Gateway when up,
  otherwise last EOD ledger (source is labeled).
- **Positions & today's trades** — open broker positions (broker truth), today's book rows.
- **Risk** — kill-switch state, daily-loss-halt usage (% of −$2,000), trades left, consecutive
  losses, VRP telemetry (ATM IV − 20d RV, best-effort from the existing Polygon plan).
- **System health** — Gateway, bot heartbeat (daily-log freshness), quote logger, morning test
  gate (PASS/FAIL @ commit).
- **Recent activity** — last 20 substantive bot actions with timestamps.
- **Controls (paper only)** — KILL SWITCH writes `logs/entries_disabled.flag`, which the bot's
  entry gate checks every poll (exits/defense/flatten are never disabled); FORCE FLATTEN runs
  the audited `--flatten` CLI (confirms flat via `ib.positions()`, distinct client id).

## Why this is NOT hosted on GitHub Codespaces
The advisor's spec asked for Codespaces. It cannot work, by design of this system:
1. **The data isn't in the repo.** `trades.db`, `logs/` (ledger, risk state, quote archives)
   are deliberately gitignored — financial data never enters git. A Codespace clone sees none
   of it.
2. **The IB Gateway is 127.0.0.1:4002 on this Mac.** A Codespace cannot reach it, so no live
   account, no positions, no controls.
3. **Free Codespaces is not always-on hosting** (~120 core-hours/month; sleeps when idle).

The remote read-only view remains the claude.ai artifact snapshot; this war room is the live
cockpit and runs where the data lives. (If remote live access is ever wanted, a Tailscale
tunnel to this Mac is the sane path — not committing financial state to git.)
