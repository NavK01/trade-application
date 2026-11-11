"""
Autonomous Institutional Runner for Indian Indices (NIFTY, BANK NIFTY, SENSEX)

Exclusively powered by DeepSeek Institutional Quant Framework:
- Evaluates GEX Regime, 4-Lines, POC/VAH/VAL, and Liquidity Sweeps every cycle
- Auto-executes 1-lot Index Options setups (1:2 R:R) during market hours
- Enforces hard +0.50% daily profit lock, 2-strike loss lockout, and 15:20 IST square-off
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from app.config import DEFAULT_ACCOUNT_ID, settings
from app.storage.cache import cache
from app.storage.db import db
from app.trading import market_hours
from app.trading.candles import get_symbol_candles
from app.trading.execution_engine import execution_engine
from app.trading.institutional_engine import (
    generate_institutional_ai_signal,
    evaluate_risk_guardrails,
    INDEX_CONFIG,
)

logger = logging.getLogger("institutional_runner")

# Indian Stocks with share prices ~₹150 to ~₹1,250 that can be bought directly within ₹10,000 capital
DEFAULT_STOCKS: List[str] = [
    "SBIN",        # State Bank of India (~₹800)
    "ITC",         # ITC Ltd (~₹480)
    "TATASTEEL",   # Tata Steel (~₹150)
    "TATAMOTORS",  # Tata Motors (~₹850)
    "ICICIBANK",   # ICICI Bank (~₹1,250)
    "AXISBANK",    # Axis Bank (~₹1,150)
    "NTPC",        # NTPC Ltd (~₹380)
    "COALINDIA",   # Coal India (~₹430)
]
DEFAULT_INDICES: List[str] = ["NIFTY", "BANKNIFTY", "SENSEX"]
# Autonomous execution exclusively trades liquid stocks (~₹150 - ₹1,400) for ₹10,000 capital accounts
DEFAULT_SYMBOLS: List[str] = list(DEFAULT_STOCKS)


class InstitutionalRunner:
    """Autonomous trading engine for Indian Stocks (~₹150-₹1,250) and Indices."""

    def __init__(self, symbols: Optional[List[str]] = None):
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.interval_seconds = 60  # Evaluates portfolio symbols every 60 seconds
        self.symbols: List[str] = list(symbols or DEFAULT_SYMBOLS)
        self.indices: List[str] = self.symbols  # backwards compatibility alias
        
        # State loaded from DB or settings
        state = db.get_account_state(DEFAULT_ACCOUNT_ID)
        self.auto_execute = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))
        
        # Memory storage for latest evaluated signals
        self.latest_signals: Dict[str, Dict[str, Any]] = {}
        self.cycle_count = 0
        self.last_run_timestamp: Optional[str] = None
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def broadcast(self, payload: Dict[str, Any]):
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._runner_loop())
            logger.info("⚡ Started Institutional Autonomous Runner (Symbols: %s). Auto-execute: %s",
                        ", ".join(self.symbols), self.auto_execute)

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("🛑 Stopped Institutional Autonomous Runner.")

    async def _runner_loop(self):
        while self.is_running:
            try:
                await self.evaluate_and_execute_cycle()
            except Exception as e:
                logger.error("Error in institutional runner cycle: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def evaluate_and_execute_cycle(self) -> Dict[str, Any]:
        """
        Executes one full cycle across tradable stocks and indices:
        1. Manages existing open positions (SL/TP check, 15:20 IST square-off)
        2. Evaluates institutional setups for each stock and index
        3. Fires auto-entry if conditions and risk gates allow
        """
        self.cycle_count += 1
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.last_run_timestamp = now_utc
        now_time = datetime.datetime.now().time()
        
        account_id = DEFAULT_ACCOUNT_ID
        acc = execution_engine.get_account(account_id)
        if not acc:
            return {"status": "NO_ACCOUNT"}

        # ---------------------------------------------------------
        # STEP 1: Monitor Active Positions & Auto Square-Off
        # ---------------------------------------------------------
        enforce_hours = getattr(settings, "ENFORCE_MARKET_HOURS", True)
        market_open = (not enforce_hours) or market_hours.is_market_open()
        entry_window = (not enforce_hours) or market_hours.is_entry_window()
        near_close = enforce_hours and market_hours.is_near_market_close()

        if market_open:
            # Check 15:20 IST End of Day square-off
            if near_close and acc.positions:
                logger.info("15:20 IST window reached; flattening all intraday positions.")
                for sym in list(acc.positions.keys()):
                    pos = acc.positions.get(sym)
                    if pos is not None and getattr(pos, "delivery_hold", False):
                        continue
                    await execution_engine.close_position(sym, account_id=account_id, reason="eod_square_off")

            # Check individual Stop Loss / Target for open positions
            for sym in list(acc.positions.keys()):
                pos = acc.positions.get(sym)
                if not pos:
                    continue
                
                # Fetch live spot price
                quote = get_symbol_candles(symbol=sym, interval="5m", period="1d")
                live_price = quote.get("latest_price", 0.0)
                if live_price <= 0:
                    live_price = cache.get_price(sym) or 0.0

                if live_price > 0:
                    pos.update_price(live_price)
                    cache.set_price(sym, live_price)

                    # Protective SL / Target exit check
                    exit_reason = None
                    if pos.stop_loss and live_price <= pos.stop_loss:
                        exit_reason = "stop_loss"
                    elif pos.target_price and live_price >= pos.target_price:
                        exit_reason = "target_reached"
                    
                    if exit_reason:
                        logger.info("Position %s hit %s at ₹%.2f. Closing...", sym, exit_reason, live_price)
                        await execution_engine.close_position(sym, account_id=account_id, reason=exit_reason)

        # ---------------------------------------------------------
        # STEP 2: Evaluate Institutional Signals for All Tradable Symbols
        # ---------------------------------------------------------
        summary_results = {}
        daily_risk = db.get_daily_risk_state(account_id)
        start_eq = float(daily_risk.get("start_equity", settings.INITIAL_CAPITAL_INR))
        realized_today = float(daily_risk.get("realized_pnl", 0.0))
        net_profit_pct = round((realized_today / start_eq) * 100.0, 2) if start_eq > 0 else 0.0
        drawdown_pct = round(abs(min(realized_today, 0.0)) / start_eq * 100.0, 2) if start_eq > 0 else 0.0

        # Calculate consecutive losses from trade ledger
        consecutive_losses = 0
        recent_trades = db.get_trades(account_id, limit=6)
        for t in recent_trades:
            if t.get("action") == "SELL":
                if float(t.get("pnl_inr", 0.0)) < 0:
                    consecutive_losses += 1
                else:
                    break

        for sym in self.symbols:
            try:
                candles_data = get_symbol_candles(symbol=sym, interval="5m", period="5d")
                spot = candles_data.get("latest_price", 0.0)
                if spot <= 0:
                    spot = cache.get_price(sym) or 0.0
                candles = candles_data.get("candles", [])
                
                df = pd.DataFrame(candles)
                if not df.empty and "open" in df.columns:
                    df = df.rename(columns={
                        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"
                    })

                sig = generate_institutional_ai_signal(
                    symbol=sym,
                    spot_price=spot,
                    df=df,
                    daily_net_profit_pct=net_profit_pct,
                    consecutive_losses=consecutive_losses,
                    current_drawdown_pct=drawdown_pct
                )
                
                self.latest_signals[sym] = sig
                summary_results[sym] = sig

                # ---------------------------------------------------------
                # STEP 3: Auto-Execute Entry If Conditions Allow
                # ---------------------------------------------------------
                if self.auto_execute and entry_window:
                    can_trade = (
                        sig.get("guardrail_status", {}).get("can_trade", False)
                        or sig.get("guardrails", {}).get("can_trade", False)
                    )
                    action = sig.get("action")
                    confidence = sig.get("confidence_score") or sig.get("confidence") or 0

                    # Only execute high-confidence A+ setups when no existing position is open in this symbol
                    if can_trade and action in ("BUY", "SELL") and confidence >= 70:
                        if sym not in acc.positions and len(acc.positions) < settings.MAX_OPEN_POSITIONS:
                            is_index = sym in INDEX_CONFIG
                            if is_index:
                                qty = 1  # 1 lot
                            else:
                                # Budget quantity: max 30% of ₹10,000 per stock position (~₹2,500 - ₹3,000)
                                qty = acc.calculate_budget_quantity(sym, spot, max_alloc_pct=0.30)
                                if qty <= 0:
                                    qty = sig.get("quantity", 1)

                            logger.info("⚡ Executing Auto-Entry for %s: %s %d qty @ ₹%.2f (SL: %.2f, TP: %.2f)",
                                        sym, action, qty, spot, sig["stop_loss"], sig["target_price"])
                            await execution_engine.fast_execute_order(
                                symbol=sym,
                                action=action,
                                quantity=qty,
                                order_type="MARKET",
                                strategy_name=sig.get("setup_name") or sig.get("setup", "institutional_sweep_reversal"),
                                account_id=account_id,
                                stop_loss=sig.get("stop_loss"),
                                target_price=sig.get("target_price"),
                            )

            except Exception as exc:
                logger.warning("Error evaluating symbol %s: %s", sym, exc)

        # Broadcast real-time snapshot
        payload = {
            "type": "institutional_update",
            "cycle": self.cycle_count,
            "timestamp": now_utc,
            "auto_execute": self.auto_execute,
            "market_open": market_hours.is_market_open(),
            "signals": self.latest_signals,
            "portfolio": execution_engine.get_portfolio_summary(account_id),
        }
        self.broadcast(payload)
        return summary_results


# Global Singleton Instance
institutional_runner = InstitutionalRunner()
