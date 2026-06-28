"""Live trading runner — main event loop.

Defaults to PAPER mode (config.MODE = 'PAPER'). To go LIVE you must:
  1. Edit live/config.py and set MODE='LIVE'
  2. Confirm front_fut_security_id is current month
  3. Run with: python live/run_live.py

The runner:
  - sleeps between polls (default 30s)
  - on each poll: checks for closed 5-min bar, runs signal cycle, runs exit cycle
  - on KILL file present: stops gracefully
  - at 15:15: squares off all open positions

Run modes:
  python live/run_live.py            -> uses config.MODE
  python live/run_live.py --dry-run  -> force DRY_RUN
  python live/run_live.py --paper    -> force PAPER (default)
  python live/run_live.py --live     -> force LIVE (real orders!)
"""
from __future__ import annotations

import os, sys, time, signal, argparse, logging
from datetime import datetime, time as dtime, date, timedelta
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from live import config, state, signal_engine, order_manager, risk, dhan_orders
from live.state import BotState, OpenPosition, load_state, save_state, roll_to_new_day
from src.dhan_client import intraday_history


log = logging.getLogger("runner")


def setup_logging():
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                   logging.FileHandler(config.LOG_PATH)])


def find_atm_option_security_id(underlying: str, opt_type: str, spot: float,
                                 strike_step: int, expiry: str) -> tuple[int, float, int]:
    """Return (security_id, strike, lot_size) by looking up scrip-master.
    Caller passes expiry as 'YYYY-MM-DD'."""
    master_path = os.path.join(ROOT, "data", "scrip-master.csv")
    df = pd.read_csv(master_path, low_memory=False, usecols=[
        "EXCH_ID","INSTRUMENT","UNDERLYING_SYMBOL","SM_EXPIRY_DATE",
        "STRIKE_PRICE","OPTION_TYPE","SECURITY_ID","LOT_SIZE"
    ])
    df["SM_EXPIRY_DATE"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")
    sub = df[(df["EXCH_ID"]=="NSE") & (df["INSTRUMENT"]=="OPTIDX")
              & (df["UNDERLYING_SYMBOL"]==underlying)
              & (df["OPTION_TYPE"]==opt_type)
              & (df["SM_EXPIRY_DATE"]==pd.Timestamp(expiry))]
    if sub.empty:
        raise RuntimeError(f"No options found for {underlying} {opt_type} expiry {expiry}")
    # Round spot to nearest strike step
    target_strike = round(spot / strike_step) * strike_step
    sub = sub.assign(dist=(sub["STRIKE_PRICE"] - target_strike).abs())
    row = sub.sort_values("dist").iloc[0]
    return int(row["SECURITY_ID"]), float(row["STRIKE_PRICE"]), int(row["LOT_SIZE"])


def get_next_expiry(underlying: str, min_dte: int = 1) -> str:
    """Find the next valid expiry from scrip-master."""
    master_path = os.path.join(ROOT, "data", "scrip-master.csv")
    df = pd.read_csv(master_path, low_memory=False, usecols=[
        "EXCH_ID","INSTRUMENT","UNDERLYING_SYMBOL","SM_EXPIRY_DATE"])
    df["SM_EXPIRY_DATE"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")
    sub = df[(df["EXCH_ID"]=="NSE") & (df["INSTRUMENT"]=="OPTIDX")
              & (df["UNDERLYING_SYMBOL"]==underlying)].dropna(subset=["SM_EXPIRY_DATE"])
    expiries = sorted(sub["SM_EXPIRY_DATE"].dt.date.unique())
    today = date.today()
    for e in expiries:
        if (e - today).days >= min_dte:
            return e.strftime("%Y-%m-%d")
    return expiries[-1].strftime("%Y-%m-%d") if expiries else ""


def fetch_option_ltp(option_security_id: int) -> float | None:
    """Fetch latest 5-min bar's close for the option as a quick LTP proxy."""
    now = datetime.now()
    from_ts = now - timedelta(minutes=15)
    df = signal_engine.fetch_bars(option_security_id, "NSE_FNO", "OPTIDX",
                                    from_ts, now, oi=False)
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def fetch_underlying_spot(u: config.Underlying) -> tuple[float | None, float | None]:
    """Return (spot_close, spot_high_since_last_check, spot_low_since_last_check)
    using the last 15 min of 5-min bars."""
    now = datetime.now()
    from_ts = now - timedelta(minutes=20)
    df = signal_engine.fetch_bars(u.spot_security_id, u.spot_exchange, u.spot_instrument,
                                   from_ts, now, oi=False)
    if df.empty:
        return None, None
    last = df.iloc[-1]
    return float(last["close"]), float(last["high"])


def market_hours_now() -> bool:
    now = datetime.now().time()
    return dtime(9, 15) <= now <= dtime(15, 30)


def square_off_time_now() -> bool:
    now = datetime.now().time()
    sq_h, sq_m = map(int, config.PARAMS.square_off.split(":"))
    return now >= dtime(sq_h, sq_m)


def signal_cycle(s: BotState, mode: str):
    """Check each underlying for a new signal on the latest closed bar."""
    for u in config.UNIVERSE:
        # Skip if already in a position on this underlying
        if any(p.underlying == u.name for p in s.open_positions):
            continue
        ok, reason = risk.can_open_new_position(s)
        if not ok:
            log.info(f"risk blocks new positions: {reason}")
            return
        try:
            sig = signal_engine.compute_signal_for(u, config.PARAMS)
        except Exception as e:
            log.error(f"signal compute error {u.name}: {e}")
            continue
        if sig is None:
            continue
        if sig["bar_ts"] == s.last_processed_bar_ts:
            continue  # already processed this bar
        log.info(f"SIGNAL {u.name}: side={sig['side']} d_price={sig['price_chg_pct']*100:.3f}% "
                 f"d_oi={sig['oi_chg_pct']*100:.3f}% vol_z={sig['vol_z']:.2f}  spot={sig['spot_close']:.2f}")

        # Select ATM option
        try:
            expiry = get_next_expiry(u.name, min_dte=config.PARAMS.min_dte)
            opt_sid, opt_strike, opt_lot = find_atm_option_security_id(
                u.name, sig["opt_type"], sig["spot_close"], u.strike_step, expiry)
        except Exception as e:
            log.error(f"option-select error {u.name}: {e}")
            continue
        # Get LTP
        ltp = fetch_option_ltp(opt_sid)
        if ltp is None or ltp <= 0:
            log.warning(f"could not fetch option LTP for sid {opt_sid}")
            continue
        # Size
        premium_per_lot = ltp * opt_lot
        lots = risk.size_for_signal(s, premium_per_lot)
        if lots * premium_per_lot > s.running_equity * 0.95:
            log.warning(f"sizing exceeded 95% of equity, capping")
            lots = max(config.RISK.min_lots_per_trade, int(s.running_equity * 0.95 // premium_per_lot))
        if lots < config.RISK.min_lots_per_trade:
            log.info(f"size says 0 lots — skipping")
            continue
        log.info(f"  -> entering {lots} lots of {u.name} {sig['opt_type']} {opt_strike} exp {expiry}  "
                 f"@ premium {ltp:.2f}  est capital {lots*premium_per_lot:,.0f}")

        pos = order_manager.place_entry_buy_option(
            underlying=u.name, opt_type=sig["opt_type"], side=sig["side"],
            strike=opt_strike, expiry=expiry, option_security_id=opt_sid,
            entry_premium_est=ltp, entry_spot=sig["spot_close"], lots=lots,
            lot_size=opt_lot, mode=mode,
        )
        if pos is not None:
            s.open_positions.append(pos)
            s.day_trades_count += 1
            s.last_processed_bar_ts = sig["bar_ts"]
            log.info(f"  ENTRY DONE. open_positions={len(s.open_positions)}")
            save_state(s, config.STATE_PATH)


def exit_cycle(s: BotState, mode: str, force_square_off: bool = False):
    """Check each open position for exit conditions."""
    p = config.PARAMS
    to_close = []
    for pos in s.open_positions:
        u = next((x for x in config.UNIVERSE if x.name == pos.underlying), None)
        if u is None:
            continue
        spot_close, spot_extreme = fetch_underlying_spot(u)
        if spot_close is None:
            continue

        # Update favorable extreme and check BE/trail
        if pos.side == "long":
            if spot_extreme > pos.spot_high_since_entry:
                pos.spot_high_since_entry = spot_extreme
            fav_pct = (pos.spot_high_since_entry - pos.entry_spot) / pos.entry_spot
            if p.breakeven_trigger_pct > 0 and fav_pct >= p.breakeven_trigger_pct:
                pos.spot_sl = max(pos.spot_sl, pos.entry_spot)
                pos.be_triggered = True
            if p.trail_stop_pct > 0 and fav_pct >= p.breakeven_trigger_pct:
                pos.spot_sl = max(pos.spot_sl, pos.spot_high_since_entry * (1 - p.trail_stop_pct))
        else:
            if spot_extreme < pos.spot_low_since_entry:
                pos.spot_low_since_entry = spot_extreme
            fav_pct = (pos.entry_spot - pos.spot_low_since_entry) / pos.entry_spot
            if p.breakeven_trigger_pct > 0 and fav_pct >= p.breakeven_trigger_pct:
                pos.spot_sl = min(pos.spot_sl, pos.entry_spot)
                pos.be_triggered = True
            if p.trail_stop_pct > 0 and fav_pct >= p.breakeven_trigger_pct:
                pos.spot_sl = min(pos.spot_sl, pos.spot_low_since_entry * (1 + p.trail_stop_pct))

        # Check exit triggers
        exit_reason = None
        if force_square_off:
            exit_reason = "TIME"
        elif pos.side == "long":
            if spot_close <= pos.spot_sl:
                exit_reason = "SL"
            elif spot_close >= pos.spot_tgt:
                exit_reason = "TGT"
        else:
            if spot_close >= pos.spot_sl:
                exit_reason = "SL"
            elif spot_close <= pos.spot_tgt:
                exit_reason = "TGT"
        # OI-flip exit
        if exit_reason is None and p.use_oi_flip_exit:
            try:
                if signal_engine.check_oi_flip(u, p, pos.side):
                    exit_reason = "OI_FLIP"
            except Exception as e:
                log.debug(f"oi-flip check error: {e}")

        if exit_reason:
            ltp = fetch_option_ltp(pos.option_security_id)
            if ltp is None: ltp = pos.entry_premium  # last resort
            closed = order_manager.place_exit_sell_option(
                pos=pos, exit_premium_est=ltp, exit_spot=spot_close,
                exit_reason=exit_reason, mode=mode,
            )
            if closed is not None:
                to_close.append(pos)

    for pos in to_close:
        s.open_positions.remove(pos)
        s.closed_today.append(pos)
        s.day_net_pnl += (pos.net_pnl or 0)
        s.running_equity += (pos.net_pnl or 0)
        order_manager.append_trade_log(pos, mode=mode)
        risk.check_and_halt(s)
        save_state(s, config.STATE_PATH)
        log.info(f"  CLOSED, day P&L now Rs {s.day_net_pnl:,.0f}, equity Rs {s.running_equity:,.0f}")


def main_loop(mode: str):
    log.info(f"=========================================")
    log.info(f"Live trading bot starting in {mode} mode")
    log.info(f"Capital: Rs {config.CAPITAL_RS:,}  Risk per trade: {config.RISK_PCT_PER_TRADE*100:.0f}%")
    log.info(f"Max concurrent positions: {config.RISK.max_open_positions}")
    log.info(f"Daily loss stop: Rs {config.RISK.daily_loss_stop_rs:,}")
    log.info(f"Square-off time: {config.PARAMS.square_off}")
    log.info(f"=========================================")
    if mode == "LIVE":
        log.warning("**** REAL MONEY ORDERS WILL BE PLACED ****")
        log.warning("**** to abort: touch live/KILL ****")

    s = load_state(config.STATE_PATH, default_equity=config.CAPITAL_RS)
    today_str = date.today().strftime("%Y-%m-%d")
    if s.today != today_str:
        log.info(f"New day. Resetting daily counters. Yesterday closed at equity Rs {s.running_equity:,.0f}")
        roll_to_new_day(s, equity_at_open=s.running_equity if s.running_equity > 0 else config.CAPITAL_RS,
                        today_str=today_str)
        save_state(s, config.STATE_PATH)

    # FIX: Remove any stale KILL file from a previous shutdown. Without this,
    # the bot would see the leftover file on its first poll and immediately
    # exit, making it look like "Start doesn't work".
    if os.path.exists(config.KILL_FILE):
        log.warning(f"Stale KILL file found at startup -> removing")
        try:
            os.remove(config.KILL_FILE)
        except Exception as e:
            log.error(f"Could not remove stale KILL file: {e}")

    stop_requested = [False]
    def _sigh(signum, frame):
        log.info(f"Signal {signum} received -> graceful shutdown")
        stop_requested[0] = True
    signal.signal(signal.SIGINT, _sigh)
    signal.signal(signal.SIGTERM, _sigh)

    last_tick = 0
    while True:
        if stop_requested[0]:
            log.info("Shutting down (signal). Open positions left as-is.")
            break
        if os.path.exists(config.KILL_FILE):
            log.warning(f"KILL file detected -> stopping. Open positions: {len(s.open_positions)}")
            # FIX: Remove the KILL file on graceful exit so manual
            # `touch live/KILL` -> bot exits -> file is cleaned automatically.
            # This makes the next start clean even if user didn't use the web UI.
            try:
                os.remove(config.KILL_FILE)
            except Exception as e:
                log.error(f"Could not remove KILL file on exit: {e}")
            break

        # Check market hours
        if not market_hours_now():
            log.debug("outside market hours -- sleeping")
            time.sleep(60)
            continue

        # Square-off enforcement
        if square_off_time_now() and s.open_positions:
            log.warning(f"Square-off time reached. Closing {len(s.open_positions)} open positions.")
            exit_cycle(s, mode, force_square_off=True)
            save_state(s, config.STATE_PATH)
            time.sleep(60)
            continue

        # Run exit checks first (more important — protect open positions)
        if s.open_positions:
            try:
                exit_cycle(s, mode)
            except Exception as e:
                log.exception(f"exit_cycle error: {e}")

        # Then signal scan
        if not s.halted:
            try:
                signal_cycle(s, mode)
            except Exception as e:
                log.exception(f"signal_cycle error: {e}")

        save_state(s, config.STATE_PATH)
        time.sleep(config.SCHED.poll_interval_sec)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Force DRY_RUN mode")
    p.add_argument("--paper", action="store_true", help="Force PAPER mode")
    p.add_argument("--live",   action="store_true", help="Force LIVE mode (real orders)")
    p.add_argument("--reset-state", action="store_true", help="Delete state.json before starting")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.reset_state and os.path.exists(config.STATE_PATH):
        os.remove(config.STATE_PATH)
        print(f"Removed {config.STATE_PATH}")
    mode = config.MODE
    if args.dry_run: mode = "DRY_RUN"
    elif args.paper: mode = "PAPER"
    elif args.live:  mode = "LIVE"

    if mode == "LIVE":
        # Allow the web UI to bypass the interactive confirmation by setting
        # DHAN_LIVE_CONFIRMED=yes in the environment (the web UI does this only
        # after the user types YES in the browser dialog).
        if os.environ.get("DHAN_LIVE_CONFIRMED") != "yes":
            confirm = input(f"\n\n!!!! LIVE MODE — real orders will be placed with real money !!!!\n"
                             f"Capital: Rs {config.CAPITAL_RS:,}\n"
                             f"Type 'YES I UNDERSTAND' to continue: ")
            if confirm.strip() != "YES I UNDERSTAND":
                print("Aborted.")
                sys.exit(1)

    setup_logging()
    issues = config.validate()
    if issues:
        log.error("CONFIG ISSUES:")
        for i in issues: log.error(f"  * {i}")
        sys.exit(1)
    try:
        main_loop(mode)
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
