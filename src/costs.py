"""Realistic Dhan cost models for intraday MIS.

Equity intraday (NSE_EQ):
  Brokerage: min(₹20, 0.03% × turnover) per side
  STT: 0.025% on sell side
  Exchange txn: 0.00297% both sides (NSE)
  SEBI: 0.0001% both sides
  Stamp: 0.003% on buy
  GST: 18% on (brokerage + exch + SEBI)

Stock futures intraday (NSE_FNO FUTSTK):
  Brokerage: min(₹20, 0.03% × turnover) per side
  STT: 0.0125% on sell side
  Exchange txn: 0.00173% both sides (NSE F&O futures)
  SEBI: 0.0001% both sides
  Stamp: 0.002% on buy
  GST: 18% on (brokerage + exch + SEBI)

Sources: Dhan brokerage calculator (matches Zerodha schedule).
"""
from __future__ import annotations

BROKERAGE_RATE = 0.0003       # 0.03%
BROKERAGE_CAP = 20.0           # Rs 20 per order

# Equity intraday
EQ_STT_SELL = 0.00025          # 0.025%
EQ_EXCH = 0.0000297            # 0.00297%
EQ_SEBI = 0.000001
EQ_STAMP_BUY = 0.00003         # 0.003%

# Futures intraday
FUT_STT_SELL = 0.000125        # 0.0125%
FUT_EXCH = 0.0000173           # 0.00173%
FUT_SEBI = 0.000001
FUT_STAMP_BUY = 0.00002        # 0.002%

GST_RATE = 0.18


def _brokerage(turnover: float) -> float:
    return min(BROKERAGE_CAP, BROKERAGE_RATE * turnover)


def round_trip_cost(buy_price: float, sell_price: float, qty: int, segment: str = "EQ") -> float:
    """`segment` in {'EQ','FUT'}."""
    buy_to = buy_price * qty
    sell_to = sell_price * qty
    brokerage = _brokerage(buy_to) + _brokerage(sell_to)
    if segment == "EQ":
        stt = EQ_STT_SELL * sell_to
        exch = EQ_EXCH * (buy_to + sell_to)
        sebi = EQ_SEBI * (buy_to + sell_to)
        stamp = EQ_STAMP_BUY * buy_to
    elif segment == "FUT":
        stt = FUT_STT_SELL * sell_to
        exch = FUT_EXCH * (buy_to + sell_to)
        sebi = FUT_SEBI * (buy_to + sell_to)
        stamp = FUT_STAMP_BUY * buy_to
    else:
        raise ValueError(f"unknown segment {segment}")
    gst = GST_RATE * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def net_pnl(entry_price: float, exit_price: float, qty: int, side: str, segment: str = "EQ") -> tuple[float, float, float]:
    if side == "long":
        buy_p, sell_p = entry_price, exit_price
    else:
        buy_p, sell_p = exit_price, entry_price
    gross = (sell_p - buy_p) * qty
    cost = round_trip_cost(buy_p, sell_p, qty, segment=segment)
    return gross - cost, gross, cost


if __name__ == "__main__":
    for p, q, seg, label in [
        (1000, 100, "EQ", "EQ Rs 1L"),
        (500, 100, "EQ", "EQ Rs 50k"),
        (1300, 500, "FUT", "FUT RELIANCE lot Rs 6.5L"),
        (3000, 350, "FUT", "FUT HDFCBANK lot Rs 10.5L"),
    ]:
        rt = round_trip_cost(p, p, q, segment=seg)
        print(f"{label}: round-trip Rs {rt:.2f} on Rs {p*q:,.0f} notional ({100*rt/(p*q):.4f}%)")

