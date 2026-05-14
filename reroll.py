"""Mint fresh TON wallets until one rolls a UR Chrono Dragon, then stake it.

Workflow per attempt:
  1. Generate a 24-word TON mnemonic + UQ address (wallet_gen.new_wallet)
  2. POST /api/wallet-login   (HashHeroes auto-creates a fresh user_id)
  3. POST /api/packs/open    (5 starter packs come free with every account)
  4. Inspect the rolled cards. If any matches our target (UR Chrono Dragon):
       * stake all 5 cards
       * write wallets/<slot>/winner.json
       * stop
     else: log the attempt and try a new wallet.

EVERY mnemonic is appended to wallets/<slot>/attempts.jsonl BEFORE we touch
the network, so a crash / Ctrl+C never loses a phrase.

Usage:
  python reroll.py --account awee22344 --once          # one attempt, sanity test
  python reroll.py --account awee22344                  # default loop (max 200)
  python reroll.py --account awee22344 --max 500
  python reroll.py --account awee22344 --no-stake       # don't stake even if won
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from colorama import Fore, Style, init as colorama_init

from api import HashHeroesAPI, HashHeroesError
from wallet_gen import new_wallet, wallet_from_mnemonic

colorama_init(autoreset=True)

ROOT = Path(__file__).parent
WALLETS_DIR = ROOT / "wallets"

# UR detection. Primary: rarity field == "UR". Backup: name match. We also
# print suspicion warnings for anything with HashPower >= 1500 even if it
# didn't match, so a server-side rename can't make us miss a winner.
TARGET_RARITY = "UR"
TARGET_NAMES = {"chrono dragon"}
HP_SUSPICIOUS = 1500

RARITY_RANK = {
    "Common": 0,
    "Uncommon": 1,
    "Rare": 2,
    "SR": 3,
    "SSR": 4,
    "UR": 5,
}


# ---------------------------------------------------------------- helpers
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(label: str, color: str, msg: str) -> None:
    print(
        f"{Fore.LIGHTBLACK_EX}[{_ts()}]{Style.RESET_ALL} "
        f"{color}{label:<6}{Style.RESET_ALL} {msg}"
    )


def info(msg: str) -> None:
    _log("INFO", Fore.CYAN, msg)


def ok(msg: str) -> None:
    _log("OK", Fore.GREEN, msg)


def warn(msg: str) -> None:
    _log("WARN", Fore.YELLOW, msg)


def err(msg: str) -> None:
    _log("ERR", Fore.RED, msg)


def crit(msg: str) -> None:
    _log("UR★", Fore.MAGENTA + Style.BRIGHT, msg)


def _hp(c: dict) -> int:
    for k in ("hash_power", "hashPower", "hashpower", "power", "hp"):
        if c.get(k) is not None:
            try:
                return int(float(c[k]))
            except (TypeError, ValueError):
                pass
    return 0


def fmt_card(c: dict) -> str:
    rarity = str(c.get("rarity") or c.get("tier") or "?")
    name = str(c.get("name") or c.get("card_name") or "?")
    cid = c.get("id", c.get("card_id", "?"))
    color = {
        "Common": Fore.LIGHTBLACK_EX,
        "Uncommon": Fore.GREEN,
        "Rare": Fore.BLUE,
        "SR": Fore.MAGENTA,
        "SSR": Fore.YELLOW,
        "UR": Fore.RED + Style.BRIGHT,
    }.get(rarity, Fore.WHITE)
    return (
        f"{color}[{rarity:<8}]{Style.RESET_ALL} #{str(cid):<6} "
        f"{name:<22} HP:{_hp(c)}"
    )


def is_ur(c: dict) -> bool:
    rarity = str(c.get("rarity") or c.get("tier") or "").strip()
    name = str(c.get("name") or c.get("card_name") or "").strip().lower()
    if rarity.upper() == TARGET_RARITY:
        return True
    if name in TARGET_NAMES:
        return True
    return False


# ---------------------------------------------------------------- I/O
def slot_dir(account: str) -> Path:
    d = WALLETS_DIR / account
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_attempt(d: Path, record: dict) -> None:
    with (d / "attempts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_mnemonic_safe(d: Path, attempt_no: int, address: str, mnemonic: list[str]) -> None:
    """Belt-and-suspenders log: every mnemonic ends up in plain text BEFORE
    we touch the network, so even a hard kernel panic can't lose a phrase."""
    line = (
        f"{datetime.now().isoformat(timespec='seconds')}\t"
        f"#{attempt_no}\t{address}\t{' '.join(mnemonic)}\n"
    )
    with (d / "mnemonics.txt").open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def write_winner(d: Path, record: dict) -> None:
    with (d / "winner.json").open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def read_winner(d: Path) -> Optional[dict]:
    f = d / "winner.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def count_existing_attempts(d: Path) -> int:
    """Count from mnemonics.txt (written BEFORE the attempt) so a crash
    mid-attempt doesn't cause a duplicate attempt number on resume."""
    f = d / "mnemonics.txt"
    if not f.exists():
        return 0
    with f.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


