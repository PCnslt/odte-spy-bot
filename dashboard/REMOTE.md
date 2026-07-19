# Remote war room (phone / anywhere) over Tailscale

See the live war room from your phone or any device — **privately**, with **no password to
leak, no secrets in the repo, and nothing hosted on the public internet.** This replaces the
advisor's "GitHub Pages + login form + public tunnel + basic-auth" design with something
simpler and materially safer for a dashboard that can place trades.

## Why Tailscale instead of GitHub Pages + a login form

The requirement was: *reach the same live metrics from my phone, free, no secrets in the repo.*

| | GitHub Pages + public tunnel + basic-auth | **Tailscale (this)** |
|---|---|---|
| Who can reach it | anyone on the internet who has the URL, gated only by a password | **only devices you enrolled** in your own tailnet |
| Auth | a password you must store, rotate, and hope isn't guessed/leaked | your device's WireGuard key — **no password exists to leak** |
| Trade controls exposed to the public internet | yes (behind one password) | **never** — the transport itself is private |
| Secrets in the repo | risk of leaking the tunnel URL / auth into the static site | **none** — the repo has no URL, no key, no account data |
| Moving parts | static site + CORS + tunnel + basic-auth server | one command; serves the war room you already run |
| Cost | free | free, no credit card |

The tailnet **is** the login. A login form on a static page can't protect a backend anyway —
only the network boundary can, and Tailscale is a far stronger boundary than a shared password.
(Historical note: the tailnet is also why dashboard controls would have been tolerable here —
but as of 2026-07-20 the owner ordered the dashboard **view-only**, so no controls exist at all.)

## One-time setup

**On the trading Mac:**

    bash ~/trading/odte-spy-bot/dashboard/setup_tunnel.sh

That installs Tailscale, logs the Mac into your tailnet (a browser opens — sign in free, no
card), installs the always-on war-room service, and runs `tailscale serve`. It prints your
private URL, e.g. `https://<mac-name>.<your-tailnet>.ts.net`.

**On your phone:**
1. Install **Tailscale** from the App Store / Play Store.
2. Sign into the **same** account.
3. Open the `https://<mac-name>.<your-tailnet>.ts.net` URL. Done — the same view-only page,
   auto-refreshing every 15s.

## What you get remotely

The exact page from `http://127.0.0.1:8090` — Account, Positions, Risk, System health,
Architecture map, Activity, Next milestones. **View-only** (owner order, 2026-07-20): the
dashboard cannot alter the bot from any device; control is terminal-only on the Mac.

## Security posture

- **serve, never funnel.** `tailscale serve` = reachable only by devices on your tailnet.
  `tailscale funnel` = public internet — **we never use it**, and `tests/test_remote_access.py`
  fails the build if the setup script ever tries to. The war-room process stays bound to
  `127.0.0.1`, so **no inbound port is opened on the Mac** and it's invisible to your LAN and
  the internet; Tailscale reaches it locally.
- **No secret in git.** No URL, no key, no password, no account data is committed. Runtime
  financial state (`trades.db`, `logs/`) stays gitignored, as always.
- **Revoke access** to a lost/old phone: Tailscale admin console → **Machines** → remove the
  device (or log it out). There's no shared password to rotate — access is per-device.
- **Controls are live.** Anyone holding an unlocked, still-enrolled device can flatten the
  paper account. Keep your devices locked; remove ones you no longer use. (Paper only — no real
  money can move.)

## Start / stop / status

    launchctl load   ~/Library/LaunchAgents/com.pcnslt.warroom.plist   # start (auto on reboot)
    launchctl unload ~/Library/LaunchAgents/com.pcnslt.warroom.plist   # stop the war room
    tailscale serve status                                             # show the URL + config
    tailscale serve --bg 8090 off                                      # stop remote exposure
                                                                       # (local :8090 still runs)

## Survives reboots

- War room: launchd `RunAtLoad` + `KeepAlive` restart it on boot and on crash.
- Tailscale: the app relaunches at login and `serve` config persists — the URL is stable.
- Gateway restarts don't affect the tunnel; the dashboard just shows Gateway as down until
  it's back.

## Notes

- The local war room at `http://127.0.0.1:8090` is unchanged; remote access is purely additive.
- The runner (`scripts/run_paper_day.sh`) **no longer starts its own war room** — the always-on
  launchd agent owns `:8090` now, so the two can't collide on the port.
