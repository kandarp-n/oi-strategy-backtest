"""Real-time signal engine and bar fetching.

Every poll cycle: fetch the latest closed 5-min bars for spot + futures,
compute indicators, check signal conditions. If any underlying triggers,
return the signal payload.
"""
from __future__ import annotations

import os, sys, logging
import pandas as pd, numpy as np
from datetime import datetime, timedelta, time as dtime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.dhan_client import intraday_history
from live import config

log = logging.getLogger("sig")


def fetch_bars(security_id: int | str, exchange: str, instrument: str,
                from_ts: datetime, to_ts: datetime, oi: bool = False) -> pd.DataFrame:
    """Fetch 5-min bars between [from_ts, to_ts] in IST."""
    raw = intraday_history(
        security_id=security_id,
        exchange_segment=exchange,
        instrument=instrument,
        interval="5",
        from_date=from_ts.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=to_ts.strftime("%Y-%m-%d %H:%M:%S"),
        oi=oi,
    )
    if not raw or not raw.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": pd.to_datetime(raw["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30),
        "open": raw["open"], "high": raw["high"], "low": raw["low"],
        "close": raw["close"], "volume": raw["volume"],
    })
    if "open_interest" in raw and raw["open_interest"]:
        df["oi"] = raw["open_interest"]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def compute_signal_for(u: config.Underlying, p: config.StrategyParams) -> Optional[dict]:
    """Fetch today's bars for this underlying's spot+future, check the latest closed
    bar for a signal. Returns a signal dict (with 'side', 'opt_type', 'spot_close')
    or None.
    """
    now = datetime.now()
    today = now.date()
    from_ts = datetime.combine(today, dtime(9, 15))
    # to_ts must be at least 1 second past last closed bar to ensure it's in API response
    to_ts = now

    spot = fetch_bars(u.spot_security_id, u.spot_exchange, u.spot_instrument,
                       from_ts, to_ts, oi=False)
    fut = fetch_bars(u.front_fut_security_id, u.fut_exchange, u.fut_instrument,
                     from_ts, to_ts, oi=True)
    if spot.empty or fut.empty:
        log.warning(f"{u.name}: spot or fut data empty")
        return None
    spot = spot.rename(columns={"open":"s_open","high":"s_high","low":"s_low",
                                   "close":"s_close","volume":"s_vol"})
    fut = fut.rename(columns={"open":"f_open","high":"f_high","low":"f_low",
                                "close":"f_close","volume":"f_vol"})
    df = pd.merge(spot, fut, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
    if len(df) < 6:
        log.info(f"{u.name}: only {len(df)} bars so far today")
        return None

    df["tod"] = df["ts"].dt.time
    # Indicators
    df["f_price_chg"] = df["f_close"].pct_change()
    df["f_oi_chg"] = df["oi"].pct_change()
    vol_mean = df["f_vol"].rolling(20, min_periods=5).mean()
    vol_std  = df["f_vol"].rolling(20, min_periods=5).std()
    df["f_vol_z"] = (df["f_vol"] - vol_mean) / vol_std.replace(0, np.nan)
    cum_vp = (df["s_close"] * df["s_vol"]).cumsum()
    cum_v  = df["s_vol"].cumsum().replace(0, np.nan)
    df["s_vwap"] = cum_vp / cum_v

    # Inspect the LAST closed bar
    last = df.iloc[-1]
    last_ts = last["ts"]
    log.debug(f"{u.name} last bar {last_ts} f_close={last['f_close']:.2f} "
              f"f_dpct={last['f_price_chg']*100:.3f}% f_doi={last['f_oi_chg']*100:.3f}% "
              f"f_volz={last['f_vol_z']:.2f}")

    # Apply trading window & filters
    t_start_h, t_start_m = map(int, p.entry_start.split(":"))
    t_end_h, t_end_m = map(int, p.entry_end.split(":"))
    if last["tod"] < dtime(t_start_h, t_start_m) or last["tod"] > dtime(t_end_h, t_end_m):
        return None
    if p.avoid_lunch and dtime(12,0) <= last["tod"] < dtime(13,0):
        return None
    # Check signal conditions
    is_long  = (last["f_price_chg"] >=  p.price_pct
                and last["f_oi_chg"] >= p.oi_pct
                and last["f_vol_z"] >= p.vol_z)
    is_short = (last["f_price_chg"] <= -p.price_pct
                and last["f_oi_chg"] >= p.oi_pct
                and last["f_vol_z"] >= p.vol_z)
    if p.require_trend_align:
        if is_long and not (last["s_close"] > last["s_vwap"]):
            is_long = False
        if is_short and not (last["s_close"] < last["s_vwap"]):
            is_short = False
    if not (is_long or is_short):
        return None

    return {
        "underlying": u.name,
        "side": "long" if is_long else "short",
        "opt_type": "CE" if is_long else "PE",
        "bar_ts": last_ts.isoformat(),
        "spot_close": float(last["s_close"]),
        "futures_close": float(last["f_close"]),
        "price_chg_pct": float(last["f_price_chg"]),
        "oi_chg_pct": float(last["f_oi_chg"]),
        "vol_z": float(last["f_vol_z"]),
    }


def check_oi_flip(u: config.Underlying, p: config.StrategyParams, side: str) -> bool:
    """For exit logic: check if OI is reversing direction with adverse price."""
    now = datetime.now()
    today = now.date()
    from_ts = datetime.combine(today, dtime(9, 15))
    fut = fetch_bars(u.front_fut_security_id, u.fut_exchange, u.fut_instrument,
                     from_ts, now, oi=True)
    if fut.empty or len(fut) < 3:
        return False
    last = fut.iloc[-1]
    prev = fut.iloc[-2]
    f_price_chg = (last["close"] - prev["close"]) / prev["close"]
    f_oi_chg = (last["oi"] - prev["oi"]) / prev["oi"] if prev["oi"] else 0
    if side == "long":
        return f_oi_chg < -p.oi_pct and f_price_chg < 0
    else:
        return f_oi_chg < -p.oi_pct and f_price_chg > 0
