# War-room dashboard

**Launch (one command, on the trading Mac):**

    bash ~/trading/odte-spy-bot/dashboard/run_warroom.sh

then open **http://127.0.0.1:8090**. Auto-refreshes every 15s.

**Always-on (recommended)** — install it as a launchd service so it stays up 24/7 (and reachable
from your phone; see below): `bash ~/trading/odte-spy-bot/dashboard/setup_tunnel.sh`. Once
installed, the launchd agent `com.pcnslt.warroom` owns `:8090`; the 09:25 runner no longer
starts its own copy.

**Remote access from your phone** — private, free, no password, nothing on GitHub: see
[REMOTE.md](REMOTE.md) (Tailscale). Not GitHub Pages — a dashboard with live trade controls
must never sit behind a public URL.

## Panels
- **Account** — NetLiq / day P&L / total P&L vs the $1M deposit; LIVE from IB Gateway when up,
  otherwise last EOD ledger (source is labeled).
- **Positions & today's trades** — open broker positions (broker truth), today's book rows.
- **Risk** — kill-switch state, daily-loss-halt usage (% of −$2,000), trades left, consecutive
  losses, VRP telemetry (ATM IV − 20d RV, best-effort from the existing Polygon plan).
- **System health** — Gateway, bot heartbeat (daily-log freshness), quote logger, morning test
  gate (PASS/FAIL @ commit), G2-FWD gate progress (sessions/trades/basis fills), VRP snap days.
- **Architecture — live module map** — CSS-only box-flow of the whole system, each node colored
  by its live health signal. Phone-readable.
- **Recent activity** — last 20 substantive bot actions with timestamps.
- **Next milestones** — dated line: XSP rehearsal, G1.5 first run, G2-FWD eligibility.

**VIEW-ONLY (owner order, 2026-07-20):** this dashboard cannot alter the bot. No kill switch,
no flatten button, no POST endpoints (`tests/test_warroom.py::test_view_only_no_post_no_controls`
fails the build if any return). Bot control is terminal-only:
`python -m src.main --flatten --mode paper`, config, or stopping the launchd agents.

## Sunday 2FA ritual (the #1 cause of missed sessions)
The bot missed 3 of 5 sessions the week of 07-13 because the IB Gateway sat unauthenticated.
IBKR requires a weekly re-login with mobile 2FA; the Gateway restarts Sunday evenings.
**Every Sunday evening (or Monday before 09:25):** open IB Gateway on this Mac, log in to the
paper account, approve the 2FA push on the phone. The runner retries every 2 min until 15:30
and trades the moment the Gateway authenticates — no restart needed. (IBC auto-restart is NOT
installed; it cannot bypass 2FA anyway — the phone tap is unavoidable.)

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
