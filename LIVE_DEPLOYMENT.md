# 🚀 NIFTY/BANKNIFTY OI-Momentum — Live Deployment Guide

This document walks you through deploying the OI-momentum strategy live
on Dhan. Read all sections before flipping the switch to LIVE.

## ⚠️ Read this first

1. **Past returns do not guarantee future returns.** The backtest covered
   ~3 months of OI data. The strategy could underperform live. **Trade with
   capital you can afford to lose.**
2. **Always start in PAPER mode** for at least 5 trading days. Verify the
   bot's signals, order flow, P&L, and SL/TGT behaviour match your
   expectations.
3. **Realistic expectation**: +1–4% per week on the deployed capital, with
   drawdown days down to –10% possible. Sharpe ~3 (good but volatile).
4. **The strategy needs OI data**, which means a current-month index
   future. The bot auto-uses the current Jun-26 contracts. **Update
   `front_fut_security_id` in `live/config.py` on the last Thursday of each
   month** when contracts roll.

## 📦 What's in `live/`

| File | Purpose |
|---|---|
| `config.py` | **Edit this** — capital, sizing, risk limits, mode |
| `dhan_orders.py` | REST client for orders / funds / quotes |
| `signal_engine.py` | Live signal generation on closed 5-min bars |
| `order_manager.py` | Places orders in LIVE / simulates in PAPER / logs in DRY_RUN |
| `risk.py` | Daily loss stop, max positions, emergency stop |
| `state.py` | Persistent state (JSON) — survives bot restarts |
| `run_live.py` | Main runner (the entry point) |
| `state.json` | Auto-created — current bot state |
| `trades.csv` | Auto-created — closed-trade audit log |
| `bot.log` | Auto-created — full bot log |
| `KILL` | Touch this file to halt the bot gracefully |

## ✅ Pre-flight checklist

Before EVERY deployment day:

- [ ] `.env` has a fresh Dhan access token (tokens expire daily ~8 AM IST)
- [ ] `data/scrip-master.csv` is from today — re-download via `python src/universe.py`
- [ ] `live/config.py` has current-month future security IDs:
  - NIFTY: lookup current month future from `data/scrip-master.csv`
  - BANKNIFTY: same
- [ ] You've validated config: `python live/config.py`
- [ ] You've verified API connectivity: `python live/dhan_orders.py`
  (should print your fund balance)
- [ ] Static IP whitelisted for orders on Dhan portal
  (settings → DhanHQ Trading APIs → Static IP)
- [ ] You have at least 2× the configured capital in your Dhan account
  (for buffer + margin oddities)

## 🎯 Deployment progression

### Stage 1 — DRY_RUN (1 day)

```powershell
python live\run_live.py --dry-run
```

What this does:
- Connects to Dhan, fetches signals every poll cycle
- Logs every signal that *would* have fired
- **No orders placed**, no P&L tracked
- Purpose: validate signal frequency matches your expectations

What to verify in `bot.log`:
- Bot wakes every 30 seconds
- Each underlying gets a signal check on each new 5-min bar
- 3-8 signals per day across NIFTY+BANKNIFTY (matches backtest)

### Stage 2 — PAPER (5 trading days)

```powershell
python live\run_live.py --paper
```

What this does:
- Full simulation: signals → entries → exits with all logic active
- "Fills" at the last 5-min bar's close + 0.5% slippage
- Tracks running P&L in `state.json`
- Writes every closed trade to `trades.csv`

What to verify:
- Daily P&L roughly tracks the backtest profile (–₹3K to +₹6K most days)
- Exit reasons are diverse (SL, TGT, OI_FLIP, TIME)
- No bugs in BE/trail logic — check `state.json` mid-day
- Square-off works at 15:15

### Stage 3 — LIVE on small capital (2 weeks)

Set capital to **₹50K–₹1L** (smaller than your eventual size), then:

```powershell
python live\run_live.py --live
```

The runner will require you to type `YES I UNDERSTAND` before starting.

Monitor closely on day 1:
- First trade placed correctly (check Dhan app)
- Exit triggers fire when expected
- P&L in `trades.csv` matches Dhan's order book

### Stage 4 — Scale up

After 2 weeks of profitable live trading on small capital, **scale up
gradually** (₹50K increments per week). Don't 10× your size in one step.

## 🚦 Daily operations

### Morning routine (9:00 AM)

