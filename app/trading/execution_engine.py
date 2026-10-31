"""Thin execution facade over the active broker."""

from typing import Any, Dict, Optional

from app.config import DEFAULT_ACCOUNT_ID
from app.trading.broker import Position, broker


class ExecutionEngine:
    """Routes orders and portfolio reads to the active paper/live broker."""

    def get_account(self, account_id: str = DEFAULT_ACCOUNT_ID):
        if hasattr(broker, "get_account"):
            return broker.get_account(account_id)
        return None

    @property
    def initial_capital(self) -> float:
        return broker.initial_capital

    @property
    def available_cash(self) -> float:
        return broker.available_cash

    @property
    def positions(self) -> Dict[str, Position]:
        return broker.positions

    @property
    def order_history(self):
        return broker.order_history

    @property
    def realized_pnl(self) -> float:
        return broker.realized_pnl

    def reset_account(self, capital: Optional[float] = None, account_id: Optional[str] = None) -> Dict[str, Any]:
        if hasattr(broker, "reset_account"):
            return broker.reset_account(account_id=account_id, capital=capital)
        return broker.get_portfolio_summary()

    def get_portfolio_summary(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Dict[str, Any]:
        return broker.get_portfolio_summary(account_id=account_id)

    async def fast_execute_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        strategy_name: Optional[str] = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
        exit_reason: Optional[str] = None,
        hold_to_close: bool = False,
    ) -> Dict[str, Any]:
        return await broker.fast_execute_order(
            symbol=symbol,
            action=action,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            strategy_name=strategy_name,
            account_id=account_id,
            stop_loss=stop_loss,
            target_price=target_price,
            exit_reason=exit_reason,
            hold_to_close=hold_to_close,
        )

    async def close_position(
        self,
        symbol: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
        reason: str = "manual_close",
    ) -> Optional[Dict[str, Any]]:
        return await broker.close_position(symbol, account_id=account_id, reason=reason)

    def update_live_prices(self, price_dict: Dict[str, float]):
        if hasattr(broker, "update_live_prices"):
            broker.update_live_prices(price_dict)


execution_engine = ExecutionEngine()
