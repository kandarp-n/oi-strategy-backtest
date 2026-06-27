"""Compare three sizing modes:
  1) GREEDY: alloc = risk_pct * FREE cash       (first-come-first-served, unfair to clustered signals)
  2) EQUITY: alloc = risk_pct * TOTAL equity    (every signal gets equal-sized slice, capped by free cash)
  3) SLOTS:  fixed N slots, each gets 1/N of equity at entry (slot-aware concurrency cap)

Run all three on both Rs 2L and Rs 4L starting capital.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from src.sim_dynamic_sizing import simulate_dynamic, SizingParams, print_summary, chart

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "results", "trades_opt_v4c_be04_trail03.csv")
RESULTS = os.path.join(ROOT, "results")


def run_set(start_capital: float):
    rows = []
    print(f"\n{'#'*72}")
    print(f"#### Rs {start_capital:,.0f} starting capital — sizing-mode comparison ####")
    print(f"{'#'*72}")

    configs = [
        # GREEDY: fraction of FREE cash
        ("greedy_free20", SizingParams(start_capital=start_capital, based_on="free", risk_pct=0.20)),
        ("greedy_free25", SizingParams(start_capital=start_capital, based_on="free", risk_pct=0.25)),
        ("greedy_free33", SizingParams(start_capital=start_capital, based_on="free", risk_pct=0.33)),
        # EQUITY: fraction of TOTAL equity (current free + locked)
        ("equity_20",     SizingParams(start_capital=start_capital, based_on="equity", risk_pct=0.20)),
        ("equity_25",     SizingParams(start_capital=start_capital, based_on="equity", risk_pct=0.25)),
        ("equity_33",     SizingParams(start_capital=start_capital, based_on="equity", risk_pct=0.33)),
        # SLOTS: explicit slot-aware
        ("slots_5x20",    SizingParams(start_capital=start_capital, based_on="slots", max_slots=5, slot_pct=0.20)),
        ("slots_4x25",    SizingParams(start_capital=start_capital, based_on="slots", max_slots=4, slot_pct=0.25)),
        ("slots_4x20",    SizingParams(start_capital=start_capital, based_on="slots", max_slots=4, slot_pct=0.20)),
        ("slots_6x16",    SizingParams(start_capital=start_capital, based_on="slots", max_slots=6, slot_pct=1/6)),
    ]
    for tag, p in configs:
        res = simulate_dynamic(TRADES, p)
        rows.append({
            "tag": tag,
            "mode": p.based_on,
            "param": f"{p.slot_pct*100:.0f}% x {p.max_slots}slots" if p.based_on=="slots" else f"{p.risk_pct*100:.0f}%",
            "trades": res["trades_taken"],
            "skipped": res["trades_skipped_zero_lots"] + res["trades_skipped_expensive"],
            "end_balance": res["ending_capital"],
            "return_pct": res["return_pct"],
            "mdd_pct": res["max_drawdown_pct_of_peak"],
            "mdd_rs": res["max_drawdown_rs"],
            "best_day": res["best_day"],
            "worst_day": res["worst_day"],
            "avg_lots": res["avg_lots"],
            "max_lots": res["max_lots_used"],
            "sharpe": res["sharpe_daily_annualized"],
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("end_balance", ascending=False)
    print(f"\n{'tag':<14} {'mode':<7} {'param':<14} {'trades':>7} {'skip':>5} {'end_bal':>11} {'ret%':>7} {'mdd%':>7} {'avg_lots':>9} {'sharpe':>7}")
    for _, r in df.iterrows():
        print(f"{r['tag']:<14} {r['mode']:<7} {r['param']:<14} {r['trades']:>7} {r['skipped']:>5} Rs {r['end_balance']:>8,.0f} {r['return_pct']:>+6.1f}% {r['mdd_pct']:>6.1f}% {r['avg_lots']:>9.1f} {r['sharpe']:>7.2f}")
    return df


df2 = run_set(200_000)
df4 = run_set(400_000)

# Save table
df2.assign(start=200_000).to_csv(os.path.join(RESULTS, "sizing_comparison_2L.csv"), index=False)
df4.assign(start=400_000).to_csv(os.path.join(RESULTS, "sizing_comparison_4L.csv"), index=False)

# Best charts
best2 = df2.iloc[0]
best4 = df4.iloc[0]
print(f"\nBest for Rs 2L: {best2['tag']}  -> Rs {best2['end_balance']:,.0f} ({best2['return_pct']:+.1f}%)")
print(f"Best for Rs 4L: {best4['tag']}  -> Rs {best4['end_balance']:,.0f} ({best4['return_pct']:+.1f}%)")

# Make charts for the slot-based variants (the user's question)
for start in [200_000, 400_000]:
    p = SizingParams(start_capital=start, based_on="slots", max_slots=5, slot_pct=0.20)
    res = simulate_dynamic(TRADES, p)
    chart(res, f"v4c slots 5x20% (Rs {start/1e5:.0f}L)",
          os.path.join(RESULTS, f"chart_v4c_slots5x20_{int(start/1e5)}L.png"))