```powershell
# 1. Refresh access token if expired
# (edit .env with new DHAN_ACCESS_TOKEN from Dhan portal)

# 2. Re-download scrip-master if it's first trading day of the month
python src\universe.py

# 3. Update current-month futures IDs in live/config.py if rolled

# 4. Validate config
python live\config.py

# 5. Start the bot (in a screen / tmux / Windows Task)
python live\run_live.py --live
```

### During market hours

- **Watch the log**: `Get-Content live\bot.log -Wait`
- **Check open positions**: `Get-Content live\state.json`
- **Manual exit**: stop the bot, manually close positions on Dhan, then restart

### To halt the bot gracefully

```powershell
# Option A: Ctrl+C in the terminal running the bot
# Option B: Create the KILL file (works from any terminal)
New-Item live\KILL -ItemType File
```

Both methods leave open positions intact. **You must manually close them
from the Dhan app** if needed.

### Square-off

The bot automatically squares off all open positions at **15:15 IST**
(configurable in `config.PARAMS.square_off`). You should still verify in
the Dhan app that all MIS positions are closed before 15:25.

## 🔧 Tuning knobs

In `live/config.py`:

| Knob | Effect | Conservative | Aggressive |
|---|---|---|---|
| `CAPITAL_RS` | Account allocation | ₹50,000 | ₹5,00,000 |
| `RISK_PCT_PER_TRADE` | % of equity per trade | 0.15 | 0.40 |
| `MAX_CONCURRENT_POSITIONS` | Cap on parallel trades | 2 | 5 |
| `PARAMS.price_pct` | Min 5-min price move to trigger | 0.0015 | 0.0005 |
| `PARAMS.oi_pct` | Min OI buildup | 0.002 | 0.0003 |
| `PARAMS.sl_pct` | Stop-loss (% of spot) | 0.003 | 0.005 |
| `PARAMS.tgt_pct` | Target (% of spot) | 0.006 | 0.012 |
| `RISK.daily_loss_stop_rs` | Day loss cap | ₹3,000 | ₹15,000 |

## 🐛 Troubleshooting

### "ImportError" or "ModuleNotFoundError"
Run from the repository root: `python live/run_live.py --paper` (not `cd live && python run_live.py`).

### Dhan API "Invalid request"
- Access token expired: regenerate from Dhan portal, update `.env`
- Wrong `securityId`: scrip-master needs refresh
- IP not whitelisted: log into Dhan portal → Trading APIs → Static IP Whitelist

### Bot doesn't fire signals
- Check `bot.log` for the per-bar diagnostic lines
- Are thresholds too tight for current market regime?
- Is the bot in the trading window (09:45–14:30, skipping 12:00–13:00)?

### Order placed but no fill
- Check Dhan app order book for rejection reason
- Common: insufficient margin, freeze quantity exceeded (use slicing API)

## 🚨 Emergency procedures

### Bot crashed mid-trade

1. Check `state.json` for last known open positions
2. Open Dhan app, verify what's actually in your portfolio
3. If discrepancy: manually reconcile in the app
4. Delete `state.json` and restart with `python live/run_live.py --paper`
   (NEVER restart in `--live` mode if positions are mismatched)

### Market crash / extreme volatility

The bot will hit daily-loss-stop and halt automatically. If you want
to force-halt before that:

```powershell
New-Item live\KILL -ItemType File
```

Then manually close all positions in the Dhan app.

### Network outage during trade

If the bot loses connection while in a position:
- It will retry on next poll cycle (default 30s)
- After 5 failed cycles, log a warning but stay running
- Your SL/TGT may not fire — **always set hard SL on Dhan side too**
  (recommended: add stop-loss orders manually after entry — Phase 2 feature)

## 📊 Monitoring suggestions

Set up a simple dashboard:
- `Get-Content live\bot.log -Wait -Tail 20` (live tail)
- Import `live\trades.csv` into Excel/Google Sheets for daily review
- After every trading day, run `python src\charts.py final` to update charts

## 📝 Audit logs

Every closed trade is appended to `live/trades.csv` with:
- Mode (LIVE / PAPER / DRY_RUN)
- Correlation ID + Dhan order ID
- Underlying, option type, strike, expiry, security ID
- Quantity, lots
- Entry timestamp, premium, spot
- Exit timestamp, premium, spot, reason
- Net P&L (after costs)

This is your ground truth for tax filing and performance review.

## 🆘 Support

This is a personal-research project — there is no SLA. If something goes
wrong:
1. Check `bot.log` for the error
2. Check Dhan API docs: https://dhanhq.co/docs/v2/
3. **Manually close positions in the Dhan app if uncertain**

**The system is provided as-is. You alone are responsible for your trades.**
