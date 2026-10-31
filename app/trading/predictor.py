"""
Autonomous AI market predictor and two-account arena.

Account A: Claude Opus 4.6 Thinking (Antigravity benchmark)
Account B: DeepSeek 4.1 Flash MaaS (primary execution model)

The loop is hard-gated to the NSE session. Off-market cycles consume no AI
tokens, and every order passes the broker risk manager.
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
from app.trading.backtester import hard_invalidation
from app.storage.db import db
from app.storage.cache import cache
from app.system.tokens import token_tracker
from app.trading import market_hours

logger = logging.getLogger("argon_predictor")

# Number of recent 5-minute bars ever serialised into an AI prompt. The veto
# rules only inspect the last few bars, so a short window keeps prompts small
# (lower latency and token cost) without changing the decision.
AI_BAR_WINDOW = 25

PREDICTOR_SYSTEM_PROMPT = """You are a VETO-ONLY risk filter for an NSE intraday MOMENTUM CONTINUATION trade.

The trade geometry (entry, stop loss, target) has already been fixed by a validated deterministic quantitative model. You may NOT change the entry, stop, or target. You may only answer EXECUTE or PASS.

DEFAULT ANSWER IS EXECUTE. This is a TREND trade that is held to the close. A high RSI or a price near the upper Bollinger band is NORMAL and EXPECTED in a momentum continuation and is NOT a reason to reject.

Answer PASS ONLY if at least one HARD invalidation is literally visible in the supplied data:
1. Price is currently BELOW both VWAP and the opening-range high (the breakout has fully failed).
2. The most recent bar is a violent reversal against the position: a large red bar closing near its low that gives back most of the breakout move.
3. The stop is more than 2.5% below entry (risk/reward unacceptable).

Do NOT answer PASS for weak reasons such as: low volume ratio, "stalling", "consolidating", "waiting for confirmation", RSI being high, price near the upper band, or general caution. If none of the three hard conditions is literally true, you MUST answer EXECUTE.

