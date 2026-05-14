"""HashHeroes API client.

Endpoints discovered from the official mini-app JS bundle (page-*.js):
  POST /api/wallet-login            { wallet_address, username, referred_by? }
  GET  /api/me/{user_id}
  GET  /api/cards/{user_id}
  POST /api/packs/open              { user_id, amount }
  POST /api/cards/stake             { user_id, card_id }
  POST /api/cards/unstake           { user_id, card_id }
  POST /api/cards/upgrade           { user_id, card_id }
  POST /api/cards/dismantle         { user_id, card_id }
  POST /api/mining/claim            { user_id }
  POST /api/referrals/claim-pack    { user_id }
  GET  /api/tasks/{user_id}
  POST /api/tasks/complete          { user_id, task_id }
  GET  /api/leaderboard/hashpower
  GET  /api/leaderboard/referral

The backend authenticates by trusting the `user_id` in the body / path, so once
you have it (returned by /wallet-login) you can call every other endpoint with
just that id.
"""

from __future__ import annotations

import random
import time
from typing import Any, Optional

import requests


class HashHeroesError(RuntimeError):
    """Raised when the API returns a non-2xx response."""


# Realistic Telegram-WebView (TDesktop on Windows) UA strings. We pick one per
# session and stick with it — flipping UA mid-session is itself a fingerprint.
_UA_POOL = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
)


def _pick_ua() -> str:
    return random.choice(_UA_POOL)


