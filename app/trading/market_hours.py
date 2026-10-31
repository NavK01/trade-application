"""Central NSE/BSE market-session helpers.

All trading code uses this module so market-hours checks cannot drift between
the predictor, strategy manager, broker, and dashboard.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN = datetime.time(9, 15)
MARKET_CLOSE = datetime.time(15, 30)
SQUARE_OFF_START = datetime.time(15, 20)


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)


def as_ist(value: Optional[datetime.datetime] = None) -> datetime.datetime:
    if value is None:
        return now_ist()
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def is_trading_day(value: Optional[datetime.datetime] = None) -> bool:
    return as_ist(value).weekday() < 5


def is_market_open(value: Optional[datetime.datetime] = None) -> bool:
    current = as_ist(value)
    if not is_trading_day(current):
        return False
    return MARKET_OPEN <= current.time() <= MARKET_CLOSE


def is_entry_window(value: Optional[datetime.datetime] = None) -> bool:
    """Entries are allowed only before the 15:20 square-off window."""
    current = as_ist(value)
    if not is_trading_day(current):
        return False
    return MARKET_OPEN <= current.time() < SQUARE_OFF_START


def is_near_market_close(value: Optional[datetime.datetime] = None) -> bool:
    current = as_ist(value)
    if not is_trading_day(current):
        return False
    return SQUARE_OFF_START <= current.time() <= MARKET_CLOSE


def today_ist_str(value: Optional[datetime.datetime] = None) -> str:
    return as_ist(value).date().isoformat()


def _next_open(current: datetime.datetime) -> datetime.datetime:
    candidate = current.replace(hour=9, minute=15, second=0, microsecond=0)
    if current.weekday() < 5 and current.time() < MARKET_OPEN:
        return candidate
    days_ahead = 1
    while True:
        candidate = (current + datetime.timedelta(days=days_ahead)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        if candidate.weekday() < 5:
            return candidate
        days_ahead += 1


def get_market_status(value: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    current = as_ist(value)
    is_open = is_market_open(current)
    is_pre_market = (
        current.weekday() < 5
        and datetime.time(9, 0) <= current.time() < MARKET_OPEN
    )
    is_weekend = current.weekday() >= 5

    if is_open:
        status_label = "OPEN"
        desc = "NSE/BSE Live Market Open (09:15 - 15:30 IST)"
    elif is_pre_market:
        status_label = "PRE_MARKET"
        desc = "Pre-Market Discovery (09:00 - 09:15 IST)"
    elif is_weekend:
        status_label = "WEEKEND_CLOSED"
        desc = "Weekend Market Closed (Opens Monday 09:15 IST)"
    else:
        status_label = "CLOSED"
        desc = "Market Closed (Opens 09:15 IST)"

    next_open = _next_open(current) if not is_open else current
    seconds_until_open = 0 if is_open else max(0, int((next_open - current).total_seconds()))

    return {
        "is_open": is_open,
        "is_entry_window": is_entry_window(current),
        "is_pre_market": is_pre_market,
        "is_near_close": is_near_market_close(current),
        "status": status_label,
        "description": desc,
        "current_time_ist": current.strftime("%Y-%m-%d %H:%M:%S IST"),
        "next_open_ist": next_open.strftime("%Y-%m-%d 09:15:00 IST"),
        "seconds_until_open": seconds_until_open,
    }
