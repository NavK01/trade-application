"""
Ultra-Fast Execution Engine wrapper for Gemini Quant Platform.
Delegates order routing and position management to the active broker (Paper or Live)
with full support for the AI Dual Arena (Gemini 3.8 Flash vs DeepSeek Flash).
"""

from typing import Dict, Any, Optional, List
from app.trading.broker import broker, Position


class ExecutionEngine:
    """Singleton facade for order routing, positions, and live price updates across dual agents."""

    def get_account(self, account_id: str = "gemini_flash"):
        """Get live Account instance for specified agent (Gemini or DeepSeek)."""
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
        """Reset dummy account back to specified initial capital (e.g. ₹10,000)."""
        if hasattr(broker, "reset_account"):
            return broker.reset_account(account_id=account_id, capital=capital)
        return broker.get_portfolio_summary()

    def get_portfolio_summary(self, account_id: str = "gemini_flash") -> Dict[str, Any]:
        return broker.get_portfolio_summary(account_id=account_id)

    async def fast_execute_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        strategy_name: Optional[str] = None,
        account_id: str = "gemini_flash",
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
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
        )

    async def close_position(self, symbol: str, account_id: str = "gemini_flash") -> Optional[Dict[str, Any]]:
        return await broker.close_position(symbol, account_id=account_id)

    def update_live_prices(self, price_dict: Dict[str, float]):
        if hasattr(broker, "update_live_prices"):
            broker.update_live_prices(price_dict)


execution_engine = ExecutionEngine()
