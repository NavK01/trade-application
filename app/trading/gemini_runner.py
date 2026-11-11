"""
Gemini 4 Autonomous Live Execution Runner
Continuously evaluates live Indian equities on NSE using the Gemini 4 Calibrated Strategy.
Manages trailing breakeven stops (1.05R), target profit booking, and protective risk boundaries.
"""

import asyncio
import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd
import pytz

from app.config import settings
from app.trading.candles import get_symbol_candles
from app.storage.db import db
from app.storage.cache import cache
from app.trading.execution_engine import execution_engine
from app.trading.gemini_strategy import (
    generate_gemini_optimized_signal,
    BREAKEVEN_TRIGGER_R,
    MAX_POSITION_COST,
)
from app.trading import market_hours

logger = logging.getLogger("gemini_runner")
IST = pytz.timezone("Asia/Kolkata")
ACCOUNT_ID = "gemini_optimized"


class GeminiOptimizedRunner:
    def __init__(self, interval_seconds: int = 15):
        self.interval_seconds = interval_seconds
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.symbols = [
            "SBIN", "ITC", "TATASTEEL", "TATAMOTORS",
            "ICICIBANK", "AXISBANK", "NTPC", "COALINDIA"
        ]
        self.latest_signals: Dict[str, Any] = {}
        self.last_run_timestamp: Optional[str] = None
        self.cycle_count = 0
        self.subscribers: List[asyncio.Queue] = []
        self.auto_execute = True

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=100)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def broadcast(self, data: Dict[str, Any]):
        for q in list(self.subscribers):
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("⚡ Gemini 4 Autonomous Execution Runner started.")

    def stop_background_loop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("🛑 Gemini 4 Autonomous Execution Runner stopped.")

    async def _run_loop(self):
        while self.is_running:
            try:
                await self.evaluate_and_execute_cycle()
            except Exception as e:
                logger.error(f"Error in Gemini 4 runner cycle: {e}", exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def evaluate_and_execute_cycle(self) -> Dict[str, Any]:
        """
        Runs one cycle of the Gemini 4 Calibrated Live Strategy:
        1. Checks open positions: Trails stop to breakeven at 1.05R, checks SL/TP.
        2. Evaluates live setups with calibrated filters (TP >= 1.25%, SL >= 0.65%, Vol >= 2.0x).
        3. Executes high-conviction trades within ₹2,800 max allocation.
        """
        self.cycle_count += 1
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.last_run_timestamp = now_utc

        acc = execution_engine.get_account(ACCOUNT_ID)
        if not acc:
            return {"status": "NO_ACCOUNT"}

        enforce_hours = getattr(settings, "ENFORCE_MARKET_HOURS", True)
        market_open = (not enforce_hours) or market_hours.is_market_open()
        entry_window = (not enforce_hours) or market_hours.is_entry_window()
        near_close = enforce_hours and market_hours.is_near_market_close()

        summary_results = {}

        if market_open:
            # 1. 15:20 IST EOD Square-Off
            if near_close and acc.positions:
                logger.info("15:20 IST reached; squaring off all Gemini 4 positions.")
                for sym in list(acc.positions.keys()):
                    await execution_engine.close_position(sym, account_id=ACCOUNT_ID, reason="eod_square_off")

            # 2. Position Management & Trailing Breakeven at 1.05R
            for sym in list(acc.positions.keys()):
                pos = acc.positions.get(sym)
                if not pos:
                    continue

                quote = get_symbol_candles(symbol=sym, interval="5m", period="1d")
                live_price = quote.get("latest_price", 0.0)
                if live_price <= 0:
                    live_price = cache.get_price(sym) or 0.0

                if live_price > 0:
                    pos.update_price(live_price)
                    cache.set_price(sym, live_price)

                    # Check Trailing Breakeven Trigger (1.05R)
                    entry_p = getattr(pos, "avg_price", getattr(pos, "average_price", 0.0))
                    sl_p = pos.stop_loss
                    if sl_p and entry_p > 0:
                        risk = abs(entry_p - sl_p)
                        # For Long positions
                        if pos.quantity > 0:
                            be_trigger = entry_p + (risk * BREAKEVEN_TRIGGER_R)
                            if live_price >= be_trigger and sl_p < entry_p:
                                new_sl = round(entry_p * 1.001, 2)  # Lock in small buffer to cover fees
                                pos.stop_loss = new_sl
                                logger.info(f"🔒 Trailed stop to BREAKEVEN for {sym} at ₹{new_sl} (+1.05R reached).")
                        # For Short positions
                        elif pos.quantity < 0:
                            be_trigger = entry_p - (risk * BREAKEVEN_TRIGGER_R)
                            if live_price <= be_trigger and sl_p > entry_p:
                                new_sl = round(entry_p * 0.999, 2)
                                pos.stop_loss = new_sl
                                logger.info(f"🔒 Trailed stop to BREAKEVEN for {sym} at ₹{new_sl} (+1.05R reached).")

                    # Protective SL / Target exit check
                    exit_reason = None
                    if pos.quantity > 0:
                        if pos.stop_loss and live_price <= pos.stop_loss:
                            exit_reason = "stop_loss"
                        elif pos.target_price and live_price >= pos.target_price:
                            exit_reason = "target_reached"
                    elif pos.quantity < 0:
                        if pos.stop_loss and live_price >= pos.stop_loss:
                            exit_reason = "stop_loss"
                        elif pos.target_price and live_price <= pos.target_price:
                            exit_reason = "target_reached"

                    if exit_reason:
                        logger.info(f"🎯 Exiting {sym} ({exit_reason}) at ₹{live_price:.2f}")
                        await execution_engine.close_position(sym, account_id=ACCOUNT_ID, reason=exit_reason)

        # 3. Evaluate Universe for High-Conviction Entries
        portfolio = execution_engine.get_portfolio_summary(ACCOUNT_ID)
        net_profit_pct = portfolio.get("net_return_pct", 0.0)
        drawdown_pct = 0.0

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

                sig = generate_gemini_optimized_signal(
                    symbol=sym,
                    spot_price=spot,
                    df=df,
                    daily_net_profit_pct=net_profit_pct,
                    consecutive_losses=0,
                    current_drawdown_pct=drawdown_pct
                )

                self.latest_signals[sym] = sig
                summary_results[sym] = sig

                # Auto-Execute High-Conviction Entry
                if self.auto_execute and entry_window:
                    can_trade = sig.get("guardrail_status", {}).get("can_trade", False)
                    action = sig.get("action")
                    confidence = sig.get("confidence", 0)

                    if can_trade and action in ("BUY", "SELL") and confidence >= 70:
                        if sym not in acc.positions and len(acc.positions) < settings.MAX_OPEN_POSITIONS:
                            # Strict capital allocation: max ₹2,800 per trade
                            alloc = min(MAX_POSITION_COST, acc.available_cash)
                            qty = int(alloc // spot) if spot > 0 else 0

                            if qty >= 1 and (qty * spot) <= acc.available_cash:
                                logger.info(
                                    f"⚡ Gemini 4 Executing Auto-Entry for {sym}: {action} {qty} qty @ ₹{spot:.2f} "
                                    f"(SL: {sig['stop_loss']}, TP: {sig['target_price']})"
                                )
                                await execution_engine.fast_execute_order(
                                    symbol=sym,
                                    action=action,
                                    quantity=qty,
                                    order_type="MARKET",
                                    strategy_name=sig.get("setup_name", "gemini_calibrated_sweep"),
                                    account_id=ACCOUNT_ID,
                                    stop_loss=sig.get("stop_loss"),
                                    target_price=sig.get("target_price"),
                                )

            except Exception as e:
                logger.warning(f"Error evaluating {sym} in Gemini 4 runner: {e}")

        # Broadcast live tick snapshot
        payload = {
            "type": "gemini_institutional_update",
            "cycle": self.cycle_count,
            "timestamp": now_utc,
            "auto_execute": self.auto_execute,
            "market_open": market_hours.is_market_open(),
            "signals": self.latest_signals,
            "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
        }
        self.broadcast(payload)
        return summary_results


gemini_runner = GeminiOptimizedRunner(interval_seconds=15)
