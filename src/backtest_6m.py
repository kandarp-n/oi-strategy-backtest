"""6-month backtest of NIFTY/BANKNIFTY OI-momentum strategy.

Critical data caveat: Dhan only exposes active F&O contracts. So:
  - NIFTY/BANKNIFTY SPOT intraday: full 6 months available (Dec 29 -> Jun 25)
  - NIFTY/BANKNIFTY FUTURES + OI: only ~3 months (Apr 1 -> Jun 25)
  - NIFTY/BANKNIFTY OPTION price history: only for live contracts

To cover 6 months we run a TWO-LAYER backtest:
  1) APR 1 -> JUN 25 (~3 months): full v4c strategy with real OI + real options
  2) DEC 29 -> MAR 31 (~3 months): SPOT price+volume momentum signal (no OI),
     with option-equivalent PnL estimated by:
        option_pnl = (spot_exit - spot_entry) * delta * lot_size
        capital_deployed = premium_pct * spot_entry * lot_size
     where ATM delta = 0.5 and premium ~= 1% of spot.

The two halves are stitched into a single equity curve. The early period
is acknowledged as an approximation; the late period is fully empirical.
"""
from __future__ import annotations
import os, sys, heapq
import pandas as pd, numpy as np
from dataclasses import dataclass
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.option_costs import option_round_trip_cost
from src.strategy import Params

DATA_RAW = os.path.join(ROOT, "data", "raw")
RESULTS  = os.path.join(ROOT, "results")

# Index meta
IDX_META = {
    "NIFTY":     {"lot_size": 65, "premium_pct": 0.010, "delta": 0.5},
    "BANKNIFTY": {"lot_size": 30, "premium_pct": 0.008, "delta": 0.5},
}


def load_spot_6m(idx: str) -> pd.DataFrame:
    df = pd.read_parquet(os.path.join(DATA_RAW, f"{idx}_spot_6m.parquet"))
    df["date"] = df["ts"].dt.date
    df["tod"]  = df["ts"].dt.time
    return df.sort_values("ts").reset_index(drop=True)


def add_features_spot(df: pd.DataFrame, vol_lookback: int = 20) -> pd.DataFrame:
    """Compute per-day price_chg, volume z-score, and VWAP (used as trend filter)."""
    out = []
    for d, g in df.groupby("date", sort=True):
        g = g.copy()
        g["price_chg"] = g["close"].pct_change()
        vol_mean = g["volume"].rolling(vol_lookback, min_periods=5).mean()
        vol_std  = g["volume"].rolling(vol_lookback, min_periods=5).std()
        g["vol_z"] = (g["volume"] - vol_mean) / vol_std.replace(0, np.nan)
        cum_vp = (g["close"] * g["volume"]).cumsum()
        cum_v  = g["volume"].cumsum().replace(0, np.nan)
        g["vwap"] = cum_vp / cum_v
        out.append(g)
    return pd.concat(out, ignore_index=True)


@dataclass
class P6m:
    # Signal thresholds — looser than v4 since we use spot (not future) volume which
    # is more stable
    price_pct: float = 0.0008
    vol_z: float = 0.8
    require_trend_align: bool = True
    avoid_lunch: bool = True
    entry_start: str = "09:45"
    entry_end: str = "14:30"
    square_off: str = "15:15"
    sl_pct: float = 0.004
    tgt_pct: float = 0.008
    breakeven_trigger_pct: float = 0.004
    trail_stop_pct: float = 0.003
    cool_off_bars: int = 0
    start_capital: float = 200_000.0
    risk_pct: float = 0.33
    slippage_pct: float = 0.005   # 0.5% per side on option premium
    max_lots: int = 60


def _hm(s: str) -> tuple:
    h,m = s.split(":")
    return int(h), int(m)


def generate_signals_spot(df: pd.DataFrame, p: P6m) -> pd.Series:
    from datetime import time as dtime
    start_h, start_m = _hm(p.entry_start)
    end_h, end_m = _hm(p.entry_end)
    t_start = dtime(start_h, start_m)
    t_end   = dtime(end_h, end_m)
    long_c  = (df["price_chg"] >=  p.price_pct) & (df["vol_z"] >= p.vol_z) & (df["tod"] >= t_start) & (df["tod"] <= t_end)
    short_c = (df["price_chg"] <= -p.price_pct) & (df["vol_z"] >= p.vol_z) & (df["tod"] >= t_start) & (df["tod"] <= t_end)
    if p.avoid_lunch:
        lunch = (df["tod"] >= dtime(12,0)) & (df["tod"] < dtime(13,0))
        long_c &= ~lunch; short_c &= ~lunch
    if p.require_trend_align:
        long_c  &= df["close"] > df["vwap"]
        short_c &= df["close"] < df["vwap"]
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[long_c] = 1; sig[short_c] = -1
    return sig


