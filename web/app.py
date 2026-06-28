"""Web UI for the NIFTY/BANKNIFTY OI-momentum trading bot.

Routes:
  /                 -> dashboard (status, P&L, positions, equity)
  /token            -> token manager (TOTP-based generation, paste fallback)
  /trades           -> closed trade history
  /config           -> view current config (read-only)
  /logs             -> live log tail (auto-refresh)
  POST /api/token/generate    -> generate fresh access token via TOTP
  POST /api/token/paste       -> save a manually-pasted token
  POST /api/bot/start         -> start bot in given mode
  POST /api/bot/stop          -> stop bot (graceful)
  GET  /api/status            -> JSON: bot + positions + day PnL + equity
  GET  /api/logs              -> JSON: last N log lines
"""
from __future__ import annotations

import os, sys, json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from web import auth_token, bot_manager
from live import config as live_config
from live import state as live_state

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024


# ============================================================================
# === HTML routes ============================================================
# ============================================================================

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/token")
def token_page():
    info = auth_token.get_current_token_info()
    return render_template("token.html", token=info)


@app.route("/trades")
def trades_page():
    path = live_config.TRADE_LOG_PATH
    trades = []
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            trades = df.tail(200).iloc[::-1].to_dict(orient="records")  # most recent first
        except Exception as e:
            trades = []
    return render_template("trades.html", trades=trades)


@app.route("/config")
def config_page():
    cfg = {
        "mode": live_config.MODE,
        "capital_rs": live_config.CAPITAL_RS,
        "risk_pct_per_trade": live_config.RISK_PCT_PER_TRADE,
        "max_concurrent_positions": live_config.MAX_CONCURRENT_POSITIONS,
        "max_lots_per_trade": live_config.MAX_LOTS_PER_TRADE,
        "universe": [
            {"name": u.name, "spot_sid": u.spot_security_id,
             "fut_sid": u.front_fut_security_id, "lot_size": u.lot_size, "strike_step": u.strike_step}
            for u in live_config.UNIVERSE
        ],
        "params": {
            "price_pct": live_config.PARAMS.price_pct,
            "oi_pct": live_config.PARAMS.oi_pct,
            "vol_z": live_config.PARAMS.vol_z,
            "sl_pct": live_config.PARAMS.sl_pct,
            "tgt_pct": live_config.PARAMS.tgt_pct,
            "breakeven_trigger_pct": live_config.PARAMS.breakeven_trigger_pct,
            "trail_stop_pct": live_config.PARAMS.trail_stop_pct,
            "entry_start": live_config.PARAMS.entry_start,
            "entry_end": live_config.PARAMS.entry_end,
            "square_off": live_config.PARAMS.square_off,
            "use_oi_flip_exit": live_config.PARAMS.use_oi_flip_exit,
            "require_trend_align": live_config.PARAMS.require_trend_align,
            "avoid_lunch": live_config.PARAMS.avoid_lunch,
        },
        "risk": {
            "daily_loss_stop_rs": live_config.RISK.daily_loss_stop_rs,
            "daily_profit_target_rs": live_config.RISK.daily_profit_target_rs,
            "max_trades_per_day": live_config.RISK.max_trades_per_day,
            "emergency_stop_equity_frac": live_config.RISK.emergency_stop_equity_frac,
        },
    }
    issues = live_config.validate()
    return render_template("config.html", cfg=cfg, issues=issues)


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


# ============================================================================
# === API: status / data =====================================================
# ============================================================================

@app.route("/api/status")
def api_status():
    bot = bot_manager.get_status()
    token = auth_token.get_current_token_info()
    # Read state
    state_obj = None
    try:
        s = live_state.load_state(live_config.STATE_PATH, default_equity=live_config.CAPITAL_RS)
        state_obj = {
            "today": s.today,
            "day_net_pnl": round(s.day_net_pnl, 2),
            "day_trades_count": s.day_trades_count,
            "running_equity": round(s.running_equity, 2),
            "halted": s.halted,
            "halt_reason": s.halt_reason,
            "open_positions": [_pos_to_dict(p) for p in s.open_positions],
            "closed_today": [_pos_to_dict(p) for p in s.closed_today],
        }
    except Exception as e:
        state_obj = {"error": str(e)}
    # Funds
    funds = None
    try:
        from live import dhan_orders
        funds = dhan_orders.get_funds()
    except Exception as e:
        funds = {"error": str(e)}
    return jsonify({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "bot": bot,
        "token": token,
        "state": state_obj,
        "funds": funds,
        "mode_config": live_config.MODE,
    })


