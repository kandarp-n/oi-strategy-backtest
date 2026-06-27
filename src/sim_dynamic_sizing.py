"""Dynamic-sizing simulator on v4c trade signals.

At each entry, lots = floor((free_cash * risk_pct) / (premium * lot_size)),
clipped to [min_lots, max_lots]. PnL and costs are recomputed for the new
lot count using the actual options cost model (since the Rs 20 flat
brokerage means cost is NOT a linear function of size).

The underlying trade signal list (entry/exit times, premiums, spot triggers)
is fixed; we only change *how many lots* we trade at each signal.
"""
from __future__ import annotations

import os
import sys
import heapq
import argparse
from dataclasses import dataclass

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.option_costs import option_net_pnl


@dataclass
class SizingParams:
    start_capital: float = 200_000.0
    risk_pct: float = 0.25          # fraction to allocate per trade
    cap_pct_of_equity: float = 1.00 # never deploy more than this fraction of current equity in one trade
    min_lots: int = 1
    max_lots: int = 20
    based_on: str = "free"          # "free" / "equity" / "slots"
    use_starting_capital_base: bool = False
    max_slots: int = 4              # used when based_on="slots": # concurrent positions cap
    slot_pct: float = 0.25          # used when based_on="slots": fraction of equity per slot


def simulate_dynamic(trades_csv: str, p: SizingParams) -> dict:
    trades = pd.read_csv(trades_csv, parse_dates=["entry_ts","exit_ts"]) \
                .sort_values("entry_ts").reset_index(drop=True)
    # Recover lot_size: qty in the CSV = lot_size * 3 (since v4c used fut_lots=3)
    trades["lot_size"] = (trades["qty"] / 3).round().astype(int)

    free = p.start_capital
    realised = 0.0
    bal_history = [(trades["entry_ts"].min() - pd.Timedelta(days=1), p.start_capital, p.start_capital)]
    records = []   # actual executed trades with re-sized qty/PnL/cost
    skipped_zero_lots = 0
    skipped_too_expensive = 0

    heap = []; counter = 0
    for i, r in trades.iterrows():
        heapq.heappush(heap, (r["entry_ts"], 0, counter, i, "entry", r)); counter += 1

    locked_per_trade: dict = {}

    while heap:
        ts, _, _, idx, kind, r = heapq.heappop(heap)
        equity_now = free + sum(locked_per_trade.values())
        if kind == "entry":
            lot_size = int(r["lot_size"])
            premium = float(r["premium_entry"])
            one_lot_cost = premium * lot_size
            if one_lot_cost <= 0:
                continue
            # decide allocation
            n_open = len(locked_per_trade)
            if p.based_on == "slots":
                if n_open >= p.max_slots:
                    skipped_zero_lots += 1
                    continue
                alloc = equity_now * p.slot_pct
            elif p.use_starting_capital_base:
                alloc = p.start_capital * p.risk_pct
            elif p.based_on == "equity":
                alloc = equity_now * p.risk_pct
            else:  # "free"
                alloc = free * p.risk_pct
            # cap by % of current equity (sanity)
            alloc = min(alloc, equity_now * p.cap_pct_of_equity)
            # cap by free cash (cannot use more than we have free)
            alloc = min(alloc, free)
            lots = int(alloc // one_lot_cost)
            lots = max(0, min(lots, p.max_lots))
            if lots < p.min_lots:
                skipped_zero_lots += 1
                continue
            qty = lots * lot_size
            capital_locked = premium * qty
            if capital_locked > free + 1e-6:
                skipped_too_expensive += 1
                continue
            free -= capital_locked
            locked_per_trade[idx] = capital_locked
            # store new sizing on the entry; will compute PnL at exit
            heap_payload = (idx, lots, qty, capital_locked, r)
            heapq.heappush(heap, (r["exit_ts"], 1, counter, idx, "exit", heap_payload)); counter += 1
        else:  # exit
            idx_e, lots, qty, capital_locked, r0 = r
            premium_in = float(r0["premium_entry"])
            premium_out = float(r0["premium_exit"])
            net_pnl, gross, cost = option_net_pnl(premium_in, premium_out, qty)
            free += capital_locked + net_pnl
            realised += net_pnl
            locked_per_trade.pop(idx_e, None)
            records.append({
                "entry_ts": r0["entry_ts"], "exit_ts": r0["exit_ts"],
                "symbol": r0["symbol"], "side": r0["side"], "opt_type": r0["opt_type"],
                "lots": lots, "qty": qty, "lot_size": int(r0["lot_size"]),
                "premium_entry": premium_in, "premium_exit": premium_out,
                "capital_locked": capital_locked,
                "gross_pnl": gross, "cost": cost, "net_pnl": net_pnl,
                "exit_reason": r0["exit_reason"], "bars_held": r0["bars_held"],
            })
            bal_history.append((ts, p.start_capital + realised, free))

    rec_df = pd.DataFrame(records)
    eq_df = pd.DataFrame(bal_history, columns=["ts","equity","free"])
    final = p.start_capital + realised
    pct = 100*realised/p.start_capital
    daily = rec_df.assign(d=rec_df["exit_ts"].dt.date).groupby("d")["net_pnl"].sum() if len(rec_df) else pd.Series(dtype=float)
    sharpe = (daily.mean()/daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() else 0
    mdd = (eq_df["equity"] - eq_df["equity"].cummax()).min()
    peak = eq_df["equity"].max(); peak_ts = eq_df.loc[eq_df["equity"].idxmax(),"ts"]
    return {
        "trades_taken": len(rec_df),
        "trades_skipped_zero_lots": skipped_zero_lots,
        "trades_skipped_expensive": skipped_too_expensive,
        "starting_capital": p.start_capital,
        "ending_capital": final,
        "total_gain": realised,
        "return_pct": pct,
        "peak": peak, "peak_ts": peak_ts,
        "max_drawdown_rs": mdd,
        "max_drawdown_pct_of_peak": 100*mdd/peak if peak else 0,
        "sharpe_daily_annualized": sharpe,
        "best_day": float(daily.max()) if len(daily) else 0,
        "worst_day": float(daily.min()) if len(daily) else 0,
        "best_day_date": daily.idxmax() if len(daily) else None,
        "worst_day_date": daily.idxmin() if len(daily) else None,
        "winning_days": int((daily > 0).sum()),
        "losing_days": int((daily < 0).sum()),
        "trading_days": len(daily),
        "avg_lots": float(rec_df["lots"].mean()) if len(rec_df) else 0,
        "max_lots_used": int(rec_df["lots"].max()) if len(rec_df) else 0,
        "avg_capital_locked": float(rec_df["capital_locked"].mean()) if len(rec_df) else 0,
        "records": rec_df,
        "equity_curve": eq_df,
        "daily": daily,
    }


def print_summary(res: dict, label: str):
    print(f"\n{'='*72}\n{label}\n{'='*72}")
    print(f"  Starting -> Ending:  Rs {res['starting_capital']:,.0f} -> Rs {res['ending_capital']:,.2f}")
    print(f"  Total gain:          Rs {res['total_gain']:+,.2f}   ({res['return_pct']:+.2f}%)")
    print(f"  Annualised:          {res['return_pct']*12/3:.1f}% (simple)")
    print(f"  Peak balance:        Rs {res['peak']:,.2f}  on {res['peak_ts'].date() if hasattr(res['peak_ts'],'date') else res['peak_ts']}")
    print(f"  Max drawdown:        Rs {res['max_drawdown_rs']:,.2f}  ({res['max_drawdown_pct_of_peak']:.1f}% of peak)")
    print(f"  Trades taken:        {res['trades_taken']}  (skipped 0-lot: {res['trades_skipped_zero_lots']}, too-expensive: {res['trades_skipped_expensive']})")
    print(f"  Avg lots per trade:  {res['avg_lots']:.1f}   Max lots: {res['max_lots_used']}")
    print(f"  Avg capital locked:  Rs {res['avg_capital_locked']:,.0f} per trade")
    print(f"  Winning days:        {res['winning_days']}/{res['trading_days']}")
    print(f"  Best day:            Rs {res['best_day']:+,.0f}  on {res['best_day_date']}")
    print(f"  Worst day:           Rs {res['worst_day']:+,.0f}  on {res['worst_day_date']}")
    print(f"  Sharpe (daily-ann):  {res['sharpe_daily_annualized']:.2f}")


def chart(res: dict, label: str, outpath: str):
    eq = res["equity_curve"].set_index("ts")
    daily = res["daily"]
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    ax = axes[0]
    ax.plot(eq.index, eq["equity"], lw=2, color="C0")
    ax.axhline(res["starting_capital"], color="grey", ls="--", alpha=0.6,
                label=f"Starting capital Rs {res['starting_capital']:,.0f}")
    ax.fill_between(eq.index, res["starting_capital"], eq["equity"],
                    where=eq["equity"]>=res["starting_capital"], alpha=0.2, color="green")
    ax.fill_between(eq.index, res["starting_capital"], eq["equity"],
                    where=eq["equity"]<res["starting_capital"], alpha=0.2, color="red")
    ax.set_title(f"{label}  --  Rs {res['starting_capital']:,.0f} -> Rs {res['ending_capital']:,.0f} ({res['return_pct']:+.1f}%)")
    ax.set_ylabel("Account balance (Rs)"); ax.grid(alpha=0.3); ax.legend(loc="lower right")
    ax.annotate(f"Peak Rs {res['peak']:,.0f}", xy=(res['peak_ts'], res['peak']),
                xytext=(res['peak_ts'], res['peak'] + 0.05*res['peak']),
                arrowprops=dict(arrowstyle="->", color="black"))
    ax = axes[1]
    colors = ["g" if x >= 0 else "r" for x in daily.values]
    daily.plot(kind="bar", ax=ax, color=colors, edgecolor="black", linewidth=0.3)
    ax.set_title("Daily Net P&L (Rs)")
    ax.tick_params(axis="x", labelrotation=90, labelsize=7)
    ax.axhline(0, color="black", lw=0.5); ax.grid(alpha=0.3, axis="y")
    fig.tight_layout(); fig.savefig(outpath, dpi=120); plt.close(fig)


if __name__ == "__main__":
    TRADES = os.path.join(ROOT, "results", "trades_opt_v4c_be04_trail03.csv")
    RESULTS = os.path.join(ROOT, "results")

    # ===== Rs 2 L starting capital -- sweep risk_pct =====
    print("\n############ Rs 2,00,000 starting capital ############")
    for risk_pct in [0.20, 0.25, 0.30, 0.35, 0.50]:
        p = SizingParams(start_capital=200_000, risk_pct=risk_pct,
                         based_on="free", min_lots=1, max_lots=20)
        res = simulate_dynamic(TRADES, p)
        print_summary(res, f"Rs 2L start | dynamic-sizing | risk={risk_pct*100:.0f}% of FREE cash")
        if risk_pct == 0.25:
            chart(res, f"v4c dynamic 25%/free (Rs 2L)",
                  os.path.join(RESULTS, "chart_v4c_dyn25_2L.png"))

    # ===== Rs 4 L starting capital =====
    print("\n############ Rs 4,00,000 starting capital ############")
    for risk_pct in [0.20, 0.25, 0.30, 0.35, 0.50]:
        p = SizingParams(start_capital=400_000, risk_pct=risk_pct,
                         based_on="free", min_lots=1, max_lots=20)
        res = simulate_dynamic(TRADES, p)
        print_summary(res, f"Rs 4L start | dynamic-sizing | risk={risk_pct*100:.0f}% of FREE cash")
        if risk_pct == 0.25:
            chart(res, f"v4c dynamic 25%/free (Rs 4L)",
                  os.path.join(RESULTS, "chart_v4c_dyn25_4L.png"))
