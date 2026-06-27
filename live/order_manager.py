"""Order manager with three modes: LIVE, PAPER, DRY_RUN.

LIVE   = real orders placed via Dhan
PAPER  = no orders placed; uses last known LTP as fill price; books P&L
DRY_RUN = no orders placed; just logs intent (no P&L tracked)
"""
from __future__ import annotations

import os, sys, time, uuid, csv, logging
from datetime import datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from live import config
from live import dhan_orders
from live.state import OpenPosition, BotState
from src.option_costs import option_round_trip_cost

log = logging.getLogger("om")


def _correlation_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def place_entry_buy_option(underlying: str, opt_type: str, side: str,
                            strike: float, expiry: str, option_security_id: int,
                            entry_premium_est: float, entry_spot: float, lots: int,
                            lot_size: int, mode: str = config.MODE) -> Optional[OpenPosition]:
    """BUY the option (we are always long premium for both CE and PE signals).
    Returns OpenPosition on success, None on failure / dry-run."""
    qty = lots * lot_size
    corr_id = _correlation_id(f"{underlying[:3]}{opt_type}")
    log.info(f"[{mode}] ENTRY  {underlying} {opt_type} {strike} exp {expiry}  "
             f"qty {qty} ({lots} lots) @ est premium {entry_premium_est:.2f}  "
             f"spot {entry_spot:.2f}  corr_id={corr_id}")
    order_id = None
    fill_price = entry_premium_est

    if mode == "LIVE":
        try:
            resp = dhan_orders.place_order(
                transaction_type="BUY",
                exchange_segment="NSE_FNO",
                security_id=option_security_id,
                quantity=qty,
                product_type=config.EXEC.product_type,
                order_type=config.EXEC.order_type,
                price=entry_premium_est if config.EXEC.order_type == "LIMIT" else 0,
                correlation_id=corr_id,
            )
            order_id = resp.get("orderId")
            log.info(f"  -> Dhan orderId={order_id}, status={resp.get('orderStatus')}")
            # Poll for fill price (simplified: just use estimate; production would query trade book)
            time.sleep(2)
            try:
                st = dhan_orders.get_order(order_id)
                avg_price = st.get("averageTradedPrice") or st.get("price") or entry_premium_est
                fill_price = float(avg_price) if avg_price else entry_premium_est
            except Exception:
                pass
        except Exception as e:
            log.error(f"ENTRY ORDER FAILED for {corr_id}: {e}")
            return None
    elif mode == "PAPER":
        # Apply paper slippage
        fill_price = entry_premium_est * (1 + config.EXEC.paper_slippage_pct)
    else:  # DRY_RUN
        return None

    # Build OpenPosition
    if side == "long":
        spot_sl = entry_spot * (1 - config.PARAMS.sl_pct)
        spot_tgt = entry_spot * (1 + config.PARAMS.tgt_pct)
    else:
        spot_sl = entry_spot * (1 + config.PARAMS.sl_pct)
        spot_tgt = entry_spot * (1 - config.PARAMS.tgt_pct)

    pos = OpenPosition(
        correlation_id=corr_id,
        order_id=order_id,
        underlying=underlying,
        opt_type=opt_type,
        side=side,
        strike=strike,
        expiry=expiry,
        option_security_id=option_security_id,
        qty=qty,
        lots=lots,
        entry_ts=datetime.now().isoformat(timespec="seconds"),
        entry_premium=fill_price,
        entry_spot=entry_spot,
        spot_sl=spot_sl,
        spot_tgt=spot_tgt,
        spot_high_since_entry=entry_spot,
        spot_low_since_entry=entry_spot,
    )
    return pos


def place_exit_sell_option(pos: OpenPosition, exit_premium_est: float,
                            exit_spot: float, exit_reason: str,
                            mode: str = config.MODE) -> Optional[OpenPosition]:
    """SELL the option to close. Sets exit fields on pos."""
    log.info(f"[{mode}] EXIT   {pos.underlying} {pos.opt_type} {pos.strike}  qty {pos.qty}  "
             f"@ est premium {exit_premium_est:.2f}  spot {exit_spot:.2f}  reason {exit_reason}")
    fill_price = exit_premium_est

    if mode == "LIVE":
        try:
            corr_id = _correlation_id(f"EX{pos.underlying[:2]}")
            resp = dhan_orders.place_order(
                transaction_type="SELL",
                exchange_segment="NSE_FNO",
                security_id=pos.option_security_id,
                quantity=pos.qty,
                product_type=config.EXEC.product_type,
                order_type="MARKET",
                correlation_id=corr_id,
            )
            order_id = resp.get("orderId")
            log.info(f"  -> Dhan EXIT orderId={order_id}")
            time.sleep(2)
            try:
                st = dhan_orders.get_order(order_id)
                avg_price = st.get("averageTradedPrice") or exit_premium_est
                fill_price = float(avg_price) if avg_price else exit_premium_est
            except Exception:
                pass
        except Exception as e:
            log.error(f"EXIT ORDER FAILED: {e}")
            return None
    elif mode == "PAPER":
        fill_price = exit_premium_est * (1 - config.EXEC.paper_slippage_pct)
    else:
        return None

    # Compute PnL
    gross = (fill_price - pos.entry_premium) * pos.qty
    cost = option_round_trip_cost(pos.entry_premium, fill_price, pos.qty)
    net = gross - cost

    pos.exit_ts = datetime.now().isoformat(timespec="seconds")
    pos.exit_premium = fill_price
    pos.exit_spot = exit_spot
    pos.exit_reason = exit_reason
    pos.net_pnl = net
    log.info(f"  CLOSED  premium {pos.entry_premium:.2f} -> {fill_price:.2f}  "
             f"qty {pos.qty}  gross Rs {gross:+,.0f}  cost Rs {cost:.0f}  NET Rs {net:+,.0f}")
    return pos


def append_trade_log(pos: OpenPosition, mode: str = config.MODE) -> None:
    """Append the closed trade to the persistent CSV log."""
    path = config.TRADE_LOG_PATH
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["mode","corr_id","order_id","underlying","opt_type","side","strike",
                        "expiry","sec_id","qty","lots","entry_ts","entry_premium","entry_spot",
                        "exit_ts","exit_premium","exit_spot","exit_reason","net_pnl"])
        w.writerow([mode, pos.correlation_id, pos.order_id, pos.underlying, pos.opt_type,
                    pos.side, pos.strike, pos.expiry, pos.option_security_id, pos.qty, pos.lots,
                    pos.entry_ts, pos.entry_premium, pos.entry_spot,
                    pos.exit_ts, pos.exit_premium, pos.exit_spot, pos.exit_reason, pos.net_pnl])
