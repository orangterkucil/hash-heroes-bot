#!/usr/bin/env bash
# One-shot VPS installer for the Hash-Heroes bot pair.
# Tested on Ubuntu 22.04 / 24.04.
#
# Usage (as root or with sudo):
#   sudo SRC_DIR=/tmp/hashheroes-bot-src bash deploy/vps-setup.sh
#
# Installs TWO systemd services:
#   - hashheroes-bot     (background worker that opens/stakes/claims)
#   - hashheroes-tgbot   (interactive Telegram bot for /status, /cards, etc.)
#
# After install:
#   1. edit /opt/hashheroes-bot/accounts.json
#   2. edit /opt/hashheroes-bot/.env (TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID)
#   3. sudo systemctl restart hashheroes-bot hashheroes-tgbot

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/hashheroes-bot}"
SERVICE_USER="${SERVICE_USER:-hashheroes}"
SRC_DIR="${SRC_DIR:-$(pwd)}"

if [[ $EUID -ne 0 ]]; then
  echo "[!] run as root or with sudo." >&2
  exit 1
fi

echo "[1/7] installing system packages…"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ca-certificates tzdata

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "[2/7] creating system user '${SERVICE_USER}'…"
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
else
  echo "[2/7] user '${SERVICE_USER}' already exists."
fi

echo "[3/7] copying source to ${INSTALL_DIR}…"
mkdir -p "${INSTALL_DIR}"
cp -f "${SRC_DIR}"/api.py "${SRC_DIR}"/bot.py "${SRC_DIR}"/tgbot.py \
      "${SRC_DIR}"/config.py "${SRC_DIR}"/notifier.py \
      "${SRC_DIR}"/requirements.txt "${INSTALL_DIR}/"
# only seed accounts.json + .env on a fresh install; never overwrite existing.
if [[ ! -f "${INSTALL_DIR}/accounts.json" ]]; then
  if [[ -f "${SRC_DIR}/accounts.json" ]]; then
    cp -f "${SRC_DIR}/accounts.json" "${INSTALL_DIR}/accounts.json"
  elif [[ -f "${SRC_DIR}/accounts.example.json" ]]; then
    cp -f "${SRC_DIR}/accounts.example.json" "${INSTALL_DIR}/accounts.json"
  fi
fi
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  if [[ -f "${SRC_DIR}/.env" ]]; then
    cp -f "${SRC_DIR}/.env" "${INSTALL_DIR}/.env"
  elif [[ -f "${SRC_DIR}/.env.example" ]]; then
    cp -f "${SRC_DIR}/.env.example" "${INSTALL_DIR}/.env"
  fi
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 600 "${INSTALL_DIR}/accounts.json" "${INSTALL_DIR}/.env" 2>/dev/null || true

echo "[4/7] creating virtualenv & installing python deps…"
sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

patch_unit() {
  local file="$1"
  sed -i "s#^User=.*#User=${SERVICE_USER}#" "$file"
  sed -i "s#^Group=.*#Group=${SERVICE_USER}#" "$file"
  sed -i "s#^WorkingDirectory=.*#WorkingDirectory=${INSTALL_DIR}#" "$file"
  sed -i "s#^EnvironmentFile=.*#EnvironmentFile=-${INSTALL_DIR}/.env#" "$file"
}

echo "[5/7] installing systemd units…"
install -m 644 "${SRC_DIR}/deploy/hashheroes-bot.service" \
  "/etc/systemd/system/hashheroes-bot.service"
install -m 644 "${SRC_DIR}/deploy/hashheroes-tgbot.service" \
  "/etc/systemd/system/hashheroes-tgbot.service"
patch_unit "/etc/systemd/system/hashheroes-bot.service"
patch_unit "/etc/systemd/system/hashheroes-tgbot.service"
sed -i "s#^ExecStart=.*#ExecStart=${INSTALL_DIR}/.venv/bin/python -u ${INSTALL_DIR}/bot.py --loop --sleep 30#" \
  "/etc/systemd/system/hashheroes-bot.service"
sed -i "s#^ExecStart=.*#ExecStart=${INSTALL_DIR}/.venv/bin/python -u ${INSTALL_DIR}/tgbot.py#" \
  "/etc/systemd/system/hashheroes-tgbot.service"

systemctl daemon-reload
systemctl enable --now hashheroes-bot.service

# Only enable tgbot if .env contains a token. Otherwise leave it stopped to
# avoid the boot-loop "no token" SystemExit.
if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "${INSTALL_DIR}/.env" 2>/dev/null; then
  systemctl enable --now hashheroes-tgbot.service
  echo "[6/7] tgbot enabled."
else
  systemctl enable hashheroes-tgbot.service
  echo "[6/7] tgbot enabled but NOT started (TELEGRAM_BOT_TOKEN missing in .env)."
fi

echo "[7/7] done."
echo
systemctl --no-pager --full status hashheroes-bot.service | head -n 12 || true
echo
systemctl --no-pager --full status hashheroes-tgbot.service | head -n 12 || true
echo
echo "edit accounts:    sudo -u ${SERVICE_USER} nano ${INSTALL_DIR}/accounts.json"
echo "edit .env:        sudo -u ${SERVICE_USER} nano ${INSTALL_DIR}/.env"
echo "follow worker:    journalctl -u hashheroes-bot -f"
echo "follow tgbot:     journalctl -u hashheroes-tgbot -f"
echo "restart all:      sudo systemctl restart hashheroes-bot hashheroes-tgbot"
