"""Risk manager: enforces daily-loss-stop, emergency-stop, max-positions."""
from __future__ import annotations

import logging
from live.state import BotState
from live import config

log = logging.getLogger("risk")


def can_open_new_position(state: BotState) -> tuple[bool, str]:
    """Return (allowed, reason_if_not)."""
    if state.halted:
        return False, f"HALTED: {state.halt_reason}"
    if len(state.open_positions) >= config.RISK.max_open_positions:
        return False, f"max open positions ({config.RISK.max_open_positions}) reached"
    if state.day_trades_count >= config.RISK.max_trades_per_day:
        return False, f"max trades/day ({config.RISK.max_trades_per_day}) reached"
    if config.RISK.daily_loss_stop_rs > 0 and state.day_net_pnl <= -config.RISK.daily_loss_stop_rs:
        return False, f"daily-loss-stop hit (Rs {state.day_net_pnl:,.0f})"
    if config.RISK.daily_profit_target_rs > 0 and state.day_net_pnl >= config.RISK.daily_profit_target_rs:
        return False, f"daily-profit-target hit (Rs {state.day_net_pnl:,.0f})"
    if config.CAPITAL_RS * config.RISK.emergency_stop_equity_frac > state.running_equity:
        return False, f"emergency stop: equity Rs {state.running_equity:,.0f} < {100*config.RISK.emergency_stop_equity_frac:.0f}% of start"
    return True, ""


def size_for_signal(state: BotState, premium_per_lot: float) -> int:
    """Return how many lots to take for this signal.
    Uses risk_pct of running_equity, capped by capital, MAX_LOTS_PER_TRADE."""
    equity = state.running_equity
    alloc = equity * config.RISK_PCT_PER_TRADE
    lots = int(alloc // premium_per_lot) if premium_per_lot > 0 else 0
    lots = max(config.RISK.min_lots_per_trade, min(lots, config.MAX_LOTS_PER_TRADE))
    return lots


def halt(state: BotState, reason: str) -> None:
    log.warning(f"RISK HALT: {reason}")
    state.halted = True
    state.halt_reason = reason


def check_and_halt(state: BotState) -> None:
    """Called after every trade closes to check if we should halt."""
    if config.RISK.daily_loss_stop_rs > 0 and state.day_net_pnl <= -config.RISK.daily_loss_stop_rs:
        halt(state, f"Daily loss stop Rs {state.day_net_pnl:,.0f} <= -{config.RISK.daily_loss_stop_rs:,.0f}")
    if config.CAPITAL_RS * config.RISK.emergency_stop_equity_frac > state.running_equity:
        halt(state, f"Emergency stop: equity Rs {state.running_equity:,.0f}")
