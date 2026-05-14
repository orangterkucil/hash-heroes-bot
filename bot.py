"""HashHeroes auto-bot.

Default flow per account:
  1. Login (wallet_login if wallet_address provided, otherwise direct user_id)
  2. Show profile + mining status
  3. Open every available pack
  4. Stake every idle card (fills mining slots)
  5. Claim mining reward when the 24h cooldown is done
  6. Persist cumulative claim totals to totals.json

Usage:
  python bot.py                # run once for all accounts in accounts.json
  python bot.py --loop         # repeat forever (sleeps to next claim window)
  python bot.py --no-open      # skip opening packs
  python bot.py --no-stake     # skip staking
  python bot.py --no-claim     # skip claiming
  python bot.py --account NAME # only run a single named account
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from colorama import Fore, Style, init as colorama_init

import config
import notifier
from api import HashHeroesAPI, HashHeroesError

colorama_init(autoreset=True)

ROOT = Path(__file__).parent
ACCOUNTS_FILE = ROOT / "accounts.json"
TOTALS_FILE = ROOT / "totals.json"

CLAIM_COOLDOWN = 86_400  # 24h in seconds
RARITY_COLOR = {
    "Common": Fore.LIGHTBLACK_EX,
    "Uncommon": Fore.GREEN,
    "Rare": Fore.BLUE,
    "SR": Fore.MAGENTA,
    "SSR": Fore.YELLOW,
    "Mythic": Fore.RED,
    "Legendary": Fore.RED,
}


# ---------------------------------------------------------------- ui helpers
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(label: str, msg: str, color: str = Fore.CYAN) -> None:
    print(
        f"{Fore.LIGHTBLACK_EX}[{_ts()}]{Style.RESET_ALL} "
        f"{color}{label:<6}{Style.RESET_ALL} {msg}"
    )


def info(msg: str, label: str = "INFO") -> None:
    log(label, msg, Fore.CYAN)


def ok(msg: str, label: str = "OK") -> None:
    log(label, msg, Fore.GREEN)


def warn(msg: str, label: str = "WARN") -> None:
    log(label, msg, Fore.YELLOW)


def err(msg: str, label: str = "ERROR") -> None:
    log(label, msg, Fore.RED)


def banner(title: str) -> None:
    bar = "=" * 64
    print(f"\n{Fore.YELLOW}{bar}\n{title.center(64)}\n{bar}{Style.RESET_ALL}")


_HP_KEYS = (
    "hash_power",
    "hashPower",
    "hashpower",
    "power",
    "hp",
    "rate",
    "mining_rate",
    "miningRateHashPerDay",
)


def _card_hp(c: dict) -> float:
    for k in _HP_KEYS:
        v = c.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def fmt_card(c: dict) -> str:
    rarity = str(c.get("rarity") or c.get("tier") or "?")
    color = RARITY_COLOR.get(rarity, Fore.WHITE)
    is_staked = bool(c.get("is_staked") or c.get("isStaked") or c.get("staked"))
    state = (
        f"{Fore.GREEN}STAKED{Style.RESET_ALL}"
        if is_staked
        else f"{Fore.LIGHTBLACK_EX}IDLE  {Style.RESET_ALL}"
    )
    name = str(c.get("name") or c.get("card_name") or "?")[:22]
    cid = c.get("id") or c.get("card_id") or "?"
    hp = _card_hp(c)
    hp_txt = f"{hp:g}" if hp else "—"
    return (
        f"{color}[{rarity:<8}]{Style.RESET_ALL} "
        f"#{str(cid):<6} {name:<22} HP:{hp_txt:<5} {state}"
    )


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------- persistence
def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        sample = [
            {
                "name": "main",
                "user_id": 1376,
                "wallet_address": "",
                "ref": "",
            }
        ]
        ACCOUNTS_FILE.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        warn(f"Created template {ACCOUNTS_FILE.name}. Edit it and rerun.")
        sys.exit(0)
    data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        err("accounts.json must be a non-empty list of account objects.")
        sys.exit(1)

    # Ensure each entry has a usable, unique name (persist_account_fields keys
    # off of it) and a login method (user_id OR wallet_address). Entries with
    # `disabled: true` are kept in the file (so we don't lose human metadata
    # like telegram_id / telegram_username) but skipped at runtime.
    seen: dict[str, int] = {}
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            err(f"accounts.json entry #{idx} is not an object.")
            sys.exit(1)
        is_disabled = bool(entry.get("disabled"))
        if not is_disabled:
            if not entry.get("user_id") and not (
                entry.get("wallet_address") or ""
            ).strip():
                err(
                    f"accounts.json entry #{idx} ('{entry.get('name','?')}') "
                    f"is missing both user_id and wallet_address. "
                    f"Set 'disabled': true to skip it temporarily."
                )
                sys.exit(1)
        raw_name = str(entry.get("name") or "").strip()
        if not raw_name:
            raw_name = f"acct{idx + 1}"
        base = raw_name
        n = 1
        while raw_name in seen:
            n += 1
            raw_name = f"{base}#{n}"
        if raw_name != entry.get("name"):
            if entry.get("name"):
                warn(
                    f"duplicate/blank name on entry #{idx}, renamed to '{raw_name}'",
                    "CFG",
                )
            entry["name"] = raw_name
        seen[raw_name] = idx
    return data


def load_totals() -> dict:
    if TOTALS_FILE.exists():
        try:
            return json.loads(TOTALS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_totals(totals: dict) -> None:
    TOTALS_FILE.write_text(json.dumps(totals, indent=2), encoding="utf-8")


def add_claim(name: str, amount: float) -> float:
    totals = load_totals()
    entry = totals.get(name) or {"hash": 0.0, "claims": 0, "last_claim": None}
    if isinstance(entry, (int, float)):  # legacy format
        entry = {"hash": float(entry), "claims": 0, "last_claim": None}
    entry["hash"] = float(entry.get("hash", 0.0)) + float(amount)
    entry["claims"] = int(entry.get("claims", 0)) + 1
    entry["last_claim"] = datetime.now().isoformat(timespec="seconds")
    totals[name] = entry
    save_totals(totals)
    return float(entry["hash"])


def persist_account_fields(name: str, updates: dict) -> bool:
    """Merge `updates` into the account matched by `name` inside accounts.json.

    Returns True when the file was actually rewritten.
    """
    if not ACCOUNTS_FILE.exists() or not updates:
        return False
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if isinstance(data, dict):
        data = [data]
    changed = False
    for entry in data:
        if entry.get("name") != name:
            continue
        for k, v in updates.items():
            if v in (None, ""):
                continue
            if entry.get(k) == v:
                continue
            entry[k] = v
            changed = True
    if changed:
        ACCOUNTS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return changed


# ---------------------------------------------------------------- display
def _user_from_me(payload: dict) -> dict:
    return payload.get("user") or payload.get("data") or payload or {}


def show_profile(user: dict, mining: dict) -> None:
    info(
        f"id={user.get('id')} username={user.get('username','?')} "
        f"wallet={(user.get('wallet_address') or '-')[:10]}…",
        "USER",
    )
    log(
        "BAL",
        f"$HASH={Fore.YELLOW}{float(user.get('hash_balance',0)):.6f}{Style.RESET_ALL}  "
        f"DUST={Fore.MAGENTA}{int(user.get('dust_balance',0))}{Style.RESET_ALL}  "
        f"HashPower={Fore.CYAN}{int(user.get('total_hashpower',0) or user.get('hashpower',0) or 0)}{Style.RESET_ALL}",
        Fore.CYAN,
    )
    starter = int(user.get("starter_packs", 0) or 0)
    paid = int(user.get("paid_packs", 0) or 0)
    log(
        "PACKS",
        f"available={starter+paid} (starter={starter} paid={paid})",
        Fore.CYAN,
    )

    if mining:
        elapsed = int(mining.get("elapsedSeconds", 0) or 0)
        cap = CLAIM_COOLDOWN if (mining.get("multiplier", 1) or 1) <= 1 else 172_800
        amount = float(mining.get("hashAmount", 0) or 0)
        rate = float(mining.get("miningRateHashPerDay", 0) or 0)
        if elapsed >= CLAIM_COOLDOWN:
            status = f"{Fore.GREEN}READY TO CLAIM{Style.RESET_ALL}"
        else:
            status = f"{Fore.YELLOW}{human_duration(CLAIM_COOLDOWN - elapsed)} left{Style.RESET_ALL}"
        log(
            "MINE",
            f"pending={Fore.YELLOW}{amount:.6f}{Style.RESET_ALL} $HASH  "
            f"rate={rate:.4f}/day  elapsed={human_duration(elapsed)}  cap={human_duration(cap)}  {status}",
            Fore.CYAN,
        )


# ---------------------------------------------------------------- actions
def do_login(api: HashHeroesAPI, acct: dict) -> bool:
    name = acct.get("name", "?")
    # Prefer direct mode when user_id is known: it's a single GET vs
    # wallet-login's POST + GET, and avoids redundant signup churn.
    if api.user_id:
        info(f"direct mode (user_id={api.user_id})", "LOGIN")
        try:
            payload = api.me()
            user = _user_from_me(payload)
            if not user.get("id"):
                err("login probe returned no user object", "LOGIN")
                return False
            ok(f"session OK for user_id={user.get('id')} username={user.get('username','?')}", "LOGIN")
            return True
        except HashHeroesError as exc:
            err(f"login probe failed: {exc}", "LOGIN")
            return False
    if api.wallet_address:
        info(f"wallet-login as {api.wallet_address[:10]}…", "LOGIN")
        try:
            data = api.wallet_login()
            user = data.get("user") or {}
            ok(f"logged in user_id={user.get('id')} username={user.get('username')}", "LOGIN")
            return True
        except HashHeroesError as exc:
            err(f"wallet-login failed: {exc}", "LOGIN")
            return False
    err(f"account '{name}' has no user_id and no wallet_address", "LOGIN")
    return False


def do_open_packs(api: HashHeroesAPI, user: dict) -> tuple[int, int]:
    available = int(user.get("starter_packs", 0) or 0) + int(user.get("paid_packs", 0) or 0)
    if available <= 0:
        info("no packs to open", "PACK")
        return 0, 0
    info(f"opening {available} pack(s)… (one at a time)", "PACK")
    try:
        data = api.open_packs(available)
    except HashHeroesError as exc:
        err(f"open failed: {exc}", "PACK")
        return 0, 0
    cards = data.get("opened_cards") or []
    ok(f"opened {len(cards)} card(s):", "PACK")
    for c in cards:
        print(f"     {fmt_card(c)}")
    
    # Auto-stake UR cards immediately after opening
    staked_count = 0
    for c in cards:
        rarity = str(c.get("rarity") or c.get("tier") or "?")
        if rarity == "UR":
            cid = c.get("id") or c.get("card_id")
            try:
                api.stake(cid)
                hp = _card_hp(c)
                hp_txt = f"HP {hp:g}" if hp else "HP —"
                ok(f"auto-staked #{cid} {c.get('name','?')} ({hp_txt})", "STAKE")
                staked_count += 1
            except HashHeroesError as exc:
                err(f"auto-stake #{cid} failed: {exc}", "STAKE")
            # Human-ish gap between taps
            time.sleep(random.uniform(1.5, 3.5))
    if staked_count > 0:
        info(f"auto-staked {staked_count} high-rarity card(s)", "PACK")
    
    return len(cards), staked_count


def do_stake_all(api: HashHeroesAPI) -> tuple[int, int]:
    info("fetching cards…", "CARDS")
    try:
        data = api.cards()
    except HashHeroesError as exc:
        err(f"cards fetch failed: {exc}", "CARDS")
        return 0, 0
    cards = data.get("cards") or []
    ur_cards = [c for c in cards if str(c.get("rarity") or "").upper() == "UR"]
    idle_ur = [c for c in ur_cards if not (c.get("is_staked") or c.get("isStaked") or c.get("staked"))]
    staked_ur = [c for c in ur_cards if c.get("is_staked") or c.get("isStaked") or c.get("staked")]
    info(
        f"total={len(cards)} UR={len(ur_cards)} (staked={len(staked_ur)} idle={len(idle_ur)})",
        "CARDS",
    )
    if not idle_ur:
        info("no idle UR cards to stake", "STAKE")
        return 0, 0
    success = 0
    failed = 0
    for c in idle_ur:
        cid = c.get("id") or c.get("card_id")
        try:
            api.stake(cid)
            hp = _card_hp(c)
            ok(f"staked UR #{cid} {c.get('name','?')} (HP {hp:g})", "STAKE")
            success += 1
        except HashHeroesError as exc:
            err(f"stake #{cid} failed: {exc}", "STAKE")
            failed += 1
        time.sleep(random.uniform(1.5, 3.5))
    return success, failed


# task_keys safe to auto-complete (server only verifies trust-on-the-client,
# the action itself isn't gated by anything on the bot side)
SAFE_AUTO_TASKS = (
    "join_channel",
    "join_discussion",
    # "open_pack" auto-completes when do_open_packs runs
)


def do_complete_tasks(api: HashHeroesAPI) -> int:
    info("fetching tasks…", "TASKS")
    try:
        data = api.tasks()
    except HashHeroesError as exc:
        err(f"task fetch failed: {exc}", "TASKS")
        return 0
    tasks = data.get("tasks") or []
    if not tasks:
        info("no tasks reported", "TASKS")
        return 0
    pending = [
        t for t in tasks
        if t.get("key") in SAFE_AUTO_TASKS and not t.get("completed")
    ]
    if not pending:
        info(
            f"all {len(SAFE_AUTO_TASKS)} auto-tasks already complete",
            "TASKS",
        )
        return 0
    done = 0
    for t in pending:
        key = t.get("key")
        try:
            api.complete_task(key)
            ok(f"task '{key}' marked complete", "TASKS")
            done += 1
        except HashHeroesError as exc:
            err(f"task '{key}' failed: {exc}", "TASKS")
        time.sleep(0.6)
    return done


def do_claim_referral_pack(api: HashHeroesAPI) -> int:
    """Claims a hero pack from referrals (1 pack per 5 valid referrals).

    Server itself enforces the 5-friend threshold, so calling when not eligible
    just returns the current standings without granting anything.
    """
    try:
        data = api.claim_referral_pack()
    except HashHeroesError as exc:
        # Some backends throw on no-eligibility; treat as informational.
        warn(f"referral claim skipped: {exc}", "REF")
        return 0
    if not data.get("claimed"):
        valid = data.get("totalValidReferrals") or data.get("valid_referrals") or 0
        info(f"no referral pack yet (valid friends: {valid})", "REF")
        return 0
    n = int(data.get("claimedPacks") or 1)
    ok(f"claimed {n} referral pack(s)", "REF")
    return n


def do_claim(api: HashHeroesAPI, mining: dict, name: str) -> float:
    elapsed = int(mining.get("elapsedSeconds", 0) or 0)
    if elapsed < CLAIM_COOLDOWN:
        info(
            f"cooldown not done ({human_duration(CLAIM_COOLDOWN - elapsed)} left)",
            "CLAIM",
        )
        return 0.0
    try:
        data = api.claim_mining()
    except HashHeroesError as exc:
        err(f"claim failed: {exc}", "CLAIM")
        return 0.0
    claimed = (data or {}).get("claimed") or {}
    amount = float(claimed.get("hashAmount", 0) or 0)
    grand = add_claim(name, amount)
    ok(
        f"claimed {Fore.YELLOW}{amount:.6f}{Style.RESET_ALL} $HASH  "
        f"(bot lifetime '{name}': {Fore.YELLOW}{grand:.6f}{Style.RESET_ALL})",
        "CLAIM",
    )
    if amount > 0 and config.NOTIFY_PER_CLAIM:
        notifier.notify_safe(
            f"💰 <b>{name}</b> claimed <code>{amount:.6f}</code> $HASH\n"
            f"lifetime: <code>{grand:.6f}</code>"
        )
    return amount


# ---------------------------------------------------------------- per-account
def run_account(acct: dict, args: argparse.Namespace) -> dict:
    name = acct.get("name") or f"user_{acct.get('user_id','?')}"
    banner(f"ACCOUNT: {name}")

    api = HashHeroesAPI(
        user_id=acct.get("user_id") or None,
        wallet_address=(acct.get("wallet_address") or "").strip() or None,
        ref=(acct.get("ref") or "").strip() or None,
    )

    if not do_login(api, acct):
        return {"name": name, "ok": False}

    me_payload = api.me()
    user = _user_from_me(me_payload)
    mining = me_payload.get("mining") or {}
    show_profile(user, mining)

    # Auto-fill accounts.json with data we only learn from the API, so the
    # config file doesn't stay mysteriously blank. wallet_address / user_id
    # come from /api/me, never overwritten if already set manually.
    try:
        updates = {
            "user_id": user.get("id"),
            "wallet_address": user.get("wallet_address"),
        }
        if persist_account_fields(name, updates):
            info(
                f"updated accounts.json with wallet_address={(user.get('wallet_address') or '')[:10]}…",
                "SYNC",
            )
    except Exception as exc:  # noqa: BLE001 - purely cosmetic
        warn(f"could not sync accounts.json: {exc}", "SYNC")

    # Flow: open > stake > claim (skip tasks and referral)
    tasks_done = 0
    ref_packs = 0

    opened = 0
    auto_staked = 0
    if not args.no_open:
        for attempt in range(1, 4):  # retry up to 3 times
            try:
                opened, auto_staked = do_open_packs(api, user)
                if opened:
                    try:
                        me_payload = api.me()
                        user = _user_from_me(me_payload)
                        mining = me_payload.get("mining") or {}
                    except HashHeroesError:
                        pass
                break
            except Exception as exc:
                if attempt < 3:
                    warn(f"open packs failed (attempt {attempt}/3): {exc}, retrying...", "OPEN")
                    time.sleep(2)
                else:
                    err(f"open packs failed after 3 attempts: {exc}", "OPEN")

    staked = failed = 0
    if not args.no_stake:
        for attempt in range(1, 4):  # retry up to 3 times
            try:
                staked, failed = do_stake_all(api)
                if staked:
                    try:
                        me_payload = api.me()
                        user = _user_from_me(me_payload)
                        mining = me_payload.get("mining") or {}
                    except HashHeroesError:
                        pass
                break
            except Exception as exc:
                if attempt < 3:
                    warn(f"stake failed (attempt {attempt}/3): {exc}, retrying...", "STAKE")
                    time.sleep(2)
                else:
                    err(f"stake failed after 3 attempts: {exc}", "STAKE")

    claimed_amount = 0.0
    if not args.no_claim:
        for attempt in range(1, 4):  # retry up to 3 times
            try:
                claimed_amount = do_claim(api, mining, name)
                if claimed_amount:
                    try:
                        me_payload = api.me()
                        user = _user_from_me(me_payload)
                        mining = me_payload.get("mining") or {}
                    except HashHeroesError:
                        pass
                break
            except Exception as exc:
                if attempt < 3:
                    warn(f"claim failed (attempt {attempt}/3): {exc}, retrying...", "CLAIM")
                    time.sleep(2)
                else:
                    err(f"claim failed after 3 attempts: {exc}", "CLAIM")

    banner(f"SUMMARY · {name}")
    show_profile(user, mining)
    totals = load_totals().get(name) or {}
    if isinstance(totals, dict) and totals.get("hash") is not None:
        info(
            f"bot lifetime: {Fore.YELLOW}{float(totals['hash']):.6f}{Style.RESET_ALL} $HASH "
            f"across {totals.get('claims',0)} claim(s)  "
            f"last={totals.get('last_claim') or '-'}",
            "TOTAL",
        )

    return {
        "name": name,
        "ok": True,
        "user_id": user.get("id"),
        "tasks": tasks_done,
        "referral_packs": ref_packs,
        "opened": opened,
        "auto_staked": auto_staked,
        "staked": staked,
        "stake_failed": failed,
        "claimed": claimed_amount,
        "hash_balance": float(user.get("hash_balance", 0) or 0),
        "mining_elapsed": int(mining.get("elapsedSeconds", 0) or 0),
    }


# ---------------------------------------------------------------- main
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HashHeroes auto bot")
    p.add_argument("--loop", action="store_true", help="loop forever, sleep until next claim")
    p.add_argument("--no-open", action="store_true", help="skip opening packs")
    p.add_argument("--no-stake", action="store_true", help="skip staking idle cards")
    p.add_argument("--no-claim", action="store_true", help="skip claiming mining")
    p.add_argument("--account", type=str, default=None, help="run only this account name")
    p.add_argument(
        "--sleep",
        type=int,
        default=0,
        help="seconds to sleep between accounts (default 0)",
    )
    return p.parse_args()


def run_once(args: argparse.Namespace) -> list[dict]:
    accounts = load_accounts()
    if args.account:
        accounts = [a for a in accounts if a.get("name") == args.account]
        if not accounts:
            err(f"no account named '{args.account}' in accounts.json")
            return []
    results: list[dict] = []
    skipped = [a for a in accounts if a.get("disabled")]
    for s in skipped:
        warn(
            f"skipping '{s.get('name')}' (disabled=true; needs user_id or wallet_address)",
            "SKIP",
        )
    active = [a for a in accounts if not a.get("disabled")]
    for idx, acct in enumerate(active):
        try:
            results.append(run_account(acct, args))
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - log and continue
            err(f"unexpected failure on '{acct.get('name','?')}': {exc}")
            results.append({"name": acct.get("name", "?"), "ok": False, "error": str(exc)})
        if args.sleep and idx < len(active) - 1:
            time.sleep(args.sleep)

    banner("RUN SUMMARY")
    grand_claim = 0.0
    for r in results:
        if not r.get("ok"):
            err(f"{r.get('name'):<20} FAILED  {r.get('error','')}", "FAIL")
            continue
        grand_claim += float(r.get("claimed", 0) or 0)
        total_staked = r.get('staked',0) + r.get('auto_staked',0)
        log(
            "DONE",
            f"{r['name']:<14} tasks={r.get('tasks',0):<1} ref={r.get('referral_packs',0):<1} "
            f"opened={r.get('opened',0):<2} staked={total_staked}/{total_staked+r.get('stake_failed',0):<2} "
            f"claimed={Fore.YELLOW}{float(r.get('claimed',0) or 0):.6f}{Style.RESET_ALL} "
            f"balance={Fore.YELLOW}{float(r.get('hash_balance',0) or 0):.6f}{Style.RESET_ALL}",
            Fore.GREEN,
        )
    if grand_claim:
        info(
            f"this run claimed total: {Fore.YELLOW}{grand_claim:.6f}{Style.RESET_ALL} $HASH",
            "GRAND",
        )
    totals = load_totals()
    if totals:
        lifetime = sum(
            float(v.get("hash", 0)) if isinstance(v, dict) else float(v)
            for v in totals.values()
        )
        info(
            f"bot lifetime across all accounts: {Fore.YELLOW}{lifetime:.6f}{Style.RESET_ALL} $HASH",
            "GRAND",
        )
    return results


def next_sleep_seconds(results: list[dict]) -> int:
    """Compute how long to sleep before the next claim window across all accounts."""
    pending: list[int] = []
    for r in results:
        if not r.get("ok"):
            continue
        elapsed = int(r.get("mining_elapsed", 0) or 0)
        # if we just claimed, elapsed resets to 0; budget a full 24h
        remaining = max(0, CLAIM_COOLDOWN - elapsed)
        # if cooldown was already done but claim was skipped, retry in 5 min
        if remaining == 0:
            remaining = 300
        pending.append(remaining)
    if not pending:
        return 600
    # add a buffer + 0–10 min random jitter so we don't hit the API on the
    # exact same second every cycle.
    return min(pending) + 60 + random.randint(0, 600)


def _send_digest(results: list[dict]) -> None:
    if not config.telegram_enabled():
        return
    grand_claim = sum(float(r.get("claimed", 0) or 0) for r in results if r.get("ok"))
    lines = []
    failures = 0
    for r in results:
        name = r.get("name", "?")
        if not r.get("ok"):
            failures += 1
            lines.append(f"❌ <b>{name}</b> — {r.get('error','failed')}")
            continue
        bal = float(r.get("hash_balance", 0) or 0)
        claimed = float(r.get("claimed", 0) or 0)
        opened = r.get("opened", 0) or 0
        staked = (r.get("staked", 0) or 0) + (r.get("auto_staked", 0) or 0)
        bits = []
        if claimed:
            bits.append(f"+{claimed:.4f} claim")
        if opened:
            bits.append(f"{opened} pack")
        if staked:
            bits.append(f"{staked} stake")
        extra = ", ".join(bits) if bits else "no-op"
        lines.append(f"✅ <b>{name}</b> · bal <code>{bal:.4f}</code> · {extra}")
    header = f"🤖 cycle done — claim total <b>{grand_claim:.6f}</b> $HASH"
    if failures:
        header += f"  ⚠️ {failures} failed"
    notifier.notify_safe(header + "\n" + "\n".join(lines))


def _send_heartbeat() -> None:
    if not config.telegram_enabled():
        return
    if config.HEARTBEAT_HOURS <= 0:
        return
    totals = load_totals()
    lifetime = sum(
        float(v.get("hash", 0)) if isinstance(v, dict) else float(v)
        for v in totals.values()
    )
    notifier.notify_safe(
        f"❤️ heartbeat · lifetime <b>{lifetime:.6f}</b> $HASH across "
        f"{len(totals)} account(s)"
    )


def main() -> None:
    args = parse_args()
    banner("HASH-HEROES BOT")
    last_heartbeat = time.monotonic()
    cycle = 0
    notifier.notify_safe("🚀 hash-heroes bot started")
    while True:
        cycle += 1
        try:
            results = run_once(args)
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            err(f"cycle failed: {exc}", "FATAL")
            notifier.notify_safe(f"💥 cycle {cycle} crashed: <code>{exc}</code>")
            results = []
        _send_digest(results)
        if not args.loop:
            break
        # heartbeat ping
        if config.HEARTBEAT_HOURS > 0:
            elapsed_hb = time.monotonic() - last_heartbeat
            if elapsed_hb >= config.HEARTBEAT_HOURS * 3600:
                _send_heartbeat()
                last_heartbeat = time.monotonic()
        sleep_for = next_sleep_seconds(results)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        info(
            f"[{ts}] next run in {human_duration(sleep_for)}…",
            "WAIT",
        )
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            warn("interrupted by user, exiting loop", "EXIT")
            notifier.notify_safe("🛑 hash-heroes bot stopped (KeyboardInterrupt)")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("interrupted by user, bye", "EXIT")
