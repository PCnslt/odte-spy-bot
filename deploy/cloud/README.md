# Cloud deployment — run the bot with ZERO Mac dependency

Target: one small always-on Linux VM runs IB Gateway (headless) + the session timer.
GitHub stays the source of truth (code, models, dashboard). The Mac becomes dev-only.

## What replaces what

| Mac today | VM |
|---|---|
| IB Gateway GUI app | IB Gateway headless under **IBC** + Xvfb (`ibgateway.service`) |
| launchd 09:25 + pmset wake | `odte-session.timer` (systemd, VM never sleeps) |
| Manual weekly 2FA on screen | x11vnc over an SSH tunnel — approve 2FA from any browser/phone |
| Local logs/dashboard | Dashboard committed to GitHub at session close (already wired) |
| `.env` in the repo clone | Same, `chmod 600`, on the VM only |

## Provisioning options (pick one)

| Option | Cost | Notes |
|---|---|---|
| AWS EC2 t3.micro, us-east-1 | free-tier eligible yr 1, then ~$8/mo | 1 GiB RAM is tight for Gateway — add 2 GiB swap (setup.sh does) |
| AWS Lightsail 2 GB | $12/mo (1 GB $5 — tight) | simplest AWS billing |
| Hetzner/OVH 2 GB VPS | ~$4–6/mo | cheapest solid option, not AWS |

Ubuntu 24.04 LTS assumed. Region: us-east-1/us-east coast (market + IBKR proximity).

## Runbook

1. **Provision** the VM, point DNS/SSH at it. `ssh ubuntu@VM`.
2. **Bootstrap:** copy this repo's deploy key or use HTTPS+token, then:
   ```bash
   git clone https://github.com/PCnslt/odte-spy-bot.git ~/odte-spy-bot
   cd ~/odte-spy-bot && sudo bash deploy/cloud/setup.sh
   ```
   setup.sh: apt deps, 2 GiB swap, venv + requirements, IB Gateway (stable, linux x64)
   + IBC download, systemd units installed (timer DISABLED until cutover).
3. **Secrets:** `scp .env ubuntu@VM:~/odte-spy-bot/.env && ssh VM 'chmod 600 ~/odte-spy-bot/.env'`
   Also create `~/ibc/config.ini` from `deploy/cloud/ibc-config.ini.template`
   (fill IbLoginId; leave password blank — type it once via VNC; IBC keeps the session).
4. **First Gateway login (the one manual step, repeats ~weekly for 2FA):**
   ```bash
   ssh -L 5900:localhost:5900 ubuntu@VM   # tunnel
   sudo systemctl start ibgateway x11vnc  # on the VM
   # connect a VNC viewer to localhost:5900, log in (Paper), approve 2FA
   ```
5. **SHADOW WEEK (no orders):** verify daily from the VM:
   ```bash
   cd ~/odte-spy-bot && ./venv/bin/python -m src.main --healthcheck --mode paper
   ./venv/bin/python -m src.main --selftest --mode paper
   ```
   The Mac keeps trading all week. Do NOT enable the VM timer yet — two traders on one
   paper account would double-manage positions.
6. **CUTOVER (one evening):**
   - Mac: `launchctl bootout gui/$UID/com.pcnslt.odte-spy-bot`
   - Copy state once: `scp ~/trading/odte-spy-bot/trades.db ubuntu@VM:~/odte-spy-bot/`
     (memory.db optional; models come from git)
   - VM: `sudo systemctl enable --now odte-session.timer`
   - Next morning: watch `journalctl -u odte-session -f` and the GitHub dashboard commit.
7. **ROLLBACK** (any week-one misbehavior): disable the VM timer, re-bootstrap launchd on
   the Mac (`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.pcnslt.odte-spy-bot.plist`),
   scp trades.db back. Total rollback time ≈ 5 minutes.

## Failure→detection map

| Failure | Caught by |
|---|---|
| Gateway session dead / 2FA expired | `--healthcheck` aborts the session loudly (log + nonzero exit); journal shows it |
| VM rebooted overnight | systemd `Persistent=true` runs the missed timer at boot; ibgateway.service auto-starts |
| Bad code pushed | pytest gate in run_paper_day.sh reverts to pre-pull commit |
| Bot crash mid-session | `Restart=on-failure` + startup `flatten_orphans()` |
| Silent no-run morning | Dashboard commit ABSENT on GitHub (check the repo) + optional healthchecks.io ping (see setup.sh HC_URL) |
| Gateway memory creep (known headless issue) | nightly `ibgateway` restart timer (mirrors the Mac's 23:45 restart) |

## Known sharp edges (read before trusting)

- IB Gateway pins poorly: keep the installed version until IBC confirms compatibility;
  do not auto-update Gateway.
- IBKR allows ONE session per username: the VM Gateway and any Mac Gateway/TWS login will
  kick each other. After cutover, don't leave the Mac Gateway running.
- Paper accounts + IBC: use the Paper toggle in config (`TradingMode=paper`).
- The dashboard push needs a GitHub token with repo write on the VM (HTTPS remote with a
  fine-grained PAT, or a deploy key with write). Keychain doesn't exist on Linux.
