"""Persistent state for live trading.

Stored as JSON in live/state.json. Tracks:
  - open positions (with entry details, current SL/TGT, trailing state)
  - daily PnL accumulator
  - day trade counter
  - last processed bar timestamp (so we don't re-process bars after restart)
"""
from __future__ import annotations

import json, os
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from typing import Optional


@dataclass
class OpenPosition:
    correlation_id: str          # our internal id (also sent to Dhan as correlationId)
    order_id: str | None         # Dhan order_id from entry (None in paper mode)
    underlying: str              # "NIFTY" or "BANKNIFTY"
    opt_type: str                # "CE" or "PE"
    side: str                    # "long" or "short" — refers to the underlying direction
    strike: float
    expiry: str                  # "YYYY-MM-DD"
    option_security_id: int
    qty: int
    lots: int
    entry_ts: str
    entry_premium: float
    entry_spot: float
    # Dynamic exit state
    spot_sl: float
    spot_tgt: float
    spot_high_since_entry: float
    spot_low_since_entry: float
    be_triggered: bool = False
    # exits + result (populated only after close)
    exit_ts: str | None = None
    exit_premium: float | None = None
    exit_spot: float | None = None
    exit_reason: str | None = None
    net_pnl: float | None = None


@dataclass
class BotState:
    today: str = ""                                # YYYY-MM-DD
    open_positions: list[OpenPosition] = field(default_factory=list)
    closed_today: list[OpenPosition] = field(default_factory=list)
    day_net_pnl: float = 0.0
    day_trades_count: int = 0
    last_processed_bar_ts: str = ""
    halted: bool = False                           # set True by risk manager when daily-loss-stop hits
    halt_reason: str = ""
    running_equity: float = 0.0                    # rolling equity used for sizing


def load_state(path: str, default_equity: float) -> BotState:
    if not os.path.exists(path):
        s = BotState(running_equity=default_equity)
        return s
    with open(path, "r") as f:
        raw = json.load(f)
    # Re-hydrate OpenPosition dataclasses
    s = BotState(
        today=raw.get("today", ""),
        day_net_pnl=raw.get("day_net_pnl", 0.0),
        day_trades_count=raw.get("day_trades_count", 0),
        last_processed_bar_ts=raw.get("last_processed_bar_ts", ""),
        halted=raw.get("halted", False),
        halt_reason=raw.get("halt_reason", ""),
        running_equity=raw.get("running_equity", default_equity),
        open_positions=[OpenPosition(**p) for p in raw.get("open_positions", [])],
        closed_today=[OpenPosition(**p) for p in raw.get("closed_today", [])],
    )
    return s


def save_state(state: BotState, path: str) -> None:
    raw = {
        "today": state.today,
        "day_net_pnl": state.day_net_pnl,
        "day_trades_count": state.day_trades_count,
        "last_processed_bar_ts": state.last_processed_bar_ts,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "running_equity": state.running_equity,
        "open_positions": [asdict(p) for p in state.open_positions],
        "closed_today": [asdict(p) for p in state.closed_today],
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2, default=str)
    os.replace(tmp, path)


def roll_to_new_day(state: BotState, equity_at_open: float, today_str: str) -> None:
    """Called at market open to reset daily counters."""
    state.today = today_str
    state.closed_today = []
    state.day_net_pnl = 0.0
    state.day_trades_count = 0
    state.halted = False
    state.halt_reason = ""
    state.running_equity = equity_at_open
