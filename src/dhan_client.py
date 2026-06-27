"""Thin Dhan HQ v2 REST client with rate limiting + retry."""
from __future__ import annotations

import os
import time
import threading
from collections import deque
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = "https://api.dhan.co/v2"
ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

# Client-id must be the dhanClientId encoded in the JWT, NOT the partner alias
# in the .env file. Decode the JWT once at import to recover it.
def _client_id_from_jwt(token: str) -> str:
    import base64, json
    try:
        payload_b64 = token.split(".")[1]
        # add padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return str(payload.get("dhanClientId", ""))
    except Exception:
        return ""

CLIENT_ID = _client_id_from_jwt(ACCESS_TOKEN or "") or os.getenv("DHAN_CLIENT_ID", "")


class _RateLimiter:
    """Sliding-window rate limiter (per-second cap)."""

    def __init__(self, max_per_sec: int):
        self.max_per_sec = max_per_sec
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > 1.0:
                self.calls.popleft()
            if len(self.calls) >= self.max_per_sec:
                sleep_for = 1.0 - (now - self.calls[0]) + 0.01
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 1.0:
                    self.calls.popleft()
            self.calls.append(now)


# Data APIs: 5/sec, 100k/day. Keep a small safety margin.
_data_limiter = _RateLimiter(max_per_sec=4)


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": ACCESS_TOKEN or "",
        "client-id": CLIENT_ID,
    }


def _post(path: str, payload: dict[str, Any], limiter: _RateLimiter, max_retries: int = 5) -> dict:
    url = f"{BASE}{path}"
    attempt = 0
    while True:
        limiter.acquire()
        try:
            r = requests.post(url, headers=_headers(), json=payload, timeout=30)
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise
            attempt += 1
            time.sleep(1.5 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt >= max_retries:
                r.raise_for_status()
            attempt += 1
            time.sleep(1.5 ** attempt)
            continue
        if not r.ok:
            # Surface API error body for easier debugging
            raise RuntimeError(f"Dhan API {path} {r.status_code}: {r.text}")
        return r.json()


def intraday_history(
    security_id: str | int,
    exchange_segment: str,
    instrument: str,
    interval: str,
    from_date: str,
    to_date: str,
    oi: bool = False,
) -> dict:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "interval": str(interval),
        "oi": oi,
        "fromDate": from_date,
        "toDate": to_date,
    }
    return _post("/charts/intraday", payload, _data_limiter)


def daily_history(
    security_id: str | int,
    exchange_segment: str,
    instrument: str,
    from_date: str,
    to_date: str,
    expiry_code: int = 0,
    oi: bool = False,
) -> dict:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "expiryCode": expiry_code,
        "oi": oi,
        "fromDate": from_date,
        "toDate": to_date,
    }
    return _post("/charts/historical", payload, _data_limiter)


if __name__ == "__main__":
    # Smoke test: fetch 5 days of RELIANCE 5-min spot data.
    import json as _json
    out = intraday_history(
        security_id="2885",  # RELIANCE NSE_EQ
        exchange_segment="NSE_EQ",
        instrument="EQUITY",
        interval="5",
        from_date="2025-06-16 09:15:00",
        to_date="2025-06-20 15:30:00",
    )
    keys = list(out.keys()) if isinstance(out, dict) else type(out)
    print("client_id:", CLIENT_ID)
    print("keys:", keys)
    if isinstance(out, dict) and "timestamp" in out:
        print("n candles:", len(out["timestamp"]))
        print("first close:", out["close"][0] if out["close"] else None)
