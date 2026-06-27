"""Generate equity-curve + per-symbol-PnL charts from trades_{tag}.csv files."""
from __future__ import annotations

import os
import sys
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")


def chart(tag: str):
    trades = pd.read_csv(os.path.join(RESULTS, f"trades_{tag}.csv"), parse_dates=["entry_ts", "exit_ts"])
    if trades.empty:
        print(f"{tag}: empty")
        return
    trades = trades.sort_values("exit_ts").reset_index(drop=True)
    trades["cum_net"] = trades["net_pnl"].cumsum()

    daily = trades.assign(d=trades["exit_ts"].dt.date).groupby("d")["net_pnl"].sum()
    daily_eq = daily.cumsum()

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))

    ax = axes[0, 0]
    ax.plot(daily_eq.index, daily_eq.values, lw=2)
    ax.set_title(f"{tag} — Cumulative Net P&L (Rs)")
    ax.set_xlabel("Date"); ax.set_ylabel("Cumulative net P&L (Rs)")
    ax.grid(alpha=0.3); ax.axhline(0, color="k", lw=0.5)

    ax = axes[0, 1]
    daily.plot(kind="bar", ax=ax, color=["g" if x >= 0 else "r" for x in daily.values])
    ax.set_title("Daily Net P&L (Rs)")
    ax.tick_params(axis="x", labelrotation=90, labelsize=7)
    ax.grid(alpha=0.3)

    ax = axes[1, 0]
    per_sym = trades.groupby("symbol")["net_pnl"].sum().sort_values()
    per_sym.plot(kind="barh", ax=ax, color=["r" if v < 0 else "g" for v in per_sym.values])
    ax.set_title("Per-Symbol Net P&L (Rs)")
    ax.grid(alpha=0.3)

    ax = axes[1, 1]
    ax.hist(trades["net_pnl"], bins=40, edgecolor="k")
    ax.axvline(0, color="k", lw=1)
    ax.set_title("Distribution of Per-Trade Net P&L (Rs)")
    ax.grid(alpha=0.3)

    fig.suptitle(f"Strategy {tag} — {len(trades)} trades", fontsize=14)
    fig.tight_layout()
    out = os.path.join(RESULTS, f"chart_{tag}.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"saved {out}")


if __name__ == "__main__":
    tags = sys.argv[1:] or ["v5_tgt_lower", "v5_relaxed_oi", "v6_combo_a"]
    for t in tags:
        chart(t)
