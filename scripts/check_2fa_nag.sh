#!/bin/bash
# Conditional 2FA nag (weekday mornings): silent when the Gateway is authenticated; a
# blocking dialog when it is not. Companion to the unconditional Sunday-evening reminder —
# added 2026-07-27 after the Sunday dialog auto-dismissed unseen and the week's first
# session (+ rehearsal day 1) was lost to an unauthenticated Gateway. Read-only check.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if nc -z 127.0.0.1 4002 2>/dev/null && \
   "$REPO/venv/bin/python" -m src.main --healthcheck --mode paper >/dev/null 2>&1; then
  exit 0   # authenticated; stay silent
fi
exec /usr/bin/osascript -e 'display dialog "IB Gateway is NOT logged in — today will not trade until you do.\n\n1. Open IB Gateway\n2. Log in to the paper account\n3. Approve the phone push\n\nThe bot starts trading the moment you finish (retries until 15:30)." with title "ODTE-SPY-BOT · GATEWAY NOT AUTHENTICATED" buttons {"Done"} default button 1 with icon stop giving up after 1800'
