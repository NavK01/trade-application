"""Paper broker with realistic Indian intraday costs and hard risk gates.

There are two independent paper accounts:
  * opus_flash     - Claude Opus 4.6 Thinking
  * deepseek_flash - DeepSeek 4.1 Flash MaaS

No synthetic fills are generated. A verified positive market price must be
available, and every entry passes the daily risk manager before execution.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from app.config import ACCOUNT_NAMES, ACCOUNT_IDS, DEFAULT_ACCOUNT_ID, settings
from app.financial_advisor.india_market import get_indian_stock_quote
from app.storage.cache import cache
from app.storage.db import DatabaseManager, db as default_db
from app.trading import market_hours
from app.trading.costs import calculate_buy_fee, calculate_sell_fee
from app.trading.risk_manager import RiskManager

logger = logging.getLogger("app.trading.broker")


class Position:
    def __init__(
        self,
        symbol: str,
        quantity: int,
        avg_price: float,
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
        entry_fee_inr: float = 0.0,
        hold_to_close: bool = False,
    ):
        self.symbol = symbol.upper()
        self.quantity = int(quantity)
        self.avg_price = round(float(avg_price), 2)
        self.current_price = round(float(avg_price), 2)
        self.stop_loss = round(float(stop_loss), 2) if stop_loss else None
        self.target_price = round(float(target_price), 2) if target_price else None
        self.entry_fee_inr = round(float(entry_fee_inr), 2)
        # hold_to_close positions are trend trades: they exit only on the
        # protective stop or the end-of-day square-off, never on a fixed
        # target or a trailing profit lock.
        self.hold_to_close = bool(hold_to_close)
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0
        self.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def update_price(self, new_price: float):
        if new_price and new_price > 0:
            self.current_price = round(float(new_price), 2)
            self.unrealized_pnl = round(
                (self.current_price - self.avg_price) * self.quantity - self.entry_fee_inr, 2
            )
            invested = self.avg_price * self.quantity
            self.unrealized_pnl_pct = (
                round((self.unrealized_pnl / invested) * 100, 2) if invested else 0.0
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
            "current_price": self.current_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "entry_fee_inr": self.entry_fee_inr,
            "hold_to_close": self.hold_to_close,
            "invested_value": round(self.avg_price * self.quantity, 2),
            "current_value": round(self.current_price * self.quantity, 2),
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "created_at": self.created_at,
        }


class Account:
    """Independent paper account backed by SQLite."""

    def __init__(
        self,
        account_id: str,
        name: str,
        database: Optional[DatabaseManager] = None,
        price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    ):
        self.db = database or default_db
        self.account_id = account_id
        self.name = name
        self.price_lookup = price_lookup
        state = self.db.get_account_state(account_id)
        if state.get("account_name"):
            self.name = state["account_name"]
        self.initial_capital = float(state.get("initial_capital", settings.INITIAL_CAPITAL_INR))
        self.available_cash = float(state.get("available_cash", settings.INITIAL_CAPITAL_INR))
        self.realized_pnl = float(state.get("total_realized_pnl", 0.0))
        self.positions: Dict[str, Position] = {}
        for p in self.db.get_open_positions(account_id):
            pos = Position(
                symbol=p["symbol"],
                quantity=p["quantity"],
                avg_price=p["avg_price"],
                stop_loss=p.get("stop_loss"),
                target_price=p.get("target_price"),
                entry_fee_inr=p.get("entry_fee_inr", 0.0),
                hold_to_close=bool(p.get("hold_to_close", 0)),
            )
            pos.created_at = p.get("created_at", pos.created_at)
            self.positions[p["symbol"]] = pos
        self.order_history: List[Dict[str, Any]] = self.db.get_trades(account_id, limit=100)

    def _live_price(self, symbol: str) -> Optional[float]:
        if self.price_lookup:
            try:
                price = self.price_lookup(symbol)
                if price and price > 0:
                    return float(price)
            except Exception:
                pass
        price = cache.get_price(symbol)
        if price and price > 0:
            return float(price)
        try:
            quote = get_indian_stock_quote(symbol)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                price = float(quote["current_price"])
                cache.set_price(symbol, price)
                return price
        except Exception as exc:
            logger.debug("Quote lookup failed for %s: %s", symbol, exc)
        return None

    def mark_to_market(self) -> None:
        for sym, pos in self.positions.items():
            live_p = self._live_price(sym)
            if live_p:
                pos.update_price(live_p)

    def calculate_budget_quantity(
        self,
        symbol: str,
        stock_price: float,
        max_alloc_pct: Optional[float] = None,
    ) -> int:
        """Whole-share quantity inside available cash and the position cap."""
        if stock_price <= 0 or self.available_cash <= 0:
            return 0
        cap_pct = settings.MAX_POSITION_PCT if max_alloc_pct is None else max_alloc_pct
        allocated_cash = min(
            self.available_cash,
            self.initial_capital * cap_pct,
        )
        qty = int(allocated_cash // stock_price)
        if qty == 0 and self.available_cash >= stock_price and stock_price <= self.initial_capital * cap_pct:
            qty = 1
        return max(qty, 0)

    def get_summary(self) -> Dict[str, Any]:
        self.mark_to_market()
        invested = sum(p.avg_price * p.quantity for p in self.positions.values())
        current_holdings = sum(p.current_price * p.quantity for p in self.positions.values())
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_portfolio_value = round(self.available_cash + current_holdings, 2)
        total_pnl = round(total_portfolio_value - self.initial_capital, 2)

        stats = self.db.get_trade_stats(self.account_id)
        total_fees = float(stats.get("fees_inr", 0.0))
        ai_cost_total = float(self.db.get_ai_cost_for_account(self.account_id))
        ai_cost_today = float(
            self.db.get_ai_cost_for_account(self.account_id, market_hours.today_ist_str())
        )
        net_after_ai = round(total_pnl - ai_cost_total, 2)
        net_return_pct = round((total_pnl / self.initial_capital) * 100, 2) if self.initial_capital else 0.0
        net_return_after_ai_pct = (
            round((net_after_ai / self.initial_capital) * 100, 2)
            if self.initial_capital
            else 0.0
        )

        return {
            "account_id": self.account_id,
            "account_name": self.name,
            "initial_capital_inr": self.initial_capital,
            "available_cash_inr": round(self.available_cash, 2),
            "invested_margin_inr": round(invested, 2),
            "current_holdings_value_inr": round(current_holdings, 2),
            "total_portfolio_value_inr": total_portfolio_value,
            "unrealized_pnl_inr": round(unrealized, 2),
            "realized_pnl_inr": round(self.realized_pnl, 2),
            "total_pnl_inr": total_pnl,
            "gross_pnl_inr": round(total_pnl + total_fees, 2),
            "total_fees_paid_inr": round(total_fees, 2),
            "ai_cost_inr": round(ai_cost_total, 2),
            "ai_cost_today_inr": round(ai_cost_today, 2),
            "net_pnl_after_ai_inr": net_after_ai,
            "net_return_pct": net_return_pct,
            "net_return_after_ai_pct": net_return_after_ai_pct,
            "open_positions_count": len(self.positions),
            "total_orders_executed": int(stats.get("total_trades", len(self.order_history))),
            "win_rate_pct": float(stats.get("win_rate_pct", 0.0)),
            "positions": [p.to_dict() for p in self.positions.values()],
            "trading_mode": "paper_testing",
        }


class BaseBroker(ABC):
    @abstractmethod
    def get_account(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Any:
        pass

    @abstractmethod
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
        pass

    @abstractmethod
    async def close_position(
        self, symbol: str, account_id: str = DEFAULT_ACCOUNT_ID, reason: str = "manual_close"
    ) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_portfolio_summary(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Dict[str, Any]:
        pass


class PaperBroker(BaseBroker):
    def __init__(
        self,
        database: Optional[DatabaseManager] = None,
        risk_manager_instance: Optional[RiskManager] = None,
        price_lookup: Optional[Callable[[str], Optional[float]]] = None,
        enforce_market_hours: bool = True,
        now_provider=None,
    ):
        self.db = database or default_db
        self.price_lookup = price_lookup
        self.enforce_market_hours = enforce_market_hours
        self.now_provider = now_provider or market_hours.now_ist
        self.risk = risk_manager_instance or RiskManager(
            self.db,
            now_provider=self.now_provider,
            enforce_market_hours=enforce_market_hours,
        )
        self.accounts: Dict[str, Account] = {
            account_id: Account(
                account_id,
                ACCOUNT_NAMES.get(account_id, account_id.replace("_", " ").title()),
                database=self.db,
                price_lookup=price_lookup,
            )
            for account_id in ACCOUNT_IDS
        }

    def get_account(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Account:
        if account_id not in self.accounts:
            self.accounts[account_id] = Account(
                account_id,
                ACCOUNT_NAMES.get(account_id, account_id.replace("_", " ").title()),
                database=self.db,
                price_lookup=self.price_lookup,
            )
        return self.accounts[account_id]

    @property
    def initial_capital(self) -> float:
        return self.accounts[DEFAULT_ACCOUNT_ID].initial_capital

    @property
    def available_cash(self) -> float:
        return self.accounts[DEFAULT_ACCOUNT_ID].available_cash

    @property
    def realized_pnl(self) -> float:
        return self.accounts[DEFAULT_ACCOUNT_ID].realized_pnl

    @property
    def positions(self) -> Dict[str, Position]:
        return self.accounts[DEFAULT_ACCOUNT_ID].positions

    @property
    def order_history(self) -> List[Dict[str, Any]]:
        return self.accounts[DEFAULT_ACCOUNT_ID].order_history

    def _market_price(self, symbol: str) -> Optional[float]:
        if self.price_lookup:
            try:
                price = self.price_lookup(symbol)
                if price and price > 0:
                    return float(price)
            except Exception:
                pass
        price = cache.get_price(symbol)
        if price and price > 0:
            return float(price)
        try:
            quote = get_indian_stock_quote(symbol)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                price = float(quote["current_price"])
                cache.set_price(symbol, price)
                return price
        except Exception as exc:
            logger.debug("Price lookup failed for %s: %s", symbol, exc)
        return None

    def _slipped_fill(self, market_price: float, action: str, order_type: str, limit_price: Optional[float]) -> Optional[float]:
        slip = max(0.0, float(settings.PAPER_SLIPPAGE_BPS)) / 10_000.0
        if action == "BUY":
            fill = market_price * (1.0 + slip)
            if order_type == "LIMIT" and limit_price:
                if market_price > limit_price:
                    return None
                fill = min(fill, float(limit_price))
        else:
            fill = market_price * (1.0 - slip)
            if order_type == "LIMIT" and limit_price:
                if market_price < limit_price:
                    return None
                fill = max(fill, float(limit_price))
        return round(fill, 2)

    def _reject(self, order_id: str, account: Account, symbol: str, action: str,
                quantity: int, reason: str, latency_ms: float = 0.05) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "account_id": account.account_id,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "status": "REJECTED",
            "reason": reason,
            "latency_ms": round(latency_ms, 2),
        }

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
        t0 = time.perf_counter_ns()
        sym = symbol.strip().upper()
        act = action.strip().upper()
        order_type = (order_type or "MARKET").upper()
        acc = self.get_account(account_id)
        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

        if act not in ("BUY", "SELL"):
            return self._reject(order_id, acc, sym, act, quantity, "Action must be BUY or SELL.")
        if quantity <= 0:
            return self._reject(order_id, acc, sym, act, quantity, "Quantity must be greater than zero.")

        market_price = self._market_price(sym)
        if not market_price or market_price <= 0:
            return self._reject(
                order_id, acc, sym, act, quantity,
                f"Verified exchange price unavailable for {sym}; paper fill refused.",
                (time.perf_counter_ns() - t0) / 1e6,
            )

        fill_price = self._slipped_fill(market_price, act, order_type, limit_price)
        if not fill_price or fill_price <= 0:
            return self._reject(
                order_id, acc, sym, act, quantity,
                "Limit order is not marketable at the verified market price.",
                (time.perf_counter_ns() - t0) / 1e6,
            )

        if act == "BUY":
            gate = self.risk.evaluate_entry(acc.account_id, acc, sym, quantity, fill_price)
            if not gate["approved"]:
                return self._reject(
                    order_id, acc, sym, act, quantity,
                    " ".join(gate["reasons"]),
                    (time.perf_counter_ns() - t0) / 1e6,
                )

            # Never silently exceed available cash. Shrink only when the
            # requested quantity cannot be funded after realistic fees.
            max_position_qty = int(
                (acc.initial_capital * settings.MAX_POSITION_PCT) // fill_price
            )
            if quantity > max_position_qty:
                return self._reject(
                    order_id, acc, sym, act, quantity,
                    f"Position cap exceeded: max {max_position_qty} shares at ₹{fill_price:.2f}.",
                    (time.perf_counter_ns() - t0) / 1e6,
                )

            while quantity > 0:
                turnover = round(fill_price * quantity, 2)
                fee = calculate_buy_fee(turnover)
                if turnover + fee <= acc.available_cash + 0.001:
                    break
                quantity -= 1
            if quantity <= 0:
                return self._reject(
                    order_id, acc, sym, act, 0,
                    f"Insufficient cash: required at least ₹{fill_price + calculate_buy_fee(fill_price):,.2f}.",
                    (time.perf_counter_ns() - t0) / 1e6,
                )

            turnover = round(fill_price * quantity, 2)
            fee = calculate_buy_fee(turnover)
            acc.available_cash = round(acc.available_cash - turnover - fee, 2)
            pos = Position(
                sym, quantity, fill_price,
                stop_loss=stop_loss,
                target_price=target_price,
                entry_fee_inr=fee,
                hold_to_close=hold_to_close,
            )
            acc.positions[sym] = pos
            self.db.save_position(
                acc.account_id,
                sym,
                pos.quantity,
                pos.avg_price,
                pos.created_at,
                stop_loss=pos.stop_loss,
                target_price=pos.target_price,
                entry_fee_inr=pos.entry_fee_inr,
                hold_to_close=1 if pos.hold_to_close else 0,
            )
            pnl = 0.0
            gross_pnl = 0.0
            entry_fee = fee
            exit_price = None
            exit_timestamp = None
            reason = None
            proceeds = turnover
        else:
            gate = self.risk.evaluate_exit(acc.account_id, acc, sym, quantity)
            if not gate["approved"]:
                return self._reject(
                    order_id, acc, sym, act, quantity,
                    " ".join(gate["reasons"]),
                    (time.perf_counter_ns() - t0) / 1e6,
                )

            pos = acc.positions[sym]
            turnover = round(fill_price * quantity, 2)
            fee = calculate_sell_fee(turnover)
            entry_fee_alloc = round(pos.entry_fee_inr * (quantity / pos.quantity), 2)
            gross_pnl = round((fill_price - pos.avg_price) * quantity, 2)
            pnl = round(gross_pnl - entry_fee_alloc - fee, 2)

            acc.available_cash = round(acc.available_cash + turnover - fee, 2)
            acc.realized_pnl = round(acc.realized_pnl + pnl, 2)

            remaining_qty = pos.quantity - quantity
            if remaining_qty <= 0:
                del acc.positions[sym]
                self.db.delete_position(acc.account_id, sym)
            else:
                # Partial exits must preserve the original bracket.
                pos.quantity = remaining_qty
                pos.entry_fee_inr = round(pos.entry_fee_inr - entry_fee_alloc, 2)
                pos.update_price(fill_price)
                self.db.save_position(
                    acc.account_id,
                    sym,
                    pos.quantity,
                    pos.avg_price,
                    pos.created_at,
                    stop_loss=pos.stop_loss,
                    target_price=pos.target_price,
                    entry_fee_inr=pos.entry_fee_inr,
                    hold_to_close=1 if pos.hold_to_close else 0,
                )

            entry_fee = entry_fee_alloc
            exit_price = fill_price
            exit_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            reason = exit_reason or ("target" if target_price and fill_price >= target_price else "manual")
            proceeds = turnover

            strat = strategy_name or f"{acc.account_id}_quant"
            self.db.update_strategy_performance(strat, pnl=pnl, won=(pnl > 0))
            self.db.record_learning(
                account_id=acc.account_id,
                category="Trade Execution",
                insight=(
                    f"[{acc.name}] Sold {quantity} {sym} at ₹{fill_price:.2f}; "
                    f"gross ₹{gross_pnl:+,.2f}, fees ₹{entry_fee_alloc + fee:,.2f}, "
                    f"net ₹{pnl:+,.2f}."
                ),
                strategy_ref=strat,
                symbol_ref=sym,
                impact_pnl=pnl,
            )

        latency_ms = round((time.perf_counter_ns() - t0) / 1e6, 2)
        order_record = {
            "order_id": order_id,
            "account_id": acc.account_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": sym,
            "action": act,
            "quantity": quantity,
            "order_type": order_type,
            "fill_price": fill_price,
            "market_price": round(market_price, 2),
            "slippage_bps": float(settings.PAPER_SLIPPAGE_BPS),
            "total_value_inr": turnover,
            "fee_inr": fee,
            "entry_fee_inr": entry_fee,
            "gross_pnl_inr": gross_pnl,
            "pnl_inr": pnl,
            "exit_price": exit_price,
            "exit_timestamp": exit_timestamp,
            "exit_reason": reason,
            "latency_ms": latency_ms,
            "status": "FILLED",
            "strategy_name": strategy_name or f"{acc.account_id}_quant",
            "available_cash_inr": round(acc.available_cash, 2),
        }

        self.db.record_trade(order_record)
        self.db.save_account_state(acc.account_id, acc.available_cash, acc.realized_pnl)
        acc.order_history.insert(0, order_record)
        acc.order_history = acc.order_history[:100]
        risk_state = self.risk.record_fill(acc.account_id, acc, act)
        order_record["daily_risk"] = risk_state
        order_record["net_pnl_after_ai_today_inr"] = risk_state.get("net_pnl_today_inr")
        logger.info(
            "[%s] %s %s x%d @ ₹%.2f fee ₹%.2f net ₹%.2f (%s)",
            acc.account_id, act, sym, quantity, fill_price, fee, pnl, order_record["status"],
        )
        return order_record

    async def close_position(
        self,
        symbol: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
        reason: str = "manual_close",
    ) -> Optional[Dict[str, Any]]:
        acc = self.get_account(account_id)
        sym = symbol.upper()
        pos = acc.positions.get(sym)
        if not pos:
            return None
        return await self.fast_execute_order(
            sym,
            "SELL",
            pos.quantity,
            "MARKET",
            strategy_name="square_off",
            account_id=acc.account_id,
            exit_reason=reason,
        )

    def update_live_prices(self, price_dict: Dict[str, float]):
        for sym, price in price_dict.items():
            if price and price > 0:
                cache.set_price(sym, price)
                for acc in self.accounts.values():
                    if sym in acc.positions:
                        acc.positions[sym].update_price(price)

    def get_portfolio_summary(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Dict[str, Any]:
        return self.get_account(account_id).get_summary()

    def reset_account(self, account_id: Optional[str] = None, capital: Optional[float] = None) -> Dict[str, Any]:
        cap = capital or settings.INITIAL_CAPITAL_INR
        ids = [account_id] if account_id else list(ACCOUNT_IDS)
        for acc_id in ids:
            self.db.reset_account(acc_id, cap)
            self.accounts[acc_id] = Account(
                acc_id,
                ACCOUNT_NAMES.get(acc_id, acc_id.replace("_", " ").title()),
                database=self.db,
                price_lookup=self.price_lookup,
            )
        return self.accounts[account_id or DEFAULT_ACCOUNT_ID].get_summary()


class LiveBroker(BaseBroker):
    """Live connector is intentionally inert until broker credentials exist."""

    def __init__(self):
        self.api_key = os.getenv("BROKER_API_KEY", "")
        self.client_id = os.getenv("BROKER_CLIENT_ID", "")

    def get_account(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Any:
        return None

    async def fast_execute_order(self, *args, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError("Live broker credentials are not configured. Use TRADING_MODE=paper.")

    async def close_position(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        raise NotImplementedError("Live broker credentials are not configured.")

    def get_portfolio_summary(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Dict[str, Any]:
        raise NotImplementedError("Live broker credentials are not configured.")


def create_broker() -> BaseBroker:
    if getattr(settings, "TRADING_MODE", "paper").lower() == "live":
        return LiveBroker()
    return PaperBroker()


broker = create_broker()
