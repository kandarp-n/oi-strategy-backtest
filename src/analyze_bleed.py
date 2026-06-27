"""Post-hoc analysis: if we'd hard-exited 'limping' trades at bar N, what would PnL be?"""
import pandas as pd, numpy as np

op = pd.read_csv("results/trades_opt_v2_spot_3lots.csv", parse_dates=["entry_ts", "exit_ts"])
baseline = op["net_pnl"].sum()
print(f"Baseline: {len(op)} trades, net Rs {baseline:,.0f}\n")

print("--- Estimated PnL if we hard-exited trades that ran past N bars ---")
print("(Assumes linear interpolation of premium between entry and final exit — rough estimate)\n")
for N in [6, 8, 10, 12, 15, 20]:
    same = op[op["bars_held"] <= N]
    early_exits = op[op["bars_held"] > N]
    # Linear interpolation of PnL: pnl_at_N = pnl_final * N / bars_held
    est_pnl = same["net_pnl"].sum() + (early_exits["net_pnl"] * N / early_exits["bars_held"]).sum()
    n_trunc = len(early_exits)
    losers_trunc = (early_exits["net_pnl"] < 0).sum()
    print(f"  N={N:2d} bars: truncate {n_trunc:3d} trades ({losers_trunc} losers), est net Rs {est_pnl:>9,.0f}  (delta Rs {est_pnl-baseline:+,.0f})")

print("\n--- The 11+ bars SL trades (the limpers/bleeders) ---")
bleed = op[(op["exit_reason"] == "SL") & (op["bars_held"] > 10)]
print(f"{len(bleed)} trades, total net Rs {bleed['net_pnl'].sum():,.0f}")
print(f"Avg bars held: {bleed['bars_held'].mean():.1f}")
print(f"Spot moved on avg: {((bleed['spot_exit']-bleed['spot_entry'])/bleed['spot_entry']*100).mean():.2f}%")
print(f"Premium decayed on avg: {((bleed['premium_exit']-bleed['premium_entry'])/bleed['premium_entry']*100).mean():.2f}%")
print(f"This is theta + adverse delta. If exited at bar 10 (proportional): est Rs {(bleed['net_pnl'] * 10 / bleed['bars_held']).sum():,.0f}")
print(f"Total saving: Rs {(bleed['net_pnl'] * 10 / bleed['bars_held']).sum() - bleed['net_pnl'].sum():+,.0f}")

print("\n--- All TIME exits (held to 15:15 square-off) ---")
te = op[op["exit_reason"] == "TIME"]
print(f"{len(te)} trades, total Rs {te['net_pnl'].sum():,.0f}, avg bars {te['bars_held'].mean():.1f}")
print(f"Premium decay: {((te['premium_exit']-te['premium_entry'])/te['premium_entry']*100).mean():.2f}%")
print(f"If exited at bar 12: est Rs {(te['net_pnl'] * 12 / te['bars_held'].clip(lower=12)).sum():,.0f}")
