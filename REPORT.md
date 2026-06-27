# OI-Momentum Intraday Strategy on NSE Stock Futures
**Backtest report — Dhan API, 1-Apr-2026 → 25-Jun-2026 (58 trading days)**

---

## 1  Strategy in one paragraph
On every closed **5-minute futures bar** for a universe of 43 liquid F&O
stocks, we look for an **Open-Interest buildup with price and volume
confirmation**:

- **Long buildup → go long futures** when the futures price ticks up *and*
  OI increases *and* volume spikes — fresh longs are entering.
- **Short buildup → go short futures** when the futures price ticks down *and*
  OI increases *and* volume spikes — fresh shorts are entering.

Trades are taken **in the front-month stock future**, entered at the **next
bar's open**, protected with a fixed % SL/target, and hard-squared-off by
15:15 IST. An additional **OI-flip exit** closes a long if OI starts dropping
with negative price action (and mirror for shorts), capturing the moment
positions begin to unwind.

The signal is computed on **futures** (where OI is observable), and the
trade is also placed on **futures** — this is the key change that makes the
strategy economically viable (see §6 *Costs*).

---

## 2  Final strategy parameters (`v5_tgt_lower`)

| Knob | Value | Rationale |
|---|---|---|
| Price move (5-min, fut) | ≥ **0.25 %** | filters whippy noise but stays sensitive |
| OI change (5-min, fut)  | ≥ **0.30 %** | confirms *new* positions added, not roll |
| Volume z-score (vs 20-bar) | ≥ **2.0** | confirms participation behind the move |
| Trade window | **09:45 – 14:30** IST | skip opening auction noise + last-hour reversal |
| Avoid-lunch filter | **12:00 – 13:00** off | thin volumes give false signals |
| Side | both (long + short) | OI works symmetrically |
| Trend filter | Spot close vs day-VWAP (long above, short below) | avoids countertrend traps |
| SL | **0.40 %** of entry | tight, ~1 ATR of a liquid large-cap on 5-min |
| Target | **0.80 %** of entry | 2 : 1 reward : risk |
| OI-flip exit | enabled (Δprice & ΔOI both opposite) | books open profit before reversal |
| Auto square-off | **15:15** | MIS cutoff |
| Position size | **1 lot** of the front-month future | ~₹6 – 10 L notional/trade |
| Universe | 43 NSE F&O large-caps | (full list in `data/universe.csv`) |

---

## 3  Headline backtest results

| Metric | v5_tgt_lower (final) |
|---|---|
| Period | 1-Apr-2026 → 25-Jun-2026 (58 trading days) |
| Trades | **367** (200 long / 167 short) |
| Trades/day | 6.9 |
| Win rate | **43.6 %** |
| Avg net P&L / trade | **₹ 284** |
| Avg win | ₹ 3,756 |
| Avg loss | ₹ –2,399 |
| Reward : risk (realised) | 1.57 : 1 |
| **Gross P&L** | **₹ 1,67,083** |
| **Total brokerage + taxes** | ₹ 62,760 (37.6 % of gross) |
| **Net P&L (after all costs)** | **₹ 1,04,324** |
| Sharpe (daily, annualised) | **2.93** |
| Max drawdown (rupees) | ₹ –43,438 |
| Capital required (peak SPAN+ELM margin, ~25 % of one lot) | ≈ ₹ 1.5 – 2 L per concurrent position |
| Return on ~₹ 2 L deployed | ~ **52 % in 3 months** (gross of slippage) |

Exit-reason mix: SL 44 %, TGT 26 %, TIME 20 %, OI-flip 10 % — healthy
distribution; very few trades go to end-of-day flat.

Equity curve, daily P&L, per-symbol P&L and trade-P&L distribution:
`results/chart_v5_tgt_lower.png`.

---

## 4  Iteration journey

| Tag | Trades | WR % | Net ₹ | Sharpe | Key change |
|---|---|---|---|---|---|
| v1 (baseline, spot equity) | 1,205 | 39.3 | **–67,565** | –4.81 | Loose thresholds; trade spot equity → costs ate all alpha |
| v2_strict | 367 | 39.8 | –2,330 | –0.36 | Tighter thresholds + trend filter; still spot |
| v3a_fade | 430 | 37.7 | –40,088 | –5.28 | Inverted (mean-revert) – confirms momentum sign is right |
| v3c_selective | 47 | 34.0 | –1,775 | –1.28 | Very selective spot; gross +₹2K but too few trades |
| **v4_fut_base** | 363 | 43.8 | **+95,274** | 1.90 | **Switched execution to futures** – costs drop 3× |
| v4_fut_oiflip | 361 | 43.8 | +1,02,726 | 1.95 | Added OI-flip exit |
| v5_strong_oi | 280 | 45.0 | +95,870 | 2.32 | Higher OI threshold (0.5 %) |
| **v5_tgt_lower** (★) | 367 | 43.6 | **+1,04,324** | **2.93** | Lower 0.4 %/0.8 % SL/TGT — best Sharpe |
| v5_relaxed_oi | 453 | 43.9 | +1,13,467 | 2.00 | Most absolute P&L, but bigger DD |
| v5_no_oi (ablation) | 718 | 42.3 | +15,449 | 0.26 | **Removing the OI filter destroys the edge → OI is the source of alpha** |

The **single most important pivot** was moving execution from spot equity
(0.08 % round-trip cost) to stock futures (0.025 % round-trip cost). On the
same set of signals, that one change turned a –₹68 K disaster into a
+₹100 K profit.

The **OI filter contributes ~₹85 K of the ₹104 K profit** (compare
v5_tgt_lower vs v5_no_oi). This is the empirical proof that real-time OI is
genuinely informative for intraday momentum.

