"""OI-momentum intraday strategy + vectorized-ish backtester.

Inputs (per symbol):
  data/raw/{sym}_spot.parquet  (5-min OHLCV on equity spot)
  data/raw/{sym}_fut.parquet   (5-min OHLCV+OI on stock futures)

Signal generation: on the FUTURES bar, detect OI buildup with price/volume
confirmation. Trade the SPOT EQUITY intraday on next bar's open.

Entry rules (LONG buildup):
  - 5-min price change of future >= +PRICE_PCT
  - 5-min OI change of future >= +OI_PCT (fresh longs adding)
  - 5-min volume z-score (vs 20-bar mean/std) >= VOL_Z
  - Time-of-day within [ENTRY_START, ENTRY_END]
  - No existing open position in this symbol

SHORT buildup (mirror): price down, OI up, volume z high.

Exit rules:
  - SL hit (intra-bar low/high vs entry)
  - Target hit
  - Time exit at SQUARE_OFF
  - "OI flip" exit: if open position long and OI starts dropping while
    price stalls/falls (added in later iterations)

Position sizing: fixed notional per trade (NOTIONAL_PER_TRADE).
"""
from __future__ import annotations

import os
import sys
import json
import argparse
from dataclasses import dataclass, asdict, field
from datetime import time as dtime
from typing import Optional, List

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.costs import net_pnl

DATA_RAW = os.path.join(ROOT, "data", "raw")
RESULTS = os.path.join(ROOT, "results")


# ---------- Strategy parameters ---------------------------------------------

@dataclass
class Params:
    price_pct: float = 0.0015      # >=0.15% 5-min move on futures
    oi_pct: float = 0.002          # >=0.20% 5-min OI delta
    vol_z: float = 1.5             # 5-min volume z-score
    sl_pct: float = 0.005          # 0.5% stop
    tgt_pct: float = 0.010         # 1.0% target
    entry_start: str = "09:45"     # avoid first 30 min noise
    entry_end: str = "14:30"       # avoid last hour reversal
    square_off: str = "15:15"      # MIS auto-squareoff cutoff
    notional: float = 100000.0     # used only when trade_segment='EQ'
    vol_lookback: int = 20         # bars for vol mean/std
    max_open_per_symbol: int = 1
    use_oi_flip_exit: bool = False
    require_trend_align: bool = False
    avoid_lunch: bool = False
    cool_off_bars: int = 0
    invert: bool = False
    multi_bar_confirm: int = 1
    min_price: float = 0.0
    use_atr_stops: bool = False
    atr_sl_mult: float = 1.0
    atr_tgt_mult: float = 2.0
    atr_window: int = 14
    # Execution segment: 'EQ' trades spot equity (notional-based qty),
    # 'FUT' trades stock futures (qty = lot_size * lots).
    trade_segment: str = "EQ"
    fut_lots: int = 1
    # Risk controls
    daily_loss_stop: float = 0.0          # if cumulative day PnL <= -this Rs, stop trading that day
    daily_profit_stop: float = 0.0        # 0 = disabled
    max_trades_per_day_per_symbol: int = 999


@dataclass
class Trade:
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    exit_reason: str
    gross_pnl: float
    cost: float
    net_pnl: float
    bars_held: int


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def load_pair(sym: str) -> Optional[pd.DataFrame]:
    spot_p = os.path.join(DATA_RAW, f"{sym}_spot.parquet")
    fut_p = os.path.join(DATA_RAW, f"{sym}_fut.parquet")
    if not (os.path.exists(spot_p) and os.path.exists(fut_p)):
        return None
    spot = pd.read_parquet(spot_p).rename(columns={
        "open": "s_open", "high": "s_high", "low": "s_low",
        "close": "s_close", "volume": "s_vol",
    })
    fut = pd.read_parquet(fut_p).rename(columns={
        "open": "f_open", "high": "f_high", "low": "f_low",
        "close": "f_close", "volume": "f_vol",
    })
    df = pd.merge(spot, fut, on="ts", how="inner")
    df = df.sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    df["tod"] = df["ts"].dt.time
    return df


