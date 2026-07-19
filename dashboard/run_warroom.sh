#!/bin/bash
# One-click war room. Run from anywhere:  bash ~/trading/odte-spy-bot/dashboard/run_warroom.sh
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pkill -f "dashboard/warroom.py" 2>/dev/null
echo "Starting war room -> http://127.0.0.1:8090"
exec "$REPO/venv/bin/python" "$REPO/dashboard/warroom.py"
