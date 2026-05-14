"""Interactive Telegram bot for the Hash-Heroes worker.

Runs as a separate process (`hashheroes-tgbot.service` on the VPS) and lets
the admin poke at every account on demand. Heavy work (API calls) happens in
a thread so we don't block the asyncio event loop.

Commands (admin-only):
  /start            — sanity ping
  /help             — list commands
  /status           — one-line summary per account
  /accounts         — names + ids + which are disabled
  /balance          — $HASH balances
  /cards [name]     — show all cards for one (or every) account
  /mining [name]    — mining rate / pending / cooldown
  /claim [name]     — force claim now (skips cooldown only when ready)
  /run              — force a full cycle (open + stake + claim) on all accounts
  /totals           — bot lifetime totals
  /tasks [name]     — list task progress
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
    MessageHandler,
)

import config
from api import HashHeroesAPI, HashHeroesError

ROOT = Path(__file__).parent
ACCOUNTS_FILE = ROOT / "accounts.json"
TOTALS_FILE = ROOT / "totals.json"

CLAIM_COOLDOWN = 86_400  # 24h, kept in sync with bot.py

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tgbot")


# ---------------------------------------------------------------- helpers
def get_base_name(name: str) -> str:
    """myaccount_3 → myaccount  |  myaccount_a611 → myaccount"""
    name = re.sub(r'_a\d+$', '', name)   # imported wallets: slot_a123
    return re.sub(r'_\d+$', '', name)    # created wallets: base_123


def group_accounts(accs: list[dict]) -> dict[str, list[dict]]:
    """Kelompokan semua akun berdasarkan base name prefix."""
    groups: dict[str, list[dict]] = {}
    for a in accs:
        base = get_base_name(a.get("name", ""))
        groups.setdefault(base, []).append(a)
    return groups


def _append_account(entry: dict) -> None:
    """Tambahkan entry baru ke accounts.json (thread-safe: dipanggil satu per satu)."""
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = []
    if isinstance(data, dict):
        data = [data]
    data.append(entry)
    ACCOUNTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [a for a in data if isinstance(a, dict)]


def find_account(name: str) -> Optional[dict]:
    name = name.lower().strip()
    for a in load_accounts():
        if str(a.get("name", "")).lower() == name:
            return a
    return None


def active_accounts() -> list[dict]:
    return [a for a in load_accounts() if not a.get("disabled")]


def load_totals() -> dict:
    if TOTALS_FILE.exists():
        try:
            return json.loads(TOTALS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def make_api(acct: dict) -> HashHeroesAPI:
    return HashHeroesAPI(
        user_id=acct.get("user_id") or None,
        wallet_address=(acct.get("wallet_address") or "").strip() or None,
        ref=(acct.get("ref") or "").strip() or None,
    )


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def short_addr(addr: str) -> str:
    addr = addr or ""
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def esc(s: Any) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------- per-account fetchers (sync, run in threadpool)
def _fetch_overview(acct: dict) -> dict:
    """One call: /api/me. Cheap snapshot for /status, /balance, /mining."""
    api = make_api(acct)
    try:
        payload = api.me()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    user = payload.get("user") or payload.get("data") or payload or {}
    mining = payload.get("mining") or {}
    return {
        "name": acct["name"],
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "hash_balance": float(user.get("hash_balance", 0) or 0),
        "dust_balance": int(user.get("dust_balance", 0) or 0),
        "starter_packs": int(user.get("starter_packs", 0) or 0),
        "paid_packs": int(user.get("paid_packs", 0) or 0),
        "wallet_address": user.get("wallet_address") or "",
        "mining": {
            "elapsed": int(mining.get("elapsedSeconds", 0) or 0),
            "rate_per_day": float(mining.get("miningRateHashPerDay", 0) or 0),
            "pending": float(mining.get("hashAmount", 0) or 0),
            "multiplier": float(mining.get("multiplier", 1) or 1),
        },
    }


def _fetch_cards(acct: dict) -> dict:
    api = make_api(acct)
    try:
        data = api.cards()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    return {
        "name": acct["name"],
        "ok": True,
        "cards": data.get("cards") or [],
    }


def _fetch_dashboard(acct: dict) -> dict:
    api = make_api(acct)
    try:
        me_data = api.me()
        cards_data = api.cards()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    user = me_data.get("user") or {}
    mining = me_data.get("mining") or {}
    cards = cards_data.get("cards") or []

    tier_counts: dict[str, int] = {"UR": 0, "SSR": 0, "SR": 0, "Rare": 0, "Uncommon": 0, "Common": 0}
    tier_staked: dict[str, int] = {"UR": 0, "SSR": 0, "SR": 0, "Rare": 0, "Uncommon": 0, "Common": 0}
    total_staked = 0
    for c in cards:
        rarity = c.get("rarity") or "?"
        is_staked = bool(c.get("is_staked") or c.get("isStaked") or c.get("staked"))
        if rarity in tier_counts:
            tier_counts[rarity] += 1
            if is_staked:
                tier_staked[rarity] += 1
                total_staked += 1

    return {
        "name": acct["name"],
        "ok": True,
        "hash_balance": float(user.get("hash_balance", 0) or 0),
        "dust_balance": int(user.get("dust_balance", 0) or 0),
        "hashpower": int(user.get("total_hashpower", 0) or user.get("hashpower", 0) or 0),
        "starter_packs": int(user.get("starter_packs", 0) or 0),
        "paid_packs": int(user.get("paid_packs", 0) or 0),
        "cards_total": len(cards),
        "cards_staked": total_staked,
        "cards_by_tier": tier_counts,
        "staked_by_tier": tier_staked,
        "mining": {
            "elapsed": int(mining.get("elapsedSeconds", 0) or 0),
            "pending": float(mining.get("hashAmount", 0) or 0),
            "rate_per_day": float(mining.get("miningRateHashPerDay", 0) or 0),
        },
    }


def _fetch_tasks(acct: dict) -> dict:
    api = make_api(acct)
    try:
        data = api.tasks()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    return {
        "name": acct["name"],
        "ok": True,
        "tasks": data.get("tasks") or [],
    }


def _force_claim(acct: dict) -> dict:
    api = make_api(acct)
    try:
        me = api.me()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    mining = me.get("mining") or {}
    elapsed = int(mining.get("elapsedSeconds", 0) or 0)
    if elapsed < CLAIM_COOLDOWN:
        return {
            "name": acct["name"],
            "ok": True,
            "claimed": 0.0,
            "cooldown_left": CLAIM_COOLDOWN - elapsed,
        }
    try:
        data = api.claim_mining()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    claimed = (data or {}).get("claimed") or {}
    amount = float(claimed.get("hashAmount", 0) or 0)
    return {"name": acct["name"], "ok": True, "claimed": amount}


def _force_cycle(acct: dict) -> dict:
    """Open + stake + claim a single account, mirroring bot.py.

    Lighter version with no console prints — suitable for /run from Telegram.
    """
    from bot import (
        do_open_packs,
        do_stake_all,
        do_claim,
        do_complete_tasks,
        do_claim_referral_pack,
        _user_from_me,
    )

    api = make_api(acct)
    try:
        me = api.me()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    user = _user_from_me(me)
    mining = me.get("mining") or {}
    name = acct["name"]
    summary: dict[str, Any] = {"name": name, "ok": True}
    try:
        summary["tasks"] = do_complete_tasks(api)
    except Exception as exc:  # noqa: BLE001
        summary["tasks_error"] = str(exc)
    try:
        summary["referral_packs"] = do_claim_referral_pack(api)
    except Exception as exc:  # noqa: BLE001
        summary["referral_error"] = str(exc)
    # refresh
    try:
        me = api.me()
        user = _user_from_me(me)
        mining = me.get("mining") or {}
    except HashHeroesError:
        pass
    try:
        opened, auto_staked = do_open_packs(api, user)
        summary["opened"] = opened
        summary["auto_staked"] = auto_staked
    except Exception as exc:  # noqa: BLE001
        summary["opened_error"] = str(exc)
    try:
        s, f = do_stake_all(api)
        summary["staked"] = s
        summary["stake_failed"] = f
    except Exception as exc:  # noqa: BLE001
        summary["stake_error"] = str(exc)
    try:
        summary["claimed"] = do_claim(api, mining, name)
    except Exception as exc:  # noqa: BLE001
        summary["claim_error"] = str(exc)
    return summary


def _parse_slot_wallets(slot: str) -> list[dict]:
    """Baca attempts.jsonl dari satu slot, return entri yang berhasil buka cards (deduplikasi by address)."""
    attempts_file = ROOT / "wallets" / slot / "attempts.jsonl"
    if not attempts_file.exists():
        return []
    seen: set[str] = set()
    valid: list[dict] = []
    with attempts_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            addr = (rec.get("address") or "").strip()
            uid = rec.get("user_id")
            if not addr or not uid:
                continue
            if addr in seen:
                continue
            if not rec.get("cards"):  # skip error/bad_grant — tidak ada kartu
                continue
            seen.add(addr)
            valid.append({
                "address": addr,
                "user_id": int(uid),
                "attempt": rec.get("attempt", 0),
                "winner": bool(rec.get("winner")),
            })
    return valid


def _force_open_packs(acct: dict) -> dict:
    """Buka semua pack yang tersedia untuk satu akun."""
    api = make_api(acct)
    try:
        me = api.me()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    user = me.get("user") or me.get("data") or me or {}
    starter = int(user.get("starter_packs", 0) or 0)
    paid = int(user.get("paid_packs", 0) or 0)
    total = starter + paid
    if total <= 0:
        return {"name": acct["name"], "ok": True, "opened": 0, "cards": [], "msg": "tidak ada pack"}
    try:
        data = api.open_packs(total)
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    cards = data.get("opened_cards") or []
    return {"name": acct["name"], "ok": True, "opened": total, "cards": cards}


def _create_wallet_and_open(acct_name: str, ref: str = "") -> dict:
    """Generate TON wallet baru → wallet_login → open starter packs → return result.

    Mnemonic disimpan ke wallets/<acct_name>/mnemonics.txt SEBELUM network activity
    supaya tidak hilang walau crash.
    """
    from wallet_gen import new_wallet

    mnemonic, address = new_wallet()

    # Simpan mnemonic dulu sebelum apapun
    wallet_dir = ROOT / "wallets" / acct_name
    wallet_dir.mkdir(parents=True, exist_ok=True)
    with (wallet_dir / "mnemonics.txt").open("a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().isoformat(timespec='seconds')}\t{address}\t{' '.join(mnemonic)}\n"
        )
        f.flush()

    api = HashHeroesAPI(
        wallet_address=address,
        ref=ref or None,
        max_retries=3,
        backoff=2.0,
        timeout=30,
    )

    try:
        login_data = api.wallet_login()
    except HashHeroesError as exc:
        return {"name": acct_name, "ok": False, "address": address,
                "mnemonic": " ".join(mnemonic), "error": f"login: {exc}"}

    user_id = ((login_data.get("user") or {}).get("id"))

    try:
        me = api.me()
    except HashHeroesError as exc:
        return {"name": acct_name, "ok": False, "address": address,
                "user_id": user_id, "mnemonic": " ".join(mnemonic), "error": f"me: {exc}"}

    actual_packs = int((me.get("user") or {}).get("starter_packs", 0) or 0)
    cards: list = []

    if actual_packs > 0:
        try:
            opened = api.open_packs(actual_packs)
            cards = opened.get("opened_cards") or []
        except HashHeroesError as exc:
            return {"name": acct_name, "ok": True, "address": address,
                    "user_id": user_id, "mnemonic": " ".join(mnemonic),
                    "packs": actual_packs, "cards": [], "warn": f"open pack gagal: {exc}"}

    return {
        "name": acct_name,
        "ok": True,
        "address": address,
        "user_id": user_id,
        "mnemonic": " ".join(mnemonic),
        "packs": actual_packs,
        "cards": cards,
    }


def _force_stake_ur_only(acct: dict) -> dict:
    """Stake HANYA kartu UR yang idle. Skip akun tanpa UR."""
    api = make_api(acct)
    try:
        data = api.cards()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    cards = data.get("cards") or []
    idle_ur = [
        c for c in cards
        if str(c.get("rarity") or "").upper() == "UR"
        and not (c.get("is_staked") or c.get("isStaked") or c.get("staked"))
    ]
    if not idle_ur:
        return {"name": acct["name"], "ok": True, "staked": 0, "failed": 0, "msg": "no idle UR"}
    success = 0
    failed = 0
    for c in idle_ur:
        cid = c.get("id") or c.get("card_id")
        try:
            api.stake(cid)
            success += 1
        except HashHeroesError:
            failed += 1
        time.sleep(random.uniform(0.8, 1.5))
    return {"name": acct["name"], "ok": True, "staked": success, "failed": failed}


def _force_unstake_all_cards(acct: dict) -> dict:
    """Unstake semua kartu yang sedang di-stake."""
    api = make_api(acct)
    try:
        data = api.cards()
    except HashHeroesError as exc:
        return {"name": acct["name"], "ok": False, "error": str(exc)}
    cards = data.get("cards") or []
    staked = [c for c in cards if c.get("is_staked") or c.get("isStaked") or c.get("staked")]
    if not staked:
        return {"name": acct["name"], "ok": True, "unstaked": 0, "failed": 0, "msg": "tidak ada yang di-stake"}
    success = 0
    failed = 0
    for c in staked:
        cid = c.get("id") or c.get("card_id")
        try:
            api.unstake(cid)
            success += 1
        except HashHeroesError:
            failed += 1
        time.sleep(random.uniform(0.8, 1.5))
    return {"name": acct["name"], "ok": True, "unstaked": success, "failed": failed}


async def run_blocking(func, *args):
    """Run a sync, IO-bound function in the default threadpool."""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


async def _fetch_group_data(group_name: str, accs: list[dict]) -> dict:
    """Fetch semua wallet dalam 1 grup secara paralel (max 5 concurrent), lalu agregasi."""
    sem = asyncio.Semaphore(5)

    async def _one(a: dict):
        async with sem:
            return await run_blocking(_fetch_dashboard, a)

    results = await asyncio.gather(*[_one(a) for a in accs], return_exceptions=True)
    ok_r = [r for r in results if isinstance(r, dict) and r.get("ok")]
    tiers: dict[str, int] = {"UR": 0, "SSR": 0, "SR": 0, "Rare": 0, "Uncommon": 0, "Common": 0}
    for r in ok_r:
        for t in tiers:
            tiers[t] += r.get("cards_by_tier", {}).get(t, 0)
    return {
        "group": group_name,
        "total_wallets": len(accs),
        "ok_count": len(ok_r),
        "total_hash": sum(r["hash_balance"] for r in ok_r),
        "total_pending": sum(r["mining"]["pending"] for r in ok_r),
        "total_staked": sum(r["cards_staked"] for r in ok_r),
        "total_cards": sum(r["cards_total"] for r in ok_r),
        "total_hp": sum(r.get("hashpower", 0) for r in ok_r),
        "ready_count": sum(1 for r in ok_r if r["mining"]["elapsed"] >= CLAIM_COOLDOWN),
        "tiers": tiers,
    }


# ---------------------------------------------------------------- guards
def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != config.TELEGRAM_ADMIN_ID:
            log.warning("rejected unauthorized user_id=%s", user.id if user else "?")
            return
        return await handler(update, context)

    return wrapper


# ---------------------------------------------------------------- formatting
def _fmt_overview_line(o: dict) -> str:
    if not o.get("ok"):
        return f"❌ <b>{esc(o['name'])}</b> · {esc(o.get('error',''))[:80]}"
    m = o["mining"]
    elapsed = m["elapsed"]
    if elapsed >= CLAIM_COOLDOWN:
        cd = "<b>READY ✅</b>"
    else:
        cd = f"⏳ {human_duration(CLAIM_COOLDOWN - elapsed)}"
    rate = m["rate_per_day"]
    return (
        f"<b>{esc(o['name'])}</b> "
        f"<code>{o['hash_balance']:.4f}</code> $HASH · "
        f"⛏ <code>{m['pending']:.4f}</code> @ <code>{rate:.2f}</code>/d · {cd}"
    )


def _rarity_emoji(r: str) -> str:
    return {
        "Common": "⬜",
        "Uncommon": "🟩",
        "Rare": "🟦",
        "SR": "🟪",
        "SSR": "🟧",
        "Mythic": "🟥",
        "Legendary": "🟥",
    }.get(str(r or ""), "⬛")


def _fmt_cards_for(o: dict) -> str:
    if not o.get("ok"):
        return f"❌ <b>{esc(o['name'])}</b> · {esc(o.get('error',''))[:120]}"
    cards = o.get("cards") or []
    if not cards:
        return f"<b>{esc(o['name'])}</b> · no cards"
    # group by name+rarity, count + how many staked
    by_key: dict[tuple, dict[str, Any]] = {}
    for c in cards:
        rarity = str(c.get("rarity") or c.get("tier") or "?")
        cname = str(c.get("name") or c.get("card_name") or "?")
        is_staked = bool(c.get("is_staked") or c.get("isStaked") or c.get("staked"))
        key = (rarity, cname)
        slot = by_key.setdefault(
            key, {"total": 0, "staked": 0, "rarity": rarity, "name": cname}
        )
        slot["total"] += 1
        if is_staked:
            slot["staked"] += 1
    # sort by rarity rank then count
    rank = {"Mythic": 0, "Legendary": 0, "SSR": 1, "SR": 2, "Rare": 3, "Uncommon": 4, "Common": 5}
    rows = sorted(
        by_key.values(),
        key=lambda r: (rank.get(r["rarity"], 9), -r["total"], r["name"]),
    )
    total_cards = sum(r["total"] for r in rows)
    total_staked = sum(r["staked"] for r in rows)
    lines = [
        f"<b>{esc(o['name'])}</b> · {total_cards} card(s), {total_staked} staked"
    ]
    for r in rows:
        em = _rarity_emoji(r["rarity"])
        flag = ""
        if r["staked"] == r["total"]:
            flag = " ✅"
        elif r["staked"] > 0:
            flag = f" ({r['staked']}/{r['total']} staked)"
        else:
            flag = " 🆓"
        lines.append(
            f"{em} <code>{r['rarity']:<8}</code> · {esc(r['name'])} ×{r['total']}{flag}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- handlers
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hash-Heroes control bot is online. /help untuk list command.",
    )


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Hash-Heroes Bot — Daftar Command</b>\n\n"
        "<b>📊 Info</b>\n"
        "/fleet — semua grup + tombol ⚡Stake UR &amp; 💰Claim (instan)\n"
        "/groupstatus [grup] — live data 1 grup wallet\n"
        "/dashboard — detail per wallet (saldo + kartu + mining)\n"
        "/cards [name] — status kartu per akun\n"
        "/totals — lifetime totals semua akun\n"
        "/accounts — list semua akun\n\n"
        "<b>⚙️ Aksi</b>\n"
        "/importwallets [slot...] — import wallet dari folder wallets/\n"
        "/createwallets [nama] [jumlah] [ref] — buat wallet baru + open pack\n"
        "/openpacks [name] — buka semua pack (semua/1 akun)\n"
        "/unstakeall [name] — unstake semua kartu (semua/1 akun)\n\n"
        "<b>💰 Mining</b>\n"
        "/claimall [name] — claim mining rewards (semua/1 akun)\n\n"
        "<i>Contoh: /groupstatus myaccount · /claimall · /cards myslot</i>"
    )
    await update.message.reply_html(text)


@admin_only
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = load_accounts()
    if not accs:
        await update.message.reply_text("(accounts.json empty)")
        return
    lines = ["<b>Accounts</b>"]
    for a in accs:
        flag = "⏸" if a.get("disabled") else "▶️"
        login = (
            f"id <code>{a.get('user_id')}</code>"
            if a.get("user_id")
            else f"wallet <code>{esc(short_addr(a.get('wallet_address','')))}</code>"
            if a.get("wallet_address")
            else "<i>no creds</i>"
        )
        tg = a.get("telegram_first_name") or a.get("telegram_id") or ""
        suffix = f" · TG: {esc(tg)}" if tg else ""
        lines.append(f"{flag} <b>{esc(a.get('name','?'))}</b> · {login}{suffix}")
    await update.message.reply_html("\n".join(lines))


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return
    msg = await update.message.reply_text("⏳ querying…")
    results = []
    for a in accs:
        results.append(await run_blocking(_fetch_overview, a))
    grand = sum(
        r["hash_balance"] for r in results if r.get("ok")
    )
    pending = sum(
        r["mining"]["pending"] for r in results if r.get("ok")
    )
    lines = [_fmt_overview_line(r) for r in results]
    text = (
        f"<b>{len(results)} account(s)</b> · total bal "
        f"<code>{grand:.4f}</code> · pending <code>{pending:.4f}</code> $HASH\n\n"
        + "\n".join(lines)
    )
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return
    msg = await update.message.reply_text("⏳ querying…")
    results = [await run_blocking(_fetch_overview, a) for a in accs]
    rows = []
    grand = 0.0
    for r in results:
        if not r.get("ok"):
            rows.append(f"❌ {esc(r['name'])}: {esc(r.get('error',''))[:60]}")
            continue
        grand += r["hash_balance"]
        rows.append(
            f"<b>{esc(r['name'])}</b>: <code>{r['hash_balance']:.6f}</code> $HASH"
            f" · DUST {r['dust_balance']}"
            + (f" · packs {r['starter_packs']+r['paid_packs']}" if r['starter_packs']+r['paid_packs'] else "")
        )
    text = "\n".join(rows) + f"\n\n<b>total</b>: <code>{grand:.6f}</code> $HASH"
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_mining(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.args[0] if context.args else None
    accs = (
        [find_account(name)] if name else active_accounts()
    )
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("no matching account")
        return
    msg = await update.message.reply_text("⏳ querying…")
    rows = []
    for a in accs:
        r = await run_blocking(_fetch_overview, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
            continue
        m = r["mining"]
        cap = CLAIM_COOLDOWN if m["multiplier"] <= 1 else 172_800
        if m["elapsed"] >= CLAIM_COOLDOWN:
            cd = "<b>READY TO CLAIM ✅</b>"
        else:
            cd = f"⏳ <b>{human_duration(CLAIM_COOLDOWN - m['elapsed'])}</b> left"
        rows.append(
            f"<b>{esc(r['name'])}</b>\n"
            f"  rate <code>{m['rate_per_day']:.4f}</code> $HASH/day\n"
            f"  pending <code>{m['pending']:.6f}</code>\n"
            f"  elapsed {human_duration(m['elapsed'])} (cap {human_duration(cap)})\n"
            f"  {cd}"
        )
    await msg.edit_text("\n\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("no matching account")
        return
    msg = await update.message.reply_text("⏳ querying…")
    rows = []
    for a in accs:
        r = await run_blocking(_fetch_cards, a)
        rows.append(_fmt_cards_for(r))
    text = "\n\n".join(rows)
    # Telegram caps at 4096 chars; chunk if needed.
    if len(text) <= 4000:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
        return
    await msg.delete()
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    for ch in chunks:
        await update.message.reply_html(ch)


@admin_only
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("no matching account")
        return
    msg = await update.message.reply_text("⏳ querying…")
    rows = []
    for a in accs:
        r = await run_blocking(_fetch_tasks, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
            continue
        ts = r.get("tasks") or []
        if not ts:
            rows.append(f"<b>{esc(r['name'])}</b>: no tasks")
            continue
        bits = []
        for t in ts:
            mark = "✅" if t.get("completed") else "⬜"
            bits.append(f"  {mark} <code>{esc(t.get('key','?'))}</code>")
        rows.append(f"<b>{esc(r['name'])}</b>\n" + "\n".join(bits))
    await msg.edit_text("\n\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("no matching account")
        return
    msg = await update.message.reply_text("⛏ claiming…")
    rows = []
    grand = 0.0
    for a in accs:
        r = await run_blocking(_force_claim, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
            continue
        if "cooldown_left" in r:
            rows.append(
                f"⏳ <b>{esc(r['name'])}</b> not ready ({human_duration(r['cooldown_left'])} left)"
            )
            continue
        amount = r.get("claimed", 0.0)
        grand += amount
        rows.append(f"💰 <b>{esc(r['name'])}</b> claimed <code>{amount:.6f}</code> $HASH")
    if grand:
        rows.append(f"\n<b>total this pass</b>: <code>{grand:.6f}</code> $HASH")
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force a full open+stake+claim cycle on every active account."""
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return
    msg = await update.message.reply_text(
        f"🔄 running full cycle on {len(accs)} account(s)…"
    )
    rows = []
    for a in accs:
        r = await run_blocking(_force_cycle, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
            continue
        bits = []
        if r.get("opened"):
            bits.append(f"opened {r['opened']}")
        staked_total = r.get("staked", 0) + r.get("auto_staked", 0)
        if staked_total:
            bits.append(f"staked {staked_total}")
        if r.get("tasks"):
            bits.append(f"task {r['tasks']}")
        if r.get("referral_packs"):
            bits.append(f"ref {r['referral_packs']}")
        if r.get("claimed"):
            bits.append(f"+{float(r['claimed']):.4f}")
        line = f"✅ <b>{esc(r['name'])}</b>" + (f" · {', '.join(bits)}" if bits else " · no-op")
        rows.append(line)
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_totals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    totals = load_totals()
    if not totals:
        await update.message.reply_text("totals.json kosong (belum ada claim).")
        return
    rows = ["<b>Bot lifetime totals</b>"]
    grand = 0.0
    for name, v in sorted(totals.items()):
        if isinstance(v, dict):
            h = float(v.get("hash", 0))
            n = int(v.get("claims", 0))
            last = v.get("last_claim") or "-"
        else:
            h = float(v)
            n = 0
            last = "-"
        grand += h
        rows.append(
            f"<b>{esc(name)}</b>: <code>{h:.6f}</code> $HASH · {n} claim(s) · last {esc(last)}"
        )
    rows.append(f"\n<b>grand</b>: <code>{grand:.6f}</code> $HASH")
    await update.message.reply_html("\n".join(rows))


@admin_only
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return
    msg = await update.message.reply_text("⏳ Memuat dashboard…")

    results = []
    for a in accs:
        results.append(await run_blocking(_fetch_dashboard, a))

    ok_r = [r for r in results if r.get("ok")]
    total_hash = sum(r["hash_balance"] for r in ok_r)
    total_pending = sum(r["mining"]["pending"] for r in ok_r)
    total_cards = sum(r["cards_total"] for r in ok_r)
    total_staked = sum(r["cards_staked"] for r in ok_r)
    fleet_tiers: dict[str, int] = {"UR": 0, "SSR": 0, "SR": 0, "Rare": 0, "Uncommon": 0, "Common": 0}
    for r in ok_r:
        for tier in fleet_tiers:
            fleet_tiers[tier] += r.get("cards_by_tier", {}).get(tier, 0)
    ready_count = sum(1 for r in ok_r if r["mining"]["elapsed"] >= CLAIM_COOLDOWN)

    SEP = "─────────────────────────"
    lines: list[str] = [
        f"<b>📊 Dashboard</b> · <b>{len(results)}</b> akun"
        f" | 💰 <code>{total_hash:.4f}</code>"
        f" | ⛏ <code>{total_pending:.4f}</code> pending"
        f" | ✅ <b>{ready_count}</b> READY\n",
    ]

    TIER_EMOJI = {"UR": "🔴", "SSR": "🟠", "SR": "🟣", "Rare": "🔵", "Uncommon": "🟢", "Common": "⚪"}

    for r in results:
        lines.append(SEP)
        if not r.get("ok"):
            lines.append(f"❌ <b>{esc(r['name'])}</b> — {esc(r.get('error','?'))[:70]}")
            continue

        m = r["mining"]
        if m["elapsed"] >= CLAIM_COOLDOWN:
            cd_str = "<b>READY ✅</b>"
        else:
            cd_str = f"⏳ {human_duration(CLAIM_COOLDOWN - m['elapsed'])}"

        packs = r.get("starter_packs", 0) + r.get("paid_packs", 0)
        pack_str = f"  📦 <b>{packs}</b> pack" if packs else ""

        tc = r["cards_by_tier"]
        st = r["staked_by_tier"]
        tier_parts = []
        for tier in ("UR", "SSR", "SR", "Rare", "Uncommon", "Common"):
            n = tc.get(tier, 0)
            if n:
                s = st.get(tier, 0)
                flag = "⚡" if s == n else f"{s}⚡"
                tier_parts.append(f"{TIER_EMOJI[tier]}<code>{tier}×{n}({flag})</code>")

        tier_line = " ".join(tier_parts) if tier_parts else "<i>no cards</i>"

        lines.append(
            f"<b>{esc(r['name'])}</b>{pack_str}\n"
            f"💰 <code>{r['hash_balance']:.4f}</code> $HASH"
            f"  HP <code>{r.get('hashpower', 0)}</code>\n"
            f"⛏ <code>{m['pending']:.4f}</code>"
            f" @ <code>{m['rate_per_day']:.2f}</code>/d · {cd_str}\n"
            f"🃏 {r['cards_total']} kartu ({r['cards_staked']} staked)\n"
            f"{tier_line}"
        )

    fleet_tier_parts = [
        f"{TIER_EMOJI[t]}×{n}" for t, n in fleet_tiers.items() if n
    ]
    lines.append(SEP)
    lines.append(
        f"<b>🚀 FLEET TOTAL</b>\n"
        f"💰 <code>{total_hash:.4f}</code> $HASH"
        f"  ⛏ pending <code>{total_pending:.4f}</code>\n"
        f"🃏 {total_cards} kartu, {total_staked} staked\n"
        f"{'  '.join(fleet_tier_parts) or 'no cards'}"
    )

    text = "\n".join(lines)
    if len(text) <= 4000:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
        return
    await msg.delete()
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    for ch in chunks:
        await update.message.reply_html(ch)


@admin_only
async def cmd_openpacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buka semua pack yang tersedia untuk semua akun (atau 1 akun)."""
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("❌ akun tidak ditemukan")
        return
    label = f"<b>{esc(name)}</b>" if name else f"<b>{len(accs)} akun</b>"
    msg = await update.message.reply_html(f"📦 Membuka pack untuk {label}…")

    RARITY_EMOJI = {"UR": "🔴", "SSR": "🟠", "SR": "🟣", "Rare": "🔵", "Uncommon": "🟢", "Common": "⚪"}
    rows: list[str] = []
    total_opened = 0

    for a in accs:
        r = await run_blocking(_force_open_packs, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
            continue
        if r.get("msg"):
            rows.append(f"⏭ <b>{esc(r['name'])}</b>: {esc(r['msg'])}")
            continue
        total_opened += r["opened"]
        cards = r.get("cards") or []
        # Hitung per rarity
        counts: dict[str, int] = {}
        for c in cards:
            rar = str(c.get("rarity") or c.get("tier") or "?")
            counts[rar] = counts.get(rar, 0) + 1
        card_parts = [
            f"{RARITY_EMOJI.get(rar, '⬛')}{rar}×{n}"
            for rar, n in sorted(counts.items(), key=lambda x: {"UR":0,"SSR":1,"SR":2,"Rare":3,"Uncommon":4,"Common":5}.get(x[0], 9))
        ]
        card_str = "  ".join(card_parts) if card_parts else "?"
        rows.append(
            f"✅ <b>{esc(r['name'])}</b>: buka <b>{r['opened']}</b> pack → {len(cards)} kartu\n"
            f"   {card_str}"
        )

    summary = f"\n\n<b>Total dibuka: {total_opened} pack</b>" if total_opened else ""
    await msg.edit_text("\n".join(rows) + summary, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_stakeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stake SEMUA kartu idle untuk semua akun (atau 1 akun). Tidak ada filter rarity."""
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("❌ akun tidak ditemukan")
        return
    label = f"<b>{esc(name)}</b>" if name else f"<b>{len(accs)} akun</b>"
    msg = await update.message.reply_html(f"⚙️ Staking semua kartu idle untuk {label}…")
    rows: list[str] = []
    for a in accs:
        r = await run_blocking(_force_stake_all_cards, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
        elif r.get("msg"):
            rows.append(f"⏭ <b>{esc(r['name'])}</b>: {esc(r['msg'])}")
        else:
            rows.append(
                f"✅ <b>{esc(r['name'])}</b>: "
                f"{r.get('staked',0)} staked"
                + (f", {r['failed']} gagal" if r.get('failed') else "")
            )
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_unstakeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unstake SEMUA kartu untuk semua akun (atau 1 akun). Kebalikan dari /stakeall."""
    name = context.args[0] if context.args else None
    accs = [find_account(name)] if name else active_accounts()
    accs = [a for a in accs if a]
    if not accs:
        await update.message.reply_text("❌ akun tidak ditemukan")
        return
    label = f"<b>{esc(name)}</b>" if name else f"<b>{len(accs)} akun</b>"
    msg = await update.message.reply_html(f"⚙️ Unstaking semua kartu untuk {label}…")
    rows: list[str] = []
    for a in accs:
        r = await run_blocking(_force_unstake_all_cards, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
        elif r.get("msg"):
            rows.append(f"⏭ <b>{esc(r['name'])}</b>: {esc(r['msg'])}")
        else:
            rows.append(
                f"✅ <b>{esc(r['name'])}</b>: "
                f"{r.get('unstaked',0)} unstaked"
                + (f", {r['failed']} gagal" if r.get('failed') else "")
            )
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_stakessr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stake semua kartu idle HANYA untuk akun yang punya SSR/UR (semua tier bawah ikut distake)."""
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return
    msg = await update.message.reply_text(f"⚙️ Staking akun ber-SSR/UR ({len(accs)} akun diperiksa)…")
    rows: list[str] = []
    for a in accs:
        r = await run_blocking(_force_stake_ssr_filter, a)
        if not r.get("ok"):
            rows.append(f"❌ <b>{esc(r['name'])}</b>: {esc(r.get('error',''))[:80]}")
        elif r.get("msg"):
            rows.append(f"⏭ <b>{esc(r['name'])}</b>: {esc(r['msg'])}")
        else:
            rows.append(
                f"✅ <b>{esc(r['name'])}</b>: "
                f"{r.get('staked',0)} staked"
                + (f", {r['failed']} gagal" if r.get('failed') else "")
            )
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_importwallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Import semua wallet dari wallets/*/attempts.jsonl ke accounts.json.

    Usage: /importwallets           — semua slot
           /importwallets myslot myaccount   — slot tertentu
    """
    wallets_dir = ROOT / "wallets"
    if not wallets_dir.exists():
        await update.message.reply_text("❌ Folder wallets/ tidak ditemukan")
        return

    if context.args:
        slots = [s for s in context.args if (wallets_dir / s).is_dir()]
        if not slots:
            await update.message.reply_text(
                "❌ Slot tidak ditemukan. "
                "Cek nama folder di wallets/ (case-sensitive)."
            )
            return
    else:
        slots = sorted(d.name for d in wallets_dir.iterdir() if d.is_dir())

    msg = await update.message.reply_html(
        f"📥 Scanning <b>{len(slots)}</b> slot dari <code>wallets/</code>…"
    )

    existing = load_accounts()
    existing_addresses: set[str] = {(a.get("wallet_address") or "").strip() for a in existing}
    existing_names: set[str] = {a.get("name", "") for a in existing}

    total_added = 0
    total_skip = 0
    rows: list[str] = []

    for slot in slots:
        entries = await run_blocking(_parse_slot_wallets, slot)
        new_entries = [e for e in entries if e["address"] not in existing_addresses]
        skip_count = len(entries) - len(new_entries)
        total_skip += skip_count
        added = 0

        for e in new_entries:
            # Nama: {slot}_a{attempt} — mudah di-trace ke jsonl
            acct_name = f"{slot}_a{e['attempt']}"
            base = acct_name
            n = 2
            while acct_name in existing_names:
                acct_name = f"{base}_{n}"
                n += 1
            existing_names.add(acct_name)
            existing_addresses.add(e["address"])
            _append_account({
                "name": acct_name,
                "user_id": e["user_id"],
                "wallet_address": e["address"],
                "ref": "",
            })
            added += 1

        total_added += added
        winners = sum(1 for e in new_entries if e.get("winner"))
        line = (
            f"✅ <b>{esc(slot)}</b>: "
            f"+{added} baru, {skip_count} sudah ada "
            f"(total {len(entries)} wallet valid)"
        )
        if winners:
            line += f" 🏆 {winners} UR!"
        rows.append(line)

    rows.append(
        f"\n<b>Total: +{total_added} wallet diimport</b>, {total_skip} dilewati\n"
        f"Semua wallet sekarang ada di <code>accounts.json</code> dan siap di-stake/claim."
    )
    await msg.edit_text("\n".join(rows), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_createwallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buat N wallet baru untuk satu akun, open starter packs, simpan ke accounts.json.

    Usage: /createwallets [base_name] [jumlah] [ref_opsional]
    Contoh: /createwallets myaccount 5
            /createwallets myaccount 3 refcode123
    """
    if len(context.args) < 2:
        await update.message.reply_html(
            "<b>Usage:</b> /createwallets [nama_akun] [jumlah] [ref_opsional]\n"
            "<b>Contoh:</b> /createwallets myaccount 5\n\n"
            "Wallet baru akan dinamai <code>myaccount_1</code>, <code>myaccount_2</code>, dst.\n"
            "Setiap wallet login sendiri, buka starter pack, lalu disimpan ke accounts.json."
        )
        return

    base_name = context.args[0]
    try:
        count = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Jumlah harus angka. Contoh: /createwallets myaccount 5")
        return
    if count < 1 or count > 100:
        await update.message.reply_text("❌ Jumlah harus antara 1–100.")
        return

    ref = context.args[2] if len(context.args) >= 3 else ""

    # Tentukan nomor urut berikutnya supaya tidak overwrite yang sudah ada
    existing_names = {a.get("name", "") for a in load_accounts()}
    start_idx = 1
    while f"{base_name}_{start_idx}" in existing_names:
        start_idx += 1

    msg = await update.message.reply_html(
        f"🔨 Membuat <b>{count}</b> wallet baru untuk akun <b>{esc(base_name)}</b>…\n"
        f"Mulai dari <code>{base_name}_{start_idx}</code>. Harap tunggu."
    )

    RARITY_EMOJI = {"UR": "🔴", "SSR": "🟠", "SR": "🟣", "Rare": "🔵", "Uncommon": "🟢", "Common": "⚪"}
    rows: list[str] = []
    created = 0

    for i in range(count):
        idx = start_idx + i
        acct_name = f"{base_name}_{idx}"

        try:
            await msg.edit_text(
                f"🔨 Wallet <b>{i + 1}/{count}</b> — <code>{acct_name}</code>…",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        r = await run_blocking(_create_wallet_and_open, acct_name, ref)

        if not r.get("ok"):
            rows.append(
                f"❌ <b>{esc(acct_name)}</b>: {esc(r.get('error', '?'))[:100]}\n"
                f"   addr: <code>{esc(r.get('address', '?'))}</code>"
            )
            continue

        # Simpan ke accounts.json
        _append_account({
            "name": acct_name,
            "user_id": r.get("user_id"),
            "wallet_address": r.get("address"),
            "ref": ref,
        })
        created += 1

        cards = r.get("cards") or []
        counts: dict[str, int] = {}
        for c in cards:
            rar = str(c.get("rarity") or c.get("tier") or "?")
            counts[rar] = counts.get(rar, 0) + 1
        card_parts = [
            f"{RARITY_EMOJI.get(rar, '⬛')}{rar}×{n}"
            for rar, n in sorted(
                counts.items(),
                key=lambda x: {"UR": 0, "SSR": 1, "SR": 2, "Rare": 3, "Uncommon": 4, "Common": 5}.get(x[0], 9),
            )
        ]
        card_str = "  ".join(card_parts) if card_parts else "–"
        warn_str = f"\n   ⚠️ {esc(r['warn'])}" if r.get("warn") else ""

        rows.append(
            f"✅ <b>{esc(acct_name)}</b>  id <code>{r.get('user_id','?')}</code>\n"
            f"   <code>{r['address']}</code>\n"
            f"   📦 {r.get('packs', 0)} pack → {len(cards)} kartu: {card_str}"
            + warn_str
        )

        # Jeda antar wallet supaya tidak kena rate-limit
        await asyncio.sleep(random.uniform(2.5, 4.5))

    summary = (
        f"\n\n<b>{'✅' if created == count else '⚠️'} {created}/{count} wallet berhasil</b> "
        f"ditambahkan ke <code>accounts.json</code>.\n"
        f"Mnemonic tersimpan di <code>wallets/{esc(base_name)}_*/mnemonics.txt</code>"
    )
    text = "\n".join(rows) + summary

    if len(text) <= 4000:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
        return
    await msg.delete()
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    for ch in chunks:
        await update.message.reply_html(ch)


@admin_only
async def cmd_fleet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grouped fleet view — INSTAN, tanpa API call.
    Tampilkan hitungan wallet + lifetime claim per grup + tombol Stake/Claim.
    Untuk live data satu grup: /groupstatus [nama]
    """
    accs = active_accounts()
    if not accs:
        await update.message.reply_text("no active accounts")
        return

    groups = group_accounts(accs)
    totals = load_totals()
    SEP = "─────────────────────────"

    grand_lifetime = 0.0
    lines: list[str] = []
    keyboards: list[list] = []

    for group_name, group_accs in groups.items():
        lifetime = 0.0
        claims = 0
        for a in group_accs:
            t = totals.get(a.get("name", ""))
            if isinstance(t, dict):
                lifetime += float(t.get("hash", 0))
                claims += int(t.get("claims", 0))
            elif isinstance(t, (int, float)):
                lifetime += float(t)
        grand_lifetime += lifetime

        lines.append(SEP)
        lines.append(
            f"<b>🏷 {esc(group_name)}</b>  <code>{len(group_accs)}</code> wallet\n"
            f"💰 lifetime: <code>{lifetime:.4f}</code> $HASH"
            + (f" · {claims} claim" if claims else "")
        )
        keyboards.append([
            InlineKeyboardButton(f"⚡ Stake UR {group_name}", callback_data=f"stake_ur:{group_name}"),
            InlineKeyboardButton(f"💰 Claim {group_name}", callback_data=f"claim_g:{group_name}"),
        ])

    header = (
        f"<b>📊 Fleet</b> · {len(groups)} grup · {len(accs)} wallet\n"
        f"💰 lifetime: <code>{grand_lifetime:.4f}</code> $HASH\n"
        f"<i>Live data per grup → /groupstatus [nama]</i>\n"
    )
    text = header + "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…"

    await update.message.reply_html(
        text,
        reply_markup=InlineKeyboardMarkup(keyboards),
    )


@admin_only
async def cmd_groupstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live status untuk SATU grup — fetch API semua wallet dalam grup tsb.
    Usage: /groupstatus myaccount
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /groupstatus [nama_grup]\nContoh: /groupstatus myaccount"
        )
        return
    group_name = context.args[0]
    accs = [a for a in active_accounts() if get_base_name(a.get("name", "")) == group_name]
    if not accs:
        await update.message.reply_text(f"❌ Grup '{group_name}' tidak ditemukan")
        return

    msg = await update.message.reply_html(
        f"⏳ Fetching live data untuk <b>{esc(group_name)}</b>"
        f" · {len(accs)} wallet…"
    )
    g = await _fetch_group_data(group_name, accs)
    TIER_EMOJI = {"UR": "🔴", "SSR": "🟠", "SR": "🟣", "Rare": "🔵", "Uncommon": "🟢", "Common": "⚪"}
    tier_parts = [
        f"{TIER_EMOJI[t]}<code>{t}×{n}</code>"
        for t, n in g["tiers"].items() if n > 0
    ]
    tier_str = "  ".join(tier_parts) if tier_parts else "<i>no cards</i>"
    text = (
        f"<b>🏷 {esc(group_name)}</b>  {g['ok_count']}/{g['total_wallets']} wallet OK\n\n"
        f"🃏 {tier_str}\n"
        f"📌 {g['total_staked']}/{g['total_cards']} staked"
        f" | HP <code>{g['total_hp']:,}</code>\n"
        f"⛏ pending <code>{g['total_pending']:.4f}</code> $HASH"
        f" | ✅ <b>{g['ready_count']}</b> READY\n"
        f"💰 balance <code>{g['total_hash']:.4f}</code> $HASH"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⚡ Stake UR {group_name}", callback_data=f"stake_ur:{group_name}"),
        InlineKeyboardButton(f"💰 Claim {group_name}", callback_data=f"claim_g:{group_name}"),
    ]])
    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk tombol inline ⚡Stake dan 💰Claim di /fleet."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    if not user or user.id != config.TELEGRAM_ADMIN_ID:
        await query.answer("⛔ Unauthorized")
        return

    data = query.data or ""
    if ":" not in data:
        await query.answer("Invalid")
        return

    action, group_name = data.split(":", 1)
    accs = [a for a in active_accounts() if get_base_name(a.get("name", "")) == group_name]

    if not accs:
        await query.answer(f"❌ Grup '{group_name}' tidak ditemukan")
        return

    if action == "stake_ur":
        await query.answer(f"⚡ Staking UR {group_name}…")
        msg = await query.message.reply_html(
            f"⚡ Staking kartu <b>UR</b> untuk grup <b>{esc(group_name)}</b>"
            f" · {len(accs)} wallet…"
        )
        sem = asyncio.Semaphore(5)

        async def _stake_ur_one(a: dict):
            async with sem:
                return await run_blocking(_force_stake_ur_only, a)

        results = await asyncio.gather(*[_stake_ur_one(a) for a in accs], return_exceptions=True)
        staked = sum(r.get("staked", 0) for r in results if isinstance(r, dict))
        failed = sum(r.get("failed", 0) for r in results if isinstance(r, dict))
        ur_accts = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and not r.get("msg"))
        await msg.edit_text(
            f"✅ <b>{esc(group_name)}</b>: {ur_accts} wallet punya UR\n"
            f"⚡ {staked} UR staked"
            + (f", {failed} gagal" if failed else ""),
            parse_mode=ParseMode.HTML,
        )

    elif action == "claim_g":
        await query.answer(f"💰 Claiming {group_name} ({len(accs)} wallet)…")
        msg = await query.message.reply_html(
            f"⛏ Claiming semua untuk grup <b>{esc(group_name)}</b>"
            f" · {len(accs)} wallet…"
        )
        sem = asyncio.Semaphore(5)

        async def _claim_one(a: dict):
            async with sem:
                return await run_blocking(_force_claim, a)

        results = await asyncio.gather(*[_claim_one(a) for a in accs], return_exceptions=True)
        grand = 0.0
        ready = 0
        waiting = 0
        errors = 0
        for r in results:
            if not isinstance(r, dict):
                errors += 1
                continue
            if not r.get("ok"):
                errors += 1
            elif "cooldown_left" in r:
                waiting += 1
            else:
                grand += r.get("claimed", 0.0)
                ready += 1

        await msg.edit_text(
            f"💰 <b>{esc(group_name)}</b>: claimed <code>{grand:.6f}</code> $HASH\n"
            f"✅ {ready} claimed · ⏳ {waiting} belum ready · ❌ {errors} error",
            parse_mode=ParseMode.HTML,
        )


@admin_only
async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("unknown command — /help")


# ---------------------------------------------------------------- menu wiring
# Order = order they appear in the popup. Keep the most-used ones on top.
COMMAND_MENU = [
    ("fleet",          "semua grup + Stake UR & Claim buttons (instan)"),
    ("groupstatus",    "live data 1 grup wallet"),
    ("dashboard",      "detail per wallet (saldo + kartu + mining)"),
    ("claimall",       "claim mining rewards (semua/1 akun)"),
    ("openpacks",      "buka semua pack (semua/1 akun)"),
    ("importwallets",  "import wallet dari folder wallets/"),
    ("createwallets",  "buat wallet baru + open pack"),
    ("unstakeall",     "unstake semua kartu (semua/1 akun)"),
    ("cards",          "status kartu per akun"),
    ("totals",         "lifetime totals semua akun"),
    ("accounts",       "list semua akun"),
    ("help",           "bantuan"),
]


async def _post_init(app: Application) -> None:
    """Register slash menu + force the menu button next to the chat input
    to be the 'commands' button (so user can tap it instead of typing '/')."""
    cmds = [BotCommand(c, d) for c, d in COMMAND_MENU]
    try:
        await app.bot.set_my_commands(cmds)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        log.info("menu button + %d commands registered", len(cmds))
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to register menu: %s", exc)


# ---------------------------------------------------------------- main
def main() -> None:
    if not config.telegram_enabled():
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_ID are not set. "
            "Copy .env.example to .env and fill them."
        )
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("fleet", cmd_fleet))
    app.add_handler(CommandHandler("groupstatus", cmd_groupstatus))
    app.add_handler(CallbackQueryHandler(handle_group_callback, pattern=r"^(stake_ur|claim_g):"))
    app.add_handler(CommandHandler("claimall", cmd_claim))
    app.add_handler(CommandHandler("openpacks", cmd_openpacks))
    app.add_handler(CommandHandler("importwallets", cmd_importwallets))
    app.add_handler(CommandHandler("createwallets", cmd_createwallets))
    app.add_handler(CommandHandler("unstakeall", cmd_unstakeall))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("totals", cmd_totals))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("cards", cmd_cards))
    # silently ignore everything else (only admin gets here anyway)
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    log.info("starting telegram bot, admin=%s", config.TELEGRAM_ADMIN_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