class HashHeroesAPI:
    BASE = "https://hashheroes.xyz/api"

    @staticmethod
    def _build_headers(ua: str) -> dict:
        # Match what Telegram-Desktop's embedded WebView2 actually sends.
        return {
            "User-Agent": ua,
            "Origin": "https://hashheroes.xyz",
            "Referer": "https://hashheroes.xyz/",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": (
                '"Chromium";v="130", "Microsoft Edge";v="130", '
                '"Not?A_Brand";v="99"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "DNT": "1",
        }

    def __init__(
        self,
        user_id: Optional[int] = None,
        wallet_address: Optional[str] = None,
        ref: Optional[str] = None,
        timeout: int = 30,
        jitter: tuple[float, float] = (0.6, 1.8),
        user_agent: Optional[str] = None,
        max_retries: int = 5,
        backoff: float = 3.0,
    ) -> None:
        self.user_id = user_id
        self.wallet_address = wallet_address
        self.ref = ref
        self.timeout = timeout
        self.jitter = jitter
        self.max_retries = max_retries
        self.default_backoff = backoff
        self._last_req_at: float = 0.0
        self.session = requests.Session()
        self.session.headers.update(self._build_headers(user_agent or _pick_ua()))

    # ---------------------------------------------------------------- core
    # Substrings inside an error envelope that indicate a transient backend
    # problem (worth retrying) rather than a real client-side rejection.
    _TRANSIENT_HINTS = (
        "database disk image is malformed",
        "database is locked",
        "timeout",
        "temporarily",
        "try again",
        "internal server error",
        "bad gateway",
        "gateway timeout",
    )

    def _pace(self) -> None:
        """Hold each request to a minimum jitter gap so traffic isn't bursty."""
        if self.jitter is None:
            return
        lo, hi = self.jitter
        gap = random.uniform(lo, hi)
        delta = time.monotonic() - self._last_req_at
        if self._last_req_at and delta < gap:
            time.sleep(gap - delta)
        self._last_req_at = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        retries: Optional[int] = None,
        backoff: Optional[float] = None,
    ) -> Any:
        if retries is None:
            retries = self.max_retries
        if backoff is None:
            backoff = self.default_backoff
        url = f"{self.BASE}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            retryable = False
            self._pace()
            try:
                resp = self.session.request(
                    method, url, json=json_body, timeout=self.timeout
                )
                # Retry on 5xx (gateway / origin hiccups). Fail fast on 4xx.
                if 500 <= resp.status_code < 600:
                    retryable = True
                    raise HashHeroesError(
                        f"{method} {path} -> {resp.status_code} (server)"
                    )
                if resp.status_code >= 400:
                    raise HashHeroesError(
                        f"{method} {path} -> {resp.status_code}: {resp.text[:300]}"
                    )
                if not resp.text:
                    return {}
                try:
                    data = resp.json()
                except ValueError as exc:
                    retryable = True
                    raise HashHeroesError(
                        f"{method} {path} -> non-JSON body: {resp.text[:200]}"
                    ) from exc
                # Some endpoints return HTTP 200 with `{ok:false, error:"..."}`
                # when the origin DB is busted. Detect that and retry transients.
                if isinstance(data, dict) and data.get("ok") is False:
                    msg = str(data.get("error") or data.get("message") or "unknown")
                    if any(h in msg.lower() for h in self._TRANSIENT_HINTS):
                        retryable = True
                    raise HashHeroesError(f"{method} {path} -> {msg}")
                return data
            except requests.RequestException as exc:
                retryable = True
                last_exc = exc
            except HashHeroesError as exc:
                last_exc = exc
                if not retryable:
                    raise
            if attempt == retries - 1:
                break
            # Exponential backoff with jitter; the upstream is famously flaky
            # (502 / DB malformed) so we wait LONG instead of hammering it.
            wait = backoff * (2 ** attempt) + random.uniform(0, backoff)
            wait = min(wait, 90.0)
            time.sleep(wait)
        assert last_exc is not None
        # Always re-raise as HashHeroesError so callers only have to catch one
        # exception type. Network errors (SSL handshake fail under load,
        # connect-reset, etc.) would otherwise leak as raw urllib3 exceptions.
        if isinstance(last_exc, requests.RequestException):
            raise HashHeroesError(
                f"{method} {path} -> network: {type(last_exc).__name__}: {last_exc}"
            ) from last_exc
        raise last_exc

    # ---------------------------------------------------------------- auth
    def wallet_login(self) -> dict:
        if not self.wallet_address:
            raise HashHeroesError("wallet_address is required for wallet_login")
        body: dict = {
            "wallet_address": self.wallet_address,
            "username": f"hero_{self.wallet_address[:6]}",
        }
        if self.ref:
            body["referred_by"] = self.ref
        data = self._request("POST", "/wallet-login", body)
        user = (data or {}).get("user") or {}
        if user.get("id"):
            self.user_id = int(user["id"])
        return data

    # ---------------------------------------------------------------- read
    def me(self) -> dict:
        return self._request("GET", f"/me/{self._uid()}")

    def cards(self) -> dict:
        return self._request("GET", f"/cards/{self._uid()}")

    def tasks(self) -> dict:
        return self._request("GET", f"/tasks/{self._uid()}")

    def leaderboard(self, kind: str = "hashpower") -> dict:
        return self._request("GET", f"/leaderboard/{kind}")

    # ---------------------------------------------------------------- write
    def open_packs(self, amount: int) -> dict:
        return self._request(
            "POST", "/packs/open", {"user_id": self._uid(), "amount": int(amount)}
        )

    def stake(self, card_id: int) -> dict:
        return self._request(
            "POST", "/cards/stake", {"user_id": self._uid(), "card_id": int(card_id)}
        )

    def unstake(self, card_id: int) -> dict:
        return self._request(
            "POST", "/cards/unstake", {"user_id": self._uid(), "card_id": int(card_id)}
        )

    def upgrade(self, card_id: int) -> dict:
        return self._request(
            "POST", "/cards/upgrade", {"user_id": self._uid(), "card_id": int(card_id)}
        )

    def dismantle(self, card_id: int) -> dict:
        return self._request(
            "POST",
            "/cards/dismantle",
            {"user_id": self._uid(), "card_id": int(card_id)},
        )

    def claim_mining(self) -> dict:
        return self._request("POST", "/mining/claim", {"user_id": self._uid()})

    def claim_referral_pack(self) -> dict:
        return self._request(
            "POST", "/referrals/claim-pack", {"user_id": self._uid()}
        )

    def complete_task(self, task_key: str) -> dict:
        return self._request(
            "POST",
            "/tasks/complete",
            {"user_id": self._uid(), "task_key": str(task_key)},
        )

    # ---------------------------------------------------------------- util
    def _uid(self) -> int:
        if not self.user_id:
            raise HashHeroesError(
                "user_id is not set; call wallet_login() or pass user_id explicitly"
            )
        return int(self.user_id)