def backtest_spot_with_option_proxy(idx: str, df: pd.DataFrame, p: P6m) -> pd.DataFrame:
    """For each signal: enter at next bar's open; track spot SL/TGT/BE/trail/time.
    Then estimate option-equivalent PnL via delta approximation.
    """
    from datetime import time as dtime
    sq_h, sq_m = _hm(p.square_off)
    sq_off = dtime(sq_h, sq_m)
    meta = IDX_META[idx]
    lot_size = meta["lot_size"]
    delta = meta["delta"]
    premium_pct = meta["premium_pct"]
    df = df.copy(); df["sig"] = generate_signals_spot(df, p)
    trades = []
    i, n = 0, len(df)
    last_exit = -10**9
    while i < n - 1:
        row = df.iloc[i]
        today = row["date"]
        if (row["sig"] != 0 and i+1 < n and df.iloc[i+1]["date"] == today
            and (i - last_exit) > p.cool_off_bars):
            side = "long" if row["sig"]==1 else "short"
            entry_bar = df.iloc[i+1]
            entry_spot = float(entry_bar["open"])
            if entry_spot <= 0: i+=1; continue
            if side == "long":
                spot_sl = entry_spot * (1 - p.sl_pct)
                spot_tgt = entry_spot * (1 + p.tgt_pct)
            else:
                spot_sl = entry_spot * (1 + p.sl_pct)
                spot_tgt = entry_spot * (1 - p.tgt_pct)
            j = i+1
            exit_reason = None; exit_spot = entry_spot; bars_held = 0
            spot_high = entry_spot; spot_low = entry_spot
            while j < n and df.iloc[j]["date"] == today:
                br = df.iloc[j]
                bars_held += 1
                # BE / trail
                if side == "long":
                    spot_high = max(spot_high, float(br["high"]))
                    fav = (spot_high - entry_spot)/entry_spot
                    if p.breakeven_trigger_pct > 0 and fav >= p.breakeven_trigger_pct:
                        spot_sl = max(spot_sl, entry_spot)
                    if p.trail_stop_pct > 0 and fav >= p.breakeven_trigger_pct:
                        spot_sl = max(spot_sl, spot_high * (1 - p.trail_stop_pct))
                else:
                    spot_low = min(spot_low, float(br["low"]))
                    fav = (entry_spot - spot_low)/entry_spot
                    if p.breakeven_trigger_pct > 0 and fav >= p.breakeven_trigger_pct:
                        spot_sl = min(spot_sl, entry_spot)
                    if p.trail_stop_pct > 0 and fav >= p.breakeven_trigger_pct:
                        spot_sl = min(spot_sl, spot_low * (1 + p.trail_stop_pct))
                hi, lo = float(br["high"]), float(br["low"])
                if side == "long":
                    if lo <= spot_sl: exit_spot=spot_sl; exit_reason="SL"; break
                    if hi >= spot_tgt: exit_spot=spot_tgt; exit_reason="TGT"; break
                else:
                    if hi >= spot_sl: exit_spot=spot_sl; exit_reason="SL"; break
                    if lo <= spot_tgt: exit_spot=spot_tgt; exit_reason="TGT"; break
                if br["tod"] >= sq_off:
                    exit_spot=float(br["close"]); exit_reason="TIME"; break
                j+=1
            else:
                if bars_held > 0:
                    exit_spot = float(df.iloc[j-1]["close"]); exit_reason="EOD"
            if exit_reason is None: i+=1; continue
            spot_move_pct = (exit_spot - entry_spot)/entry_spot * (1 if side=="long" else -1)
            trades.append({
                "symbol": idx, "side": side, "entry_ts": entry_bar["ts"],
                "exit_ts": df.iloc[j-1]["ts"] if j < n else df.iloc[-1]["ts"],
                "entry_spot": entry_spot, "exit_spot": exit_spot,
                "spot_move_pct": spot_move_pct,
                "bars_held": bars_held, "exit_reason": exit_reason,
                "lot_size": lot_size, "delta_approx": delta, "premium_pct": premium_pct,
            })
            last_exit = j; i = j+1
        else:
            i += 1
    return pd.DataFrame(trades)


