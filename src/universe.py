"""Download Dhan instrument master and select F&O stock universe.

We pick the most liquid F&O stocks by current-month futures lot value, then
resolve their NSE_EQ spot security IDs and NSE_FNO current-month futures
security IDs. Saved to data/universe.csv for the data fetch step to consume.
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
MASTER_PATH = os.path.join(DATA, "scrip-master.csv")


def download_master(force: bool = False) -> pd.DataFrame:
    if force or not os.path.exists(MASTER_PATH):
        print(f"Downloading scrip master from {MASTER_URL} ...")
        r = requests.get(MASTER_URL, timeout=120)
        r.raise_for_status()
        with open(MASTER_PATH, "wb") as f:
            f.write(r.content)
        print(f"  saved {len(r.content)/1e6:.1f} MB")
    df = pd.read_csv(MASTER_PATH, low_memory=False)
    return df


# A static curated list of liquid F&O stock symbols (NSE F&O bluechips,
# covering financials, energy, IT, autos, FMCG, metals, telecom, pharma).
LIQUID_FNO = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS", "AXISBANK",
    "KOTAKBANK", "BAJFINANCE", "BHARTIARTL", "ITC", "LT", "HINDUNILVR",
    "MARUTI", "M&M", "TATAMOTORS", "TATASTEEL", "HCLTECH", "WIPRO",
    "ADANIENT", "ADANIPORTS", "ASIANPAINT", "BAJAJFINSV", "BAJAJ-AUTO",
    "SUNPHARMA", "CIPLA", "DRREDDY", "JSWSTEEL", "HINDALCO", "COALINDIA",
    "NTPC", "POWERGRID", "ONGC", "GRASIM", "ULTRACEMCO", "TITAN",
    "NESTLEIND", "EICHERMOT", "DIVISLAB", "DLF", "TRENT", "TECHM",
    "INDUSINDBK", "PNB",
]


def select_universe(master: pd.DataFrame) -> pd.DataFrame:
    cols = master.columns.tolist()
    # Normalize key column names we'll use
    # Expected: EXCH_ID, SEGMENT, INSTRUMENT, UNDERLYING_SYMBOL, SYMBOL_NAME,
    # DISPLAY_NAME, SM_EXPIRY_DATE, SECURITY_ID
    # The detailed master uses these exact names per docs.
    if "SECURITY_ID" not in cols:
        raise SystemExit(f"Unexpected master columns: {cols[:20]}")

    # 1) Spot equities on NSE
    eq = master[
        (master["EXCH_ID"] == "NSE")
        & (master["SEGMENT"] == "E")
        & (master["INSTRUMENT"].isin(["EQUITY"]))
        & (master["SERIES"] == "EQ")
    ].copy()
    eq = eq[eq["UNDERLYING_SYMBOL"].isin(LIQUID_FNO) | eq["SYMBOL_NAME"].isin(LIQUID_FNO)]

    # Build spot mapping: symbol -> securityId
    spot_map: dict[str, int] = {}
    for _, row in eq.iterrows():
        sym = row.get("UNDERLYING_SYMBOL") or row.get("SYMBOL_NAME")
        if pd.notna(sym) and sym in LIQUID_FNO and sym not in spot_map:
            spot_map[sym] = int(row["SECURITY_ID"])

    # 2) Stock futures (FUTSTK) on NSE F&O
    fut = master[
        (master["EXCH_ID"] == "NSE")
        & (master["SEGMENT"] == "D")
        & (master["INSTRUMENT"] == "FUTSTK")
    ].copy()
    fut["SM_EXPIRY_DATE"] = pd.to_datetime(fut["SM_EXPIRY_DATE"], errors="coerce")
    fut = fut[fut["UNDERLYING_SYMBOL"].isin(LIQUID_FNO)]

    rows = []
    for sym in LIQUID_FNO:
        if sym not in spot_map:
            print(f"  WARN: spot not found for {sym}")
            continue
        f = fut[fut["UNDERLYING_SYMBOL"] == sym].sort_values("SM_EXPIRY_DATE")
        if f.empty:
            print(f"  WARN: futures not found for {sym}")
            continue
        # For data fetch we'll iterate over all monthly futures contracts
        # that expired in our 6-month window (so OI is the front-month each day).
        for _, frow in f.iterrows():
            rows.append({
                "symbol": sym,
                "spot_security_id": spot_map[sym],
                "fut_security_id": int(frow["SECURITY_ID"]),
                "fut_expiry": frow["SM_EXPIRY_DATE"].strftime("%Y-%m-%d") if pd.notna(frow["SM_EXPIRY_DATE"]) else "",
                "lot_size": int(frow["LOT_SIZE"]) if pd.notna(frow["LOT_SIZE"]) else 0,
            })
    out = pd.DataFrame(rows)
    return out


def main():
    os.makedirs(DATA, exist_ok=True)
    master = download_master()
    print("master rows:", len(master))
    print("master cols:", list(master.columns)[:25])
    uni = select_universe(master)
    uni.to_csv(os.path.join(DATA, "universe.csv"), index=False)
    print(f"universe contracts: {len(uni)}  unique symbols: {uni['symbol'].nunique()}")
    print(uni.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
