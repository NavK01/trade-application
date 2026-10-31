"""Hard pre-trade and daily risk gates.

The risk manager is deliberately boring: it never forecasts, never sizes a
trade upward, and never allows a new position after the daily loss limit,
daily profit target, trade cap, or concurrent-position cap has been reached.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

from app.config import settings
from app.storage.db import DatabaseManager, db as default_db
from app.trading import market_hours

logger = logging.getLogger("app.trading.risk_manager")


class RiskManager:
    def __init__(
        self,
        database: Optional[DatabaseManager] = None,
        now_provider=None,
        enforce_market_hours: bool = True,
    ):
        self.db = database or default_db
        self.now_provider = now_provider or market_hours.now_ist
        self.enforce_market_hours = enforce_market_hours

    # ------------------------------------------------------------------
    # Daily state
    # ------------------------------------------------------------------
    def today(self) -> str:
        return market_hours.today_ist_str(self.now_provider())

    def _starting_equity(self, account_id: str, date_str: str) -> float:
        previous = self.db.get_last_daily_balance_before(account_id, date_str)
        if previous is not None:
            return float(previous)
        state = self.db.get_account_state(account_id)
        return float(state.get("initial_capital", settings.INITIAL_CAPITAL_INR))

    def _ai_cost_today(self, account_id: str) -> float:
        return float(self.db.get_ai_cost_for_account(account_id, self.today()))

    def get_state(self, account_id: str, account: Any = None) -> Dict[str, Any]:
        date_str = self.today()
        start_equity = self._starting_equity(account_id, date_str)
        state = self.db.get_daily_risk_state(
            account_id, date_str, default_equity=start_equity
        )

        # The daily guard is driven by REALIZED P&L (closed trades) plus AI cost,
        # exactly like the backtester. Marked-to-market equity is deliberately not
        # used: an open hold-to-close position can dip into an unrealized drawdown
        # and later recover, and locking the day on that would cut a winner short.
        # The open position's own stop is the risk control while it is live.
        equity = start_equity
        if account is not None:
            try:
                equity = float(account.get_summary()["total_portfolio_value_inr"])
            except Exception:
                pass
        else:
            summary = self.db.get_account_state(account_id)
            equity = float(summary.get("available_cash", start_equity))

        ai_cost = self._ai_cost_today(account_id)
        realized_pnl = round(self.db.get_today_realized_pnl(account_id), 2)
        net_pnl = round(realized_pnl - ai_cost, 2)
        trades_today = max(
            int(state.get("trades_count", 0)),
            self.db.get_today_trade_count(account_id),
        )

        max_loss = -abs(float(state.get("start_equity", start_equity)) * settings.MAX_DAILY_LOSS_PCT / 100.0)
        target = abs(float(state.get("start_equity", start_equity)) * settings.TARGET_NET_PROFIT_PCT / 100.0)

        locked = bool(state.get("locked", 0))
        lock_reason = state.get("lock_reason")

        if not locked:
            if net_pnl <= max_loss:
                locked = True
                lock_reason = (
                    f"Daily loss limit reached: realized net ₹{net_pnl:,.2f} after AI cost "
                    f"(limit ₹{max_loss:,.2f}, -{settings.MAX_DAILY_LOSS_PCT:.2f}%)."
                )
            elif net_pnl >= target:
                locked = True
                lock_reason = (
                    f"Daily profit target reached: realized net ₹{net_pnl:,.2f} after AI cost "
                    f"(target +{settings.TARGET_NET_PROFIT_PCT:.2f}%). New entries stopped."
                )
            elif trades_today >= settings.MAX_TRADES_PER_DAY:
                locked = True
                lock_reason = f"Daily trade cap reached ({trades_today}/{settings.MAX_TRADES_PER_DAY})."

        if locked != bool(state.get("locked", 0)) or lock_reason != state.get("lock_reason"):
            self.db.update_daily_risk_state(
                account_id,
                date_str,
                realized_pnl=realized_pnl,
                ai_cost_inr=ai_cost,
                trades_count=trades_today,
                locked=1 if locked else 0,
                lock_reason=lock_reason,
            )
            state = self.db.get_daily_risk_state(account_id, date_str, default_equity=start_equity)

        return {
            **state,
            "date": date_str,
            "start_equity": float(state.get("start_equity", start_equity)),
            "current_equity": round(equity, 2),
            "realized_pnl_today_inr": realized_pnl,
            "trading_pnl": realized_pnl,
            "ai_cost_today_inr": round(ai_cost, 2),
            "net_pnl_today_inr": net_pnl,
            "max_daily_loss_inr": round(max_loss, 2),
            "daily_profit_target_inr": round(target, 2),
            "trades_today": trades_today,
            "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            "locked": locked,
            "lock_reason": lock_reason,
            "market_open": market_hours.is_market_open(self.now_provider()),
            "entry_window": market_hours.is_entry_window(self.now_provider()),
        }

    # ------------------------------------------------------------------
    # Pre-trade gates
    # ------------------------------------------------------------------
    def evaluate_entry(
        self,
        account_id: str,
        account: Any,
        symbol: str,
        quantity: int,
        price: float,
    ) -> Dict[str, Any]:
        state = self.get_state(account_id, account)
        reasons = []

        if quantity <= 0:
            reasons.append("Quantity must be greater than zero.")
        if price <= 0:
            reasons.append("A verified positive market price is required.")
        if self.enforce_market_hours and not market_hours.is_entry_window(self.now_provider()):
            reasons.append(
                "Entries are allowed only during the NSE session (09:15-15:20 IST, Mon-Fri)."
            )
        if state["locked"]:
            reasons.append(state.get("lock_reason") or "Account is locked for the day.")
        if state["trades_today"] >= settings.MAX_TRADES_PER_DAY:
            reasons.append(
                f"Daily trade cap reached ({state['trades_today']}/{settings.MAX_TRADES_PER_DAY})."
            )
        if symbol.upper() in getattr(account, "positions", {}):
            reasons.append(f"Duplicate position rejected: {symbol.upper()} is already open.")
        open_count = len(getattr(account, "positions", {}))
        if open_count >= settings.MAX_OPEN_POSITIONS:
            reasons.append(
                f"Maximum concurrent positions reached ({open_count}/{settings.MAX_OPEN_POSITIONS})."
            )

        max_position_value = float(getattr(account, "initial_capital", settings.INITIAL_CAPITAL_INR)) * settings.MAX_POSITION_PCT
        notional = round(float(price) * int(quantity), 2)
        if notional > max_position_value + 0.01:
            reasons.append(
                f"Position size ₹{notional:,.2f} exceeds the "
                f"{settings.MAX_POSITION_PCT * 100:.0f}% cap (₹{max_position_value:,.2f})."
            )

        return {
            "approved": not reasons,
            "status": "PASS" if not reasons else "BLOCK",
            "reasons": reasons,
            "state": state,
            "max_position_value_inr": round(max_position_value, 2),
        }

    def evaluate_exit(self, account_id: str, account: Any, symbol: str, quantity: int) -> Dict[str, Any]:
        state = self.get_state(account_id, account)
        reasons = []
        if quantity <= 0:
            reasons.append("Quantity must be greater than zero.")
        if self.enforce_market_hours and not market_hours.is_market_open(self.now_provider()):
            reasons.append("Exits are allowed only while the NSE market is open.")
        pos = getattr(account, "positions", {}).get(symbol.upper())
        if pos is None:
            reasons.append(f"No open position in {symbol.upper()}.")
        elif quantity > pos.quantity:
            reasons.append(f"Only {pos.quantity} shares are held in {symbol.upper()}.")
        return {
            "approved": not reasons,
            "status": "PASS" if not reasons else "BLOCK",
            "reasons": reasons,
            "state": state,
        }

    def record_fill(self, account_id: str, account: Any, action: str) -> Dict[str, Any]:
        # The trade has already been persisted, so get_state() reads the
        # authoritative BUY count from SQLite and refreshes the lock state.
        return self.get_state(account_id, account)

    def should_force_exit(self, account_id: str, account: Any) -> bool:
        state = self.get_state(account_id, account)
        return bool(
            state["locked"]
            and state.get("net_pnl_today_inr", 0.0)
            <= state.get("max_daily_loss_inr", -1.0)
        )
