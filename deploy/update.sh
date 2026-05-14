#!/usr/bin/env bash
# Drop-in updater. Run from the repo dir AFTER pulling new source.
# Replaces api.py / bot.py / requirements.txt in /opt/hashheroes-bot, refreshes
# the venv and restarts the service. Leaves accounts.json + totals.json alone.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/hashheroes-bot}"
SERVICE_USER="${SERVICE_USER:-hashheroes}"
SERVICE_NAME="hashheroes-bot"
SRC_DIR="${SRC_DIR:-$(pwd)}"

if [[ $EUID -ne 0 ]]; then
  echo "[!] run with sudo." >&2; exit 1
fi

echo "[1/3] copying updated sources from ${SRC_DIR} → ${INSTALL_DIR}…"
for f in api.py bot.py tgbot.py config.py notifier.py requirements.txt; do
  if [[ -f "${SRC_DIR}/${f}" ]]; then
    cp -f "${SRC_DIR}/${f}" "${INSTALL_DIR}/${f}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/${f}"
  fi
done

echo "[2/3] refreshing python deps…"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

echo "[3/3] restarting services…"
systemctl restart hashheroes-bot.service
if systemctl is-enabled --quiet hashheroes-tgbot.service 2>/dev/null; then
  systemctl restart hashheroes-tgbot.service || true
fi
systemctl --no-pager --full status hashheroes-bot.service | head -n 12
echo
systemctl --no-pager --full status hashheroes-tgbot.service | head -n 12 || true