def _pos_to_dict(p) -> dict:
    return {
        "corr_id": p.correlation_id,
        "underlying": p.underlying,
        "opt_type": p.opt_type,
        "side": p.side,
        "strike": p.strike,
        "expiry": p.expiry,
        "lots": p.lots,
        "qty": p.qty,
        "entry_ts": p.entry_ts,
        "entry_premium": round(p.entry_premium, 2),
        "entry_spot": round(p.entry_spot, 2),
        "spot_sl": round(p.spot_sl, 2),
        "spot_tgt": round(p.spot_tgt, 2),
        "be_triggered": p.be_triggered,
        "exit_ts": p.exit_ts,
        "exit_premium": round(p.exit_premium, 2) if p.exit_premium else None,
        "exit_reason": p.exit_reason,
        "net_pnl": round(p.net_pnl, 2) if p.net_pnl is not None else None,
    }


@app.route("/api/logs")
def api_logs():
    n = int(request.args.get("n", 200))
    return jsonify({"log": bot_manager.tail_log(n_lines=n)})


# ============================================================================
# === API: token management =================================================
# ============================================================================

@app.route("/api/token/generate", methods=["POST"])
def api_token_generate():
    """TOTP-based token generation. Body: {client_id, pin, totp}."""
    data = request.get_json(force=True, silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    pin = (data.get("pin") or "").strip()
    totp = (data.get("totp") or "").strip()
    if not (client_id and pin and totp):
        return jsonify({"ok": False, "error": "client_id, pin, totp are all required"}), 400
    try:
        resp = auth_token.generate_token(client_id, pin, totp)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    access_token = resp.get("accessToken")
    if not access_token:
        return jsonify({"ok": False, "error": "no accessToken in response", "raw": resp}), 400
    auth_token.update_env_with_token(access_token, client_id=client_id)
    # Reload dhan_client (so the new token is picked up live)
    try:
        import importlib
        from src import dhan_client as dc
        importlib.reload(dc)
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "client_name": resp.get("dhanClientName"),
        "expiry_time": resp.get("expiryTime"),
        "token_preview": access_token[:24] + "..." + access_token[-12:],
    })


@app.route("/api/token/paste", methods=["POST"])
def api_token_paste():
    """Save a manually-pasted token. Body: {token, client_id (optional)}."""
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()
    client_id = (data.get("client_id") or "").strip() or None
    if not token:
        return jsonify({"ok": False, "error": "token is required"}), 400
    try:
        auth_token.update_env_with_token(token, client_id=client_id)
        try:
            import importlib
            from src import dhan_client as dc
            importlib.reload(dc)
        except Exception:
            pass
        return jsonify({"ok": True, "expiry": auth_token.decode_token_expiry(token)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================================
# === API: bot control ======================================================
# ============================================================================

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "PAPER").upper()
    if mode == "LIVE":
        # Require an explicit confirmation field
        if data.get("confirm") != "YES":
            return jsonify({"ok": False,
                            "error": "Pass {\"confirm\": \"YES\"} in JSON to start LIVE mode."}), 400
        # Also require valid (non-expired) token
        token = auth_token.get_current_token_info()
        if not token["token_present"] or token["expired"]:
            return jsonify({"ok": False,
                            "error": "Access token is missing or expired. Refresh from /token first."}), 400
    res = bot_manager.start(mode)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    res = bot_manager.stop(graceful=True)
    return jsonify(res), (200 if res.get("ok") else 400)


# ============================================================================
# === Health check ==========================================================
# ============================================================================

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1", help="Default localhost only — DO NOT expose this to the internet without auth")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print(f"\n{'='*60}")
    print(f"Trading bot web UI starting on http://{args.host}:{args.port}")
    print(f"{'='*60}")
    print(f"Pages:")
    print(f"  /         Dashboard (status, P&L, positions)")
    print(f"  /token    Generate/refresh Dhan API access token")
    print(f"  /trades   Closed trade history")
    print(f"  /config   View bot configuration")
    print(f"  /logs     Live log tail")
    print(f"\nWARNING: this UI has no authentication. Run only on localhost.")
    print(f"{'='*60}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)