def add_features(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    # Daily-reset features: compute per group then re-assemble.
    df = df.sort_values("ts").reset_index(drop=True).copy()
    parts = []
    for _, grp in df.groupby("date", sort=True):
        grp = grp.copy()
        grp["f_price_chg"] = grp["f_close"].pct_change()
        grp["f_oi_chg"] = grp["oi"].pct_change()
        vol_mean = grp["f_vol"].rolling(p.vol_lookback, min_periods=5).mean()
        vol_std = grp["f_vol"].rolling(p.vol_lookback, min_periods=5).std()
        grp["f_vol_z"] = (grp["f_vol"] - vol_mean) / vol_std.replace(0, np.nan)
        cum_vp = (grp["s_close"] * grp["s_vol"]).cumsum()
        cum_v = grp["s_vol"].cumsum().replace(0, np.nan)
        grp["s_vwap"] = cum_vp / cum_v
        # Spot ATR for dynamic stops (in pct)
        tr = pd.concat([
            (grp["s_high"] - grp["s_low"]).abs(),
            (grp["s_high"] - grp["s_close"].shift()).abs(),
            (grp["s_low"] - grp["s_close"].shift()).abs(),
        ], axis=1).max(axis=1)
        grp["s_atr"] = tr.rolling(p.atr_window, min_periods=5).mean()
        grp["s_atr_pct"] = grp["s_atr"] / grp["s_close"]
        parts.append(grp)
    out = pd.concat(parts, ignore_index=True)
    return out


def generate_signals(df: pd.DataFrame, p: Params) -> pd.Series:
    """Return 0/+1/-1 signal column."""
    t_start = _parse_hhmm(p.entry_start)
    t_end = _parse_hhmm(p.entry_end)

    long_cond = (
        (df["f_price_chg"] >= p.price_pct)
        & (df["f_oi_chg"] >= p.oi_pct)
        & (df["f_vol_z"] >= p.vol_z)
        & (df["tod"] >= t_start)
        & (df["tod"] <= t_end)
    )
    short_cond = (
        (df["f_price_chg"] <= -p.price_pct)
        & (df["f_oi_chg"] >= p.oi_pct)
        & (df["f_vol_z"] >= p.vol_z)
        & (df["tod"] >= t_start)
        & (df["tod"] <= t_end)
    )
    if p.avoid_lunch:
        lunch = (df["tod"] >= dtime(12, 0)) & (df["tod"] < dtime(13, 0))
        long_cond &= ~lunch
        short_cond &= ~lunch
    if p.require_trend_align:
        long_cond &= df["s_close"] > df["s_vwap"]
        short_cond &= df["s_close"] < df["s_vwap"]
    if p.multi_bar_confirm > 1:
        # require last N futures close changes to be same sign as signal
        # (i.e., consecutive up bars for long, down bars for short)
        ups = (df["f_close"].diff() > 0)
        downs = (df["f_close"].diff() < 0)
        for k in range(1, p.multi_bar_confirm):
            long_cond &= ups.shift(k).fillna(False).astype(bool)
            short_cond &= downs.shift(k).fillna(False).astype(bool)
    if p.min_price > 0:
        long_cond &= df["s_close"] >= p.min_price
        short_cond &= df["s_close"] >= p.min_price
    sig = pd.Series(0, index=df.index, dtype=int)
    if p.invert:
        # FADE the buildup: short the long-buildup, long the short-buildup
        sig[long_cond] = -1
        sig[short_cond] = 1
    else:
        sig[long_cond] = 1
        sig[short_cond] = -1
    return sig


def _load_lot_sizes() -> dict:
    uni = pd.read_csv(os.path.join(ROOT, "data", "universe.csv"))
    uni["fut_expiry"] = pd.to_datetime(uni["fut_expiry"])
    front = uni.sort_values("fut_expiry").groupby("symbol", as_index=False).first()
    return dict(zip(front["symbol"], front["lot_size"]))


_LOT_SIZES = None


def lot_size(sym: str) -> int:
    global _LOT_SIZES
    if _LOT_SIZES is None:
        _LOT_SIZES = _load_lot_sizes()
    return int(_LOT_SIZES.get(sym, 0))


def backtest_symbol(sym: str, p: Params) -> List[Trade]:
    df = load_pair(sym)
    if df is None or df.empty:
        return []
    df = add_features(df, p)
    df["sig"] = generate_signals(df, p)
    square_off = _parse_hhmm(p.square_off)

    # Choose execution column prefix and segment-specific cost
    if p.trade_segment == "FUT":
        op_c, hi_c, lo_c, cl_c = "f_open", "f_high", "f_low", "f_close"
        seg = "FUT"
        ls = lot_size(sym)
        if ls <= 0:
            return []
        qty_fixed = ls * max(1, p.fut_lots)
    else:
        op_c, hi_c, lo_c, cl_c = "s_open", "s_high", "s_low", "s_close"
        seg = "EQ"
        qty_fixed = None  # computed from notional per trade

    trades: list[Trade] = []
    i = 0
    n = len(df)
    last_exit_bar = -10**9
    day_pnl: dict = {}
    day_trade_count: dict = {}
    while i < n - 1:
        row = df.iloc[i]
        today = row["date"]
        # Daily risk controls
        if p.daily_loss_stop > 0 and day_pnl.get(today, 0) <= -p.daily_loss_stop:
            i += 1; continue
        if p.daily_profit_stop > 0 and day_pnl.get(today, 0) >= p.daily_profit_stop:
            i += 1; continue
        if day_trade_count.get(today, 0) >= p.max_trades_per_day_per_symbol:
            i += 1; continue
        if (
            row["sig"] != 0
            and i + 1 < n
            and df.iloc[i + 1]["date"] == row["date"]
            and (i - last_exit_bar) > p.cool_off_bars
        ):
            side = "long" if row["sig"] == 1 else "short"
            entry_row = df.iloc[i + 1]
            entry_price = float(entry_row[op_c])
            if entry_price <= 0 or pd.isna(entry_price):
                i += 1; continue
            qty = qty_fixed if qty_fixed is not None else max(1, int(p.notional / entry_price))
            if p.use_atr_stops and pd.notna(entry_row.get("s_atr_pct", np.nan)):
                atrp = float(entry_row["s_atr_pct"])
                sl_pct_eff = max(0.001, atrp * p.atr_sl_mult)
                tgt_pct_eff = max(0.001, atrp * p.atr_tgt_mult)
            else:
                sl_pct_eff, tgt_pct_eff = p.sl_pct, p.tgt_pct
            if side == "long":
                sl = entry_price * (1 - sl_pct_eff)
                tgt = entry_price * (1 + tgt_pct_eff)
            else:
                sl = entry_price * (1 + sl_pct_eff)
                tgt = entry_price * (1 - tgt_pct_eff)

            exit_price = None
            exit_reason = None
            exit_ts = entry_row["ts"]
            bars_held = 0
            j = i + 1
            while j < n and df.iloc[j]["date"] == entry_row["date"]:
                br = df.iloc[j]
                bars_held += 1
                hi = br[hi_c]; lo = br[lo_c]
                if side == "long":
                    if lo <= sl:
                        exit_price = sl; exit_reason = "SL"; exit_ts = br["ts"]; break
                    if hi >= tgt:
                        exit_price = tgt; exit_reason = "TGT"; exit_ts = br["ts"]; break
                else:
                    if hi >= sl:
                        exit_price = sl; exit_reason = "SL"; exit_ts = br["ts"]; break
                    if lo <= tgt:
                        exit_price = tgt; exit_reason = "TGT"; exit_ts = br["ts"]; break
                if p.use_oi_flip_exit and j > i + 1:
                    if side == "long" and br["f_oi_chg"] < -p.oi_pct and br["f_price_chg"] < 0:
                        exit_price = float(br[cl_c]); exit_reason = "OI_FLIP"; exit_ts = br["ts"]; break
                    if side == "short" and br["f_oi_chg"] < -p.oi_pct and br["f_price_chg"] > 0:
                        exit_price = float(br[cl_c]); exit_reason = "OI_FLIP"; exit_ts = br["ts"]; break
                if br["tod"] >= square_off:
                    exit_price = float(br[cl_c]); exit_reason = "TIME"; exit_ts = br["ts"]; break
                j += 1
            else:
                if bars_held > 0:
                    br = df.iloc[j - 1]
                    exit_price = float(br[cl_c]); exit_reason = "EOD"; exit_ts = br["ts"]

            if exit_price is None:
                i += 1
                continue
            net, gross, cost = net_pnl(entry_price, exit_price, qty, side, segment=seg)
            trades.append(Trade(
                symbol=sym, side=side, entry_ts=entry_row["ts"], exit_ts=exit_ts,
                entry_price=entry_price, exit_price=exit_price, qty=qty,
                exit_reason=exit_reason, gross_pnl=gross, cost=cost,
                net_pnl=net, bars_held=bars_held,
            ))
            day_pnl[today] = day_pnl.get(today, 0) + net
            day_trade_count[today] = day_trade_count.get(today, 0) + 1
            last_exit_bar = j
            i = j + 1
        else:
            i += 1
    return trades


def summarize(trades: List[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    df = pd.DataFrame([asdict(t) for t in trades])
    total_net = df["net_pnl"].sum()
    wins = df[df["net_pnl"] > 0]; losses = df[df["net_pnl"] <= 0]
    daily = df.assign(d=df["exit_ts"].dt.date).groupby("d")["net_pnl"].sum()
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() else 0.0
    equity = daily.cumsum()
    peak = equity.cummax()
    dd = (equity - peak).min()
    summary = {
        "n_trades": int(len(df)),
        "n_long": int((df["side"] == "long").sum()),
        "n_short": int((df["side"] == "short").sum()),
        "win_rate": float((df["net_pnl"] > 0).mean()),
        "avg_net": float(df["net_pnl"].mean()),
        "avg_win": float(wins["net_pnl"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["net_pnl"].mean()) if len(losses) else 0.0,
        "expectancy_per_trade": float(df["net_pnl"].mean()),
        "total_net_pnl": float(total_net),
        "total_gross": float(df["gross_pnl"].sum()),
        "total_cost": float(df["cost"].sum()),
        "cost_pct_of_gross": float(df["cost"].sum() / df["gross_pnl"].abs().sum()) if df["gross_pnl"].abs().sum() else None,
        "trading_days": int(daily.shape[0]),
        "avg_trades_per_day": float(len(df) / max(1, daily.shape[0])),
        "sharpe_daily_annualized": float(sharpe),
        "max_drawdown_rs": float(dd) if pd.notna(dd) else 0.0,
        "exit_reason_counts": df["exit_reason"].value_counts().to_dict(),
        "per_symbol_net": df.groupby("symbol")["net_pnl"].sum().sort_values().to_dict(),
    }
    return summary


def run_all(p: Params, tag: str = "v1") -> dict:
    import glob, re
    syms = sorted({os.path.basename(f).split("_")[0] for f in glob.glob(os.path.join(DATA_RAW, "*_spot.parquet"))})
    all_trades: List[Trade] = []
    for s in syms:
        all_trades.extend(backtest_symbol(s, p))
    summary = summarize(all_trades)
    summary["params"] = asdict(p)
    summary["tag"] = tag
    os.makedirs(RESULTS, exist_ok=True)
    pd.DataFrame([asdict(t) for t in all_trades]).to_csv(
        os.path.join(RESULTS, f"trades_{tag}.csv"), index=False)
    with open(os.path.join(RESULTS, f"summary_{tag}.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary


def _print_summary(s: dict):
    print(f"\n=== {s.get('tag','?')} ===")
    print(f"trades: {s['n_trades']}  (L:{s.get('n_long')} S:{s.get('n_short')})  "
          f"days: {s.get('trading_days')}  trades/day: {s.get('avg_trades_per_day',0):.2f}")
    print(f"win rate: {s['win_rate']*100:.1f}%   avg net/trade: Rs {s['avg_net']:.2f}")
    print(f"  avg win: Rs {s['avg_win']:.2f}    avg loss: Rs {s['avg_loss']:.2f}")
    print(f"total net PnL: Rs {s['total_net_pnl']:,.0f}   gross: Rs {s['total_gross']:,.0f}   costs: Rs {s['total_cost']:,.0f}")
    print(f"sharpe (daily-annualized): {s['sharpe_daily_annualized']:.2f}")
    print(f"max drawdown: Rs {s['max_drawdown_rs']:,.0f}")
    print(f"exit reasons: {s['exit_reason_counts']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--price_pct", type=float)
    ap.add_argument("--oi_pct", type=float)
    ap.add_argument("--vol_z", type=float)
    ap.add_argument("--sl_pct", type=float)
    ap.add_argument("--tgt_pct", type=float)
    ap.add_argument("--oi_flip", action="store_true")
    ap.add_argument("--trend", action="store_true")
    ap.add_argument("--avoid_lunch", action="store_true")
    ap.add_argument("--cool_off_bars", type=int)
    args = ap.parse_args()
    p = Params()
    for k in ("price_pct","oi_pct","vol_z","sl_pct","tgt_pct","cool_off_bars"):
        v = getattr(args, k)
        if v is not None: setattr(p, k, v)
    if args.oi_flip: p.use_oi_flip_exit = True
    if args.trend: p.require_trend_align = True
    if args.avoid_lunch: p.avoid_lunch = True
    s = run_all(p, tag=args.tag)
    _print_summary(s)
