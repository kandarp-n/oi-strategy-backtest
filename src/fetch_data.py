"""Fetch 5-min intraday OHLC + OI + volume data for:
  - Spot equity (NSE_EQ EQUITY)
  - Front-month stock futures (NSE_FNO FUTSTK) — with OI

Dhan intraday API caps 90 days per call, so we do single-shot fetches.
Saves one parquet per (symbol, kind).
"""
from __future__ import annotations

import os
import time
import sys
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
from tqdm import tqdm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.dhan_client import intraday_history

DATA_RAW = os.path.join(ROOT, "data", "raw")
UNIVERSE_PATH = os.path.join(ROOT, "data", "universe.csv")

# Date range: 90 days back from today. Front-month futures contracts only
# expose data from their listing date (T-3M from expiry).
END = datetime(2026, 6, 26, 15, 30)
START = END - timedelta(days=88)
FROM_STR = START.strftime("%Y-%m-%d 09:15:00")
TO_STR = END.strftime("%Y-%m-%d %H:%M:%S")


def _to_df(raw: dict) -> pd.DataFrame:
    if not raw or not raw.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": pd.to_datetime(raw["timestamp"], unit="s"),
        "open": raw["open"],
        "high": raw["high"],
        "low": raw["low"],
        "close": raw["close"],
        "volume": raw["volume"],
    })
    if "open_interest" in raw and raw["open_interest"]:
        df["oi"] = raw["open_interest"]
    # Convert UTC epoch to IST (UTC+5:30)
    df["ts"] = df["ts"] + pd.Timedelta(hours=5, minutes=30)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def fetch_spot(sym: str, sec_id: int) -> pd.DataFrame:
    raw = intraday_history(
        security_id=sec_id,
        exchange_segment="NSE_EQ",
        instrument="EQUITY",
        interval="5",
        from_date=FROM_STR,
        to_date=TO_STR,
        oi=False,
    )
    return _to_df(raw)


def fetch_fut(sym: str, sec_id: int) -> pd.DataFrame:
    raw = intraday_history(
        security_id=sec_id,
        exchange_segment="NSE_FNO",
        instrument="FUTSTK",
        interval="5",
        from_date=FROM_STR,
        to_date=TO_STR,
        oi=True,
    )
    return _to_df(raw)


def main():
    os.makedirs(DATA_RAW, exist_ok=True)
    uni = pd.read_csv(UNIVERSE_PATH)
    # Pick front-month future per symbol (earliest expiry >= today)
    uni["fut_expiry"] = pd.to_datetime(uni["fut_expiry"])
    front = (
        uni.sort_values("fut_expiry")
           .groupby("symbol", as_index=False)
           .first()
    )
    print(f"Symbols: {len(front)}  Window: {FROM_STR} to {TO_STR}")

    failures = []
    for _, row in tqdm(front.iterrows(), total=len(front), desc="fetch"):
        sym = row["symbol"]
        spot_path = os.path.join(DATA_RAW, f"{sym}_spot.parquet")
        fut_path = os.path.join(DATA_RAW, f"{sym}_fut.parquet")

        try:
            if not os.path.exists(spot_path):
                df_spot = fetch_spot(sym, int(row["spot_security_id"]))
                if not df_spot.empty:
                    df_spot.to_parquet(spot_path, index=False)
                else:
                    failures.append((sym, "spot empty"))
            if not os.path.exists(fut_path):
                df_fut = fetch_fut(sym, int(row["fut_security_id"]))
                if not df_fut.empty:
                    df_fut.to_parquet(fut_path, index=False)
                else:
                    failures.append((sym, "fut empty"))
        except Exception as e:
            failures.append((sym, str(e)[:120]))

    print("\nDone.")
    print(f"failures: {len(failures)}")
    for f in failures:
        print(" ", f)


if __name__ == "__main__":
    main()