# ---------------------------------------------------------------- core
def run_attempt(
    d: Path,
    attempt_no: int,
    do_stake: bool,
    seed_mnemonic: Optional[list[str]] = None,
) -> dict:
    if seed_mnemonic is not None:
        mnemonic, address = wallet_from_mnemonic(seed_mnemonic)
    else:
        mnemonic, address = new_wallet()
    record: dict = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "slot": d.name,
        "attempt": attempt_no,
        "address": address,
        "mnemonic": " ".join(mnemonic),
    }
    # Belt-and-suspenders: dump the mnemonic to a flat text file BEFORE any
    # network activity. The structured record gets appended to attempts.jsonl
    # at the END of this function (so it includes user_id, cards, etc.).
    append_mnemonic_safe(d, attempt_no, address, mnemonic)

    info(f"#{attempt_no:<3} {Fore.LIGHTBLACK_EX}{address}{Style.RESET_ALL}")
    # Fast-fail config: 2 retries, 1.5s backoff, 30s timeout, tight jitter.
    # When the backend is flaky (DB malformed under airdrop traffic), we'd
    # rather burn through 10 attempts in a minute than wait 4 minutes per
    # bad attempt. The bot worker still uses the default 5/3.0/30s.
    api = HashHeroesAPI(
        wallet_address=address,
        max_retries=2,
        backoff=1.5,
        jitter=(0.3, 0.9),
        timeout=30,
    )

    try:
        login = api.wallet_login()
    except HashHeroesError as exc:
        record["error"] = f"login: {exc}"
        err(f"   login failed: {exc}")
        record["transient"] = True
        append_attempt(d, record)
        return record
    record["user_id"] = api.user_id
    response_packs = int((login.get("user") or {}).get("starter_packs", 0) or 0)

    # The wallet_login response routinely LIES about starter_packs (says 5,
    # DB says 0) when the server is overloaded. Verify against /api/me
    # before wasting a /packs/open call.
    try:
        me = api.me()
    except HashHeroesError as exc:
        record["error"] = f"me: {exc}"
        warn(f"   /api/me failed (server flake): {exc}")
        record["transient"] = True
        append_attempt(d, record)
        return record
    actual_packs = int((me.get("user") or {}).get("starter_packs", 0) or 0)
    record["starter_packs_response"] = response_packs
    record["starter_packs_actual"] = actual_packs

    if actual_packs <= 0:
        record["error"] = "no_packs_granted"
        record["bad_grant"] = True
        warn(
            f"   server granted 0 packs (login response said {response_packs}) "
            f"— skipping. user_id={api.user_id}"
        )
        append_attempt(d, record)
        return record

    try:
        opened = api.open_packs(actual_packs)
    except HashHeroesError as exc:
        record["error"] = f"open: {exc}"
        if "not enough packs" in str(exc).lower():
            warn(f"   open said 'not enough packs' even though /api/me said {actual_packs}")
            record["bad_grant"] = True
        else:
            err(f"   open failed: {exc}")
            record["transient"] = True
        append_attempt(d, record)
        return record
    cards = opened.get("opened_cards") or []
    record["cards"] = cards

    found_ur = []
    suspicious = []
    for c in cards:
        if is_ur(c):
            found_ur.append(c)
        elif _hp(c) >= HP_SUSPICIOUS:
            suspicious.append(c)

    # Sort cards by rarity rank desc for nicer print
    sorted_cards = sorted(
        cards, key=lambda c: RARITY_RANK.get(str(c.get("rarity")), -1), reverse=True
    )
    for c in sorted_cards:
        line = "   " + fmt_card(c)
        if is_ur(c):
            line += f"  {Fore.MAGENTA}{Style.BRIGHT}★UR★{Style.RESET_ALL}"
        print(line)

    if suspicious and not found_ur:
        warn(
            f"   suspicious high-HP card(s) with non-UR rarity — verify: "
            + ", ".join(f"{c.get('name')!r}({_hp(c)})" for c in suspicious)
        )

    if not found_ur:
        append_attempt(d, record)
        return record

    # ------------------------------------------------------------- WIN
    record["winner"] = True
    crit(
        f"   🎉 UR FOUND: {found_ur[0].get('name')} "
        f"(HP {_hp(found_ur[0])})  user_id={api.user_id}  addr={address}"
    )
    write_winner(d, record)

    if not do_stake:
        info("   --no-stake → skipping stake step")
        append_attempt(d, record)
        return record

    info("   staking all 5 cards…")
    record["staked"] = []
    record["stake_failed"] = []
    for c in cards:
        cid = c.get("id") or c.get("card_id")
        try:
            api.stake(cid)
            ok(f"   staked #{cid} {c.get('name')}")
            record["staked"].append(cid)
        except HashHeroesError as exc:
            err(f"   stake #{cid} failed: {exc}")
            record["stake_failed"].append({"card_id": cid, "error": str(exc)})
        time.sleep(random.uniform(1.5, 3.0))
    # Refresh winner with stake results
    write_winner(d, record)
    append_attempt(d, record)
    return record


