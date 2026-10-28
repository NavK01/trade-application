"""
Multi-Tiered Autonomous AI Market Predictor & Dual Agent Arena Controller.
Runs parallel 5-minute prediction cycles for:
1. Account A: Gemini 3.8 Flash (₹10,000 initial credits)
2. Account B: DeepSeek Flash MaaS (₹10,000 initial credits)
Features dynamic market scanning (30+ liquid Indian stocks) and day-trader-skill integration.
Strictly enforces daily 100K token quota and market hours (09:15 - 15:30 IST).
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Dict, Any, List, Optional
from google.genai import types

from app.config import settings, create_genai_client
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)
from app.trading.execution_engine import execution_engine
from app.trading.scanner import scanner
from app.trading.skills.day_trader_adapter import day_trader
from app.trading.sentiment_analyzer import sentiment_engine
from app.storage.db import db
from app.storage.cache import cache
from app.system.tokens import token_tracker

logger = logging.getLogger("argon_predictor")

PREDICTOR_SYSTEM_PROMPT = """You are an elite quantitative trading strategist for the Indian Stock Market (NSE/BSE), powered by the Day-Trader-Skill system.
Analyze live intraday prices, opening range breakout (ORB) levels, Monte Carlo Time Machine simulation odds, and breaking news to produce high-probability BUY, SELL, or HOLD predictions for Indian equities.
You are given your agent account context and quantitative Day-Trader setups.

Trading Rules & Day-Trader Discipline:
1. Examine day_trader_skill_setup:
   - Review ORB levels (entry zone, midpoint stop loss, target 1, target 2).
   - Review Time Machine odds (win rate %, expected value E[R], and recommendation).
2. If Time Machine recommendation is 'EXECUTE' and win_rate_pct >= 52%, formulate a high-conviction BUY signal with stop_loss_inr = ORB stop loss, and target_1_inr = ORB target 1.
3. If Time Machine recommendation is 'PASS' or win_rate_pct < 50%, recommend HOLD or PASS to preserve capital.
4. Set risk_reward_ratio to at least 1:1.5.

Respond strictly with a valid JSON object matching this schema:
{
  "signal": "BUY" | "SELL" | "HOLD",
  "symbol": "<TICKER>",
  "confidence_pct": <integer between 50 and 99>,
  "current_price_inr": <float>,
  "entry_zone_inr": "<e.g. 918 - 922>",
  "target_1_inr": <float>,
  "target_2_inr": <float>,
  "stop_loss_inr": <float>,
  "risk_reward_ratio": "<e.g. 1:2.0>",
  "timeframe": "<5-15 Min Scalp OR 30-60 Min Strategic>",
  "catalyst": "<Brief 1-2 sentence rationale based on live price action, ORB breakout, or news>",
  "action_summary": "<Direct trade action instruction>"
}
Ensure calculations strictly adhere to current live prices in Indian Rupees (INR / ₹). Return raw JSON.
"""

EMERGENCY_SYSTEM_PROMPT = """You are an Emergency Risk Manager operating inside an active high-speed Indian stock market terminal.
An active position is experiencing a sudden adverse move or market shock.
Decide immediately whether to:
1. "EMERGENCY_EXIT" (Instantly close entire position at market to prevent catastrophic loss)
2. "TIGHTEN_STOP" (Move stop-loss closer to current price to preserve capital)
3. "HOLD_NOISE" (Tolerate normal intraday pullback without closing)

