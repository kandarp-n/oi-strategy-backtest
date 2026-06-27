"""Cost model for Dhan F&O OPTIONS (intraday or carry — same charges).

For Dhan / Zerodha schedule on options:
  Brokerage: Rs 20 flat per executed order (no % component)
  STT: 0.0625% on the *sell-side premium* (post Oct 2023 schedule)
        — assumes BUY then SELL (no exercise). On exercise STT is higher.
  Exchange transaction: 0.03503% on premium turnover (NSE options)
  SEBI: 0.0001% on premium turnover
  Stamp duty: 0.003% on the buy-side premium
  GST: 18% on (brokerage + exch + SEBI)
"""
from __future__ import annotations

OPT_BROKERAGE_FLAT = 20.0          # Rs per order
OPT_STT_SELL = 0.000625            # 0.0625%
OPT_EXCH = 0.0003503               # 0.03503%
OPT_SEBI = 0.000001                # 0.0001% = Rs 10/cr
OPT_STAMP_BUY = 0.00003            # 0.003%
GST_RATE = 0.18


def option_round_trip_cost(buy_premium: float, sell_premium: float, lots_qty: int) -> float:
    """`lots_qty` is total option qty (lots * lot_size).  Returns rupees."""
    buy_to = buy_premium * lots_qty
    sell_to = sell_premium * lots_qty
    brokerage = 2 * OPT_BROKERAGE_FLAT
    stt = OPT_STT_SELL * sell_to
    exch = OPT_EXCH * (buy_to + sell_to)
    sebi = OPT_SEBI * (buy_to + sell_to)
    stamp = OPT_STAMP_BUY * buy_to
    gst = GST_RATE * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def option_net_pnl(buy_premium: float, sell_premium: float, lots_qty: int) -> tuple[float, float, float]:
    """Long-only options: BUY at entry, SELL at exit (for both bullish CE and bearish PE)."""
    gross = (sell_premium - buy_premium) * lots_qty
    cost = option_round_trip_cost(buy_premium, sell_premium, lots_qty)
    return gross - cost, gross, cost


if __name__ == "__main__":
    # Sanity: ATM RELIANCE option bought at Rs 30, sold at Rs 35, lot 500
    nett, gross, cost = option_net_pnl(30, 35, 500)
    print(f"30->35 x500: gross Rs {gross:.2f}  cost Rs {cost:.2f}  net Rs {nett:.2f}")
    nett, gross, cost = option_net_pnl(30, 25, 500)
    print(f"30->25 x500: gross Rs {gross:.2f}  cost Rs {cost:.2f}  net Rs {nett:.2f}")
    # Heavier example: ATM HDFCBANK at Rs 80, lot 550
    nett, gross, cost = option_net_pnl(80, 90, 550)
    print(f"80->90 x550 HDFCBANK: gross Rs {gross:.2f}  cost Rs {cost:.2f}  net Rs {nett:.2f}")
