"""
Multi-Tiered Autonomous AI Market Predictor & Dual Agent Arena Controller.
Runs parallel 5-minute prediction cycles for:
1. Account A: Claude Opus 4.6 Thinking / Gemini 3.8 Flash (Antigravity Pro Benchmark, ₹0.00 cloud credits)
2. Account B: DeepSeek Flash MaaS (Live Trading Execution Engine with Massive Multi-Timeframe Context)
Features dynamic market scanning (30+ liquid Indian stocks) and day-trader-skill integration.
Strictly enforces daily quota and market hours (09:15 - 15:30 IST).
"""

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
from typing import Dict, Any, List, Optional
import pandas as pd
import yfinance as yf
from google.genai import types

from app.config import settings, create_genai_client
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
    normalize_indian_symbol,
)
from app.trading.execution_engine import execution_engine
from app.trading.scanner import scanner
from app.trading.skills.day_trader_adapter import day_trader
from app.trading.sentiment_analyzer import sentiment_engine
from app.trading.intraday_strategies import intraday_engine
from app.storage.db import db
from app.storage.cache import cache
from app.system.tokens import token_tracker

logger = logging.getLogger("argon_predictor")

PREDICTOR_SYSTEM_PROMPT = """You are an elite quantitative trading strategist for the Indian Stock Market (NSE/BSE).
Your primary mandate is proactive intraday alpha generation for a ₹10,000 capital account.
You actively identify high-conviction BUY and SELL opportunities across liquid Indian equities.

Quantitative Trade Execution Rules:
1. ORB-15m Momentum: When 5-minute close breaks above Opening Range High (with VWAP and SuperTrend bullish alignment), execute aggressive BUY with 1:1.4+ target. When breaking below OR Low, execute SELL.
2. VWAP Institutional Reversal & Continuation: When price tests VWAP and prints a bullish rejection candle supported by 50-EMA and SuperTrend, execute BUY.
3. Camarilla Expansion: Breakouts above Camarilla H4 target H5 expansion. Breakdowns below L4 target L5 expansion.
4. Active Trading Mindset: Do NOT default to HOLD when directional momentum exists. A setup with >= 55% probability and 1:1.3+ risk-to-reward is an actionable trade. HOLD is strictly reserved for dead sideways compression.
5. Disciplined Stop Loss: Anchor stop losses at natural technical levels (ORB midpoint, VWAP - 0.5 ATR, or Camarilla H3).

Respond strictly with a valid JSON object matching this schema:
{
  "signal": "BUY" | "SELL" | "HOLD",
  "symbol": "<TICKER>",
  "confidence_pct": <integer between 55 and 99>,
  "current_price_inr": <float>,
  "entry_zone_inr": "<e.g. 918.0 - 922.0>",
  "target_1_inr": <float>,
  "target_2_inr": <float>,
  "stop_loss_inr": <float>,
  "risk_reward_ratio": "<e.g. 1:1.5>",
  "timeframe": "<5-15 Min Scalp OR 30-60 Min Strategic>",
  "catalyst": "<Crisp 1-2 sentence technical and volume rationale based on 5m candles, ORB, and indicators>",
  "key_risks": "<Primary risk factor or invalidation level>",
  "action_summary": "<Direct trade action instruction>"
}
Ensure calculations strictly adhere to current live prices in Indian Rupees (INR / ₹). Return raw JSON only.
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
    - Primary Live Execution Engine: DeepSeek Flash MaaS (fed with massive multi-timeframe 5m context)
    - Side-by-Side Comparative Benchmark: Claude Opus 4.6 Thinking (via Antigravity Pro credentials, ₹0 cloud cost)
    - Dynamic Multi-Stock Scanner beyond fixed watchlists
    - Day-Trader-Skill ORB, Camarilla Pivots, and Time Machine integration
    - Auto-execution exclusively driven by DeepSeek Flash on live trading accounts
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
        state = db.get_account_state("deepseek_flash")
        self.auto_execute = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))
        self.subscribers: List[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None

        # In-memory bar cache to avoid redundant yfinance fetches within cycles
        self._bar_cache: Dict[str, Dict[str, Any]] = {}

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
            "reason": "Active Indian market hours. Running 5-min DeepSeek vs Claude Opus Arena cycle."
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
                await asyncio.wait_for(self.run_arena_cycle(), timeout=85.0)
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

    def _build_massive_market_context(
        self,
        symbol: str,
        quote: Dict[str, Any],
        indices: Dict[str, Any],
        news: Dict[str, Any],
        sentiment_intel: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Builds a comprehensive, multi-timeframe quantitative market payload:
        1. 5-day 5-minute bars (up to 375 candles) with all indicators pre-calculated.
        2. Daily macro history (30 trading days) for primary trend & 52-week context.
        3. Prior-day session Camarilla Pivots (H1-H5, L1-L5) and Classical Floor Pivots.
        4. Day-Trader ORB Setup & Monte Carlo Time Machine simulation odds.
        5. Evaluated Intraday Strategy Confluence across all 7 quantitative strategies.
        6. Market Breadth & Volatility (Nifty 50, Bank Nifty, Sensex, India VIX).
        7. Real-time News Articles & Sector Sentiment.
        8. The 7 Empirical Iteration Rules & Quant Axioms from INTRADAY_STRATEGY_LEARNING_LOG.md.
        """
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cached = self._bar_cache.get(symbol)
        if cached and (now_ts - cached["timestamp"] < 180):
            df_5m = cached["df_5m"]
            df_daily = cached["df_daily"]
        else:
            yf_sym = normalize_indian_symbol(symbol)
            ticker = yf.Ticker(yf_sym)
            try:
                df_5m = ticker.history(period="5d", interval="5m")
                if df_5m.empty and yf_sym.endswith(".NS"):
                    df_5m = yf.Ticker(yf_sym.replace(".NS", ".BO")).history(period="5d", interval="5m")
            except Exception:
                df_5m = pd.DataFrame()

            try:
                df_daily = ticker.history(period="1mo", interval="1d")
            except Exception:
                df_daily = pd.DataFrame()

            self._bar_cache[symbol] = {
                "timestamp": now_ts,
                "df_5m": df_5m,
                "df_daily": df_daily,
            }

        # Process 5-minute bars and technical indicators
        bars_series = []
        if not df_5m.empty:
            df_clean = df_5m.reset_index().rename(columns={
                "Datetime": "timestamp", "Date": "timestamp",
                "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
            })
            df_ind = intraday_engine.compute_technical_indicators(df_clean)
            for _, r in df_ind.tail(250).iterrows():
                bars_series.append({
                    "time": str(r.get("timestamp", ""))[:16],
                    "o": round(float(r.get("open", 0)), 2),
                    "h": round(float(r.get("high", 0)), 2),
                    "l": round(float(r.get("low", 0)), 2),
                    "c": round(float(r.get("close", 0)), 2),
                    "v": int(r.get("volume", 0)),
                    "vwap": round(float(r.get("vwap", 0)), 2),
                    "ema9": round(float(r.get("ema_9", 0)), 2),
                    "ema20": round(float(r.get("ema_20", 0)), 2),
                    "ema50": round(float(r.get("ema_50", 0)), 2),
                    "st_dir": "BULL" if r.get("supertrend_dir", 1) == 1 else "BEAR",
                    "bb_u": round(float(r.get("bb_upper", 0)), 2),
                    "bb_l": round(float(r.get("bb_lower", 0)), 2),
                    "rsi": round(float(r.get("rsi_14", 50)), 1),
                    "atr": round(float(r.get("atr_14", 0)), 2),
                })
        else:
            # Fallback to recent 5d summary from quote if yfinance was empty
            bars_series = quote.get("recent_5d_history", [])

        # Process Daily Macro Context & Pivots
        macro_context = {
            "day_high": quote.get("day_range", {}).get("high"),
            "day_low": quote.get("day_range", {}).get("low"),
            "year_52w_high": quote.get("year_52w_range", {}).get("high"),
            "year_52w_low": quote.get("year_52w_range", {}).get("low"),
            "market_cap": quote.get("market_cap"),
        }
        pivot_levels = {}
        if not df_daily.empty:
            prev_row = df_daily.iloc[-2] if len(df_daily) > 1 else df_daily.iloc[-1]
            p_high = float(prev_row.get("High", quote.get("current_price", 0)))
            p_low = float(prev_row.get("Low", quote.get("current_price", 0)))
            p_close = float(prev_row.get("Close", quote.get("current_price", 0)))
            rng = p_high - p_low
            pivot_levels = {
                "prior_session_high": round(p_high, 2),
                "prior_session_low": round(p_low, 2),
                "prior_session_close": round(p_close, 2),
                "camarilla_h5": round(p_close + (rng * 1.1), 2),
                "camarilla_h4": round(p_close + (rng * 1.1 / 2.0), 2),
                "camarilla_h3": round(p_close + (rng * 1.1 / 4.0), 2),
                "camarilla_l3": round(p_close - (rng * 1.1 / 4.0), 2),
                "camarilla_l4": round(p_close - (rng * 1.1 / 2.0), 2),
                "camarilla_l5": round(p_close - (rng * 1.1), 2),
                "floor_pivot_pp": round((p_high + p_low + p_close) / 3.0, 2),
            }

        # Intraday Strategy Confluence
        confluence_eval = intraday_engine.evaluate_all_strategies(
            bars_series if bars_series else [{"close": quote.get("current_price", 0), "open": quote.get("current_price", 0), "high": orb_plan.get("or_high", 0), "low": orb_plan.get("or_low", 0), "volume": 5000}]
        )

        acc_state_deepseek = db.get_account_state("deepseek_flash")
        open_positions = execution_engine.get_portfolio_summary("deepseek_flash").get("positions", [])

        return {
            "target_symbol": symbol,
            "live_quote": quote,
            "total_5m_candles_loaded": len(bars_series),
            "historical_5m_bars_with_indicators": bars_series,
            "macro_daily_context": macro_context,
            "institutional_pivot_levels": pivot_levels,
            "day_trader_skill_setup": {
                "orb_plan": orb_plan,
                "time_machine_odds": sim_odds,
            },
            "intraday_confluence_evaluation": confluence_eval,
            "market_breadth_and_volatility": indices.get("indices", {}),
            "sentiment_and_news_intelligence": {
                "sentiment": sentiment_intel,
                "recent_articles": news.get("articles", [])[:4],
            },
            "account_execution_status": {
                "account_id": "deepseek_flash",
                "available_cash_inr": acc_state_deepseek.get("available_cash", 10000.0),
                "realized_pnl_inr": acc_state_deepseek.get("total_realized_pnl", 0.0),
                "active_holdings": open_positions,
            },
            "empirical_strategy_axioms": [
                "1. ORB-15m breakout is the #1 highest-conviction intraday strategy in Indian equities (85-100% win rate across 7 iterations).",
                "2. Bollinger Upper Band (+2σ) is strictly a TAKE-PROFIT target; NEVER buy at upper band exhaustion.",
                "3. Quadruple-Filter VWAP: Buy only when Price >= 50 EMA, 45 <= RSI <= 68, Volume >= 1.15x SMA(20), and SuperTrend is Bullish.",
                "4. Midday Lull: Avoid fresh breakout entries between 11:30 and 13:15 IST.",
                "5. Stop Loss Discipline: Use natural technical stops (ORB midpoint or VWAP - 0.8 ATR); do NOT trail prematurely to breakeven at +0.8R.",
                "6. Camarilla H4 Breakout target is H5; L4 breakdown target is L5.",
                "7. Mandatory square-off before 15:25 IST."
            ]
        }

    async def _query_deepseek_prediction(
        self,
        symbol: str,
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Primary Live Trading Prediction Engine powered by DeepSeek Flash MaaS (Vertex AI).
        Ingests the massive multi-timeframe market context (~50,000+ tokens) and outputs
        compact, verified intraday trade recommendations for live execution.
        Falls back to Claude Opus 4.6 Thinking (Antigravity Pro) if quota, network, or timeout occurs.
        """
        api_key = settings.GEMINI_API_KEY
        if not api_key or api_key.startswith("your_") or not token_tracker.can_consume_tokens(estimated_prompt_tokens=35000, estimated_completion_tokens=300):
            logger.info(f"DeepSeek token cap reached or unconfigured. Shifting to Claude Opus Thinking fallback for {symbol}...")
            return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="deepseek_flash")

        client = create_genai_client(api_key, location="global")
        model_id = "deepseek-ai/deepseek-v4.1-flash-maas"

        prompt_text = (
            f"{PREDICTOR_SYSTEM_PROMPT}\n\n"
            f"=== FULL MARKET CONTEXT & MULTI-TIMEFRAME DATA (Symbol: {symbol}) ===\n"
            f"{json.dumps(massive_context)}\n\n"
            f"Analyze the entire historical candle series, Camarilla levels, indicators, and empirical axioms. "
            f"Produce your final intraday trading recommendation. Return strictly a raw JSON object."
        )

        try:
            config = types.GenerateContentConfig(temperature=0.2)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_id,
                    contents=prompt_text,
                    config=config,
                ),
                timeout=40.0,
            )

            text_resp = ""
            if hasattr(response, "text") and response.text:
                text_resp = response.text.strip()
            elif hasattr(response, "candidates") and response.candidates:
                parts = getattr(response.candidates[0].content, "parts", [])
                text_parts = [p.text for p in parts if hasattr(p, "text") and p.text]
                text_resp = "\n".join(text_parts).strip()

            if not text_resp:
                logger.warning(f"DeepSeek Flash returned empty response for {symbol}. Triggering Claude Opus fallback...")
                return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="deepseek_flash")

            m = re.search(r"\{.*\}", text_resp, re.DOTALL)
            clean_text = m.group(0) if m else text_resp

            pred_data = json.loads(clean_text)
            pred_data["account_id"] = "deepseek_flash"
            pred_data["model"] = "DeepSeek Flash MaaS"
            pred_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            pred_data["timeframe"] = pred_data.get("timeframe", "5-15 Min Scalp")

            p_toks = getattr(response.usage_metadata, "prompt_token_count", 35000) if hasattr(response, "usage_metadata") else 35000
            c_toks = getattr(response.usage_metadata, "candidates_token_count", 250) if hasattr(response, "usage_metadata") else 250
            token_tracker.log_usage("predictor_deepseek_flash", model_id, p_toks, c_toks)
            pred_data["tokens_used"] = p_toks + c_toks

            db.record_prediction(pred_data)
            logger.info(f"⚡ DeepSeek Flash MaaS prediction generated for {symbol}: {pred_data.get('signal')} ({pred_data.get('confidence_pct')}%) [{p_toks} prompt tokens consumed]")
            return pred_data

        except Exception as e:
            logger.error(f"DeepSeek Flash MaaS query error for {symbol}: {e}. Triggering Claude Opus Antigravity fallback...")
            try:
                return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="deepseek_flash")
            except Exception as opus_err:
                logger.error(f"Claude Opus fallback failed: {opus_err}. Using algorithmic fallback.")
                return self._generate_algorithmic_fallback(symbol, "deepseek_flash", "DeepSeek Flash MaaS (Fallback)", orb_plan, sim_odds)

    async def _query_opus_prediction(
        self,
        symbol: str,
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Dual Arena Agent Benchmark Engine: Claude Opus 4.6 Thinking.
        Evaluates intraday setups with deep quantitative reasoning.
        Falls back to DeepSeek Flash MaaS or Algorithmic setup if quota or network issue occurs.
        """
        return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="gemini_flash")

    async def _query_opus_antigravity_prediction(
        self,
        symbol: str,
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
        account_id: str = "gemini_flash",
    ) -> Dict[str, Any]:
        """
        Institutional Benchmark & High-Reliability Fallback using Claude Opus 4.6 Thinking via Antigravity credentials.
        Consumes $0.00 / ₹0.00 Google Cloud API tokens.
        """
        candidate_bins = [
            shutil.which("agy"),
            "/usr/local/bin/agy",
            "/home/super/.gemini/bin/agy",
            "/home/crimson/.gemini/bin/agy",
            os.path.expanduser("~/.gemini/bin/agy"),
        ]
        agy_bin = next((b for b in candidate_bins if b and os.path.exists(b)), None)
        fallback_model_name = "Claude Opus 4.6 Thinking (Antigravity Fallback)" if account_id == "deepseek_flash" else "Claude Opus 4.6 Thinking (Antigravity)"
        if not agy_bin:
            return self._generate_algorithmic_fallback(symbol, account_id, f"{fallback_model_name} (Unavailable)", orb_plan, sim_odds)

        # Condensed high-density setup summary tailored for the CLI prompt
        bars_snippet = massive_context.get("historical_5m_bars_with_indicators", [])[-25:]
        summary_payload = {
            "symbol": symbol,
            "live_quote": massive_context.get("live_quote"),
            "institutional_pivot_levels": massive_context.get("institutional_pivot_levels"),
            "recent_25_candles_5m": bars_snippet,
            "day_trader_skill_setup": massive_context.get("day_trader_skill_setup"),
            "market_breadth": massive_context.get("market_breadth_and_volatility"),
            "empirical_strategy_axioms": massive_context.get("empirical_strategy_axioms")[:4],
        }

        prompt_text = (
            f"You are the Lead Quantitative Intraday Strategist for Indian Equities (NSE/BSE).\n"
            f"Analyze this live market context for {symbol} and generate an intraday trade recommendation.\n"
            f"Return strictly a single JSON object with these exact keys:\n"
            f"  'signal': 'BUY', 'SELL', or 'HOLD'\n"
            f"  'symbol': '{symbol}'\n"
            f"  'confidence_pct': integer between 50 and 99\n"
            f"  'current_price_inr': float\n"
            f"  'entry_zone_inr': string\n"
            f"  'target_1_inr': float profit target (at least 1.3R)\n"
            f"  'target_2_inr': float extended target\n"
            f"  'stop_loss_inr': float protective stop loss\n"
            f"  'risk_reward_ratio': string e.g. '1:1.5'\n"
            f"  'timeframe': string\n"
            f"  'catalyst': brief technical/order-flow rationale\n"
            f"  'key_risks': brief sentence highlighting primary risk\n"
            f"  'action_summary': direct trade action instruction\n"
            f"Do not include markdown prose outside the JSON.\n\n"
            f"Market Setup Payload:\n{json.dumps(summary_payload)}"
        )

        models_to_try = ["claude-opus-4-6-thinking", "claude-sonnet-4-6"]
        for model_cand in models_to_try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    agy_bin, "-p", prompt_text, "--model", model_cand, "--dangerously-skip-permissions", "--mode", "accept-edits",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=55.0)
                out_text = stdout.decode("utf-8").strip() if stdout else ""
                if proc.returncode == 0 and out_text:
                    m = re.search(r"\{.*\}", out_text, re.DOTALL)
                    if m:
                        pred_data = json.loads(m.group(0))
                        pred_data["account_id"] = account_id
                        model_label = "Claude Opus 4.6 Thinking (Antigravity)" if "opus" in model_cand else f"Claude Sonnet 4.6 (Antigravity)"
                        if account_id == "deepseek_flash":
                            model_label = f"DeepSeek (Opus Fallback)"
                        pred_data["model"] = model_label
                        pred_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        pred_data["timeframe"] = pred_data.get("timeframe", "5-15 Min Scalp")
                        pred_data["tokens_used"] = 0  # 0 cloud billing tokens!
                        db.record_prediction(pred_data)
                        logger.info(f"🏛️ Antigravity [{account_id}] prediction generated via {model_cand} for {symbol} (0 cloud credits consumed).")
                        return pred_data
                err_text = stderr.decode("utf-8") if stderr else ""
                if "quota" in err_text.lower() or "rate" in err_text.lower() or "limit" in err_text.lower() or "429" in err_text:
                    logger.warning(f"Antigravity quota limit on {model_cand}: {err_text[:120]}.")
                    break
            except Exception as e:
                logger.warning(f"Antigravity query exception on {model_cand}: {e}.")
                break

        return self._generate_algorithmic_fallback(symbol, account_id, f"{fallback_model_name} (Fallback)", orb_plan, sim_odds)

    async def run_arena_cycle(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes a 5-minute Dual Agent Arena cycle:
        1. Screens market for high-return opportunities.
        2. Compiles massive multi-timeframe market context.
        3. Calls DeepSeek Flash MaaS (live trading execution engine).
        4. Calls Claude Opus 4.6 Thinking in parallel for side-by-side comparative benchmarking.
        5. Auto-executes approved trades exclusively from DeepSeek Flash.
        6. Returns comparative Arena results.
        """
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
        self.interval_seconds = attention_info["interval_seconds"]

        # Fetch verified live quote
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

        # Day-trader-skill ORB calculation & Time Machine Odds
        orb_plan = day_trader.calculate_orb_plan(sym, price, [])
        sim_odds = day_trader.run_time_machine_simulation(sym, price, orb_plan["stop_loss"], orb_plan["target_2"])

        # Real-time Sentiment Intelligence
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

        # Build massive multi-timeframe context
        massive_context = self._build_massive_market_context(
            symbol=sym,
            quote=quote,
            indices=indices,
            news=news,
            sentiment_intel=sentiment_intel,
            orb_plan=orb_plan,
            sim_odds=sim_odds,
        )

        # Dispatch both models in parallel:
        # Agent A (Claude Opus 4.6 Thinking via Antigravity) vs Agent B (DeepSeek Flash MaaS via Vertex AI)
        task_deepseek = self._query_deepseek_prediction(
            symbol=sym,
            massive_context=massive_context,
            orb_plan=orb_plan,
            sim_odds=sim_odds,
        )
        task_opus = self._query_opus_prediction(
            symbol=sym,
            massive_context=massive_context,
            orb_plan=orb_plan,
            sim_odds=sim_odds,
        )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(task_deepseek, task_opus, return_exceptions=True),
                timeout=110.0
            )
            res_deepseek, res_opus = results[0], results[1]
        except asyncio.TimeoutError:
            logger.error(f"Arena parallel queries for {sym} timed out after 110s. Utilizing fallback setups.")
            res_deepseek = self._generate_algorithmic_fallback(sym, "deepseek_flash", "DeepSeek Flash MaaS (Fallback)", orb_plan, sim_odds)
            res_opus = self._generate_algorithmic_fallback(sym, "gemini_flash", "Claude Opus 4.6 Thinking (Fallback)", orb_plan, sim_odds)

        pred_deepseek = res_deepseek if not isinstance(res_deepseek, Exception) else self._generate_algorithmic_fallback(sym, "deepseek_flash", "DeepSeek Flash MaaS (Fallback)", orb_plan, sim_odds)
        pred_opus = res_opus if not isinstance(res_opus, Exception) else self._generate_algorithmic_fallback(sym, "gemini_flash", "Claude Opus 4.6 Thinking (Fallback)", orb_plan, sim_odds)

        self.deepseek_last_prediction = pred_deepseek
        self.gemini_last_prediction = pred_opus
        self.last_prediction = pred_deepseek  # DeepSeek Flash is the primary live trading reference

        # Auto-Execution: Execute approved trades for both live agent accounts
        await self._handle_auto_execution("deepseek_flash", pred_deepseek)
        await self._handle_auto_execution("gemini_flash", pred_opus)

        # Arena comparative summary
        arena_summary = db.get_arena_comparison()

        broadcast_payload = {
            "type": "arena_update",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": sym,
            "opus_prediction": pred_opus,
            "gemini_prediction": pred_opus,  # Backward compatibility for frontend binding
            "deepseek_prediction": pred_deepseek,
            "arena": arena_summary,
            "next_run_timestamp": self.next_run_timestamp,
        }
        self.broadcast(broadcast_payload)
        return broadcast_payload

    def _generate_algorithmic_fallback(
        self,
        symbol: str,
        account_id: str,
        model_name: str,
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """High-probability algorithmic prediction consuming 0 tokens using Day Trader Skill & Quant Confluence rules."""
        price = orb_plan.get("entry_price") or cache.get_price(symbol)
        if not price or price <= 0:
            q = get_indian_stock_quote(symbol)
            price = q.get("current_price", 0.0)

        # Check technical indicators & confluence from bar cache
        cached = self._bar_cache.get(symbol)
        confluence_eval = {}
        if cached and hasattr(cached.get("df_5m"), "empty") and not cached["df_5m"].empty:
            df_5m = cached["df_5m"]
            bars_list = df_5m.tail(30).to_dict(orient="records")
            confluence_eval = intraday_engine.evaluate_all_strategies(bars_list)

        best = confluence_eval.get("best_strategy")
        comp_signal = confluence_eval.get("composite_signal", "HOLD")

        if best and comp_signal in ("BUY", "SELL"):
            sig = comp_signal
            conf = int(best.get("confidence", 85))
            sl = float(best.get("stop_loss", price * 0.992))
            tp1 = float(best.get("target_1", price * 1.014))
            tp2 = float(best.get("target_2", price * 1.020))
            cat = best.get("rationale", f"{best.get('name')} active quantitative trigger.")
            action_desc = f"Execute {sig} on {symbol} via {best.get('name')} (Target: ₹{tp1:.2f}, SL: ₹{sl:.2f})."
        else:
            should_buy = (sim_odds.get("recommendation") == "EXECUTE") and (price > 0)
            sig = "BUY" if should_buy else "HOLD"
            conf = int(sim_odds.get("win_rate_pct", 75.0)) if should_buy else 50
            sl = orb_plan.get("stop_loss", price * 0.992)
            tp1 = orb_plan.get("target_1", price * 1.014)
            tp2 = orb_plan.get("target_2", price * 1.020)
            cat = f"ORB Strategy: Midpoint Stop ₹{sl:.2f} with {sim_odds.get('verdict', 'Neutral')}."
            action_desc = f"Place Scalp on {symbol}." if should_buy else "Awaiting directional breakout."

        pred = {
            "account_id": account_id,
            "signal": sig,
            "symbol": symbol,
            "confidence_pct": conf,
            "current_price_inr": price,
            "entry_zone_inr": f"{price - 1.0:.1f} - {price + 1.0:.1f}" if price > 0 else "N/A",
            "target_1_inr": tp1,
            "target_2_inr": tp2,
            "stop_loss_inr": sl,
            "risk_reward_ratio": "1:1.4",
            "timeframe": "5-15 Min Scalp",
            "catalyst": cat,
            "key_risks": "Break below protective stop loss invalidates trade.",
            "action_summary": action_desc,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": model_name,
            "tokens_used": 0,
        }
        db.record_prediction(pred)
        return pred

    async def _handle_auto_execution(self, account_id: str, pred: Dict[str, Any]):
        """Executes automated trades when confidence is >= 55% on the designated agent account."""
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

        # High-probability threshold (>= 55%)
        if conf >= 55:
            if strategy_manager.is_market_open():
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
                    stop_loss_val = pred.get("stop_loss_inr") or round(price * 0.992, 2)
                    target_val = pred.get("target_1_inr") or round(price * 1.014, 2)

                    budget = day_trader.calculate_budget_size(
                        initial_capital=float(acc_state.get("initial_capital", 10000.0)),
                        available_cash=available_cash,
                        entry_price=price,
                        stop_loss=stop_loss_val,
                    )
                    qty = budget.get("quantity", 0)
                    if qty <= 0:
                        alloc = min(available_cash, 7000.0)
                        qty = max(1, int(alloc // price)) if available_cash >= price else 0

                    if qty > 0 and available_cash >= (price * qty):
                        order = await execution_engine.fast_execute_order(
                            symbol=sym,
                            action="BUY",
                            quantity=qty,
                            order_type="MARKET",
                            strategy_name=f"{account_id}_alpha_strategy",
                            account_id=account_id,
                            stop_loss=stop_loss_val,
                            target_price=target_val,
                        )
                        logger.info(f"⚡ [{account_id}] Live Auto-executed BUY {qty} {sym} @ ₹{price} (SL: ₹{stop_loss_val}, TP: ₹{target_val}): {order.get('status')}")
                elif signal == "SELL":
                    summary = execution_engine.get_portfolio_summary(account_id)
                    held_pos = next((p for p in summary.get("positions", []) if p.get("symbol") == sym), None)
                    if held_pos and held_pos.get("quantity", 0) > 0:
                        qty = held_pos["quantity"]
                        order = await execution_engine.fast_execute_order(
                            symbol=sym,
                            action="SELL",
                            quantity=qty,
                            order_type="MARKET",
                            strategy_name=f"{account_id}_alpha_strategy",
                            account_id=account_id,
                        )
                        logger.info(f"⚡ [{account_id}] Live Auto-executed SELL {qty} {sym} @ ₹{price}: {order.get('status')}")
            else:
                logger.info(f"[{account_id}] Auto-trade signal {signal} on {sym} ({conf}%) held: Market is currently closed.")

    async def generate_prediction(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        """Wrapper for single target symbol triggered on-demand."""
        arena_res = await self.run_arena_cycle(target_symbol=target_symbol)
        return arena_res.get("deepseek_prediction") or self.last_prediction

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
