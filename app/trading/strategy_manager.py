"""
Autonomous Strategy Manager & Pre-Market Intelligence Orchestrator.
Coordinates active strategies, performs Google Search pre-market screening,
executes budget-constrained trades (under ₹10,000), manages market hours,
and automatically squares off positions at market close.
"""

import asyncio
import datetime
import json
import logging
from typing import Dict, Any, List, Optional
from google.genai import types

from app.config import settings, create_genai_client
from app.storage.db import db
from app.storage.cache import cache
from app.trading.execution_engine import execution_engine
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)
from app.system.tokens import token_tracker

logger = logging.getLogger("app.trading.strategy_manager")

# Curated high-liquidity NSE stocks with prices ideal for ₹10,000 testing budget
BUDGET_WATCHLIST = [
    {"symbol": "ITC", "typical_price": 470.0, "category": "FMCG / High Dividend"},
    {"symbol": "SBIN", "typical_price": 810.0, "category": "Public Sector Banking"},
    {"symbol": "TATAMOTORS", "typical_price": 384.0, "category": "Automotive / EV Growth"},
    {"symbol": "WIPRO", "typical_price": 540.0, "category": "IT / Tech Services"},
    {"symbol": "ICICIBANK", "typical_price": 1260.0, "category": "Private Banking"},
    {"symbol": "INFY", "typical_price": 1850.0, "category": "IT Giant"},
    {"symbol": "RELIANCE", "typical_price": 1280.0, "category": "Energy & Retail Conglomerate"},
]


