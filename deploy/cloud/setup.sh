#!/bin/bash
# One-shot VM bootstrap (Ubuntu 24.04). Run: sudo bash deploy/cloud/setup.sh
# Idempotent-ish. Installs deps, swap, venv, IB Gateway + IBC, systemd units (timer
# left DISABLED — enable at cutover, see deploy/cloud/README.md).
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/odte-spy-bot}"
RUN_USER="${RUN_USER:-ubuntu}"
IBC_VER="${IBC_VER:-3.20.0}"           # check https://github.com/IbcAlpha/IBC/releases
HC_URL="${HC_URL:-}"                    # optional healthchecks.io ping URL

echo "== apt deps =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git unzip xvfb x11vnc openjdk-17-jre \
  libgtk-3-0t64 libasound2t64 libnss3 curl

echo "== timezone (session logic assumes ET) =="
timedatectl set-timezone America/New_York

echo "== 2 GiB swap (Gateway on small VMs) =="
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== python venv =="
sudo -u "$RUN_USER" bash -c "cd '$REPO_DIR' && python3 -m venv venv && \
  ./venv/bin/pip install -q --upgrade pip && \
  ./venv/bin/pip install -q -r requirements.txt pytest ib_insync"

echo "== IB Gateway (stable, linux x64) =="
GW_DIR="/home/$RUN_USER/ibgateway"
if [ ! -d "$GW_DIR" ]; then
  sudo -u "$RUN_USER" bash -c "
    cd /tmp &&
    curl -sO https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh &&
    sh ibgateway-stable-standalone-linux-x64.sh -q -dir '$GW_DIR'"
fi

echo "== IBC =="
IBC_DIR="/opt/ibc"
if [ ! -d "$IBC_DIR" ]; then
  mkdir -p "$IBC_DIR" && cd /tmp
  curl -sL "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VER}/IBCLinux-${IBC_VER}.zip" -o ibc.zip
  unzip -q ibc.zip -d "$IBC_DIR" && chmod +x "$IBC_DIR"/*.sh "$IBC_DIR"/scripts/*.sh
fi
sudo -u "$RUN_USER" mkdir -p "/home/$RUN_USER/ibc"
if [ ! -f "/home/$RUN_USER/ibc/config.ini" ]; then
  sudo -u "$RUN_USER" cp "$REPO_DIR/deploy/cloud/ibc-config.ini.template" \
    "/home/$RUN_USER/ibc/config.ini"
  echo ">>> EDIT /home/$RUN_USER/ibc/config.ini (IbLoginId etc.) before starting."
fi

echo "== systemd units =="
for u in ibgateway.service x11vnc.service odte-session.service odte-session.timer \
         ibgateway-restart.timer ibgateway-restart.service; do
  sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__USER__|$RUN_USER|g" -e "s|__HC_URL__|$HC_URL|g" \
      "$REPO_DIR/deploy/cloud/systemd/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable ibgateway.service ibgateway-restart.timer
# NOTE: odte-session.timer intentionally NOT enabled here — enable at cutover:
#   sudo systemctl enable --now odte-session.timer

echo "== done =="
echo "Next: scp your .env into $REPO_DIR (chmod 600), edit ~/ibc/config.ini,"
echo "then: sudo systemctl start ibgateway x11vnc  and complete the VNC login."
