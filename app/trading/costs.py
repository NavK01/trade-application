"""Realistic Indian equity intraday transaction-cost model.

The paper broker must charge the same statutory and brokerage costs a retail
intraday account would pay.  Keeping the calculation in one module makes the
backtester, broker, and dashboard use identical numbers.
"""

from __future__ import annotations

from typing import Dict

# Zerodha-style equity intraday tariff (per executed order leg).
BROKERAGE_RATE = 0.0003          # 0.03%
BROKERAGE_CAP = 20.0             # ₹20 per order
STT_SELL_RATE = 0.00025          # 0.025% on sell turnover
EXCHANGE_TXN_RATE = 0.0000297    # NSE transaction charge
GST_RATE = 0.18                  # on brokerage + exchange transaction
SEBI_RATE = 0.000001             # ₹10 per crore
STAMP_BUY_RATE = 0.00003         # 0.003% on buy turnover


def _brokerage(turnover: float) -> float:
    return min(BROKERAGE_CAP, turnover * BROKERAGE_RATE)


def calculate_buy_fee(turnover: float) -> float:
    """Total cost of a BUY leg, excluding slippage."""
    if turnover <= 0:
        return 0.0
    brokerage = _brokerage(turnover)
    exchange = turnover * EXCHANGE_TXN_RATE
    gst = (brokerage + exchange) * GST_RATE
    sebi = turnover * SEBI_RATE
    stamp = turnover * STAMP_BUY_RATE
    return round(brokerage + exchange + gst + sebi + stamp, 2)


def calculate_sell_fee(turnover: float) -> float:
    """Total cost of a SELL leg, excluding slippage."""
    if turnover <= 0:
        return 0.0
    brokerage = _brokerage(turnover)
    stt = turnover * STT_SELL_RATE
    exchange = turnover * EXCHANGE_TXN_RATE
    gst = (brokerage + exchange) * GST_RATE
    sebi = turnover * SEBI_RATE
    return round(brokerage + stt + exchange + gst + sebi, 2)


def calculate_round_trip_fees(buy_value: float, sell_value: float) -> Dict[str, float]:
    buy_fee = calculate_buy_fee(buy_value)
    sell_fee = calculate_sell_fee(sell_value)
    return {
        "entry_fee_inr": buy_fee,
        "exit_fee_inr": sell_fee,
        "total_fee_inr": round(buy_fee + sell_fee, 2),
    }
