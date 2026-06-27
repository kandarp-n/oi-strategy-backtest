# OI-Based Intraday Momentum Strategy — Plan

## Goal
Build an intraday momentum strategy using Dhan's historical OI data (on
stock futures) + spot price + volume, backtest on the last ~6 months, account
for realistic Dhan brokerage + STT + statutory charges, iterate to improve.

## Data Approach
Dhan does **not** expose historical OI per option strike, but **does** expose
historical OI on stock futures via `/v2/charts/intraday` with `oi: true`.
Futures OI is the canonical sentiment signal for an underlying. We'll use:

- **NSE_FNO** current-month futures: 5-min OHLC + OI + volume
- **NSE_EQ** equity spot: 5-min OHLC + volume (trading instrument)

## Universe
~25–30 most liquid F&O stocks (we trade the spot equity intraday).

## Strategy Idea (v1)
On 5-min closed candles, on the **futures**:
- Long Buildup = price up ≥ X% AND OI up ≥ Y% AND vol surge.
- Short Buildup = price down ≥ X% AND OI up ≥ Y% AND vol surge.
Enter the spot equity intraday on next candle's open with fixed % SL/Target,
square off mandatorily by 15:15. Avoid first 15 min (noise).

## Costs Model (Dhan Equity Intraday)
- Brokerage: min(₹20, 0.03% × turnover) per executed order, per side
- STT: 0.025% on the sell-side turnover (intraday equity)
- Exchange txn charges: NSE 0.00297% on both sides
- SEBI charges: 0.0001% on both sides
- Stamp duty: 0.003% on buy-side only
- GST: 18% on (brokerage + exchange txn + SEBI)

## Phases
1. Setup + auth
2. Select universe + resolve futures security IDs
3. Fetch 6 months 5-min data (spot + futures, w/ OI) — store as parquet
4. Build v1 strategy + backtester + costs
5. Run, analyze, iterate (v2, v3)
6. Final report
