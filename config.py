"""Tiny zero-dependency config loader.

Looks for env vars first, then falls back to a `.env` file next to this module.
Returns sensible defaults so the bot works even with no config (notifications
just become no-ops).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_ENV_FILE = ROOT / ".env"


def _load_env_file() -> None:
    if not _ENV_FILE.exists():
        return
    # `utf-8-sig` strips any BOM at the start of the file (Windows-edited
    # .env files often carry one and break the first key).
    for raw in _ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        # Don't override values already set in the real env (systemd / shell).
        os.environ.setdefault(key, value)


_load_env_file()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Telegram notifier / interactive bot
TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID: int = int(_get("TELEGRAM_ADMIN_ID", "0") or 0)

# Heartbeat ping every N hours (0 to disable). Lets you confirm the bot is
# actually alive without spamming you.
HEARTBEAT_HOURS: int = int(_get("HEARTBEAT_HOURS", "12") or 12)

# Send a tg notification on every successful claim (otherwise only the
# end-of-cycle digest is sent).
NOTIFY_PER_CLAIM: bool = _get("NOTIFY_PER_CLAIM", "false").lower() in (
    "1", "true", "yes", "on"
)


def telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN) and TELEGRAM_ADMIN_ID > 0