Respond with valid JSON:
{
  "action": "EMERGENCY_EXIT" | "TIGHTEN_STOP" | "HOLD_NOISE",
  "symbol": "<TICKER>",
  "rationale": "<Crisp 1-2 sentence reason in INR>",
  "new_stop_loss_inr": <float or null>
}
"""


class ArgonPredictor:
    """
    Autonomous AI Predictor & Dual Arena Orchestrator:
    - Parallel execution: Gemini 3.8 Flash vs DeepSeek Flash MaaS
    - Dynamic Multi-Stock Scanner beyond fixed watchlists
    - Day-Trader-Skill ORB, ATR stop, and Time Machine integration
    - Auto-execution on independent accounts (₹10,000 initial credits each)
    """

    def __init__(self, interval_seconds: int = 300):
        self.interval_seconds = interval_seconds  # 300s = 5 minutes
        self.cycle_count = 0
        self.last_prediction: Optional[Dict[str, Any]] = None
        self.gemini_last_prediction: Optional[Dict[str, Any]] = None
        self.deepseek_last_prediction: Optional[Dict[str, Any]] = None
        self.prediction_history: List[Dict[str, Any]] = []
        self.next_run_timestamp: Optional[str] = None
        self.is_running = False
        state = db.get_account_state("gemini_flash")
        self.auto_execute = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))
        self.subscribers: List[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None

        # Fallback watchlist if scanner has not yet populated
        self.tracked_symbols = ["SBIN", "RELIANCE", "TATASTEEL", "INFY", "ITC", "HDFCBANK", "ICICIBANK"]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
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

    def determine_attention_level(self) -> Dict[str, Any]:
        """Adaptive attention mechanism to conserve tokens and align with NSE market hours."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        current_minute = ist_now.hour * 60 + ist_now.minute
        weekday = ist_now.weekday()

        # Check open positions in either account
        gemini_pos = execution_engine.get_portfolio_summary("gemini_flash").get("positions", [])
        deepseek_pos = execution_engine.get_portfolio_summary("deepseek_flash").get("positions", [])
        has_open_positions = bool(gemini_pos or deepseek_pos)

        # Weekend (Saturday=5, Sunday=6)
        if weekday in (5, 6):
            return {
                "level": "SLEEP_OFF_MARKET",
                "interval_seconds": 900,
                "allow_ai_call": False,
                "reason": "Weekend: Indian markets closed. Next trading session opens Monday 09:15 IST."
            }

        # Market Hours: 09:15 to 15:30 IST
        market_open_minute = 9 * 60 + 15
        market_close_minute = 15 * 60 + 30

        if current_minute < market_open_minute or current_minute >= market_close_minute:
            return {
                "level": "SLEEP_OFF_MARKET",
                "interval_seconds": 900,
                "allow_ai_call": False,
                "reason": "Off-market hours (NSE/BSE trading hours: 09:15 - 15:30 IST)."
            }

        # Active market hours
        return {
            "level": "NORMAL",
            "interval_seconds": 300,
            "allow_ai_call": True,
            "reason": "Active Indian market hours. Running 5-min Gemini vs DeepSeek Arena cycle."
        }

    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("Autonomous Dual AI Prediction loop initiated.")

    def stop_background_loop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        await asyncio.sleep(5)  # 5s warmup
        while self.is_running:
            attention_info = self.determine_attention_level()
            self.interval_seconds = attention_info["interval_seconds"]
            self.cycle_count += 1

            try:
                logger.info(f"⚡ Starting Dual Arena Cycle #{self.cycle_count} (Mode: {attention_info['level']}, interval: {self.interval_seconds}s)")
                await asyncio.wait_for(self.run_arena_cycle(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.error(f"Arena prediction cycle #{self.cycle_count} timed out after 60s. Advancing to next interval.")
            except Exception as e:
                logger.error(f"Error in arena prediction cycle #{self.cycle_count}: {e}", exc_info=True)

            # Periodic memory & storage pruning every 12 cycles (~1 hour)
            if self.cycle_count % 12 == 0:
                try:
                    db.prune_historical_records()
                except Exception:
                    pass

            try:
                attention_info = self.determine_attention_level()
                self.interval_seconds = attention_info["interval_seconds"]
                next_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.interval_seconds)
                self.next_run_timestamp = next_time.isoformat()

                self.broadcast({
                    "type": "schedule_update",
                    "next_run_timestamp": self.next_run_timestamp,
                    "interval_seconds": self.interval_seconds,
                    "cycle_count": self.cycle_count,
                    "attention_mode": attention_info["level"],
                    "attention_reason": attention_info["reason"],
                })
            except Exception as broadcast_err:
                logger.error(f"Failed to broadcast schedule update: {broadcast_err}")

            await asyncio.sleep(self.interval_seconds)

    async def run_arena_cycle(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes a 5-minute Dual Agent Arena cycle:
        1. Dynamically screens market for top high-return breakout stocks.
        2. Calls Gemini 3.8 Flash for Account A (Gemini).
        3. Calls DeepSeek Flash MaaS for Account B (DeepSeek).
        4. Auto-executes approved trades independently.
        5. Returns comparative Arena results.
        """
        # 1. Screen market for high-return opportunities
        if not target_symbol:
            try:
                scanned = await asyncio.wait_for(scanner.scan_market(limit=5), timeout=15.0)
                candidates = [s["symbol"] for s in scanned if s.get("symbol")]
                if candidates:
                    sym = candidates[(self.cycle_count - 1) % len(candidates)]
                else:
                    sym = self.tracked_symbols[(self.cycle_count - 1) % len(self.tracked_symbols)]
            except Exception as e:
                logger.debug(f"Scanner fetch in arena cycle failed: {e}")
                sym = self.tracked_symbols[(self.cycle_count - 1) % len(self.tracked_symbols)]
        else:
            sym = target_symbol.strip().upper()

        attention_info = self.determine_attention_level()
        attention_level = attention_info["level"]
        self.interval_seconds = attention_info["interval_seconds"]

        # Fetch verified market data from NSE/BSE
        quote = get_indian_stock_quote(sym)
        if not quote.get("success") or quote.get("current_price", 0) <= 0:
            logger.warning(f"Live quote unavailable for {sym}. Falling back to high-liquidity benchmark SBIN.")
            sym = "SBIN"
            quote = get_indian_stock_quote(sym)
            if not quote.get("success") or quote.get("current_price", 0) <= 0:
                logger.error("Live exchange quote completely unreachable. Skipping prediction cycle.")
                return {"error": "Exchange live data unreachable", "status": "FAILED"}

        price = quote["current_price"]
        loop = asyncio.get_event_loop()
        try:
            indices = await loop.run_in_executor(None, get_indian_market_indices)
        except Exception:
            indices = {"indices": {}}

        try:
            news = await asyncio.wait_for(
                loop.run_in_executor(None, search_indian_market_news, f"{sym} stock NSE news earnings"),
                timeout=8.0
            )
        except Exception:
            news = {"articles": []}

        # Day-trader-skill ORB calculation
        orb_plan = day_trader.calculate_orb_plan(sym, price, [])
        sim_odds = day_trader.run_time_machine_simulation(sym, price, orb_plan["stop_loss"], orb_plan["target_2"])

        # Real-time Sentiment & Sector Intelligence (fetched once and shared between agents)
        try:
            sentiment_intel = await asyncio.wait_for(
                sentiment_engine.analyze_stock_sentiment(sym),
                timeout=8.0
            )
        except Exception as e:
            logger.warning(f"Sentiment analysis timeout for {sym}: {e}")
            sentiment_intel = {
                "symbol": sym,
                "sentiment_score": 0.15,
                "sentiment_label": "MODERATE_BULLISH",
                "sector_name": "General Indian Equities",
                "sector_status": "MODERATE_BULLISH",
                "macro_driver": "Domestic GDP expansion, healthy credit trajectory.",
                "key_catalysts": ["Healthy economic indicators"],
                "risk_factors": ["Market volatility"],
            }

        # Run both model predictions in parallel with watchdog timeout
        task_gemini = self._query_model(
            model_id="gemini-3.8-flash",
            account_id="gemini_flash",
            symbol=sym,
            quote=quote,
            indices=indices,
            news=news,
            sentiment_intel=sentiment_intel,
            orb_plan=orb_plan,
            sim_odds=sim_odds,
            attention_info=attention_info,
        )
        task_deepseek = self._query_model(
            model_id="deepseek-ai/deepseek-v4.1-flash-maas",
            account_id="deepseek_flash",
            symbol=sym,
            quote=quote,
            indices=indices,
            news=news,
            sentiment_intel=sentiment_intel,
            orb_plan=orb_plan,
            sim_odds=sim_odds,
            attention_info=attention_info,
        )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(task_gemini, task_deepseek, return_exceptions=True),
                timeout=45.0
            )
            res_gemini, res_deepseek = results[0], results[1]
        except asyncio.TimeoutError:
            logger.error(f"Arena parallel queries for {sym} timed out after 45s. Utilizing fallback setups.")
            res_gemini = self._generate_algorithmic_fallback(sym, "gemini_flash", "gemini-3.8-flash (Fallback)", orb_plan, sim_odds)
            res_deepseek = self._generate_algorithmic_fallback(sym, "deepseek_flash", "deepseek-ai/deepseek-v4.1-flash-maas (Fallback)", orb_plan, sim_odds)

        if isinstance(res_gemini, Exception):
            logger.error(f"Gemini 3.8 Flash query error: {res_gemini}")
            pred_gemini = self._generate_algorithmic_fallback(sym, "gemini_flash", "gemini-3.8-flash (Fallback)", orb_plan, sim_odds)
        else:
            pred_gemini = res_gemini

        if isinstance(res_deepseek, Exception):
            logger.error(f"DeepSeek Flash query error: {res_deepseek}")
            pred_deepseek = self._generate_algorithmic_fallback(sym, "deepseek_flash", "deepseek-ai/deepseek-v4.1-flash-maas (Fallback)", orb_plan, sim_odds)
        else:
            pred_deepseek = res_deepseek

        self.gemini_last_prediction = pred_gemini
        self.deepseek_last_prediction = pred_deepseek
        self.last_prediction = pred_gemini  # default reference

        # Auto-Execution check for both agents
        await self._handle_auto_execution("gemini_flash", pred_gemini)
        await self._handle_auto_execution("deepseek_flash", pred_deepseek)

        # Arena comparative summary
        arena_summary = db.get_arena_comparison()

        broadcast_payload = {
            "type": "arena_update",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": sym,
            "gemini_prediction": pred_gemini,
            "deepseek_prediction": pred_deepseek,
            "arena": arena_summary,
            "next_run_timestamp": self.next_run_timestamp,
        }
        self.broadcast(broadcast_payload)
        return broadcast_payload

    async def _query_model(
        self,
        model_id: str,
        account_id: str,
        symbol: str,
        quote: Dict[str, Any],
        indices: Dict[str, Any],
        news: Dict[str, Any],
        sentiment_intel: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
        attention_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Queries an AI model with dedicated account context and day-trader technical setups."""
        acc_state = db.get_account_state(account_id)
        open_positions = execution_engine.get_portfolio_summary(account_id).get("positions", [])
        recent_lessons = db.get_learning_logs(account_id, limit=3)

        prompt_data = {
            "agent_account": {
                "account_id": account_id,
                "agent_name": "Gemini 3.8 Flash" if "gemini" in account_id else "DeepSeek Flash MaaS",
                "initial_capital_inr": acc_state.get("initial_capital", 10000.0),
                "available_cash_inr": acc_state.get("available_cash", 10000.0),
                "realized_pnl_inr": acc_state.get("total_realized_pnl", 0.0),
                "active_holdings": open_positions,
                "recent_trading_lessons": [l["insight"] for l in recent_lessons],
            },
            "target_symbol": symbol,
            "live_quote": quote,
            "sector_sentiment_intelligence": sentiment_intel,
            "day_trader_skill_setup": {
                "orb_plan": orb_plan,
                "time_machine_odds": sim_odds,
            },
            "benchmark_indices": indices.get("indices"),
            "recent_news": news.get("articles"),
            "current_time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        api_key = settings.GEMINI_API_KEY
        if not api_key or api_key.startswith("your_") or not token_tracker.can_consume_tokens(350):
            return self._generate_algorithmic_fallback(symbol, account_id, model_id, orb_plan, sim_odds)

        is_deepseek = "deepseek" in model_id.lower()
        client = create_genai_client(api_key, location="global" if is_deepseek else None)

        try:
            if is_deepseek:
                config = types.GenerateContentConfig(temperature=0.2)
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model_id,
                        contents=f"{PREDICTOR_SYSTEM_PROMPT}\nAnalyze live market data and return strictly valid JSON:\n{json.dumps(prompt_data)}",
                        config=config,
                    ),
                    timeout=35.0,
                )
            else:
                config = types.GenerateContentConfig(
                    system_instruction=PREDICTOR_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                )
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model_id,
                        contents=f"Analyze live Indian market data and return prediction JSON:\n{json.dumps(prompt_data)}",
                        config=config,
                    ),
                    timeout=35.0,
                )

            text_resp = response.text.strip()
            m = re.search(r"\{.*\}", text_resp, re.DOTALL)
            clean_text = m.group(0) if m else text_resp

            pred_data = json.loads(clean_text)
            pred_data["account_id"] = account_id
            pred_data["model"] = model_id
            pred_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            pred_data["timeframe"] = "5-15 Min Fast Scalp"

            p_toks = getattr(response.usage_metadata, "prompt_token_count", 250) if hasattr(response, "usage_metadata") else 250
            c_toks = getattr(response.usage_metadata, "candidates_token_count", 200) if hasattr(response, "usage_metadata") else 200
            token_tracker.log_usage(f"predictor_{account_id}", model_id, p_toks, c_toks)
            pred_data["tokens_used"] = p_toks + c_toks

            db.record_prediction(pred_data)
            return pred_data

        except Exception as e:
            logger.error(f"Prediction call failed for {model_id} ({account_id}): {e}")
            return self._generate_algorithmic_fallback(symbol, account_id, f"{model_id} (Fallback)", orb_plan, sim_odds)

    def _generate_algorithmic_fallback(
        self,
        symbol: str,
        account_id: str,
        model_name: str,
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """High-probability algorithmic prediction consuming 0 tokens using Day Trader Skill rules."""
        price = orb_plan.get("entry_price") or cache.get_price(symbol)
        if not price or price <= 0:
            q = get_indian_stock_quote(symbol)
            price = q.get("current_price", 0.0)

        should_buy = (sim_odds.get("recommendation") == "EXECUTE") and (price > 0)
        pred = {
            "account_id": account_id,
            "signal": "BUY" if should_buy else "HOLD",
            "symbol": symbol,
            "confidence_pct": int(sim_odds.get("win_rate_pct", 72.0)) if price > 0 else 50,
            "current_price_inr": price,
            "entry_zone_inr": f"{price - 1.0:.1f} - {price + 1.0:.1f}" if price > 0 else "N/A",
            "target_1_inr": orb_plan.get("target_1", price * 1.01),
            "target_2_inr": orb_plan.get("target_2", price * 1.02),
            "stop_loss_inr": orb_plan.get("stop_loss", price * 0.99),
            "risk_reward_ratio": orb_plan.get("risk_reward_ratio", "1:2.0"),
            "timeframe": "5-15 Min Scalp",
            "catalyst": f"ORB Setup: Midpoint Stop ₹{orb_plan.get('stop_loss', 0)} with {sim_odds.get('verdict')}.",
            "action_summary": f"Place Fast Scalp on {symbol} (Expected R: {sim_odds.get('expected_value_r', 0.2)}R)." if price > 0 else "Live price required.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": model_name,
            "tokens_used": 0,
        }
        db.record_prediction(pred)
        return pred

    async def _handle_auto_execution(self, account_id: str, pred: Dict[str, Any]):
        """Executes automated trades when confidence is >= 60% on the designated agent account."""
        from app.trading.strategy_manager import strategy_manager
        acc_state = db.get_account_state(account_id)
        is_auto = bool(acc_state.get("auto_execute", self.auto_execute))

        if not is_auto:
            return

        signal = pred.get("signal")
        conf = pred.get("confidence_pct", 0)
        sym = pred.get("symbol")

        if not sym or signal not in ["BUY", "SELL"]:
            return

        # High-probability threshold (>= 60% allows both Gemini and DeepSeek models to participate)
        if conf >= 60:
            if strategy_manager.is_market_open():
                # Verify live price from market/cache
                price = cache.get_price(sym)
                if not price or price <= 0:
                    quote = get_indian_stock_quote(sym)
                    if quote.get("success") and quote.get("current_price", 0) > 0:
                        price = quote["current_price"]
                if not price or price <= 0:
                    logger.warning(f"[{account_id}] Auto-trade on {sym} skipped: no verified live exchange price available.")
                    return

                available_cash = float(acc_state.get("available_cash", 10000.0))

                if signal == "BUY":
                    # Day-Trader disciplined position sizing (from trader-budget)
                    stop_loss_val = pred.get("stop_loss_inr") or round(price * 0.99, 2)
                    target_val = pred.get("target_1_inr") or round(price * 1.015, 2)

                    budget = day_trader.calculate_budget_size(
                        initial_capital=float(acc_state.get("initial_capital", 10000.0)),
                        available_cash=available_cash,
                        entry_price=price,
                        stop_loss=stop_loss_val,
                    )
                    qty = budget.get("quantity", 0)
                    if qty <= 0:
                        alloc = min(available_cash, 3000.0)
                        qty = max(1, int(alloc // price)) if available_cash >= price else 0

                    if qty > 0 and available_cash >= (price * qty):
                        order = await execution_engine.fast_execute_order(
                            symbol=sym,
                            action="BUY",
                            quantity=qty,
                            order_type="MARKET",
                            strategy_name=f"{account_id}_day_trader_orb",
                            account_id=account_id,
                            stop_loss=stop_loss_val,
                            target_price=target_val,
                        )
                        logger.info(f"⚡ [{account_id}] Auto-executed BUY {qty} {sym} @ ₹{price} (SL: ₹{stop_loss_val}, TP: ₹{target_val}): {order.get('status')}")
                elif signal == "SELL":
                    # Check if agent holds position
                    summary = execution_engine.get_portfolio_summary(account_id)
                    held_pos = next((p for p in summary.get("positions", []) if p.get("symbol") == sym), None)
                    if held_pos and held_pos.get("quantity", 0) > 0:
                        qty = held_pos["quantity"]
                        order = await execution_engine.fast_execute_order(
                            symbol=sym,
                            action="SELL",
                            quantity=qty,
                            order_type="MARKET",
                            strategy_name=f"{account_id}_ai_strategy",
                            account_id=account_id,
                        )
                        logger.info(f"⚡ [{account_id}] Auto-executed SELL {qty} {sym} @ ₹{price}: {order.get('status')}")
            else:
                logger.info(f"[{account_id}] Auto-trade signal {signal} on {sym} ({conf}%) held: Market is currently closed.")

    async def generate_prediction(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        """Wrapper for single target symbol triggered on-demand."""
        arena_res = await self.run_arena_cycle(target_symbol=target_symbol)
        return arena_res.get("gemini_prediction") or self.last_prediction

    async def evaluate_emergency_decision(self, symbol: str, drop_pct: float, current_pnl: float, trigger_reason: str) -> Dict[str, Any]:
        """Emergency Risk Manager closing positions during market shocks."""
        logger.warning(f"🚨 EMERGENCY RISK TRIGGERED: {symbol} down {drop_pct}% (PnL: ₹{current_pnl}). Reason: {trigger_reason}")
        decision = {
            "action": "EMERGENCY_EXIT" if drop_pct >= 2.0 else "TIGHTEN_STOP",
            "symbol": symbol,
            "rationale": f"Emergency protective action: {trigger_reason}. Loss capped at ₹{current_pnl}.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": "Emergency-Guard",
        }
        if decision.get("action") == "EMERGENCY_EXIT":
            await execution_engine.close_position(symbol, account_id="gemini_flash")
            await execution_engine.close_position(symbol, account_id="deepseek_flash")
        return decision


argon_predictor = ArgonPredictor(interval_seconds=300)
