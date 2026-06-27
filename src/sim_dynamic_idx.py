"""Dynamic sizing on NIFTY/BANKNIFTY index-options trades.

Reuse the equity-based sizing logic, but on the idx_v4_vloose trade list.
At each entry: lots = floor((risk_pct * current_equity) / (premium * lot_size)),
clipped to [min_lots, max_lots]. Re-compute PnL & costs for the new qty.

Also tests "slippage haircut" scenarios since real fills won't be at
printed prices: NIFTY/BANKNIFTY ATM options have 0.2-0.5% per-side bid-ask.
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
from src.option_costs import option_net_pnl

TRADES = os.path.join(ROOT, "results", "trades_idx_v4_vloose.csv")
RESULTS = os.path.join(ROOT, "results")


def simulate(start_capital: float, risk_pct: float, mode: str,
              max_slots: int, slot_pct: float, slippage: float,
              min_lots: int = 1, max_lots: int = 200,
              trades_path: str = TRADES) -> dict:
    trades = pd.read_csv(trades_path, parse_dates=["entry_ts","exit_ts"]) \
                .sort_values("entry_ts").reset_index(drop=True)
    # Each trade uses 1 lot in baseline → qty = lot_size
    trades["lot_size"] = trades["qty"].astype(int)

    free = start_capital
    realised = 0.0
    bal_history = [(trades["entry_ts"].min() - pd.Timedelta(days=1), start_capital)]
    locked = {}
    records = []
    skipped = 0

    heap = []; counter = 0
    for i, r in trades.iterrows():
        heapq.heappush(heap, (r["entry_ts"], 0, counter, i, "entry", r)); counter += 1

    while heap:
        ts, _, _, idx, kind, r = heapq.heappop(heap)
        equity_now = free + sum(locked.values())
        if kind == "entry":
            lot_size = int(r["lot_size"])
            # Apply slippage: pay higher to BUY at entry
            premium_in = float(r["premium_entry"]) * (1 + slippage)
            one_lot_cost = premium_in * lot_size
            if one_lot_cost <= 0:
                continue
            n_open = len(locked)
            if mode == "slots":
                if n_open >= max_slots:
                    skipped += 1; continue
                alloc = equity_now * slot_pct
            elif mode == "equity":
                alloc = equity_now * risk_pct
            else:  # "free"
                alloc = free * risk_pct
            alloc = min(alloc, free)
            lots = max(0, min(int(alloc // one_lot_cost), max_lots))
            if lots < min_lots:
                skipped += 1; continue
            qty = lots * lot_size
            capital_locked = premium_in * qty
            if capital_locked > free + 1e-6:
                skipped += 1; continue
            free -= capital_locked
            locked[idx] = capital_locked
            heapq.heappush(heap, (r["exit_ts"], 1, counter, idx, "exit",
                                    (idx, lots, qty, capital_locked, premium_in, r)))
            counter += 1
        else:
            idx_e, lots, qty, capital_locked, premium_in, r0 = r
            premium_out = float(r0["premium_exit"]) * (1 - slippage)
            net, gross, cost = option_net_pnl(premium_in, premium_out, qty)
            free += capital_locked + net
            realised += net
            locked.pop(idx_e, None)
            records.append({
                "entry_ts": r0["entry_ts"], "exit_ts": r0["exit_ts"],
                "symbol": r0["symbol"], "side": r0["side"], "opt_type": r0["opt_type"],
                "strike": float(r0["strike"]), "lots": lots, "qty": qty,
                "premium_in": premium_in, "premium_out": premium_out,
                "capital_locked": capital_locked,
                "gross": gross, "cost": cost, "net": net,
                "exit_reason": r0["exit_reason"],
            })
            bal_history.append((ts, start_capital + realised))

    rec = pd.DataFrame(records)
    eq = pd.DataFrame(bal_history, columns=["ts","equity"])
    daily = rec.assign(d=rec["exit_ts"].dt.date).groupby("d")["net"].sum() if len(rec) else pd.Series(dtype=float)
    sharpe = (daily.mean()/daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() else 0
    mdd = (eq["equity"] - eq["equity"].cummax()).min()
    peak = eq["equity"].max(); peak_ts = eq.loc[eq["equity"].idxmax(), "ts"]
    return {
        "trades_taken": len(rec), "skipped": skipped,
        "start": start_capital,
        "end": start_capital + realised,
        "return_pct": 100 * realised / start_capital,
        "peak": peak, "peak_ts": peak_ts,
        "mdd_rs": mdd,
        "mdd_pct": 100 * mdd / peak if peak else 0,
        "sharpe": sharpe,
        "winning_days": int((daily > 0).sum()),
        "losing_days": int((daily < 0).sum()),
        "trading_days": len(daily),
        "best_day": float(daily.max()) if len(daily) else 0,
        "worst_day": float(daily.min()) if len(daily) else 0,
        "avg_lots": float(rec["lots"].mean()) if len(rec) else 0,
        "max_lots": int(rec["lots"].max()) if len(rec) else 0,
        "avg_capital_locked": float(rec["capital_locked"].mean()) if len(rec) else 0,
        "max_capital_locked": float(rec["capital_locked"].max()) if len(rec) else 0,
        "equity_curve": eq, "daily": daily, "records": rec,
    }


def print_row(name, r):
    print(f"{name:<28} {r['trades_taken']:>4} {r['skipped']:>4}  "
          f"Rs {r['end']:>9,.0f}  {r['return_pct']:>+6.1f}%  {r['mdd_pct']:>6.1f}%  "
          f"{r['avg_lots']:>4.1f}/{r['max_lots']:>3d} lots  "
          f"avg/peak cap Rs {r['avg_capital_locked']:>5,.0f}/Rs {r['max_capital_locked']:>6,.0f}  "
          f"Sharpe {r['sharpe']:>5.2f}")


for start in [200_000, 400_000]:
    print(f"\n{'#'*120}")
    print(f"#### NIFTY/BANKNIFTY idx_v4_vloose — Rs {start:,.0f} starting capital, dynamic sizing ####")
    print(f"{'#'*120}")
    print(f"{'config':<28} {'tr':>4} {'sk':>4}  {'end_bal':>11}   {'ret':>7}   {'mdd':>7}  {'lots':>14}  "
          f"{'capital deployed':>30}  {'Sharpe':>7}")
    print("-" * 130)
    for slip, slip_label in [(0.0, "0.0% slip (theoretical)"),
                              (0.003, "0.3% slip (best-case)"),
                              (0.005, "0.5% slip (realistic)")]:
        print(f"\n--- {slip_label} ---")
        for mode_tag, p in [
            ("greedy_free_25",  dict(mode="free",   risk_pct=0.25, max_slots=0, slot_pct=0)),
            ("equity_25",       dict(mode="equity", risk_pct=0.25, max_slots=0, slot_pct=0)),
            ("equity_33",       dict(mode="equity", risk_pct=0.33, max_slots=0, slot_pct=0)),
            ("equity_50",       dict(mode="equity", risk_pct=0.50, max_slots=0, slot_pct=0)),
            ("slots_4x25",      dict(mode="slots",  risk_pct=0,    max_slots=4, slot_pct=0.25)),
            ("slots_5x20",      dict(mode="slots",  risk_pct=0,    max_slots=5, slot_pct=0.20)),
        ]:
            r = simulate(start_capital=start, slippage=slip, **p)
            print_row(mode_tag, r)


# Generate chart for the best Rs 2L configuration
print("\n\nGenerating equity-curve chart for Rs 2L equity_33 @ 0.5% slip...")
res = simulate(200_000, risk_pct=0.33, mode="equity", max_slots=0, slot_pct=0, slippage=0.005)
fig, axes = plt.subplots(2, 1, figsize=(14, 8))
ax = axes[0]
eq = res["equity_curve"]
ax.plot(eq["ts"], eq["equity"], lw=2, color="C0")
ax.axhline(200_000, color="grey", ls="--", alpha=0.5, label="Starting Rs 2,00,000")
ax.fill_between(eq["ts"], 200_000, eq["equity"], where=eq["equity"]>=200_000, alpha=0.2, color="green")
ax.fill_between(eq["ts"], 200_000, eq["equity"], where=eq["equity"]<200_000, alpha=0.2, color="red")
ax.set_title(f"NIFTY/BANKNIFTY idx_v4_vloose — Rs 2L, equity-33% sizing, 0.5% slip — "
              f"Rs 2L -> Rs {res['end']:,.0f} ({res['return_pct']:+.1f}%)")
ax.set_ylabel("Account balance (Rs)"); ax.grid(alpha=0.3); ax.legend(loc="upper left")
ax = axes[1]
colors = ["g" if x >= 0 else "r" for x in res["daily"].values]
res["daily"].plot(kind="bar", ax=ax, color=colors, edgecolor="black", linewidth=0.3)
ax.set_title("Daily P&L")
ax.tick_params(axis="x", labelrotation=90, labelsize=7)
ax.axhline(0, color="black", lw=0.5); ax.grid(alpha=0.3, axis="y")
fig.tight_layout()
fig.savefig(os.path.join(RESULTS, "chart_idx_v4_dyn_2L.png"), dpi=120)
print(f"saved: {os.path.join(RESULTS, 'chart_idx_v4_dyn_2L.png')}")

# Save records of the best run
res["records"].to_csv(os.path.join(RESULTS, "trades_idx_v4_dyn_eq33_2L.csv"), index=False)
