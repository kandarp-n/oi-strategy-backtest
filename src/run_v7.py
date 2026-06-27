"""Run a set of v7 backtests with daily-loss circuit breakers."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.strategy import Params, run_all

common = dict(trade_segment='FUT', require_trend_align=True, avoid_lunch=True, use_oi_flip_exit=True)

runs = [
    ('v7_dls10k',         dict(price_pct=0.0025, oi_pct=0.005, vol_z=2.5, sl_pct=0.004, tgt_pct=0.008, daily_loss_stop=10000)),
    ('v7_dls15k',         dict(price_pct=0.0025, oi_pct=0.005, vol_z=2.5, sl_pct=0.004, tgt_pct=0.008, daily_loss_stop=15000)),
    ('v7_clean_baseline', dict(price_pct=0.0025, oi_pct=0.005, vol_z=2.5, sl_pct=0.004, tgt_pct=0.008)),
    ('v7_relaxed_oi',     dict(price_pct=0.003,  oi_pct=0.001, vol_z=2.0, sl_pct=0.005, tgt_pct=0.012)),
    ('v7_relaxed_oi_dls', dict(price_pct=0.003,  oi_pct=0.001, vol_z=2.0, sl_pct=0.005, tgt_pct=0.012, daily_loss_stop=15000)),
]

for tag, kw in runs:
    run_all(Params(**common, **kw), tag=tag)

for tag, _ in runs:
    s = json.load(open(f'results/summary_{tag}.json'))
    print(f"\n=== {tag} ===")
    print(f"trades:{s['n_trades']}  WR:{s['win_rate']*100:.1f}%  net:Rs {s['total_net_pnl']:,.0f}  costs:Rs {s['total_cost']:,.0f}  Sharpe:{s['sharpe_daily_annualized']:.2f}  MDD:Rs {s['max_drawdown_rs']:,.0f}")
    print(f"exit: {s['exit_reason_counts']}")
