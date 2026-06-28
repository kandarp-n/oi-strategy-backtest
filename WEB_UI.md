# 🌐 Web UI Guide — NIFTY/BANKNIFTY Trading Bot

The web UI gives you a one-page dashboard for **token refresh, bot control,
live P&L, positions, trade history, and log tailing** — accessible from any
browser on your network.

## ⚡ Quick start

```powershell
# Set a strong password (otherwise one is auto-generated and printed at startup)
$env:WEB_UI_PASSWORD = "your-strong-password-here"

# Optional: custom username (default: admin)
$env:WEB_UI_USERNAME = "admin"

# Start the UI (binds to all interfaces by default)
python web\app.py
```

Then open in any browser on your network:
- Locally: **http://127.0.0.1:5005**
- From LAN: **http://<your-machine-ip>:5005** (the URL is printed at startup)

The first thing you'll see is a login page. Use the credentials shown in
the server console.

## 🔐 Authentication

All routes require a valid session cookie, **except** `/login`, `/logout`,
and `/healthz`.

### Credentials

| Env var | Default | Notes |
|---|---|---|
| `WEB_UI_USERNAME` | `admin` | Optional |
| `WEB_UI_PASSWORD` | (auto-generated) | If unset, a random 16-char URL-safe token is printed to console on startup |

**Always set `WEB_UI_PASSWORD` to a strong value when binding to non-loopback.**

### Failed-login rate limit

5 failed attempts per IP per 60 seconds → 429 response. Successful login
resets the counter.

### Session timeout

12 hours (configurable via `app.config["PERMANENT_SESSION_LIFETIME"]`).

## 🌍 Network exposure

The default bind is **`0.0.0.0`** (all interfaces). Recommended deployments:

| Scenario | Recommendation |
|---|---|
| **Local laptop only** | `python web\app.py --host 127.0.0.1` |
| **LAN (home/office)** | Default `0.0.0.0` is fine + strong `WEB_UI_PASSWORD` |
| **Public internet (VPS)** | Put behind HTTPS reverse proxy (Caddy / nginx). NEVER expose port 5005 raw — passwords would be in plaintext. |

### Recommended secure remote setup (preferred)

**SSH tunnel** — no public port, fully encrypted, no extra setup:

```bash
ssh -L 5005:127.0.0.1:5005 user@your-vps
```

Then open http://127.0.0.1:5005 in your local browser.

### HTTPS reverse proxy (Caddy example)

If you must expose the port to the internet, put Caddy in front:

```
trader.example.com {
    reverse_proxy 127.0.0.1:5005
}
```

Then set `WEB_TRUST_XFF=1` so the UI logs the real client IP instead of
Caddy's local IP for rate limiting.

## 🔑 Token generation

Dhan tokens expire every 24 hours. The web UI lets you regenerate them
in two ways:

### Option A — TOTP (one-click, recommended)

One-time setup (5 minutes):
1. Open https://web.dhan.co
2. My Profile → **Access DhanHQ APIs**
3. Enable TOTP — scan the QR code with **Google Authenticator** (or Authy / 1Password)

Daily:
1. Open `/token`
2. Enter your **Client ID**, **6-digit PIN**, and the **current TOTP** from the authenticator
3. Click **Generate** — a fresh 24-hour token is fetched, saved to `.env`, and the bot picks it up automatically.

### Option B — Paste manually

1. Generate the token in the Dhan portal manually
2. Paste it into the textarea on `/token`
3. Click **Save Token**

Either way, the token is written to `.env` atomically. No restart needed.

## 📊 Dashboard

`/`

| Widget | Shows |
|---|---|
| **Bot Status** | Running / Stopped, mode, PID, since when |
| **Today's P&L** | Net realised P&L for the trading day |
| **Running Equity** | Live equity (starting + day P&L) |
| **Dhan Balance** | Available cash in your Dhan account (live API call) |
| **Open Positions** | Symbol, side, strike, lots, entry, current SL/TGT, BE-triggered flag |
| **Closed Today** | All trades closed today with exit reason + P&L |
| **Bot Control** | Mode dropdown + Start/Stop buttons |

Auto-refreshes every 5 seconds.

## 🎛 Bot Control

