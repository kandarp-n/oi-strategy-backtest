"""Apply a realistic slippage haircut to entry/exit premiums and re-simulate.

For ATM stock options on Dhan, realistic intraday slippage is:
  - Liquid names (RELIANCE, HDFCBANK, ICICIBANK, INFY...): 1-2% per side on the premium
  - Mid-liquid names (TITAN, COALINDIA, GRASIM, etc.):     2-4% per side
  - Thin names (BAJAJ-AUTO, HCLTECH, BAJAJFINSV):          4-8% per side

We simulate the BAD CASE: 2% slippage per side flat across all symbols,
then 4% per side as a "thin-market" worst case.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd, numpy as np, heapq
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from src.option_costs import option_net_pnl
from src.sim_dynamic_sizing import SizingParams, chart

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "results", "trades_opt_v4c_be04_trail03.csv")
RESULTS = os.path.join(ROOT, "results")


def simulate_with_slippage(slippage_pct_per_side: float, p: SizingParams):
    trades = pd.read_csv(TRADES, parse_dates=["entry_ts","exit_ts"]) \
                .sort_values("entry_ts").reset_index(drop=True)
    trades["lot_size"] = (trades["qty"] / 3).round().astype(int)
    # Apply slippage: we BUY higher than the printed open, SELL lower than the printed close
    trades["premium_entry_slip"] = trades["premium_entry"] * (1 + slippage_pct_per_side)
    trades["premium_exit_slip"]  = trades["premium_exit"]  * (1 - slippage_pct_per_side)
    # Recompute PnL for each trade given new sizing
    free = p.start_capital; realised = 0; records=[]; skipped=0
    bal_history = [(trades["entry_ts"].min() - pd.Timedelta(days=1), p.start_capital)]
    heap=[]; counter=0; locked={}
    for i,r in trades.iterrows():
        heapq.heappush(heap, (r["entry_ts"], 0, counter, i, "entry", r)); counter+=1
    while heap:
        ts,_,_,idx,kind,r = heapq.heappop(heap)
        equity_now = free + sum(locked.values())
        if kind == "entry":
            lot_size = int(r["lot_size"]); premium = float(r["premium_entry_slip"])
            one_lot_cost = premium * lot_size
            if one_lot_cost<=0: continue
            base = free if p.based_on=="free" else equity_now
            alloc = base * p.risk_pct
            alloc = min(alloc, equity_now * p.cap_pct_of_equity, free)
            lots = max(0, min(int(alloc//one_lot_cost), p.max_lots))
            if lots < p.min_lots:
                skipped+=1; continue
            qty = lots * lot_size; locked_cap = premium * qty
            if locked_cap > free + 1e-6: skipped+=1; continue
            free -= locked_cap; locked[idx] = locked_cap
            heapq.heappush(heap,(r["exit_ts"],1,counter,idx,"exit",(idx,lots,qty,locked_cap,r))); counter+=1
        else:
            idx_e,lots,qty,locked_cap,r0 = r
            net,gross,cost = option_net_pnl(float(r0["premium_entry_slip"]), float(r0["premium_exit_slip"]), qty)
            free += locked_cap + net; realised += net
            locked.pop(idx_e, None)
            records.append({"symbol":r0["symbol"], "lots":lots, "net":net,
                            "entry_ts":r0["entry_ts"], "exit_ts":r0["exit_ts"]})
            bal_history.append((ts, p.start_capital + realised))
    eq = pd.DataFrame(bal_history, columns=["ts","equity"])
    daily = pd.DataFrame(records).assign(d=lambda x:x["exit_ts"].dt.date).groupby("d")["net"].sum() if records else pd.Series(dtype=float)
    sharpe = (daily.mean()/daily.std()*np.sqrt(252)) if len(daily)>1 and daily.std() else 0
    mdd = (eq["equity"] - eq["equity"].cummax()).min()
    peak = eq["equity"].max()
    return {
        "slippage_pct": slippage_pct_per_side*100,
        "trades": len(records), "skipped": skipped,
        "start_capital": p.start_capital,
        "end_balance": p.start_capital + realised,
        "return_pct": 100*realised/p.start_capital,
        "mdd": mdd, "mdd_pct": 100*mdd/peak if peak else 0,
        "sharpe": sharpe,
        "worst_day": float(daily.min()) if len(daily) else 0,
        "best_day": float(daily.max()) if len(daily) else 0,
        "avg_lots": float(pd.DataFrame(records)["lots"].mean()) if records else 0,
    }


def run_all(start_capital: float):
    print(f"\n{'#'*72}\n#### Rs {start_capital:,.0f} starting capital -- slippage haircut sweep ####\n{'#'*72}")
    print("Sizing: equity-based 25% per trade (the recommended balanced mode)\n")
    rows=[]
    for slip in [0.000, 0.005, 0.010, 0.015, 0.020, 0.030, 0.050]:
        p = SizingParams(start_capital=start_capital, based_on="equity", risk_pct=0.25)
        r = simulate_with_slippage(slip, p)
        rows.append(r)
        print(f"slip {slip*100:>4.1f}% per side:  trades {r['trades']:>3}  "
              f"end Rs {r['end_balance']:>9,.0f}  ret {r['return_pct']:>+7.1f}%  "
              f"mdd {r['mdd_pct']:>+6.1f}%  sharpe {r['sharpe']:>5.2f}  "
              f"avg-lots {r['avg_lots']:>4.1f}")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RESULTS, f"slippage_sweep_{int(start_capital/1e5)}L.csv"), index=False)
    return df


df2 = run_all(200_000)
df4 = run_all(400_000)

# Combined chart: cumulative balance trajectories at different slippages
fig, ax = plt.subplots(figsize=(14, 6))
for slip in [0.0, 0.01, 0.02, 0.03]:
    p = SizingParams(start_capital=200_000, based_on="equity", risk_pct=0.25)
    # Manual: re-run and grab equity curve
    trades = pd.read_csv(TRADES, parse_dates=["entry_ts","exit_ts"]) \
                .sort_values("entry_ts").reset_index(drop=True)
    trades["lot_size"] = (trades["qty"] / 3).round().astype(int)
    trades["pe"] = trades["premium_entry"] * (1 + slip)
    trades["px"] = trades["premium_exit"]  * (1 - slip)
    free = p.start_capital; realised = 0
    bal = [(trades["entry_ts"].min() - pd.Timedelta(days=1), p.start_capital)]
    heap=[]; counter=0; locked={}
    for i,r in trades.iterrows():
        heapq.heappush(heap,(r["entry_ts"],0,counter,i,"entry",r)); counter+=1
    while heap:
        ts,_,_,idx,kind,r = heapq.heappop(heap)
        eq_now = free + sum(locked.values())
        if kind=="entry":
            ls = int(r["lot_size"]); prem = float(r["pe"])
            if prem<=0: continue
            alloc = min(eq_now*p.risk_pct, free)
            lots = max(0, min(int(alloc//(prem*ls)), p.max_lots))
            if lots<1: continue
            qty=lots*ls; cap=prem*qty
            if cap>free+1e-6: continue
            free-=cap; locked[idx]=cap
            heapq.heappush(heap,(r["exit_ts"],1,counter,idx,"exit",(idx,lots,qty,cap,r))); counter+=1
        else:
            idx_e,lots,qty,cap,r0=r
            net,_,_=option_net_pnl(float(r0["pe"]), float(r0["px"]), qty)
            free+=cap+net; realised+=net; locked.pop(idx_e,None)
            bal.append((ts, p.start_capital+realised))
    df = pd.DataFrame(bal, columns=["ts","eq"])
    ax.plot(df["ts"], df["eq"], label=f"{slip*100:.1f}% slippage per side  -> Rs {df['eq'].iloc[-1]:,.0f}", lw=2)

ax.axhline(200_000, color="grey", ls="--", alpha=0.5)
ax.set_title("v4c (equity-25% sizing on Rs 2L) — Impact of slippage haircut")
ax.set_ylabel("Account balance (Rs)"); ax.legend(loc="upper left"); ax.grid(alpha=0.3)
fig.tight_layout()
out = os.path.join(RESULTS, "chart_slippage_impact_2L.png")
fig.savefig(out, dpi=120); plt.close(fig)
print(f"\nSaved: {out}")
