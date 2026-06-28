# 🌐 Web UI Guide — NIFTY/BANKNIFTY Trading Bot

The web UI gives you a one-page dashboard for **token refresh, bot control,
live P&L, positions, trade history, and log tailing** — all in your browser.

## ⚡ Quick start

```powershell
# 1. From the repo root
python web\app.py
```

Then open **http://127.0.0.1:5005** in your browser.

You'll see the dashboard. On first run, your access token is probably
expired (Dhan tokens last 24 hours), so visit **/token** first to refresh.

## 🔑 Token generation (the killer feature)

Dhan tokens expire every 24 hours. The web UI lets you regenerate them
in two ways:

### Option A — TOTP (one-click, recommended)

One-time setup (5 minutes):
1. Open https://web.dhan.co
2. My Profile → **Access DhanHQ APIs**
3. Enable TOTP — scan the QR code with **Google Authenticator** (or Authy / 1Password)

Daily:
1. Open http://127.0.0.1:5005/token
2. Enter your **Client ID**, **6-digit PIN**, and the **current TOTP** from the authenticator
3. Click **Generate** — a fresh 24-hour token is fetched, saved to `.env`, and the bot picks it up automatically.

### Option B — Paste manually

1. Generate the token in the Dhan portal manually
2. Paste it into the textarea on `/token`
3. Click **Save Token**

Either way, the token is written to `.env` atomically. No restart needed.

## 📊 Dashboard

`http://127.0.0.1:5005/`

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

`http://127.0.0.1:5005/trades`

Shows the last 200 closed trades from `live/trades.csv`, latest first.
Each row: entry time, exit time, instrument, side, strike, lots, entry/exit
premium, spot move, exit reason, net P&L.

## ⚙️ Config page

`http://127.0.0.1:5005/config`

**Read-only view** of `live/config.py`. To modify a value, edit the file
in a text editor and restart both the web app and the bot.

## 📜 Logs page

`http://127.0.0.1:5005/logs`

Live tail of `live/bot.log`. Refreshes every 5 seconds, with last 50/200/500
line options. Auto-scrolls to the bottom unless you've scrolled up to
inspect history.

## 🔐 Security

**This UI has no authentication.** It binds to `127.0.0.1` (localhost) by
default, so only you can access it. **Do not expose to the internet** —
anyone who can reach the port can trigger live orders.

If you need remote access, use **SSH tunneling**:
```bash
ssh -L 5005:127.0.0.1:5005 user@your-vps
```
Then open http://127.0.0.1:5005 on your local browser. Traffic is encrypted
end-to-end via SSH.

## 🚨 What the UI does NOT do

- ❌ Modify `live/config.py` (read-only) — edit it in a text editor and restart
- ❌ Place ad-hoc manual orders — use Dhan app for that
- ❌ Cancel pending orders — the strategy uses market orders, no pending orders to cancel
- ❌ Show backtest charts — those are static PNGs in `results/`

## 📡 API endpoints (for scripting)

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

## 🚀 Running on a VPS / always-on machine

If you want the bot running 24/7 (or rather 9:15-15:30 IST daily):

1. Deploy on a small VPS (₹500/month: e.g., Hetzner CX11, AWS t3.micro, etc.)
2. Use **systemd** (Linux) or **NSSM** (Windows) to keep `web/app.py` running as a service
3. Use **cron** to refresh the token automatically each morning:
   - Could use a small Python script that calls `/api/token/generate` with stored TOTP secret (security concern — store TOTP secret encrypted)
   - Or just open the web UI each morning manually

## ✅ Daily checklist

Each trading morning:

- [ ] Open http://127.0.0.1:5005/token
- [ ] Enter Client ID + PIN + current TOTP
- [ ] Click Generate (token good for 24 hours)
- [ ] On dashboard, pick PAPER or LIVE mode
- [ ] Click Start
- [ ] Check `/logs` shows the bot polling successfully
- [ ] Check back periodically through the day — dashboard auto-refreshes
