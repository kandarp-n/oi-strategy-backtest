"""Web UI for the NIFTY/BANKNIFTY OI-momentum trading bot.

Routes:
  /login            -> login form
  /logout           -> logout
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

Authentication:
  - All routes require login except /login, /logout, /healthz, /static/*
  - Credentials via env vars: WEB_UI_USERNAME, WEB_UI_PASSWORD
  - If WEB_UI_PASSWORD is not set, a random one is generated at startup and
    printed to stdout (you must save it -- it changes on every restart).
  - Failed login attempts are rate-limited (5 / minute / IP).
"""
from __future__ import annotations

import os, sys, json, secrets, hmac, time
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict, deque
from flask import (Flask, render_template, request, jsonify, redirect,
                    url_for, abort, session, make_response, g)
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from web import auth_token, bot_manager
from live import config as live_config
from live import state as live_state

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# ============================================================================
# === Auth setup ============================================================
# ============================================================================

WEB_USERNAME = os.environ.get("WEB_UI_USERNAME", "admin")
_provided_password = os.environ.get("WEB_UI_PASSWORD")
if _provided_password:
    WEB_PASSWORD = _provided_password
    _PASSWORD_AUTO = False
else:
    WEB_PASSWORD = secrets.token_urlsafe(16)
    _PASSWORD_AUTO = True

# Persistent secret key across restarts: store on disk in web/.secret
_SECRET_PATH = os.path.join(ROOT, "web", ".secret")
if os.path.exists(_SECRET_PATH):
    with open(_SECRET_PATH, "rb") as f:
        app.secret_key = f.read()
else:
    app.secret_key = secrets.token_bytes(32)
    with open(_SECRET_PATH, "wb") as f:
        f.write(app.secret_key)

# Session config — same-site lax, secure flag set conditionally
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

# Rate limiting on /login (5 failures per minute per IP)
_login_attempts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))
_LOGIN_WINDOW = 60.0
_LOGIN_LIMIT = 5


def _client_ip() -> str:
    # Honour standard reverse-proxy header but only when explicitly enabled
    if os.environ.get("WEB_TRUST_XFF") == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _is_authenticated() -> bool:
    return session.get("authed") is True and session.get("user") == WEB_USERNAME


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _is_authenticated():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = _client_ip()
    # Rate limit
    now = time.monotonic()
    q = _login_attempts[ip]
    while q and now - q[0] > _LOGIN_WINDOW:
        q.popleft()
    if request.method == "POST":
        if len(q) >= _LOGIN_LIMIT:
            return render_template("login.html", error="Too many attempts. Wait 60s.",
                                    next_=request.form.get("next") or "/"), 429
        u = (request.form.get("username") or "").strip()
        p = (request.form.get("password") or "")
        # Constant-time compare
        ok_u = hmac.compare_digest(u.encode("utf-8"), WEB_USERNAME.encode("utf-8"))
        ok_p = hmac.compare_digest(p.encode("utf-8"), WEB_PASSWORD.encode("utf-8"))
        if ok_u and ok_p:
            session.clear()
            session["authed"] = True
            session["user"] = WEB_USERNAME
            session["login_ts"] = datetime.now().isoformat()
            session.permanent = True
            nxt = request.form.get("next") or "/"
            if not nxt.startswith("/"):
                nxt = "/"
            return redirect(nxt)
        else:
            q.append(now)
            return render_template("login.html", error="Invalid credentials.",
                                    next_=request.form.get("next") or "/"), 401
    return render_template("login.html", error=None,
                            next_=request.args.get("next") or "/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================================
# === HTML routes ============================================================
# ============================================================================

@app.route("/")
@require_auth
def dashboard():
    return render_template("dashboard.html")


@app.route("/token")
@require_auth
def token_page():
    info = auth_token.get_current_token_info()
    return render_template("token.html", token=info)


@app.route("/trades")
@require_auth
def trades_page():
    path = live_config.TRADE_LOG_PATH
    trades = []
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            trades = df.tail(200).iloc[::-1].to_dict(orient="records")
        except Exception:
            trades = []
    return render_template("trades.html", trades=trades)


@app.route("/config")
@require_auth
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
@require_auth
def logs_page():
    return render_template("logs.html")


# ============================================================================
# === API: status / data =====================================================
# ============================================================================

@app.route("/api/status")
@require_auth
def api_status():
    bot = bot_manager.get_status()
    token = auth_token.get_current_token_info()
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
@require_auth
def api_logs():
    n = int(request.args.get("n", 200))
    return jsonify({"log": bot_manager.tail_log(n_lines=n)})


# ============================================================================
# === API: token management =================================================
# ============================================================================

@app.route("/api/token/generate", methods=["POST"])
@require_auth
def api_token_generate():
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
@require_auth
def api_token_paste():
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
@require_auth
def api_bot_start():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "PAPER").upper()
    if mode == "LIVE":
        if data.get("confirm") != "YES":
            return jsonify({"ok": False,
                            "error": "Pass {\"confirm\": \"YES\"} in JSON to start LIVE mode."}), 400
        token = auth_token.get_current_token_info()
        if not token["token_present"] or token["expired"]:
            return jsonify({"ok": False,
                            "error": "Access token is missing or expired. Refresh from /token first."}), 400
    res = bot_manager.start(mode)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/bot/stop", methods=["POST"])
@require_auth
def api_bot_stop():
    res = bot_manager.stop(graceful=True)
    return jsonify(res), (200 if res.get("ok") else 400)


# ============================================================================
# === Public endpoints (no auth) ============================================
# ============================================================================

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": datetime.now().isoformat(timespec="seconds")})


# ============================================================================
# === Entry point ============================================================
# ============================================================================

def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


if __name__ == "__main__":
    import argparse, socket
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address (default: 0.0.0.0 = all interfaces).")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "(unknown)"

    print()
    print("=" * 72)
    print("Trading bot web UI starting")
    print("=" * 72)
    if _is_loopback(args.host):
        print(f"  Listening on http://{args.host}:{args.port}  (localhost only)")
    else:
        print(f"  Listening on http://{args.host}:{args.port}")
        print(f"  Reachable on: http://{local_ip}:{args.port}  (LAN)")
        print(f"  Reachable on: http://<your-public-ip>:{args.port}  (if firewall allows)")
        print()
        print(f"  *** WARNING: bound to a non-loopback interface.")
        print(f"  *** Anyone who reaches this port can attempt to log in.")
        print(f"  *** Credentials below MUST be strong. HTTPS is recommended.")
    print()
    print(f"  Login:")
    print(f"    Username:  {WEB_USERNAME}")
    if _PASSWORD_AUTO:
        print(f"    Password:  {WEB_PASSWORD}    <- AUTO-GENERATED; save this!")
        print(f"               (set env var WEB_UI_PASSWORD to use a fixed password)")
    else:
        print(f"    Password:  (loaded from env var WEB_UI_PASSWORD)")
    print()
    print(f"  Failed-login rate limit: {_LOGIN_LIMIT} per {int(_LOGIN_WINDOW)}s per IP")
    print()
    if not _is_loopback(args.host):
        print(f"  STRONGLY RECOMMENDED for non-loopback:")
        print(f"    1. Use a strong password via WEB_UI_PASSWORD env var")
        print(f"    2. Put behind HTTPS reverse proxy (caddy/nginx)")
        print(f"    3. Restrict source IPs at firewall / cloud security group")
        print(f"    4. Or, prefer: SSH tunnel + bind to 127.0.0.1")
    print("=" * 72)
    print()
    app.run(host=args.host, port=args.port, debug=args.debug)