class StrategyManager:
    """Orchestrates independent trading strategies, learning loops, and market schedules."""

    def __init__(self):
        state = db.get_account_state()
        self.auto_execute = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.last_pre_market_report: Optional[Dict[str, Any]] = None
        self._last_pre_market_date: Optional[str] = None
        self._market_open_notified: bool = False
        self._last_order_timestamp: float = 0.0
        self._candidate_index: int = 0

    def is_market_open(self) -> bool:
        """Check if current time is within active NSE market hours (09:15 - 15:30 IST, Mon-Fri)."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        # 0 = Monday, 6 = Sunday
        if ist_now.weekday() >= 5:
            return False  # Weekend
        market_open = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = ist_now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= ist_now <= market_close

    def is_near_market_close(self) -> bool:
        """Check if time is between 15:25 and 15:30 IST for auto square-off."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        if ist_now.weekday() >= 5:
            return False
        square_off_start = ist_now.replace(hour=15, minute=25, second=0, microsecond=0)
        market_close = ist_now.replace(hour=15, minute=30, second=0, microsecond=0)
        return square_off_start <= ist_now <= market_close

    def get_market_status(self) -> Dict[str, Any]:
        """
        Get detailed NSE/BSE market schedule and status in Indian Standard Time (IST).
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        is_weekend = ist_now.weekday() >= 5
        market_open_time = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close_time = ist_now.replace(hour=15, minute=30, second=0, microsecond=0)
        pre_market_time = ist_now.replace(hour=9, minute=0, second=0, microsecond=0)

        is_open = (not is_weekend) and (market_open_time <= ist_now <= market_close_time)
        is_pre_market = (not is_weekend) and (pre_market_time <= ist_now < market_open_time)

        # Calculate next market open timestamp
        if is_weekend:
            days_ahead = (7 - ist_now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 1
            next_open = (ist_now + datetime.timedelta(days=days_ahead)).replace(hour=9, minute=15, second=0, microsecond=0)
        elif ist_now < market_open_time:
            next_open = market_open_time
        else:
            days_ahead = 3 if ist_now.weekday() == 4 else 1
            next_open = (ist_now + datetime.timedelta(days=days_ahead)).replace(hour=9, minute=15, second=0, microsecond=0)

        seconds_until_open = max(0, int((next_open - ist_now).total_seconds())) if not is_open else 0

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

        return {
            "is_open": is_open,
            "is_pre_market": is_pre_market,
            "status": status_label,
            "description": desc,
            "current_time_ist": ist_now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "next_open_ist": next_open.strftime("%Y-%m-%d 09:15:00 IST"),
            "seconds_until_open": seconds_until_open,
            "auto_execute_configured": self.auto_execute,
            "auto_execute_active": bool(self.auto_execute and is_open),
            "trading_mode": getattr(settings, "TRADING_MODE", "paper"),
        }

    def set_auto_execute(self, enabled: bool) -> bool:
        """Toggle autonomous trade execution across all components."""
        self.auto_execute = enabled
        try:
            from app.trading.predictor import argon_predictor
            argon_predictor.auto_execute = enabled
        except Exception:
            pass
        db.save_account_state(
            available_cash=execution_engine.available_cash,
            total_realized_pnl=execution_engine.realized_pnl,
            auto_execute=enabled
        )
        status_info = self.get_market_status()
        if enabled:
            if status_info["is_open"]:
                logger.info("Auto-execute enabled: Market is OPEN. Autonomous trading active.")
            else:
                logger.info(f"Auto-execute enabled on STANDBY: Market is CLOSED. Trading starts at {status_info['next_open_ist']}.")
        else:
            logger.info("Auto-execute disabled: Autonomous trading paused.")
        return self.auto_execute

    async def run_pre_market_analysis(self) -> Dict[str, Any]:
        """
        Conduct pre-market intelligence using built-in Google Search grounding.
        Analyzes Gift Nifty, global cues, and screens stocks under the ₹10,000 testing budget.
        """
        api_key = settings.GEMINI_API_KEY
        query = "Indian stock market pre market Gift Nifty NSE Nifty 50 key stocks to watch today"
        
        grounded_text = ""
        sources = []
        tokens_used = 0

        if api_key and not api_key.startswith("your_"):
            try:
                client = create_genai_client(api_key)
                prompt = f"""
                You are a senior quantitative strategist for the Indian Stock Market (NSE/BSE).
                Perform a pre-market morning analysis:
                1. What are the key macroeconomic drivers and Gift Nifty sentiment today?
                2. Which budget-friendly stocks (e.g. Tata Motors, SBI, ITC, Wipro, ICICI Bank, Infosys) have positive catalysts?
                3. Provide 2-3 specific BUY/SELL bias recommendations for an investor with ₹10,000 initial capital.
                """
                response = await client.aio.models.generate_content(
                    model=settings.GEMINI_CHAT_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction="Provide a concise, quantitative pre-market plan for Indian equities with actionable levels in INR (₹).",
                        temperature=0.2,
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                    )
                )
                grounded_text = response.text or ""

                # Extract grounding metadata
                if response.candidates and response.candidates[0].grounding_metadata:
                    gm = response.candidates[0].grounding_metadata
                    chunks = getattr(gm, "grounding_chunks", None)
                    if chunks:
                        for c in chunks:
                            if hasattr(c, "web") and c.web:
                                sources.append({
                                    "title": getattr(c.web, "title", "Source"),
                                    "url": getattr(c.web, "uri", ""),
                                })

                # Estimate tokens
                prompt_toks = getattr(response.usage_metadata, "prompt_token_count", 350) if hasattr(response, "usage_metadata") else 350
                comp_toks = getattr(response.usage_metadata, "candidates_token_count", 400) if hasattr(response, "usage_metadata") else 400
                tokens_used = prompt_toks + comp_toks
                token_tracker.log_usage("pre_market_analysis", settings.GEMINI_CHAT_MODEL, prompt_toks, comp_toks)

            except Exception as e:
                logger.warning(f"Google Search Pre-Market API call: {e}. Using DuckDuckGo fallback.")

        if not grounded_text:
            # Fallback web search using DuckDuckGo
            news_res = search_indian_market_news(query, max_results=4)
            articles = news_res.get("articles", [])
            sources = [{"title": a.get("title", ""), "url": a.get("url", "")} for a in articles]
            headlines = "\n".join([f"- {a.get('title')}: {a.get('snippet')}" for a in articles[:3]])
            grounded_text = (
                f"### 🌅 Pre-Market Intelligence Brief (Simulated & Web Fallback)\n\n"
                f"**Market Tone:** Indian indices trading steady with cautious optimism near 22,500 levels.\n"
                f"**Recent Headlines:**\n{headlines}\n\n"
                f"**Budget Allocation Strategy (₹10,000 Initial Capital):**\n"
                f"- **Tata Motors (~₹920):** Target 2-3 shares on pullback near VWAP.\n"
                f"- **SBI (~₹810):** Target 3-4 shares with strict 1% stop-loss.\n"
                f"- **ITC (~₹470):** Defensive defensive hedge (5-6 shares)."
            )

        report = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "analysis_markdown": grounded_text,
            "sources": sources,
            "tokens_used": tokens_used,
            "recommended_budget_stocks": BUDGET_WATCHLIST[:4],
        }

        self.last_pre_market_report = report

        # Record learning entry into SQLite
        db.record_learning(
            category="Pre-Market Screening",
            insight="Pre-market scan completed. Screened budget-friendly Indian equities for ₹10,000 account.",
            strategy_ref="pre_market_gap_scalp"
        )

        return report

    async def auto_square_off_all_positions(self) -> List[Dict[str, Any]]:
        """Automatically close all open positions across all agent accounts at market close."""
        closed_orders = []
        for acc_id in ["gemini_flash", "deepseek_flash"]:
            acc = execution_engine.get_account(acc_id)
            if not acc:
                continue
            for sym in list(acc.positions.keys()):
                closed = await execution_engine.close_position(sym, account_id=acc_id)
                if closed:
                    closed_orders.append(closed)
                    logger.info(f"[{acc_id}] Auto square-off executed for {sym}: {closed.get('status')}")

        # Update daily performance snapshot
        portfolio = execution_engine.get_portfolio_summary()
        today_str = datetime.date.today().isoformat()
        token_sum = db.get_token_summary()

        for a_id in ["gemini_flash", "deepseek_flash"]:
            p_agent = execution_engine.get_portfolio_summary(a_id)
            db.record_daily_snapshot(
                date_str=today_str,
                starting_balance=p_agent["initial_capital_inr"],
                ending_balance=p_agent["total_portfolio_value_inr"],
                realized_pnl=p_agent["realized_pnl_inr"],
                trades_count=p_agent["total_orders_executed"],
                win_rate_pct=p_agent.get("net_return_pct", 0.0),
                total_tokens=token_sum.get("total_tokens", 0) // 2,
                ai_cost_inr=token_sum.get("total_cost_inr", 0.0) / 2.0,
                account_id=a_id,
            )

        return closed_orders

    async def evaluate_budget_trade(self, symbol: str, signal: str, confidence: int) -> Optional[Dict[str, Any]]:
        """
        Evaluate and execute an order sized specifically for the ₹10,000 initial capital.
        STRICT REQUIREMENT: Auto-execution only runs AFTER market open (09:15 - 15:30 IST).
        If market is closed, execution safely stands by even if auto-execute is enabled.
        """
        if not self.auto_execute:
            return None

        # STRICT CHECK: Do NOT execute automated orders outside market hours!
        if not self.is_market_open():
            logger.info(
                f"⏸️ Auto-execute is ENABLED, but NSE market is currently CLOSED. "
                f"Holding trade on {symbol} ({signal}). Execution will start automatically when market opens at 09:15 IST."
            )
            return {
                "order_id": None,
                "symbol": symbol,
                "action": signal,
                "status": "STANDBY_MARKET_CLOSED",
                "reason": "Market is closed. Autonomous trading runs strictly between 09:15 - 15:30 IST.",
                "market_open": False,
            }

        # Fetch verified market price from cache or exchange
        price = cache.get_price(symbol)
        if not price or price <= 0:
            quote = get_indian_stock_quote(symbol)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                price = quote["current_price"]
                cache.set_price(symbol, price)
            else:
                logger.warning(f"Real-time exchange quote unavailable for {symbol}. Trade aborted.")
                return None

        avail_cash = execution_engine.available_cash

        if signal == "BUY" and confidence >= 70:
            # Sizing: Allocate up to 35% of available funds or 1 share minimum
            allocated = min(avail_cash, settings.INITIAL_CAPITAL_INR * 0.35)
            if allocated < price:
                allocated = avail_cash
            qty = int(allocated // price)
            if qty > 0 and avail_cash >= (price * qty):
                return await execution_engine.fast_execute_order(
                    symbol=symbol,
                    action="BUY",
                    quantity=qty,
                    order_type="MARKET",
                    strategy_name="argon_ai_momentum"
                )

        elif signal == "SELL":
            if symbol in execution_engine.positions:
                held_qty = execution_engine.positions[symbol].quantity
                return await execution_engine.fast_execute_order(
                    symbol=symbol,
                    action="SELL",
                    quantity=held_qty,
                    order_type="MARKET",
                    strategy_name="argon_ai_momentum"
                )

        return None

    def _select_next_budget_candidate(self) -> str:
        """Pick next stock from budget watchlist suitable for ₹10,000 testing capital."""
        sym = BUDGET_WATCHLIST[self._candidate_index % len(BUDGET_WATCHLIST)]["symbol"]
        self._candidate_index += 1
        return sym

    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._strategy_loop())
            logger.info("Started Autonomous Strategy Manager background task.")

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("Stopped Autonomous Strategy Manager background task.")

    async def _strategy_loop(self):
        """Continuous background loop checking pre-market, intraday signals, active positions, and EOD square-offs."""
        while self.is_running:
            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)

                # Pre-Market Screening trigger (runs automatically between 09:00 and 09:14 IST once per day)
                if ist_now.weekday() < 5 and ist_now.hour == 9 and 0 <= ist_now.minute < 14:
                    today_str = ist_now.date().isoformat()
                    if self._last_pre_market_date != today_str:
                        self._last_pre_market_date = today_str
                        logger.info("Triggering automated Pre-Market Google Search analysis...")
                        await self.run_pre_market_analysis()

                # Market Open Notification & Learning Record
                if self.is_market_open():
                    if not self._market_open_notified:
                        self._market_open_notified = True
                        logger.info("🔔 NSE Market is OPEN (09:15 IST)! Autonomous trading is ACTIVE.")
                        db.record_learning(
                            category="Market Schedule",
                            insight="NSE Market Open (09:15 IST). Autonomous budget execution is active.",
                            strategy_ref="market_schedule"
                        )

                    # INTRADAY RISK ENGINE (During Market Open)
                    # Manage Active Positions across all trading agent accounts: Day Trader SL/TP or target (+1.5%) / SL (-1.2%)
                    if self.auto_execute:
                        for acc_id in ["gemini_flash", "deepseek_flash"]:
                            acc = execution_engine.get_account(acc_id)
                            if not acc:
                                continue
                            for sym in list(acc.positions.keys()):
                                pos = acc.positions.get(sym)
                                if not pos:
                                    continue

                                # Update with verified live price
                                live_p = cache.get_price(sym)
                                if not live_p or live_p <= 0:
                                    quote = get_indian_stock_quote(sym)
                                    if quote.get("success") and quote.get("current_price", 0) > 0:
                                        live_p = quote["current_price"]
                                        cache.set_price(sym, live_p)
                                if live_p and live_p > 0:
                                    pos.update_price(live_p)

                                # Day-Trader Exit Check: Target or Midpoint Stop Hit
                                if pos.target_price and pos.current_price >= pos.target_price:
                                    logger.info(f"🎯 [{acc_id}] Day-Trader Target (₹{pos.target_price}) reached for {sym} (current: ₹{pos.current_price}). Auto square-off.")
                                    await execution_engine.close_position(sym, account_id=acc_id)
                                elif pos.stop_loss and pos.current_price <= pos.stop_loss:
                                    logger.info(f"🛡️ [{acc_id}] Day-Trader Stop-Loss (₹{pos.stop_loss}) hit for {sym} (current: ₹{pos.current_price}). Protecting capital.")
                                    await execution_engine.close_position(sym, account_id=acc_id)
                                elif pos.unrealized_pnl_pct >= 1.5:
                                    logger.info(f"🎯 [{acc_id}] Target Profit (+{pos.unrealized_pnl_pct}%) reached for {sym}. Auto square-off.")
                                    await execution_engine.close_position(sym, account_id=acc_id)
                                elif pos.unrealized_pnl_pct <= -1.2:
                                    logger.info(f"🛡️ [{acc_id}] Stop-Loss ({pos.unrealized_pnl_pct}%) hit for {sym}. Protecting capital.")
                                    await execution_engine.close_position(sym, account_id=acc_id)
                else:
                    self._market_open_notified = False

                # Check for EOD auto square-off (at 15:25 IST)
                if self.is_near_market_close():
                    has_any_positions = any(
                        len(acc.positions) > 0
                        for a in ["gemini_flash", "deepseek_flash"]
                        if (acc := execution_engine.get_account(a)) is not None
                    )
                    if has_any_positions:
                        logger.info("Near NSE market close (15:25 IST). Triggering automated square-off across all agent accounts...")
                        await self.auto_square_off_all_positions()

            except Exception as e:
                logger.error(f"Error in strategy manager loop: {e}")

            await asyncio.sleep(10.0)


# Global Strategy Manager instance
strategy_manager = StrategyManager()