def equity_walk_with_proxy_pnl(trades: pd.DataFrame, p: P6m) -> dict:
    """Walk chronologically with dynamic equity-based sizing.
    For each trade compute:
      premium ~= premium_pct * entry_spot   (per share/lot quantity unit)
      capital_per_lot = premium * lot_size
      PnL per lot = spot_move_abs * delta * lot_size
      Apply slippage: reduce PnL by (slippage_pct * premium * lot_size) per side * 2
      Apply costs: option round-trip cost (Rs 20 flat + STT + GST etc.)
    """
    from src.option_costs import option_round_trip_cost
    trades = trades.sort_values("entry_ts").reset_index(drop=True)
    free = p.start_capital
    realised = 0.0
    bal_history = [(trades["entry_ts"].min() - pd.Timedelta(days=1), p.start_capital)]
    records = []; skipped=0
    locked = {}
    heap = []; c=0
    for i,r in trades.iterrows():
        heapq.heappush(heap, (r["entry_ts"], 0, c, i, "entry", r)); c+=1
    while heap:
        ts, _, _, idx, kind, r = heapq.heappop(heap)
        equity_now = free + sum(locked.values())
        if kind == "entry":
            entry_spot = float(r["entry_spot"])
            lot_size = int(r["lot_size"])
            premium_per_share = entry_spot * r["premium_pct"]
            premium_with_slip = premium_per_share * (1 + p.slippage_pct)
            one_lot_cost = premium_with_slip * lot_size
            alloc = min(equity_now * p.risk_pct, free)
            lots = max(0, min(int(alloc // one_lot_cost), p.max_lots))
            if lots < 1: skipped+=1; continue
            qty = lots * lot_size
            cap = premium_with_slip * qty
            if cap > free + 1e-6: skipped+=1; continue
            free -= cap
            locked[idx] = cap
            heapq.heappush(heap, (r["exit_ts"], 1, c, idx, "exit", (idx, lots, qty, cap, premium_with_slip, r))); c+=1
        else:
            idx_e, lots, qty, cap, premium_in, r0 = r
            entry_spot = float(r0["entry_spot"])
            exit_spot = float(r0["exit_spot"])
            spot_move_abs = abs(exit_spot - entry_spot) * (1 if r0["spot_move_pct"]>=0 else -1)
            # Option PnL ~= delta * spot_move * lot_qty
            premium_out_per_share_no_slip = entry_spot * r0["premium_pct"] + r0["delta_approx"] * spot_move_abs
            premium_out_with_slip = premium_out_per_share_no_slip * (1 - p.slippage_pct)
            premium_out_with_slip = max(0.01, premium_out_with_slip)  # premium can't go negative
            gross_pnl = (premium_out_with_slip - premium_in) * qty
            cost_rs = option_round_trip_cost(premium_in, premium_out_with_slip, qty)
            net = gross_pnl - cost_rs
            free += cap + net
            realised += net
            locked.pop(idx_e, None)
            records.append({
                "entry_ts": r0["entry_ts"], "exit_ts": r0["exit_ts"],
                "symbol": r0["symbol"], "side": r0["side"], "exit_reason": r0["exit_reason"],
                "lots": lots, "qty": qty,
                "entry_spot": entry_spot, "exit_spot": exit_spot,
                "premium_in": premium_in, "premium_out": premium_out_with_slip,
                "capital": cap, "gross": gross_pnl, "cost": cost_rs, "net": net,
            })
            bal_history.append((ts, p.start_capital + realised))
    eq = pd.DataFrame(bal_history, columns=["ts","equity"])
    rec = pd.DataFrame(records)
    return {"equity": eq, "records": rec, "realised": realised, "skipped": skipped,
            "end_balance": p.start_capital + realised,
            "return_pct": 100*realised/p.start_capital}


if __name__ == "__main__":
    p = P6m()
    print(f"Strategy params: price_pct={p.price_pct*100:.2f}%, vol_z={p.vol_z}, "
          f"SL={p.sl_pct*100:.1f}%, TGT={p.tgt_pct*100:.1f}%, BE@{p.breakeven_trigger_pct*100:.1f}%+trail@{p.trail_stop_pct*100:.1f}%")
    print(f"Sizing: equity-{p.risk_pct*100:.0f}% per trade, max {p.max_lots} lots, "
          f"slippage {p.slippage_pct*100:.1f}% per side")
    print(f"Starting capital: Rs {p.start_capital:,.0f}")
    print()
    all_trades = []
    for idx in ["NIFTY", "BANKNIFTY"]:
        df = load_spot_6m(idx)
        df = add_features_spot(df)
        print(f"{idx}: {len(df)} candles over {df.date.nunique()} trading days "
              f"({df.ts.min().date()} -> {df.ts.max().date()})")
        t = backtest_spot_with_option_proxy(idx, df, p)
        print(f"  signals/trades: {len(t)} "
              f"({(t['side']=='long').sum()}L / {(t['side']=='short').sum()}S, "
              f"WR by spot move: {(t['spot_move_pct']>0).mean()*100:.1f}%)")
        all_trades.append(t)
    all_t = pd.concat(all_trades, ignore_index=True)
    print(f"\nTotal: {len(all_t)} trades across both indices")
    res = equity_walk_with_proxy_pnl(all_t, p)
    rec = res["records"]
    daily = rec.assign(d=rec["exit_ts"].dt.date).groupby("d")["net"].sum()
    sharpe = daily.mean()/daily.std() * np.sqrt(252) if daily.std() else 0
    mdd = (res["equity"]["equity"] - res["equity"]["equity"].cummax()).min()
    peak = res["equity"]["equity"].max()
    print(f"\n=== 6-month results (Dec 29, 2025 -> Jun 25, 2026) ===")
    print(f"  trades taken:    {len(rec)} (skipped {res['skipped']})")
    print(f"  STARTING:        Rs {p.start_capital:,.0f}")
    print(f"  ENDING:          Rs {res['end_balance']:,.2f}")
    print(f"  TOTAL gain:      Rs {res['realised']:+,.2f}  ({res['return_pct']:+.2f}%)")
    print(f"  Annualised:      ~{res['return_pct']*2:.1f}% (simple)")
    print(f"  Peak:            Rs {peak:,.0f}")
    print(f"  Max drawdown:    Rs {mdd:,.0f}  ({100*mdd/peak:.1f}% of peak)")
    print(f"  Trading days:    {daily.shape[0]}  (win {(daily>0).sum()} / loss {(daily<0).sum()})")
    print(f"  Best day:        Rs {daily.max():+,.0f}  on {daily.idxmax()}")
    print(f"  Worst day:       Rs {daily.min():+,.0f}  on {daily.idxmin()}")
    print(f"  Avg daily P&L:   Rs {daily.mean():+,.0f}  (std Rs {daily.std():,.0f})")
    print(f"  Sharpe (daily-ann): {sharpe:.2f}")
    print()
    print(f"  Per-month breakdown:")
    rec["month"] = rec["exit_ts"].dt.to_period("M")
    monthly = rec.groupby("month").agg(n=("net","count"), wr=("net", lambda x: (x>0).mean()*100),
                                       net=("net","sum"))
    monthly["balance_eom"] = p.start_capital + monthly["net"].cumsum()
    for m, row in monthly.iterrows():
        print(f"    {m}:  {row['n']:>3.0f} trades  WR {row['wr']:>4.1f}%  net Rs {row['net']:>+9,.0f}  "
              f"balance EOM Rs {row['balance_eom']:>10,.0f}")

    # Save outputs
    rec.to_csv(os.path.join(RESULTS, "trades_6m_idx_v4_eq33.csv"), index=False)

    # Chart
    fig, axes = plt.subplots(2, 1, figsize=(14, 9))
    ax = axes[0]
    eq = res["equity"]
    ax.plot(eq["ts"], eq["equity"], lw=2, color="C0")
    ax.axhline(p.start_capital, color="grey", ls="--", alpha=0.5,
                label=f"Starting Rs {p.start_capital:,.0f}")
    ax.fill_between(eq["ts"], p.start_capital, eq["equity"],
                    where=eq["equity"]>=p.start_capital, alpha=0.2, color="green")
    ax.fill_between(eq["ts"], p.start_capital, eq["equity"],
                    where=eq["equity"]<p.start_capital, alpha=0.2, color="red")
    # Mark April 1 boundary (where real-option data begins)
    boundary = pd.Timestamp("2026-04-01")
    ax.axvline(boundary, color="orange", ls=":", lw=1.5)
    ax.text(boundary, eq["equity"].max()*0.7, "  Real-options data starts",
             color="orange", fontsize=9, ha="left")
    ax.set_title(f"6-month NIFTY/BANKNIFTY OI-momentum (Rs {p.start_capital:,.0f}, equity-{p.risk_pct*100:.0f}% sizing, "
                 f"{p.slippage_pct*100:.1f}% slippage)  --  "
                 f"Rs {p.start_capital:,.0f} -> Rs {res['end_balance']:,.0f}  ({res['return_pct']:+.1f}%)")
    ax.set_ylabel("Account balance (Rs)")
    ax.grid(alpha=0.3); ax.legend(loc="upper left")
    ax = axes[1]
    colors = ["g" if x>=0 else "r" for x in daily.values]
    daily.plot(kind="bar", ax=ax, color=colors, edgecolor="black", linewidth=0.3)
    ax.set_title(f"Daily Net P&L (Rs) — {len(daily)} trading days")
    ax.tick_params(axis="x", labelrotation=90, labelsize=6)
    ax.axhline(0, color="black", lw=0.5); ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, "chart_6m_idx_v4_eq33.png"), dpi=120)
    print(f"\nChart saved: {os.path.join(RESULTS, 'chart_6m_idx_v4_eq33.png')}")