# ---------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description="Reroll TON wallets until UR.")
    p.add_argument("--account", required=True, help="slot name, e.g. awee22344")
    p.add_argument("--max", type=int, default=200, help="max attempts (default 200)")
    p.add_argument("--once", action="store_true", help="single attempt, then stop")
    p.add_argument("--no-stake", action="store_true", help="don't stake even on win")
    p.add_argument(
        "--gap",
        type=str,
        default="3,7",
        help="seconds to sleep between attempts: 'lo,hi' (default 3,7)",
    )
    p.add_argument(
        "--mnemonic",
        type=str,
        default=None,
        help="24 words in quotes — use this seed for the FIRST attempt instead "
             "of generating a new one (e.g. an already-registered wallet "
             "with starter packs).",
    )
    args = p.parse_args()

    try:
        gap_lo, gap_hi = (float(x) for x in args.gap.split(","))
    except ValueError:
        sys.exit("--gap must be 'lo,hi', e.g. 3,7")

    d = slot_dir(args.account)
    existing_winner = read_winner(d)
    if existing_winner and not args.once:
        warn(
            f"slot '{args.account}' already has winner.json "
            f"(addr={existing_winner.get('address')}). Delete it to reroll."
        )
        sys.exit(0)

    start_n = count_existing_attempts(d)
    if start_n:
        info(f"resuming: {start_n} previous attempt(s) recorded.")
    info(f"target: rarity={TARGET_RARITY!r} or name in {TARGET_NAMES!r}")
    info(f"slot:   {d}")

    n = start_n
    winners: list[dict] = []
    bad_grant_streak = 0
    transient_streak = 0
    successful_opens = 0
    seed: Optional[list[str]] = None
    if args.mnemonic:
        seed = args.mnemonic.split()
        if len(seed) != 24:
            sys.exit(f"--mnemonic must be 24 words, got {len(seed)}")
        info(f"first attempt will use the provided seed (24 words)")
    try:
        while True:
            n += 1
            rec = run_attempt(
                d, n, do_stake=not args.no_stake,
                seed_mnemonic=seed if n == start_n + 1 else None,
            )
            if rec.get("winner"):
                winners.append(rec)

            # Track backend health.
            if rec.get("bad_grant"):
                bad_grant_streak += 1
                transient_streak = 0
            elif rec.get("transient"):
                transient_streak += 1
            elif rec.get("cards"):
                # any successful open resets all streaks
                successful_opens += 1
                bad_grant_streak = 0
                transient_streak = 0

            if args.once:
                info("(--once → exiting after 1 attempt)")
                break
            done_in_session = n - start_n
            if done_in_session >= args.max:
                warn(f"hit --max {args.max} attempts. exiting.")
                break

            # Escalating cooldown when backend is acting up.
            if bad_grant_streak >= 5 or transient_streak >= 5:
                # 5 → 60s, 10 → 240s, 15 → 960s, capped at 30 minutes
                streak = max(bad_grant_streak, transient_streak)
                cool = min(60 * (2 ** ((streak - 5) // 2)), 1800)
                reason = "bad_grant" if bad_grant_streak >= 5 else "transient"
                warn(
                    f"backend looks broken ({reason} streak={streak}) — "
                    f"cooling down for {cool}s. session opens={successful_opens}."
                )
                # Reset streak after sleep so we re-evaluate
                time.sleep(cool)
                bad_grant_streak = 0
                transient_streak = 0
            else:
                time.sleep(random.uniform(gap_lo, gap_hi))
    except KeyboardInterrupt:
        print()
        warn(
            f"interrupted — session opens={successful_opens}, found {len(winners)} UR(s), "
            f"all phrases saved in mnemonics.txt"
        )
        sys.exit(130)

    if winners:
        print()
        ok("=" * 60)
        ok(f"  FOUND {len(winners)} UR(s) — slot '{args.account}' after {n} total attempt(s)")
        ok("=" * 60)
        for i, rec in enumerate(winners, 1):
            print()
            print(f"  UR #{i}:")
            print(f"    address:   {rec.get('address')}")
            print(f"    user_id:   {rec.get('user_id')}")
            print(f"    mnemonic:  {Fore.YELLOW}{rec.get('mnemonic')}{Style.RESET_ALL}")
            print(f"    card:      {rec.get('cards', [{}])[0].get('name', '?')}")
        print()
        print("  Update accounts.json for each slot:")
        for i, rec in enumerate(winners, 1):
            print(f'  # UR #{i}:')
            print(f'    "wallet_address": "{rec.get("address")}",')
            print(f'    "user_id": {rec.get("user_id")},')
        print()


if __name__ == "__main__":
    main()