Respond strictly with a valid JSON object matching this schema:
{
  "decision": "EXECUTE" | "PASS",
  "symbol": "<TICKER>",
  "confidence_pct": <integer between 50 and 95>,
  "reasoning": "<one crisp sentence>"
}
Do not invent data. Do not return stop or target fields. Return raw JSON only.
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
        self.opus_last_prediction: Optional[Dict[str, Any]] = None
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
        """Conserve tokens outside the entry window and near the close."""
        now = market_hours.now_ist()
        if not market_hours.is_entry_window(now):
            status = market_hours.get_market_status(now)
            return {
                "level": "SLEEP_OFF_MARKET",
                "interval_seconds": 900,
                "allow_ai_call": False,
                "reason": f"{status['description']}. No AI calls outside the entry window.",
            }
        return {
            "level": "NORMAL",
            "interval_seconds": 300,
            "allow_ai_call": True,
            "reason": "Active NSE entry window. Running DeepSeek vs Claude Opus arena cycle.",
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
                if not attention_info["allow_ai_call"]:
                    logger.info(
                        "Cycle #%s skipped: %s", self.cycle_count, attention_info["reason"]
                    )
                else:
                    logger.info(
                        "Starting arena cycle #%s (interval %ss)",
                        self.cycle_count, self.interval_seconds,
                    )
                    await asyncio.wait_for(self.run_arena_cycle(), timeout=125.0)
            except asyncio.TimeoutError:
                logger.error("Arena cycle #%s timed out.", self.cycle_count)
            except Exception as e:
                logger.error("Error in arena cycle #%s: %s", self.cycle_count, e, exc_info=True)

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

    def _load_market_frames(self, symbol: str):
        """Load and cache 5-minute and daily frames for a symbol."""
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cached = self._bar_cache.get(symbol)
        if cached and (now_ts - cached["timestamp"] < 180):
            return cached["df_5m"], cached["df_daily"]

        yf_sym = normalize_indian_symbol(symbol)
        ticker = yf.Ticker(yf_sym)
        try:
            df_5m = ticker.history(period="5d", interval="5m")
            if df_5m.empty and yf_sym.endswith(".NS"):
                df_5m = yf.Ticker(yf_sym.replace(".NS", ".BO")).history(
                    period="5d", interval="5m"
                )
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
        return df_5m, df_daily

    @staticmethod
    def _today_orb_bars(df_5m) -> List[Dict[str, float]]:
        if df_5m is None or getattr(df_5m, "empty", True):
            return []
        frame = df_5m.reset_index()
        frame.columns = [str(c) for c in frame.columns]
        ts_col = "Datetime" if "Datetime" in frame.columns else (
            "Date" if "Date" in frame.columns else frame.columns[0]
        )
        frame["_date"] = pd.to_datetime(frame[ts_col], errors="coerce").dt.date
        today = market_hours.now_ist().date()
        today_frame = frame[frame["_date"] == today]
        if today_frame.empty:
            today_frame = frame.tail(3)
        bars = []
        for _, row in today_frame.head(3).iterrows():
            bars.append({
                "open": float(row.get("Open", 0)),
                "high": float(row.get("High", 0)),
                "low": float(row.get("Low", 0)),
                "close": float(row.get("Close", 0)),
                "volume": float(row.get("Volume", 0)),
            })
        return bars

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
        Builds the market context used by the veto-only AI layer.

        The deterministic engine consumes the full indicator series; the AI
        only ever sees a short recent window (see ``AI_BAR_WINDOW``). Keeping
        the payload small cuts latency and token cost without changing the
        decision, because the veto rules only look at the last few bars.
        """
        df_5m, df_daily = self._load_market_frames(symbol)

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

        # Intraday Strategy Confluence (deterministic engine input)
        confluence_eval = intraday_engine.evaluate_all_strategies(
            bars_series if bars_series else [{"close": quote.get("current_price", 0), "open": quote.get("current_price", 0), "high": orb_plan.get("or_high", 0), "low": orb_plan.get("or_low", 0), "volume": 5000}]
        )

        acc_state_deepseek = db.get_account_state("deepseek_flash")
        open_positions = execution_engine.get_portfolio_summary("deepseek_flash").get("positions", [])

        # Only the recent window is ever shown to the AI. The full series is
        # kept in memory for the deterministic engine but never serialised
        # into a prompt.
        ai_bars = bars_series[-AI_BAR_WINDOW:]

        return {
            "target_symbol": symbol,
            "live_quote": quote,
            "total_5m_candles_loaded": len(bars_series),
            "historical_5m_bars_with_indicators": ai_bars,
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
                "available_cash_inr": acc_state_deepseek.get("available_cash", settings.INITIAL_CAPITAL_INR),
                "realized_pnl_inr": acc_state_deepseek.get("total_realized_pnl", 0.0),
                "active_holdings": open_positions,
                "max_position_pct": settings.MAX_POSITION_PCT,
                "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
                "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            },
            "empirical_strategy_axioms": [
                "1. The validated primary edge is INTRADAY MOMENTUM CONTINUATION: first hour (09:15-10:15) closes above the day open, then price breaks the opening-range high. Hold to the close.",
                "2. A high RSI or price at the upper Bollinger band is NORMAL in a momentum continuation and is NOT a rejection reason.",
                "3. The deterministic engine owns entry, stop, and target. The AI is a veto-only filter and may never move them.",
                "4. Stop loss is anchored at the opening-range midpoint (natural invalidation of the momentum thesis).",
                "5. Reject only on genuine invalidation: price back below VWAP AND below the opening-range high, or a violent reversal bar against the position.",
                "6. The secondary strategies (ORB, VWAP, EMA ribbon, SuperTrend, Camarilla, Bollinger, RSI) are context only; they have no positive expectancy after costs.",
                "7. Mandatory square-off before 15:20 IST."
            ]
        }

    # ------------------------------------------------------------------
    # Veto-only AI architecture
    # ------------------------------------------------------------------
    # The deterministic momentum engine owns trade geometry (entry, stop,
    # target). The AI may only answer EXECUTE or PASS. This mirrors the
    # validated backtester so live behaviour matches the tested edge.
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_primary_setup(
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Extract the deterministic momentum-continuation setup, if any.

        Returns a dict with fixed geometry (entry/stop/target) or None when
        the deterministic conditions are not met. The AI never sees or
        modifies these numbers; it only votes EXECUTE/PASS.
        """
        confluence = massive_context.get("intraday_confluence_evaluation") or {}
        evaluated = confluence.get("strategies_evaluated") or {}
        setup = evaluated.get("momentum")
        if not setup or not setup.get("active"):
            return None
        if setup.get("signal") not in ("BUY", "SELL"):
            return None

        entry = float(setup.get("entry_price") or 0.0)
        stop = float(setup.get("stop_loss") or 0.0)
        target = float(setup.get("target_1") or 0.0)
        if entry <= 0 or stop <= 0 or stop >= entry:
            return None
        if target <= entry:
            # hold_to_close trades have no fixed target; use a wide placeholder
            target = round(entry * 1.05, 2)

        return {
            "strategy": "momentum_continuation",
            "signal": setup.get("signal"),
            "entry_price": round(entry, 2),
            "stop_loss": round(stop, 2),
            "target_1": round(target, 2),
            "hold_to_close": bool(setup.get("hold_to_close", True)),
            "confidence": int(setup.get("confidence", 88)),
            "rationale": setup.get("rationale", "First-hour trend up with opening-range breakout."),
            "or_high": float(orb_plan.get("or_high") or 0.0),
            "or_low": float(orb_plan.get("or_low") or 0.0),
        }

    def _build_veto_prediction(
        self,
        symbol: str,
        account_id: str,
        model_name: str,
        setup: Dict[str, Any],
        decision: str,
        confidence_pct: int,
        reasoning: str,
        tokens_used: int = 0,
    ) -> Dict[str, Any]:
        """Assemble a prediction dict from a fixed deterministic setup plus
        the AI's EXECUTE/PASS vote. Geometry is never taken from the AI."""
        entry = float(setup["entry_price"])
        stop = float(setup["stop_loss"])
        target = float(setup["target_1"])
        signal = setup["signal"] if decision == "EXECUTE" else "HOLD"
        pred = {
            "account_id": account_id,
            "signal": signal,
            "symbol": symbol,
            "confidence_pct": int(confidence_pct),
            "current_price_inr": entry,
            "entry_zone_inr": f"{entry - 1.0:.1f} - {entry + 1.0:.1f}",
            "target_1_inr": target,
            "target_2_inr": round(target * 1.01, 2),
            "stop_loss_inr": stop,
            "risk_reward_ratio": "hold-to-close",
            "timeframe": "Intraday Momentum (hold to close)",
            "hold_to_close": bool(setup.get("hold_to_close", True)),
            "strategy": "momentum_continuation",
            "catalyst": reasoning or setup.get("rationale", ""),
            "key_risks": "Momentum failure: price back below VWAP and the opening-range high.",
            "action_summary": (
                f"{'Execute' if decision == 'EXECUTE' else 'Skip'} momentum continuation on {symbol} "
                f"(entry ₹{entry:.2f}, stop ₹{stop:.2f}, hold to close)."
            ),
            "ai_decision": decision,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": model_name,
            "tokens_used": tokens_used,
        }
        db.record_prediction(pred)
        return pred

    def _deterministic_setup_prediction(
        self,
        symbol: str,
        account_id: str,
        model_name: str,
        setup: Dict[str, Any],
        tokens_used: int = 0,
    ) -> Dict[str, Any]:
        """Fallback when the AI is unavailable: trust the validated
        deterministic engine and execute the setup directly."""
        return self._build_veto_prediction(
            symbol=symbol,
            account_id=account_id,
            model_name=model_name,
            setup=setup,
            decision="EXECUTE",
            confidence_pct=int(setup.get("confidence", 88)),
            reasoning=f"Deterministic engine (AI unavailable): {setup.get('rationale', '')}",
            tokens_used=tokens_used,
        )

    def _no_setup_prediction(
        self,
        symbol: str,
        account_id: str,
        model_name: str,
        reason: str = "No valid momentum-continuation setup.",
    ) -> Dict[str, Any]:
        """Neutral HOLD prediction when the deterministic engine finds no edge."""
        price = cache.get_price(symbol) or 0.0
        pred = {
            "account_id": account_id,
            "signal": "HOLD",
            "symbol": symbol,
            "confidence_pct": 50,
            "current_price_inr": price,
            "entry_zone_inr": "N/A",
            "target_1_inr": None,
            "target_2_inr": None,
            "stop_loss_inr": None,
            "risk_reward_ratio": "N/A",
            "timeframe": "Intraday Momentum (hold to close)",
            "hold_to_close": False,
            "strategy": "none",
            "catalyst": reason,
            "key_risks": "No trade taken.",
            "action_summary": "No action: deterministic engine found no momentum-continuation setup.",
            "ai_decision": "PASS",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": model_name,
            "tokens_used": 0,
        }
        db.record_prediction(pred)
        return pred

    async def _query_deepseek_prediction(
        self,
        symbol: str,
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Veto-only AI filter powered by DeepSeek Flash MaaS (Vertex AI).

        The deterministic momentum engine has already fixed the trade
        geometry. DeepSeek only answers EXECUTE or PASS. If there is no
        deterministic setup, no AI call is made at all (token savings).
        Falls back to Claude Opus 4.6 Thinking, then to the deterministic
        engine, if the AI is unavailable.
        """
        setup = self._extract_primary_setup(massive_context, orb_plan)
        if not setup:
            logger.info("No deterministic momentum setup for %s; skipping AI call.", symbol)
            return self._no_setup_prediction(symbol, "deepseek_flash", "DeepSeek Flash MaaS")

        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_") or not token_tracker.can_consume_tokens(estimated_prompt_tokens=900, estimated_completion_tokens=120):
            logger.info("DeepSeek unavailable or token-capped; using Opus benchmark fallback for %s.", symbol)
            return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="deepseek_flash")

        client = create_genai_client(api_key, location="global")
        model_id = settings.DEEPSEEK_MODEL

        bars = massive_context.get("historical_5m_bars_with_indicators", [])[-3:]
        indicators = massive_context.get("intraday_confluence_evaluation", {}).get("indicators", {})
        # Minimal payload: only what the hard-invalidation rules inspect.
        # Dropping the full quote, breadth and long bar history cuts prompt
        # tokens (and therefore cost/latency) with no change in the decision.
        veto_payload = {
            "symbol": symbol,
            "strategy": setup["strategy"],
            "fixed_geometry": {
                "entry_price_inr": setup["entry_price"],
                "stop_loss_inr": setup["stop_loss"],
                "hold_to_close": setup["hold_to_close"],
            },
            "opening_range": {"or_high": setup["or_high"], "or_low": setup["or_low"]},
            "current_price_inr": setup["entry_price"],
            "vwap": indicators.get("vwap"),
            "last_3_bars_5m": bars,
        }

        prompt_text = (
            f"{PREDICTOR_SYSTEM_PROMPT}\n\n"
            f"=== FIXED SETUP + RECENT CONTEXT (Symbol: {symbol}) ===\n"
            f"{json.dumps(veto_payload, default=str)}\n\n"
            f"The deterministic engine has fixed the entry and stop. "
            f"Answer EXECUTE or PASS only. Return strictly a raw JSON object."
        )

        try:
            config = types.GenerateContentConfig(temperature=0.1)
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
            ai_data = json.loads(clean_text)

            decision = str(ai_data.get("decision", "PASS")).strip().upper()
            if decision not in ("EXECUTE", "PASS"):
                decision = "PASS"
            if decision == "PASS":
                # The AI may only veto on a HARD invalidation. If the
                # deterministic guard finds none, the veto is a weak signal
                # (low volume, "stalling", high RSI) and is downgraded so it
                # cannot destroy the validated momentum edge.
                hard = hard_invalidation(
                    setup,
                    indicators,
                    bars,
                )
                if hard is None:
                    logger.info(
                        "DeepSeek veto on %s downgraded (no hard invalidation): %s",
                        symbol, ai_data.get("reasoning", ""),
                    )
                    decision = "EXECUTE"
            conf = int(ai_data.get("confidence_pct", setup.get("confidence", 88)) or setup.get("confidence", 88))
            conf = max(50, min(95, conf))
            reasoning = str(ai_data.get("reasoning", ""))[:300]

            p_toks = getattr(response.usage_metadata, "prompt_token_count", 1500) if hasattr(response, "usage_metadata") else 1500
            c_toks = getattr(response.usage_metadata, "candidates_token_count", 120) if hasattr(response, "usage_metadata") else 120
            token_tracker.log_usage(
                "predictor_deepseek_flash", model_id, p_toks, c_toks,
                account_id="deepseek_flash",
            )

            pred = self._build_veto_prediction(
                symbol=symbol,
                account_id="deepseek_flash",
                model_name="DeepSeek Flash MaaS",
                setup=setup,
                decision=decision,
                confidence_pct=conf,
                reasoning=reasoning,
                tokens_used=p_toks + c_toks,
            )
            logger.info(
                "⚡ DeepSeek veto for %s: %s (%s%%) [%s prompt tokens]",
                symbol, decision, conf, p_toks,
            )
            return pred

        except Exception as e:
            logger.error(f"DeepSeek Flash MaaS query error for {symbol}: {e}. Triggering Claude Opus Antigravity fallback...")
            try:
                return await self._query_opus_antigravity_prediction(symbol, massive_context, orb_plan, sim_odds, account_id="deepseek_flash")
            except Exception as opus_err:
                logger.error(f"Claude Opus fallback failed: {opus_err}. Using deterministic engine.")
                return self._deterministic_setup_prediction(
                    symbol, "deepseek_flash", "DeepSeek Flash MaaS (Deterministic)", setup
                )

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
        return await self._query_opus_antigravity_prediction(
            symbol, massive_context, orb_plan, sim_odds, account_id="opus_flash"
        )

    async def _query_opus_antigravity_prediction(
        self,
        symbol: str,
        massive_context: Dict[str, Any],
        orb_plan: Dict[str, Any],
        sim_odds: Dict[str, Any],
        account_id: str = "opus_flash",
    ) -> Dict[str, Any]:
        """Claude Opus 4.6 Thinking veto filter through the local Antigravity CLI.

        Opus uses the SAME veto-only architecture as DeepSeek: the
        deterministic engine owns the geometry and Opus may only answer
        EXECUTE or PASS. This keeps the benchmark account on the validated
        edge instead of letting it invent its own stop/target.
        """
        setup = self._extract_primary_setup(massive_context, orb_plan)
        if not setup:
            logger.info("No deterministic momentum setup for %s; skipping Opus call.", symbol)
            return self._no_setup_prediction(symbol, account_id, "Claude Opus 4.6 Thinking")

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
            return self._deterministic_setup_prediction(
                symbol, account_id, f"{fallback_model_name} (Unavailable)", setup
            )

        bars = massive_context.get("historical_5m_bars_with_indicators", [])[-3:]
        indicators = massive_context.get("intraday_confluence_evaluation", {}).get("indicators", {})
        veto_payload = {
            "symbol": symbol,
            "strategy": setup["strategy"],
            "fixed_geometry": {
                "entry_price_inr": setup["entry_price"],
                "stop_loss_inr": setup["stop_loss"],
                "hold_to_close": setup["hold_to_close"],
            },
            "opening_range": {"or_high": setup["or_high"], "or_low": setup["or_low"]},
            "current_price_inr": setup["entry_price"],
            "vwap": indicators.get("vwap"),
            "last_3_bars_5m": bars,
        }

        prompt_text = (
            f"{PREDICTOR_SYSTEM_PROMPT}\n\n"
            f"=== FIXED SETUP + RECENT CONTEXT (Symbol: {symbol}) ===\n"
            f"{json.dumps(veto_payload, default=str)}\n\n"
            f"The deterministic engine has fixed the entry and stop. "
            f"Answer EXECUTE or PASS only. Return strictly a raw JSON object."
        )

        models_to_try = [settings.OPUS_MODEL]
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
                        ai_data = json.loads(m.group(0))
                        decision = str(ai_data.get("decision", "PASS")).strip().upper()
                        if decision not in ("EXECUTE", "PASS"):
                            decision = "PASS"
                        if decision == "PASS":
                            hard = hard_invalidation(setup, indicators, bars)
                            if hard is None:
                                logger.info(
                                    "Opus veto on %s downgraded (no hard invalidation): %s",
                                    symbol, ai_data.get("reasoning", ""),
                                )
                                decision = "EXECUTE"
                        conf = int(ai_data.get("confidence_pct", setup.get("confidence", 88)) or setup.get("confidence", 88))
                        conf = max(50, min(95, conf))
                        model_label = "Claude Opus 4.6 Thinking (Antigravity)" if "opus" in model_cand else "Claude Sonnet 4.6 (Antigravity)"
                        if account_id == "deepseek_flash":
                            model_label = "DeepSeek (Opus Fallback)"
                        pred = self._build_veto_prediction(
                            symbol=symbol,
                            account_id=account_id,
                            model_name=model_label,
                            setup=setup,
                            decision=decision,
                            confidence_pct=conf,
                            reasoning=str(ai_data.get("reasoning", ""))[:300],
                            tokens_used=0,  # 0 cloud billing tokens
                        )
                        logger.info(
                            "Antigravity [%s] veto for %s: %s (%s%%) via %s (0 cloud credits).",
                            account_id, symbol, decision, conf, model_cand,
                        )
                        return pred
                err_text = stderr.decode("utf-8") if stderr else ""
                if "quota" in err_text.lower() or "rate" in err_text.lower() or "limit" in err_text.lower() or "429" in err_text:
                    logger.warning(f"Antigravity quota limit on {model_cand}: {err_text[:120]}.")
                    break
            except Exception as e:
                logger.warning(f"Antigravity query exception on {model_cand}: {e}.")
                break

        return self._deterministic_setup_prediction(
            symbol, account_id, f"{fallback_model_name} (Fallback)", setup
        )

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
        if not market_hours.is_entry_window():
            status = market_hours.get_market_status()
            logger.info("Arena cycle skipped outside entry window: %s", status["description"])
            return {
                "status": "SKIPPED_MARKET_CLOSED",
                "reason": status["description"],
                "market_status": status,
            }

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

        # Day-trader ORB uses the actual first three 5-minute bars of today.
        df_5m, _ = self._load_market_frames(sym)
        orb_bars = self._today_orb_bars(df_5m)
        orb_plan = day_trader.calculate_orb_plan(sym, price, orb_bars)
        sim_odds = day_trader.run_time_machine_simulation(
            sym, price, orb_plan["stop_loss"], orb_plan["target_2"], bars=orb_bars
        )

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
            logger.error("Arena parallel queries for %s timed out.", sym)
            res_deepseek = self._generate_algorithmic_fallback(
                sym, "deepseek_flash", "DeepSeek 4.1 Flash (Fallback)", orb_plan, sim_odds, massive_context
            )
            res_opus = self._generate_algorithmic_fallback(
                sym, "opus_flash", "Claude Opus 4.6 Thinking (Fallback)", orb_plan, sim_odds, massive_context
            )

        pred_deepseek = res_deepseek if not isinstance(res_deepseek, Exception) else self._generate_algorithmic_fallback(
            sym, "deepseek_flash", "DeepSeek 4.1 Flash (Fallback)", orb_plan, sim_odds, massive_context
        )
        pred_opus = res_opus if not isinstance(res_opus, Exception) else self._generate_algorithmic_fallback(
            sym, "opus_flash", "Claude Opus 4.6 Thinking (Fallback)", orb_plan, sim_odds, massive_context
        )

        self.deepseek_last_prediction = pred_deepseek
        self.opus_last_prediction = pred_opus
        self.last_prediction = pred_deepseek
        self.prediction_history.insert(0, pred_deepseek)
        self.prediction_history = self.prediction_history[:20]

        await self._handle_auto_execution("deepseek_flash", pred_deepseek)
        await self._handle_auto_execution("opus_flash", pred_opus)

        # Arena comparative summary
        arena_summary = db.get_arena_comparison()

        broadcast_payload = {
            "type": "arena_update",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": sym,
            "opus_prediction": pred_opus,
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
        massive_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Deterministic fallback consuming 0 tokens.

        This path must stay on the validated edge. It extracts the SAME
        momentum-continuation setup the AI layer would have vetoed and
        executes it directly (or returns a neutral HOLD when no setup
        exists). It never falls back to the legacy confluence engine, which
        has no positive expectancy after costs.
        """
        setup = None
        if massive_context:
            setup = self._extract_primary_setup(massive_context, orb_plan)
        if not setup:
            # Rebuild the context from the bar cache if the caller did not
            # supply one, so the fallback still sees the momentum engine.
            cached = self._bar_cache.get(symbol)
            if cached and hasattr(cached.get("df_5m"), "empty") and not cached["df_5m"].empty:
                try:
                    df_clean = cached["df_5m"].reset_index().rename(columns={
                        "Datetime": "timestamp", "Date": "timestamp",
                        "Open": "open", "High": "high", "Low": "low",
                        "Close": "close", "Volume": "volume",
                    })
                    df_ind = intraday_engine.compute_technical_indicators(df_clean)
                    bars_series = []
                    for _, r in df_ind.tail(250).iterrows():
                        bars_series.append({
                            "time": str(r.get("timestamp", ""))[:16],
                            "o": round(float(r.get("open", 0)), 2),
                            "h": round(float(r.get("high", 0)), 2),
                            "l": round(float(r.get("low", 0)), 2),
                            "c": round(float(r.get("close", 0)), 2),
                            "v": int(r.get("volume", 0)),
                            "vwap": round(float(r.get("vwap", 0)), 2),
                        })
                    confluence_eval = intraday_engine.evaluate_all_strategies(bars_series)
                    setup = self._extract_primary_setup(
                        {"intraday_confluence_evaluation": confluence_eval}, orb_plan
                    )
                except Exception as e:
                    logger.debug("Fallback context rebuild failed for %s: %s", symbol, e)

        if not setup:
            return self._no_setup_prediction(symbol, account_id, model_name)

        return self._deterministic_setup_prediction(symbol, account_id, model_name, setup)

    async def _handle_auto_execution(self, account_id: str, pred: Dict[str, Any]):
        """Execute a long entry or a protective exit, subject to all risk gates."""
        acc_state = db.get_account_state(account_id)
        if not bool(acc_state.get("auto_execute", self.auto_execute)):
            return
        if not market_hours.is_entry_window():
            return

        signal = pred.get("signal")
        conf = int(pred.get("confidence_pct", 0) or 0)
        sym = (pred.get("symbol") or "").strip().upper()
        if not sym or signal not in ("BUY", "SELL"):
            return

        # Veto-only flow: the deterministic engine owns the setup and the AI
        # only votes EXECUTE/PASS. A valid EXECUTE vote must not be dropped by
        # a low confidence number, so gate on the decision itself. Legacy
        # (non-veto) predictions still fall back to the confidence gate.
        ai_decision = str(pred.get("ai_decision", "") or "").strip().upper()
        if ai_decision:
            if ai_decision != "EXECUTE":
                logger.info(
                    "[%s] AI vetoed %s (%s); no entry.", account_id, sym, ai_decision
                )
                return
        elif conf < settings.MIN_CONFIDENCE:
            return

        acc = execution_engine.get_account(account_id)
        price = cache.get_price(sym)
        if not price or price <= 0:
            quote = get_indian_stock_quote(sym)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                price = float(quote["current_price"])
                cache.set_price(sym, price)
        if not price or price <= 0:
            logger.warning("[%s] No verified price for %s; auto-trade skipped.", account_id, sym)
            return

        if signal == "SELL":
            # Long-only paper account: SELL closes an existing holding, never shorts.
            if sym in acc.positions:
                order = await execution_engine.fast_execute_order(
                    symbol=sym,
                    action="SELL",
                    quantity=acc.positions[sym].quantity,
                    order_type="MARKET",
                    strategy_name=f"{account_id}_alpha_strategy",
                    account_id=account_id,
                    exit_reason="model_exit",
                )
                logger.info("[%s] Auto SELL %s: %s", account_id, sym, order.get("status"))
            return

        stop_loss_val = float(pred.get("stop_loss_inr") or price * 0.992)
        target_val = float(pred.get("target_1_inr") or price * 1.014)
        hold_to_close = bool(pred.get("hold_to_close", False))
        if stop_loss_val >= price:
            stop_loss_val = round(price * 0.992, 2)
        if target_val <= price:
            target_val = round(price * 1.014, 2)

        budget = day_trader.calculate_budget_size(
            initial_capital=float(acc.initial_capital),
            available_cash=float(acc.available_cash),
            entry_price=price,
            stop_loss=stop_loss_val,
            max_loss_pct=settings.RISK_PER_TRADE_PCT / 100.0,
            max_position_pct=settings.MAX_POSITION_PCT,
        )
        qty = int(budget.get("quantity", 0))
        if qty <= 0:
            return

        order = await execution_engine.fast_execute_order(
            symbol=sym,
            action="BUY",
            quantity=qty,
            order_type="MARKET",
            strategy_name=f"{account_id}_alpha_strategy",
            account_id=account_id,
            stop_loss=stop_loss_val,
            target_price=target_val,
            hold_to_close=hold_to_close,
        )
        logger.info(
            "[%s] Auto BUY %s x%d @ market ₹%.2f (SL ₹%.2f, TP ₹%.2f, hold_to_close=%s): %s %s",
            account_id, sym, qty, price, stop_loss_val, target_val, hold_to_close,
            order.get("status"), order.get("reason", ""),
        )

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
            for account_id in ("opus_flash", "deepseek_flash"):
                await execution_engine.close_position(
                    symbol, account_id=account_id, reason="emergency_exit"
                )
        return decision


argon_predictor = ArgonPredictor(interval_seconds=300)
