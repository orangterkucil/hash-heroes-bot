"""Tiny Telegram notifier used by the worker (bot.py).

Avoids pulling in python-telegram-bot just to send a message. We POST to the
Bot API directly. Failures here NEVER raise into the worker — the bot keeps
running even if Telegram is unreachable.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

import config

log = logging.getLogger("notifier")

_API = "https://api.telegram.org"


def _send_raw(token: str, chat_id: int, text: str, parse_mode: Optional[str] = "HTML") -> bool:
    try:
        resp = requests.post(
            f"{_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("telegram send failed: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.warning("telegram send exception: %s", exc)
        return False


def notify(text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """Send a one-off message to the admin. No-op when not configured."""
    if not config.telegram_enabled():
        return False
    return _send_raw(
        config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_ADMIN_ID, text, parse_mode=parse_mode
    )


def notify_safe(text: str) -> None:
    """Same as notify() but really, truly never raises."""
    try:
        notify(text)
    except Exception:  # noqa: BLE001
        log.exception("notifier failed silently")
