"""Apply liquidity-aware sizing on top of equity-based sizing.
Cap each trade's qty at liq_cap_pct of the average option volume in the
previous N bars (we don't see future volume when placing the entry).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from src.sim_dynamic_sizing import simulate_dynamic, SizingParams, print_summary, chart

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "results", "trades_opt_v4c_be04_trail03.csv")
RESULTS = os.path.join(ROOT, "results")


def run_liq_set(start_capital: float):
    print(f"\n{'#'*72}\n#### Rs {start_capital:,.0f} -- liquidity-aware sizing comparison ####\n{'#'*72}")
    configs = [
        # No liquidity cap (baseline)
        ("equity_25_NOLIQ",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.25, liq_cap_pct=0)),
        # Strict caps
        ("equity_25_liq05",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.25, liq_cap_pct=0.05)),
        ("equity_25_liq10",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.25, liq_cap_pct=0.10)),
        ("equity_25_liq20",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.25, liq_cap_pct=0.20)),
        # Higher risk + strict liq cap
        ("equity_33_liq10",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.33, liq_cap_pct=0.10)),
        ("equity_50_liq10",   SizingParams(start_capital=start_capital, based_on="equity",
                                            risk_pct=0.50, liq_cap_pct=0.10)),
    ]
    rows = []
    for tag, p in configs:
        res = simulate_dynamic(TRADES, p)
        rows.append({"tag": tag, "trades": res["trades_taken"], "skip": res["trades_skipped_zero_lots"],
                     "end_balance": res["ending_capital"], "ret%": res["return_pct"],
                     "mdd%": res["max_drawdown_pct_of_peak"], "mdd_rs": res["max_drawdown_rs"],
                     "avg_lots": res["avg_lots"], "max_lots": res["max_lots_used"],
                     "sharpe": res["sharpe_daily_annualized"],
                     "worst_day": res["worst_day"], "best_day": res["best_day"]})
    df = pd.DataFrame(rows)
    print(f"\n{'tag':<20} {'trades':>7} {'skip':>5} {'end_bal':>11} {'ret%':>7} {'mdd%':>7} {'avg_lots':>9} {'max_lots':>9} {'sharpe':>7}")
    for _, r in df.iterrows():
        print(f"{r['tag']:<20} {r['trades']:>7} {r['skip']:>5} Rs {r['end_balance']:>8,.0f} {r['ret%']:>+6.1f}% {r['mdd%']:>6.1f}% {r['avg_lots']:>9.1f} {r['max_lots']:>9.0f} {r['sharpe']:>7.2f}")
    return df


df2 = run_liq_set(200_000)
df4 = run_liq_set(400_000)

# Chart best realistic config for each capital
for start, df in [(200_000, df2), (400_000, df4)]:
    p = SizingParams(start_capital=start, based_on="equity", risk_pct=0.25, liq_cap_pct=0.10)
    res = simulate_dynamic(TRADES, p)
    chart(res, f"v4c equity-25% + liq-cap 10% (Rs {int(start/1e5)}L)",
          os.path.join(RESULTS, f"chart_v4c_equity25_liq10_{int(start/1e5)}L.png"))

df2.assign(start=200_000).to_csv(os.path.join(RESULTS, "liquidity_sizing_2L.csv"), index=False)
df4.assign(start=400_000).to_csv(os.path.join(RESULTS, "liquidity_sizing_4L.csv"), index=False)
