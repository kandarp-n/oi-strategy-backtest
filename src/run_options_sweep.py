"""Sweep of options strategy variants."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.strategy_options import OptParams, run_all_options, _print_opt_summary

FROM_STR = "2026-03-30 09:15:00"
TO_STR = "2026-06-26 15:30:00"

common = dict(trade_segment="OPT", require_trend_align=True,
              avoid_lunch=True, use_oi_flip_exit=True, min_dte=14)

variants = {
    # spot-based stops (replica of futures rules)
    "opt_v2_spot":      OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0, sl_pct=0.004, tgt_pct=0.008,
                                  use_premium_stops=False, fut_lots=1, **common),
    # premium-based stops (typical for options trading)
    "opt_v2_prem30_60": OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
                                  use_premium_stops=True, premium_sl_pct=0.30, premium_tgt_pct=0.60,
                                  fut_lots=1, **common),
    "opt_v2_prem25_75": OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
                                  use_premium_stops=True, premium_sl_pct=0.25, premium_tgt_pct=0.75,
                                  fut_lots=1, **common),
    "opt_v2_prem40_80": OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
                                  use_premium_stops=True, premium_sl_pct=0.40, premium_tgt_pct=0.80,
                                  fut_lots=1, **common),
    # 3 lots (to amortise the Rs 40 flat brokerage)
    "opt_v2_spot_3lots":  OptParams(price_pct=0.0025, oi_pct=0.003, vol_z=2.0, sl_pct=0.004, tgt_pct=0.008,
                                    use_premium_stops=False, fut_lots=3, **common),
    # tighter signal (selective)
    "opt_v2_strict":    OptParams(price_pct=0.003, oi_pct=0.005, vol_z=2.5, sl_pct=0.004, tgt_pct=0.008,
                                  use_premium_stops=False, fut_lots=1, **common),
    # looser to capture more signals
    "opt_v2_loose":     OptParams(price_pct=0.002, oi_pct=0.001, vol_z=1.5, sl_pct=0.005, tgt_pct=0.010,
                                  use_premium_stops=False, fut_lots=1, **common),
}

for tag, p in variants.items():
    s = run_all_options(p, tag=tag, from_dt=FROM_STR, to_dt=TO_STR)
    _print_opt_summary(s)