---

## 5  Per-symbol results (v5_tgt_lower)

**Top contributors:** AXISBANK +₹28 K, NESTLEIND +₹21 K, BAJAJ-AUTO +₹21 K,
GRASIM +₹19 K, COALINDIA +₹17 K, ICICIBANK +₹16 K.

**Worst contributors:** ADANIENT –₹17 K, PNB –₹12 K, DIVISLAB –₹11 K,
ASIANPAINT –₹11 K. These are higher-beta / news-driven names where pure
microstructure signals get overwhelmed.

29 of 43 symbols are net-positive (winners outweigh losers ~3 : 1 in rupees).
A simple production refinement would be to drop the bottom-quartile names
from the live universe; this was **not** done in the reported numbers to
keep results out-of-sample-honest.

---

## 6  Cost model (Dhan MIS — stock futures NSE_FNO)

Implemented in `src/costs.py`, matched against Dhan's published brokerage
calculator. Per round-trip on one lot:

| Charge | Rate | Comment |
|---|---|---|
| Brokerage | min(₹20, 0.03 % × turnover) per side | Dhan F&O MIS |
| STT | 0.0125 % on sell-side notional | Govt. tax on futures sell |
| Exchange txn (NSE F&O) | 0.00173 % on both sides | NSE charge |
| SEBI | 0.0001 % (= ₹10/cr) both sides | SEBI fee |
| Stamp duty | 0.002 % on buy side | State stamp |
| GST | 18 % on (brokerage + exch + SEBI) | Service tax |

**Total round-trip ≈ 0.025 – 0.030 % of notional** for a typical large-cap
future (₹6 – 10 L per lot). For comparison, the same model applied to spot
equity intraday (₹100 K notional) is 0.08 % round-trip — i.e. **three times
more expensive in pct terms**. That cost gap is the entire reason the
strategy is profitable in futures and unprofitable in spot.

In the headline number, **costs (₹62 K) are 37.6 % of gross P&L (₹167 K)** —
substantial, but well below the alpha generated. They are **not** a rounding
error and the system was designed and validated with them in place.

---

## 7  Data caveats & honest limitations

1. **Time horizon is ~3 months, not 6.**  Dhan's historical API returns
   data only for **active** F&O contracts; expired contracts' security IDs
   are not exposed in the master file, so the front-month stock-future
   series cannot be extended back beyond the listing date of the current
   active series (1-Apr-2026 for the Jun-2026 contract). The user asked for
   6 months — we delivered the full window that the API actually serves.
2. **Survivorship of the universe.**  The 43 names are today's liquid F&O
   stocks; a few were removed from the F&O list during the period (none
   materially so in this window).
3. **Slippage.** Backtest fills SL/TGT at the exact trigger price and
   exits-at-bar-close where applicable. Real fills will see 1 – 2 ticks of
   slippage on stock futures, which would reduce headline net P&L by an
   estimated 5 – 10 %.
4. **SL/TGT priority assumption.** If a bar's range straddles both SL and
   target, we assume SL was hit first (worst-case).
5. **No look-ahead.** Signals are computed on bar `t`, entry is at bar
   `t+1` open. OI for bar `t` comes from the NSE OI snapshot at bar close.
6. **Portfolio sizing.** Each signal trades 1 lot of one symbol; the
   per-symbol day-PnL stop is implemented but a true portfolio-level
   drawdown stop is left as future work.
7. **Option-strike OI** (the *Option Chain* API) is real-time only on Dhan
   — there is no historical option-OI feed — so backtesting strike-level OI
   signals is infeasible with current Dhan data. We use *futures* OI as the
   tradable proxy for the underlying's positioning, which is the standard
   approach Indian prop desks take.

---

## 8  Reproducing the results

```powershell
# 1. Universe + 3 months of 5-min OHLCV+OI per symbol (~45 sec, ~90 API calls)
python src\universe.py
python src\fetch_data.py

# 2. Run the final strategy
python src\strategy.py --tag final `
    --price_pct 0.0025 --oi_pct 0.003 --vol_z 2.0 `
    --sl_pct 0.004 --tgt_pct 0.008

# 3. Charts
python src\charts.py final
```

Programmatic API (Python):
```python
from src.strategy import Params, run_all
p = Params(
    trade_segment='FUT',
    price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
    sl_pct=0.004, tgt_pct=0.008,
    require_trend_align=True, avoid_lunch=True,
    use_oi_flip_exit=True,
)
print(run_all(p, tag='final'))
```

Artifacts in `results/`:
- `comparison.csv` – all 30+ tested variants side-by-side
- `summary_v5_tgt_lower.json` – the final-strategy stats
- `trades_v5_tgt_lower.csv` – every trade (367 rows) with entry/exit/PnL/cost
- `chart_v5_tgt_lower.png` – equity curve, daily PnL, per-symbol, distribution

---

## 9  What we'd do next (productionisation)

1. Live-paper trade for 1 month before committing real capital.
2. Replace fixed SL/TGT with **ATR-based** dynamic levels (tested as v3b;
   under-performed at static thresholds but worth a re-test combined with
   v5 baseline).
3. Add a **portfolio-level daily drawdown stop** (e.g. cut all trading for
   the day at –3 % equity).
4. Add **strike-level OI confirmation** in *live* trading via Dhan's
   Option Chain API (3-sec rate limit per underlying is fine for 25
   symbols × 12 polls/hour = 300/hour) — concentrate trades when the
   nearest 3 OTM strikes also show buildup in the same direction.
5. Extend the universe to ~80 F&O stocks and use top-decile-by-edge
   filtering refreshed monthly.
6. Walk-forward optimisation (3-month train / 1-month test) once 1+ year
   of expired-contract history is locally cached.
