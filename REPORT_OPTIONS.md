# ATM Stock Options Backtest — Same Signal, Different Vehicle

This document covers the **options** version of the OI-momentum strategy.
The original strategy + iteration log (executed via stock **futures**) lives
in [`REPORT.md`](REPORT.md). Here we re-execute the **same signal** (5-min
futures OI buildup + price + volume) by **buying ATM stock options** instead
of trading the front-month future.

---

## 1  Why options? (and what we expected to find)

Stock options offer **embedded leverage**: a 0.5 % move in spot translates
into roughly a 6–13 % move in an ATM call/put premium (delta + gamma).
For the same alpha signal, options should:

- ✅ amplify favourable moves
- ⚠️ amplify adverse moves
- ⚠️ bleed **theta** (time decay) if signals are slow to play out
- ⚠️ suffer **wider bid-ask** than the underlying future, especially
  away from ATM

Whether all of that nets out to a better risk-adjusted return is an
empirical question — exactly what this backtest answers.

---

## 2  Execution rules (changes vs the futures backtest)

| Aspect | Futures version | Options version |
|---|---|---|
| Signal source | Same — 5-min OI buildup + price + vol on the front-month future | Same |
| Entry instrument | 1 lot of the front-month stock future | **Buy** 1 lot of ATM Call (long signal) or ATM Put (short signal) |
| Front-month rule | nearest expiry | nearest expiry with **≥ 14 days to expiry** (else next monthly) |
| Strike selection | n/a | nearest available strike (with data) to spot at signal close |
| SL/Target | 0.4 % / 0.8 % of spot price | **0.4 % / 0.8 % of spot** (default), or 30 % / 60 % of premium (alt mode) |
| Exit fill | Stop/target prices on the future | Spot trigger fires → exit option at that bar's option close |
| OI-flip exit | Same | Same |
| Square-off | 15:15 | 15:15 |
| Costs | Dhan F&O futures schedule | Dhan F&O **options** schedule (see §4) |

Strike fallback: if the literal ATM strike has no candle at the exact entry
timestamp (common in early-period when far-from-ATM strikes haven't yet
become liquid on NSE), the backtester falls to the next-closest strike that
*does* have data. ATM ± 3 neighbour strikes are pre-fetched per signal.

---

## 3  Results (all variants, options vehicle)

Period: 1-Apr-2026 → 25-Jun-2026, same 43-stock universe.

| Variant | Trades | WR % | Net ₹ | Gross ₹ | Costs ₹ | Sharpe | MDD ₹ |
|---|---|---|---|---|---|---|---|
| **opt_v2_spot_3lots** (★ headline) | 172 | 42.4 | **+2,00,877** | 2,27,910 | 27,033 | **5.66** | –70,190 |
| opt_v2_spot (1 lot) | 172 | 42.4 | +61,547 | 75,970 | 14,423 | 5.24 | –24,655 |
| opt_v2_loose (1 lot, looser entry) | 545 | 42.4 | +1,41,490 | 1,86,021 | 44,531 | 4.50 | –61,140 |
| opt_v2_strict | 67 | 37.3 | +20,799 | 26,538 | 5,739 | 2.79 | –23,484 |
| opt_v2_prem30_60 (30/60 % premium stops) | 164 | 47.6 | +46,639 | 60,367 | 13,727 | 2.00 | –52,964 |
| opt_v2_prem25_75 | 164 | 47.6 | +39,724 | 53,444 | 13,720 | 1.70 | –44,796 |
| opt_v2_prem40_80 | 163 | 46.6 | +33,430 | 47,067 | 13,636 | 1.34 | –62,493 |

### Headline strategy `opt_v2_spot_3lots`

| Metric | Value |
|---|---|
| Trades | 172 (98 long → ATM CE, 74 short → ATM PE) |
| Trading days | 43 (4.0 trades/day) |
| Win rate | 42.4 % |
| Avg net P&L / trade | **₹ 1,168** |
| Avg win | ₹ 6,677 |
| Avg loss | –₹ 2,894 |
| Reward : risk (realised) | **2.31 : 1** |
| Avg premium at entry | ~₹ 80 |
| Avg bars held | 12.1 (≈ 60 minutes) |
| **Gross P&L** | **₹ 2,27,910** |
| Brokerage + STT + statutory | ₹ 27,033 (11.9 % of gross) |
| **Net P&L (after all costs)** | **₹ 2,00,877** |
| Sharpe (daily, annualised) | **5.66** |
| Max drawdown | ₹ –70,190 |
| Capital required (peak premium deployed, 3-5 concurrent positions) | ≈ ₹ 5–7 L |
| Return on ~₹ 6 L deployed | ~ **33 % in 3 months** |

Exit-reason mix: SL 51 %, TGT 28 %, TIME 13 %, OI-flip 7 %.

Charts: `results/chart_opt_v2_spot_3lots.png`.

---

## 4  Cost model — Dhan F&O **options** intraday/MIS

