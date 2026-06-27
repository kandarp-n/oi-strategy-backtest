"""Extended Dhan REST client for live trading: orders, positions, funds, quotes.

Reuses the rate-limited HTTP layer from src/dhan_client.py and adds the
endpoints needed for live trading. The base client gives us:
  - access token and client id (from .env via JWT decode)
  - rate-limited POST helper
"""
from __future__ import annotations

import os, sys, time
from typing import Any
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.dhan_client import ACCESS_TOKEN, CLIENT_ID, BASE, _data_limiter, _RateLimiter, _headers

# Order APIs: 10 per second, 250 per minute, 1000 per hour, 7000 per day
order_limiter = _RateLimiter(max_per_sec=8)


def _request(method: str, path: str, json_body: dict | None = None,
              limiter: _RateLimiter | None = None, max_retries: int = 3) -> dict:
    url = f"{BASE}{path}"
    lim = limiter or order_limiter
    attempt = 0
    while True:
        lim.acquire()
        try:
            r = requests.request(method, url, headers=_headers(), json=json_body, timeout=20)
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
            try:
                err = r.json()
            except Exception:
                err = {"errorMessage": r.text}
            raise RuntimeError(f"Dhan API {method} {path} {r.status_code}: {err}")
        try:
            return r.json()
        except Exception:
            return {}


def place_order(transaction_type: str, exchange_segment: str, security_id: str | int,
                quantity: int, product_type: str = "INTRADAY",
                order_type: str = "MARKET", price: float = 0,
                trigger_price: float = 0, validity: str = "DAY",
                correlation_id: str | None = None) -> dict:
    """transaction_type: 'BUY' or 'SELL'.  order_type: MARKET/LIMIT/STOP_LOSS/STOP_LOSS_MARKET."""
    body = {
        "dhanClientId": CLIENT_ID,
        "correlationId": correlation_id or "",
        "transactionType": transaction_type,
        "exchangeSegment": exchange_segment,
        "productType": product_type,
        "orderType": order_type,
        "validity": validity,
        "securityId": str(security_id),
        "quantity": int(quantity),
        "price": float(price) if order_type == "LIMIT" else 0,
        "triggerPrice": float(trigger_price) if order_type in ("STOP_LOSS","STOP_LOSS_MARKET") else 0,
        "afterMarketOrder": False,
        "amoTime": "",
        "boProfitValue": "",
        "boStopLossValue": "",
        "disclosedQuantity": 0,
    }
    return _request("POST", "/orders", body)


def cancel_order(order_id: str) -> dict:
    return _request("DELETE", f"/orders/{order_id}")


def get_order(order_id: str) -> dict:
    return _request("GET", f"/orders/{order_id}")


def list_orders() -> list[dict]:
    out = _request("GET", "/orders")
    return out if isinstance(out, list) else []


def get_positions() -> list[dict]:
    out = _request("GET", "/positions")
    return out if isinstance(out, list) else []


def get_holdings() -> list[dict]:
    out = _request("GET", "/holdings")
    return out if isinstance(out, list) else []


def get_funds() -> dict:
    """Returns the user's fund details. Important for emergency-stop checks."""
    return _request("GET", "/fundlimit")


def get_ltp_quote(security_ids_by_segment: dict[str, list[str]]) -> dict:
    """Quote API: real-time LTP for instruments.
    `security_ids_by_segment`: {"NSE_FNO": ["62329","62326"], "IDX_I": ["13","25"]}.
    Rate limit: 1/sec.  Use sparingly.
    """
    body = security_ids_by_segment
    quote_lim = _RateLimiter(max_per_sec=1)
    return _request("POST", "/marketfeed/ltp", body, limiter=quote_lim)


if __name__ == "__main__":
    print("Smoke test: getting funds...")
    try:
        f = get_funds()
        print("Funds:", f)
    except Exception as e:
        print("ERR:", e)

    print("\nSmoke test: LTP quote for NIFTY/BANKNIFTY spot...")
    try:
        q = get_ltp_quote({"IDX_I": ["13", "25"]})
        print("LTP:", q)
    except Exception as e:
        print("ERR:", e)