From the dashboard, pick a mode and click **Start**:

- **DRY_RUN** — logs signals only, no orders
- **PAPER** — full simulation with simulated fills + 0.5% slippage
- **LIVE** — real money orders (requires browser confirmation)

The Stop button uses the graceful KILL-file mechanism: it asks the bot to
finish what it's doing and exit cleanly. Open positions are left as-is —
the bot will not square-off when stopped. You must manually close them in
Dhan if needed.

The bot runs as a separate `python live/run_live.py` subprocess and
**survives the web UI shutting down** (detached on Windows). To kill the
bot independently of the UI, look at the PID stored in `web/bot.pid`.

## 📋 Trades page

`/trades`

Shows the last 200 closed trades from `live/trades.csv`, latest first.
Each row: entry time, exit time, instrument, side, strike, lots, entry/exit
premium, spot move, exit reason, net P&L.

## ⚙️ Config page

`/config`

**Read-only view** of `live/config.py`. To modify a value, edit the file
in a text editor and restart both the web app and the bot.

## 📜 Logs page

`/logs`

Live tail of `live/bot.log`. Refreshes every 5 seconds.

## 📡 API endpoints (for scripting)

All API endpoints (except `/healthz`) require a valid session cookie. Log
in with a browser, then export the `session` cookie value for use in
external clients:

```python
import requests
sess = requests.Session()
sess.post("http://your-host:5005/login",
           data={"username":"admin","password":"...","next":"/"})
print(sess.get("http://your-host:5005/api/status").json())
```

| Method | Path | Body | Returns |
|---|---|---|---|
| GET  | `/healthz` | – | `{ok, ts}` |
| GET  | `/api/status` | – | full bot + token + state + funds JSON |
| GET  | `/api/logs?n=200` | – | `{log: "<last N lines>"}` |
| POST | `/api/token/generate` | `{client_id, pin, totp}` | `{ok, client_name, expiry_time}` |
| POST | `/api/token/paste` | `{token, client_id?}` | `{ok, expiry}` |
| POST | `/api/bot/start` | `{mode, confirm?}` | `{ok, pid, mode}` |
| POST | `/api/bot/stop` | – | `{ok, method}` |

## 🐛 Troubleshooting

### "ImportError: cannot import name 'flask'"
```powershell
pip install flask
```

### "Address already in use"
Web UI port 5005 is taken. Either kill the other process or run on a different port:
```powershell
python web\app.py --port 5006
```

### "Bot won't start" after clicking Start
Open `live/bot.log` (or visit `/logs`) to see the error. Common causes:
- Access token expired — refresh from `/token`
- Static IP not whitelisted on Dhan for orders
- Stale `web/bot.pid` from a previous crash — delete it manually

### Token generation returns "Invalid"
- TOTP code is time-sensitive — must be entered within 30 seconds of generation
- Make sure your device clock is synced with NTP
- PIN must be your Dhan PIN (4-6 digits)

### Forgot the auto-generated password
Restart the web app — it'll print a new one. Or set `WEB_UI_PASSWORD` env var.

### "Too many attempts. Wait 60s."
You hit the failed-login rate limit. Wait 60 seconds and try again.

## 🚀 Running on a VPS / always-on machine

If you want the bot running 24/7 (or rather 9:15-15:30 IST daily):

1. Deploy on a small VPS (₹500/month: e.g., Hetzner CX11, AWS t3.micro, etc.)
2. Use **systemd** (Linux) or **NSSM** (Windows) to keep `web/app.py` running as a service
3. Set `WEB_UI_PASSWORD` as a strong env var
4. **Either** SSH-tunnel for access, **or** put behind Caddy/nginx with TLS
5. Optional: cron a daily token refresh (security tradeoff — would need to store TOTP secret)

## ✅ Daily checklist

Each trading morning:

- [ ] Open the UI at your configured URL
- [ ] Log in
- [ ] Go to `/token`, enter Client ID + PIN + current TOTP
- [ ] Click Generate (token good for 24 hours)
- [ ] On dashboard, pick PAPER or LIVE mode
- [ ] Click Start
- [ ] Check `/logs` shows the bot polling successfully
- [ ] Check back periodically through the day — dashboard auto-refreshes
