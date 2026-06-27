"""Live trading configuration. EDIT THIS FILE BEFORE DEPLOYING.

DANGER: switching MODE='LIVE' will place real orders with real money.
Always run in PAPER mode first for at least 5 trading days.
"""
from __future__ import annotations
from dataclasses import dataclass, field


# ============================================================================
# === GLOBAL MODE — set this carefully ======================================
# ============================================================================
MODE = "PAPER"   # "PAPER" or "LIVE" or "DRY_RUN"
# PAPER  = no orders placed, but full strategy execution simulated
# DRY_RUN = no orders placed, just log "would-be" signals  (read-only)
# LIVE   = REAL orders placed via Dhan API


# ============================================================================
# === Account configuration =================================================
# ============================================================================
CAPITAL_RS = 200_000     # how much capital to allocate to this strategy
RISK_PCT_PER_TRADE = 0.25   # 25% of equity per trade (max ~4 concurrent)
SLOT_MODE = True         # True = enforce max_concurrent_positions slot system
MAX_CONCURRENT_POSITIONS = 4
MAX_LOTS_PER_TRADE = 30   # hard cap on lots per single trade


# ============================================================================
# === Universe — only NIFTY and BANKNIFTY ===================================
# ============================================================================
@dataclass
class Underlying:
    name: str
    spot_security_id: int
    spot_exchange: str       # IDX_I for indices
    spot_instrument: str
    front_fut_security_id: int  # UPDATE THIS each month — see resolve_fut() helper
    fut_exchange: str
    fut_instrument: str
    lot_size: int
    strike_step: int         # NIFTY=50, BANKNIFTY=100

UNIVERSE: list[Underlying] = [
    Underlying("NIFTY",     spot_security_id=13, spot_exchange="IDX_I", spot_instrument="INDEX",
               front_fut_security_id=62329, fut_exchange="NSE_FNO", fut_instrument="FUTIDX",
               lot_size=65, strike_step=50),
    Underlying("BANKNIFTY", spot_security_id=25, spot_exchange="IDX_I", spot_instrument="INDEX",
               front_fut_security_id=62326, fut_exchange="NSE_FNO", fut_instrument="FUTIDX",
               lot_size=30, strike_step=100),
]


# ============================================================================
# === Strategy parameters (idx_v4_vloose) ===================================
# ============================================================================
@dataclass
class StrategyParams:
    # Signal thresholds (on the futures bar)
    price_pct: float = 0.0008      # 0.08% 5-min price change on futures
    oi_pct: float = 0.0005         # 0.05% 5-min OI change
    vol_z: float = 0.8             # vol z-score >= 0.8
    require_trend_align: bool = True   # spot close vs day-VWAP
    avoid_lunch: bool = True
    entry_start: str = "09:45"
    entry_end: str = "14:30"
    square_off: str = "15:15"
    # Exits (on the spot price)
    sl_pct: float = 0.004
    tgt_pct: float = 0.008
    breakeven_trigger_pct: float = 0.004
    trail_stop_pct: float = 0.003
    use_oi_flip_exit: bool = True
    # Option selection
    min_dte: int = 1                # min days-to-expiry for the option used
    use_atm_offset: int = 0         # 0 = strict ATM; +1 = next ITM, -1 = next OTM
    cool_off_bars: int = 0

PARAMS = StrategyParams()


# ============================================================================
# === Risk controls ==========================================================
# ============================================================================
@dataclass
class RiskLimits:
    daily_loss_stop_rs: float = 10_000      # halt at -Rs 10K for the day
    daily_profit_target_rs: float = 0       # 0 = disabled
    max_trades_per_day: int = 20            # hard cap
    max_open_positions: int = 4
    min_lots_per_trade: int = 1
    # If the account equity drops below this fraction of starting capital, stop
    emergency_stop_equity_frac: float = 0.70  # = 30% drawdown -> halt

RISK = RiskLimits()


# ============================================================================
# === Execution / Slippage assumptions ======================================
# ============================================================================
@dataclass
class ExecutionConfig:
    order_type: str = "MARKET"     # MARKET or LIMIT
    limit_offset_pct: float = 0.002  # if LIMIT: place buy at ask*(1+0.2%) for entry
    product_type: str = "INTRADAY"  # MIS
    # Slippage budget for paper mode (best-effort estimate; ignored in LIVE)
    paper_slippage_pct: float = 0.005

EXEC = ExecutionConfig()


# ============================================================================
# === Scheduling ============================================================
# ============================================================================
@dataclass
class Schedule:
    market_open: str = "09:15"
    market_close: str = "15:30"
    poll_interval_sec: int = 30        # how often the bot wakes up
    bar_check_window_sec: int = 60     # check for closed 5-min bar at start of each new bar
    pre_market_buffer_min: int = 5     # idle until N min after market_open

SCHED = Schedule()


# ============================================================================
# === Paths and logging =====================================================
# ============================================================================
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "live", "state.json")
TRADE_LOG_PATH = os.path.join(ROOT, "live", "trades.csv")
LOG_PATH = os.path.join(ROOT, "live", "bot.log")
KILL_FILE = os.path.join(ROOT, "live", "KILL")   # touch this file to stop the bot


# ============================================================================
# === Validation =============================================================
# ============================================================================
def validate() -> list[str]:
    """Return a list of warnings/errors. Empty list = OK to deploy."""
    issues = []
    if MODE not in ("PAPER", "DRY_RUN", "LIVE"):
        issues.append(f"MODE must be PAPER/DRY_RUN/LIVE, got {MODE}")
    if MODE == "LIVE":
        if CAPITAL_RS < 50_000:
            issues.append("LIVE mode with capital < Rs 50K is risky")
        if MAX_LOTS_PER_TRADE > 50:
            issues.append("MAX_LOTS_PER_TRADE > 50 may exceed exchange freeze limits")
    return issues


if __name__ == "__main__":
    print(f"Mode:    {MODE}")
    print(f"Capital: Rs {CAPITAL_RS:,}")
    print(f"Risk:    {RISK_PCT_PER_TRADE*100:.0f}% per trade, max {MAX_CONCURRENT_POSITIONS} concurrent")
    for u in UNIVERSE:
        print(f"  {u.name}: spot={u.spot_security_id}, fut={u.front_fut_security_id}, "
              f"lot={u.lot_size}, step={u.strike_step}")
    issues = validate()
    if issues:
        print(f"\nISSUES:")
        for x in issues: print(f"  * {x}")
    else:
        print(f"\n[OK] Config valid.")
