"""ATM stock-options backtester.

Reuses the same OI-momentum signal computed on futures (`src/strategy.py`),
but executes each signal by **buying** the front-month ATM option:
  - Long signal  -> BUY ATM Call (CE)
  - Short signal -> BUY ATM Put (PE)

Position management:
  - SL/TGT triggers checked on the **spot** price (same 0.4%/0.8% rules as
    the futures backtest), but exit fill is at the option's price at exit
    bar's close.
  - Optional SL/TGT on the option *premium* itself (alternative mode).
  - Mandatory square-off by 15:15.

Front-month expiry roller: use earliest expiry with >= min_dte days. The
Dhan instrument master only contains currently-active expiries (June, July,
August 2026 at the time of the backtest), so the practical universe is:
  - Apr 1 .. (Jun expiry - min_dte)   -> use Jun 2026
  - thereafter                        -> use Jul 2026
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import dataclass, asdict, field
from datetime import time as dtime, date
from typing import Optional

import numpy as np
import pandas as pd
from tqdm import tqdm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.dhan_client import intraday_history
from src.option_costs import option_net_pnl
from src.strategy import Params, load_pair, add_features, generate_signals, _parse_hhmm

DATA_RAW = os.path.join(ROOT, "data", "raw")
DATA_OPT = os.path.join(ROOT, "data", "opt")
RESULTS = os.path.join(ROOT, "results")
MASTER_PATH = os.path.join(ROOT, "data", "scrip-master.csv")


@dataclass
class OptParams(Params):
    trade_segment: str = "OPT"
    min_dte: int = 14                    # min days-to-expiry; roll to next month otherwise
    use_premium_stops: bool = False      # if True, sl/tgt are on premium %, else on spot %
    premium_sl_pct: float = 0.30         # 30% stop on premium when use_premium_stops=True
    premium_tgt_pct: float = 0.60        # 60% target on premium
    fetch_only: bool = False             # if True, only pre-fetch data, no backtest


# ---------------- Option master & lookup ------------------------------------

class OptionUniverse:
    """Build (symbol, expiry, opt_type, strike) -> security_id index and strike steps."""

    def __init__(self, master_path: str):
        m = pd.read_csv(master_path, low_memory=False)
        opts = m[(m["EXCH_ID"] == "NSE") & (m["INSTRUMENT"] == "OPTSTK")].copy()
        opts["SM_EXPIRY_DATE"] = pd.to_datetime(opts["SM_EXPIRY_DATE"], errors="coerce")
        opts = opts.dropna(subset=["SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE",
                                    "UNDERLYING_SYMBOL", "SECURITY_ID", "LOT_SIZE"])
        opts["STRIKE_PRICE"] = opts["STRIKE_PRICE"].astype(float)
        opts["SECURITY_ID"] = opts["SECURITY_ID"].astype(int)
        opts["LOT_SIZE"] = opts["LOT_SIZE"].astype(int)
        self.df = opts
        # Index by (sym, expiry_date, opt_type)
        self.by_chain: dict = {}
        for (sym, exp, ot), grp in opts.groupby(["UNDERLYING_SYMBOL", "SM_EXPIRY_DATE", "OPTION_TYPE"]):
            grp_sorted = grp.sort_values("STRIKE_PRICE")
            self.by_chain[(sym, exp.date(), ot)] = grp_sorted[["STRIKE_PRICE", "SECURITY_ID", "LOT_SIZE"]].reset_index(drop=True)
        # Available expiries per symbol (sorted)
        self.expiries_by_sym: dict = (
            opts.groupby("UNDERLYING_SYMBOL")["SM_EXPIRY_DATE"]
                .apply(lambda s: sorted(set(s.dt.date.tolist())))
                .to_dict()
        )

    def pick_expiry(self, sym: str, on_date: date, min_dte: int) -> Optional[date]:
        exps = self.expiries_by_sym.get(sym, [])
        for e in exps:
            if (e - on_date).days >= min_dte:
                return e
        return exps[-1] if exps else None

    def atm_strike(self, sym: str, expiry: date, opt_type: str, spot: float) -> Optional[dict]:
        chain = self.by_chain.get((sym, expiry, opt_type))
        if chain is None or chain.empty or not np.isfinite(spot):
            return None
        idx = (chain["STRIKE_PRICE"] - spot).abs().idxmin()
        row = chain.loc[idx]
        return {"strike": float(row["STRIKE_PRICE"]),
                "security_id": int(row["SECURITY_ID"]),
                "lot_size": int(row["LOT_SIZE"])}


# ---------------- Plan the option fetches based on signals ------------------

def plan_option_fetches(sym: str, p: OptParams, ouni: OptionUniverse) -> pd.DataFrame:
    """Compute signals on this symbol's futures data and decide which option
    contracts (security_ids) need to be fetched. Returns a DataFrame with
    rows per signal: signal_ts, side, spot_at_signal, expiry, opt_type,
    strike, security_id, lot_size. Also includes ±N neighbor strikes per
    signal so the backtester can pick the best-available at execution time.
    """
    df = load_pair(sym)
    if df is None or df.empty:
        return pd.DataFrame()
    df = add_features(df, p)
    df["sig"] = generate_signals(df, p)
    sigs = df[df["sig"] != 0].copy()
    if sigs.empty:
        return pd.DataFrame()
    sigs["side"] = np.where(sigs["sig"] == 1, "long", "short")
    sigs["opt_type"] = np.where(sigs["sig"] == 1, "CE", "PE")

    NEIGHBORS = 3  # ATM ± 3 strikes each side
    rows = []
    for _, r in sigs.iterrows():
        d = r["ts"].date()
        exp = ouni.pick_expiry(sym, d, p.min_dte)
        if exp is None:
            continue
        spot = float(r["s_close"])
        chain = ouni.by_chain.get((sym, exp, r["opt_type"]))
        if chain is None or chain.empty:
            continue
        # nearest index
        idx = (chain["STRIKE_PRICE"] - spot).abs().idxmin()
        lo = max(0, idx - NEIGHBORS)
        hi = min(len(chain), idx + NEIGHBORS + 1)
        for k in range(lo, hi):
            row = chain.iloc[k]
            rows.append({
                "symbol": sym,
                "signal_ts": r["ts"],
                "side": r["side"],
                "opt_type": r["opt_type"],
                "spot": spot,
                "expiry": exp,
                "strike": float(row["STRIKE_PRICE"]),
                "security_id": int(row["SECURITY_ID"]),
                "lot_size": int(row["LOT_SIZE"]),
            })
    return pd.DataFrame(rows)


def prefetch_options(plans: pd.DataFrame, from_dt: str, to_dt: str) -> dict:
    """Fetch intraday 5-min OHLC for each unique option security_id.
    Returns dict: security_id -> DataFrame with ts,o,h,l,c,vol.
    """
    os.makedirs(DATA_OPT, exist_ok=True)
    unique = plans.drop_duplicates("security_id")[["security_id", "symbol", "expiry", "strike", "opt_type"]]
    cache: dict = {}
    failures = []
    for _, r in tqdm(unique.iterrows(), total=len(unique), desc="opt-fetch"):
        sid = int(r["security_id"])
        path = os.path.join(DATA_OPT, f"opt_{sid}.parquet")
        if os.path.exists(path):
            try:
                cache[sid] = pd.read_parquet(path)
                continue
            except Exception:
                pass
        try:
            raw = intraday_history(
                security_id=sid, exchange_segment="NSE_FNO",
                instrument="OPTSTK", interval="5",
                from_date=from_dt, to_date=to_dt, oi=False,
            )
        except Exception as e:
            failures.append((sid, str(e)[:120]))
            continue
        if not raw or not raw.get("timestamp"):
            cache[sid] = pd.DataFrame()
            continue
        d = pd.DataFrame({
            "ts": pd.to_datetime(raw["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30),
            "o_open": raw["open"], "o_high": raw["high"],
            "o_low": raw["low"], "o_close": raw["close"], "o_vol": raw["volume"],
        }).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        d.to_parquet(path, index=False)
        cache[sid] = d
    if failures:
        print(f"  fetch failures: {len(failures)}")
        for f in failures[:10]:
            print("   ", f)
    return cache


# ---------------- Backtest executor ----------------------------------------

@dataclass
class OptTrade:
    symbol: str
    side: str
    opt_type: str
    expiry: date
    strike: float
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    spot_entry: float
    spot_exit: float
    premium_entry: float
    premium_exit: float
    qty: int
    exit_reason: str
    gross_pnl: float
    cost: float
    net_pnl: float
    bars_held: int


def _pick_executable_strike(sym: str, expiry: date, opt_type: str,
                            spot: float, entry_ts: pd.Timestamp,
                            ouni: OptionUniverse, opt_cache: dict) -> Optional[dict]:
    """Return the strike (with valid o_open at entry_ts) closest to spot.
    Returns dict with keys: strike, security_id, lot_size, opt_row.
    """
    chain = ouni.by_chain.get((sym, expiry, opt_type))
    if chain is None or chain.empty:
        return None
    # iterate strikes from nearest to farthest from spot
    order = (chain["STRIKE_PRICE"] - spot).abs().sort_values().index
    for idx in order:
        row = chain.loc[idx]
        sid = int(row["SECURITY_ID"])
        opt_df = opt_cache.get(sid)
        if opt_df is None or opt_df.empty:
            continue
        hit = opt_df.loc[opt_df["ts"] == entry_ts]
        if hit.empty:
            continue
        opt_row = hit.iloc[0]
        if not np.isfinite(opt_row["o_open"]) or opt_row["o_open"] <= 0:
            continue
        return {
            "strike": float(row["STRIKE_PRICE"]),
            "security_id": sid,
            "lot_size": int(row["LOT_SIZE"]),
            "opt_df": opt_df,
            "opt_row": opt_row,
        }
    return None


def _option_at(opt_df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    if opt_df is None or opt_df.empty:
        return None
    hit = opt_df.loc[opt_df["ts"] == ts]
    if not hit.empty:
        return hit.iloc[0]
    return None


def backtest_symbol_options(sym: str, p: OptParams, ouni: OptionUniverse, opt_cache: dict) -> list[OptTrade]:
    df = load_pair(sym)
    if df is None or df.empty:
        return []
    df = add_features(df, p)
    df["sig"] = generate_signals(df, p)
    square_off = _parse_hhmm(p.square_off)
    trades: list[OptTrade] = []

    i, n = 0, len(df)
    last_exit_bar = -10**9
    day_pnl: dict = {}

    while i < n - 1:
        row = df.iloc[i]
        today = row["date"]
        if p.daily_loss_stop > 0 and day_pnl.get(today, 0) <= -p.daily_loss_stop:
            i += 1; continue
        if (
            row["sig"] != 0
            and i + 1 < n
            and df.iloc[i + 1]["date"] == row["date"]
            and (i - last_exit_bar) > p.cool_off_bars
        ):
            side = "long" if row["sig"] == 1 else "short"
            opt_type = "CE" if side == "long" else "PE"
            d = today
            exp = ouni.pick_expiry(sym, d, p.min_dte)
            if exp is None:
                i += 1; continue
            entry_bar = df.iloc[i + 1]
            entry_ts = entry_bar["ts"]
            picked = _pick_executable_strike(sym, exp, opt_type,
                                              float(row["s_close"]), entry_ts,
                                              ouni, opt_cache)
            if picked is None:
                i += 1; continue
            opt_df = picked["opt_df"]
            opt_entry_row = picked["opt_row"]

            premium_entry = float(opt_entry_row["o_open"])
            spot_entry_for_signal = float(entry_bar["s_open"])
            qty = picked["lot_size"] * max(1, p.fut_lots)

            # Spot triggers (long is bullish on spot, short is bearish on spot)
            if side == "long":
                spot_sl = spot_entry_for_signal * (1 - p.sl_pct)
                spot_tgt = spot_entry_for_signal * (1 + p.tgt_pct)
            else:
                spot_sl = spot_entry_for_signal * (1 + p.sl_pct)
                spot_tgt = spot_entry_for_signal * (1 - p.tgt_pct)

            # Premium triggers (if enabled)
            prem_sl = premium_entry * (1 - p.premium_sl_pct)
            prem_tgt = premium_entry * (1 + p.premium_tgt_pct)

            exit_reason = None
            exit_ts = entry_ts
            premium_exit = premium_entry
            spot_exit = spot_entry_for_signal
            bars_held = 0
            j = i + 1
            while j < n and df.iloc[j]["date"] == d:
                br = df.iloc[j]
                bars_held += 1
                # Get option price at this bar
                opt_row = _option_at(opt_df, br["ts"])
                if opt_row is None or not np.isfinite(opt_row["o_close"]):
                    # If option has no data for this bar, fall back to last known price (no exit)
                    if br["tod"] >= square_off:
                        # forced exit; use last known premium
                        premium_exit = premium_exit
                        spot_exit = float(br["s_close"])
                        exit_reason = "TIME"
                        exit_ts = br["ts"]; break
                    j += 1; continue

                # Spot-based triggers (default mode)
                if not p.use_premium_stops:
                    s_hi, s_lo = br["s_high"], br["s_low"]
                    if side == "long":
                        if s_lo <= spot_sl:
                            premium_exit = float(opt_row["o_close"])
                            spot_exit = spot_sl
                            exit_reason = "SL"; exit_ts = br["ts"]; break
                        if s_hi >= spot_tgt:
                            premium_exit = float(opt_row["o_close"])
                            spot_exit = spot_tgt
                            exit_reason = "TGT"; exit_ts = br["ts"]; break
                    else:
                        if s_hi >= spot_sl:
                            premium_exit = float(opt_row["o_close"])
                            spot_exit = spot_sl
                            exit_reason = "SL"; exit_ts = br["ts"]; break
                        if s_lo <= spot_tgt:
                            premium_exit = float(opt_row["o_close"])
                            spot_exit = spot_tgt
                            exit_reason = "TGT"; exit_ts = br["ts"]; break
                else:
                    # Premium-based triggers
                    o_hi, o_lo = float(opt_row["o_high"]), float(opt_row["o_low"])
                    if o_lo <= prem_sl:
                        premium_exit = prem_sl
                        spot_exit = float(br["s_close"])
                        exit_reason = "SL"; exit_ts = br["ts"]; break
                    if o_hi >= prem_tgt:
                        premium_exit = prem_tgt
                        spot_exit = float(br["s_close"])
                        exit_reason = "TGT"; exit_ts = br["ts"]; break

                # OI-flip on futures (signal substrate) — optional
                if p.use_oi_flip_exit and j > i + 1:
                    if side == "long" and br["f_oi_chg"] < -p.oi_pct and br["f_price_chg"] < 0:
                        premium_exit = float(opt_row["o_close"])
                        spot_exit = float(br["s_close"])
                        exit_reason = "OI_FLIP"; exit_ts = br["ts"]; break
                    if side == "short" and br["f_oi_chg"] < -p.oi_pct and br["f_price_chg"] > 0:
                        premium_exit = float(opt_row["o_close"])
                        spot_exit = float(br["s_close"])
                        exit_reason = "OI_FLIP"; exit_ts = br["ts"]; break

                # Square-off
                if br["tod"] >= square_off:
                    premium_exit = float(opt_row["o_close"])
                    spot_exit = float(br["s_close"])
                    exit_reason = "TIME"; exit_ts = br["ts"]; break
                j += 1
            else:
                if bars_held > 0:
                    br = df.iloc[j - 1]
                    opt_row = _option_at(opt_df, br["ts"])
                    if opt_row is not None and np.isfinite(opt_row["o_close"]):
                        premium_exit = float(opt_row["o_close"])
                        spot_exit = float(br["s_close"])
                        exit_reason = "EOD"
                        exit_ts = br["ts"]

            if exit_reason is None:
                i += 1
                continue

            net, gross, cost = option_net_pnl(premium_entry, premium_exit, qty)
            trades.append(OptTrade(
                symbol=sym, side=side, opt_type=opt_type, expiry=exp,
                strike=picked["strike"],
                entry_ts=entry_ts, exit_ts=exit_ts,
                spot_entry=spot_entry_for_signal, spot_exit=spot_exit,
                premium_entry=premium_entry, premium_exit=premium_exit,
                qty=qty, exit_reason=exit_reason,
                gross_pnl=gross, cost=cost, net_pnl=net, bars_held=bars_held,
            ))
            day_pnl[today] = day_pnl.get(today, 0) + net
            last_exit_bar = j
            i = j + 1
        else:
            i += 1
    return trades


def summarize_opt(trades: list[OptTrade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    df = pd.DataFrame([asdict(t) for t in trades])
    total_net = df["net_pnl"].sum()
    wins = df[df["net_pnl"] > 0]; losses = df[df["net_pnl"] <= 0]
    daily = df.assign(d=df["exit_ts"].dt.date).groupby("d")["net_pnl"].sum()
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() else 0.0
    eq = daily.cumsum(); peak = eq.cummax(); dd = (eq - peak).min()
    return {
        "n_trades": int(len(df)),
        "n_ce": int((df["opt_type"] == "CE").sum()),
        "n_pe": int((df["opt_type"] == "PE").sum()),
        "win_rate": float((df["net_pnl"] > 0).mean()),
        "avg_net": float(df["net_pnl"].mean()),
        "avg_win": float(wins["net_pnl"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["net_pnl"].mean()) if len(losses) else 0.0,
        "avg_premium_entry": float(df["premium_entry"].mean()),
        "total_net_pnl": float(total_net),
        "total_gross": float(df["gross_pnl"].sum()),
        "total_cost": float(df["cost"].sum()),
        "trading_days": int(daily.shape[0]),
        "avg_trades_per_day": float(len(df) / max(1, daily.shape[0])),
        "sharpe_daily_annualized": float(sharpe),
        "max_drawdown_rs": float(dd) if pd.notna(dd) else 0.0,
        "exit_reason_counts": df["exit_reason"].value_counts().to_dict(),
        "per_symbol_net": df.groupby("symbol")["net_pnl"].sum().sort_values().to_dict(),
        "avg_bars_held": float(df["bars_held"].mean()),
    }


def run_all_options(p: OptParams, tag: str, from_dt: str, to_dt: str) -> dict:
    import glob
    ouni = OptionUniverse(MASTER_PATH)
    syms = sorted({os.path.basename(f).split("_")[0] for f in glob.glob(os.path.join(DATA_RAW, "*_spot.parquet"))})

    # 1) Plan: figure out which option contracts we need
    print("Planning option fetches based on signals...")
    plans = []
    for s in syms:
        p_df = plan_option_fetches(s, p, ouni)
        if not p_df.empty:
            plans.append(p_df)
    plan_df = pd.concat(plans, ignore_index=True) if plans else pd.DataFrame()
    print(f"  signals: {len(plan_df)}   unique options: {plan_df['security_id'].nunique() if not plan_df.empty else 0}")

    # 2) Fetch all unique options
    opt_cache = prefetch_options(plan_df, from_dt, to_dt) if not plan_df.empty else {}

    if p.fetch_only:
        return {"fetched": len(opt_cache)}

    # 3) Backtest
    all_trades: list[OptTrade] = []
    for s in syms:
        all_trades.extend(backtest_symbol_options(s, p, ouni, opt_cache))

    summary = summarize_opt(all_trades)
    summary["params"] = asdict(p)
    summary["tag"] = tag
    os.makedirs(RESULTS, exist_ok=True)
    pd.DataFrame([asdict(t) for t in all_trades]).to_csv(
        os.path.join(RESULTS, f"trades_{tag}.csv"), index=False)
    with open(os.path.join(RESULTS, f"summary_{tag}.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary


def _print_opt_summary(s: dict):
    if s.get("n_trades", 0) == 0:
        print("no trades"); return
    print(f"\n=== {s.get('tag','?')} ===")
    print(f"trades:{s['n_trades']}  CE:{s['n_ce']} PE:{s['n_pe']}  days:{s['trading_days']}  trades/day:{s['avg_trades_per_day']:.2f}")
    print(f"win rate:{s['win_rate']*100:.1f}%   avg net/trade:Rs {s['avg_net']:.2f}  avg premium entry:Rs {s['avg_premium_entry']:.2f}")
    print(f"  avg win Rs {s['avg_win']:.2f}    avg loss Rs {s['avg_loss']:.2f}   avg bars held {s['avg_bars_held']:.1f}")
    print(f"total net PnL: Rs {s['total_net_pnl']:,.0f}   gross: Rs {s['total_gross']:,.0f}   costs: Rs {s['total_cost']:,.0f}")
    print(f"sharpe (daily-ann): {s['sharpe_daily_annualized']:.2f}   MDD: Rs {s['max_drawdown_rs']:,.0f}")
    print(f"exit reasons: {s['exit_reason_counts']}")


if __name__ == "__main__":
    # Match the dates used in fetch_data.py
    FROM_STR = "2026-03-30 09:15:00"
    TO_STR = "2026-06-26 15:30:00"

    common = dict(trade_segment="OPT", require_trend_align=True,
                  avoid_lunch=True, use_oi_flip_exit=True)

    # opt_v1: replica of best futures params (v5_tgt_lower) but executed via options
    p = OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
                  sl_pct=0.004, tgt_pct=0.008,
                  use_premium_stops=False,
                  min_dte=14, fut_lots=1, **common)
    s = run_all_options(p, tag="opt_v1_spot_stops", from_dt=FROM_STR, to_dt=TO_STR)
    _print_opt_summary(s)