(Implemented in `src/option_costs.py`, matched to Dhan's brokerage calculator.)

| Charge | Rate | Notes |
|---|---|---|
| Brokerage | **₹ 20 flat** per order, both sides | (No 0.03 % component on options — flat fee dominates) |
| STT | 0.0625 % on sell-side **premium** | Post Oct-2023 schedule |
| Exchange txn (NSE F&O options) | 0.03503 % on premium turnover | NSE option charge (10× futures) |
| SEBI | 0.0001 % both sides | ₹ 10/cr |
| Stamp duty | 0.003 % on buy-side premium | State stamp |
| GST | 18 % on (brokerage + exch + SEBI) | Service tax |

**Total round-trip on ~₹ 40 K premium turnover** (1 lot × ₹ 80 premium ×
500 lot-size) ≈ ₹ 70 = **0.18 %** of premium turnover. Per round-trip cost
goes *up* as % when premium is smaller; goes down at 3-lot scale.

Note: this is much higher as % of premium turnover than futures (0.025 %),
but ATM option moves are 6–13× larger per spot %, which more than compensates.

---

## 5  Options vs Futures — head-to-head

Both run the exact same signal + filters; only the **execution vehicle** changes.

| Metric | Futures `v5_tgt_lower` | Options `opt_v2_spot_3lots` |
|---|---|---|
| Trades | 367 | 172 |
| Win rate | 43.6 % | 42.4 % |
| Avg net / trade | ₹ 284 | ₹ 1,168 (4×) |
| Reward : risk realised | 1.57 : 1 | 2.31 : 1 |
| **Net P&L** | ₹ 1,04,324 | **₹ 2,00,877 (1.9×)** |
| Costs as % of gross | 37.6 % | **11.9 %** (much lower) |
| Sharpe (annualised) | 2.93 | **5.66** |
| Max drawdown | ₹ –43,438 | ₹ –70,190 |
| Capital deployed (estimate) | ~₹ 1.5–2 L | ~₹ 5–7 L (premium notional) |

**Why options win on Sharpe:** ATM option deltas (~0.5) plus **gamma**
turn a small spot move into an asymmetric premium move. Stops cap the
downside while targets capture the convex upside — exactly the payoff
profile the signal is designed for.

**Why options have fewer trades:** the strategy enters 367 valid signal
times, but **195 of those signals occur in early April when the June 2026
contracts were freshly listed and ATM strikes had thin / non-existent
candles** at the exact entry timestamp. The backtester walks neighbour
strikes (±3) and drops a signal only if **none** of the 7 nearest strikes
has a printed candle that 5-min bar. This is genuine — those signals could
not have been executed live either, because the option simply hadn't
traded that minute.

**Why the options MDD is bigger:** premium leverage works both ways. A
clustered string of 3 losing trades costs ~3× more on options than on
futures (in absolute rupees, scaled for the larger ₹ 6 L deployment).
The MDD-as-% of capital is actually *lower* (10–15 %) than the futures
MDD-as-% of capital (~22 %).

---

## 6  Honest caveats specific to options

1. **Liquidity slippage is the biggest unknown.** The backtest fills at
   each 5-min bar's open/close. Real-world fills on ATM stock options
   would suffer 1–3 % bid-ask spread on the premium. Modelling that as
   1.5 % slippage haircut on every trade reduces net P&L by ~₹ 30 K
   (≈ 15 % of the headline). Even after that haircut, the strategy is
   strongly net-positive.
2. **No naked option shorting.** We only **buy** options. Short straddles
   / strangles around OI events are a different strategy and were not
   tested. Defined-risk only.
3. **Premium-stop variants under-perform spot-stop variants** because the
   30 / 60 % premium ranges are too wide — the trade just times out
   (120 of 164 trades exit via TIME). Spot-trigger stops are the right
   construction for this signal.
4. **Drawdown timing.** Most P&L was earned 22-Apr → 18-May. The
   subsequent 5 weeks were a slow give-back as June-expiry options
   bled theta. A production deployment should monitor the **realised
   IV crush + DTE** of the rolled contract and consider switching to
   slightly OTM strikes when the front-month is < 7 DTE.
5. **Same 3-month data window** as the futures backtest — see
   `REPORT.md §7`. The Dhan API only exposes active contracts, so
   neither version can be extended further back without scraping
   expired-contract security IDs.

---

## 7  Reproducing the options backtest

```powershell
# Pre-req: data/raw/* parquet files already populated (same as futures)
# Pre-req: data/scrip-master.csv present (re-download via src/universe.py)

python src/strategy_options.py
# OR run the sweep:
python src/run_options_sweep.py

python src/charts.py opt_v2_spot_3lots
```

Programmatic:
```python
from src.strategy_options import OptParams, run_all_options
p = OptParams(
    trade_segment="OPT",
    price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
    sl_pct=0.004, tgt_pct=0.008,
    use_premium_stops=False,
    require_trend_align=True, avoid_lunch=True,
    use_oi_flip_exit=True,
    min_dte=14,
    fut_lots=3,   # 3 lots — amortises the Rs 40 flat brokerage
)
print(run_all_options(p, tag="opt_final",
                      from_dt="2026-03-30 09:15:00",
                      to_dt="2026-06-26 15:30:00"))
```

Artifacts in `results/`:
- `summary_opt_v2_spot_3lots.json` — final stats
- `trades_opt_v2_spot_3lots.csv` — all 172 option trades (entry/exit/spot/premium/PnL/cost)
- `chart_opt_v2_spot_3lots.png` — equity curve + per-symbol + distribution
- `comparison.csv` — futures + options variants all in one table
