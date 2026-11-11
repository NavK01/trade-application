"""AI-DECIDES trading mode — the model owns the whole trade decision.

Why this exists
---------------
Every shipped path so far is *veto-only*: a deterministic engine picks the
symbol, direction, entry, stop, and target, and the AI may only answer
EXECUTE or PASS. That is safe, but it means the AI can never *add* P&L — it can
only remove trades. The user asked for the opposite: let the AI decide the
trade (buy/sell, at what price, with what stop and target) so the system can be
used for real trading, then measure on one month of data whether the AI
actually increases P&L versus the deterministic engine.

Design
------
`AIDecisionEngine` is a drop-in replacement for the deterministic strategy
suite. It is called once per (symbol, bar) with the same bar history the
shipped engine sees, and it returns a setup dict in exactly the same shape, so
it runs through the identical execution, sizing, fee, and risk machinery in
`backtester.run_backtest` (via the `strategy_override` hook).

The AI is given:
  * the symbol and the current price,
  * the recent bars (OHLCV),
  * the computed indicators (VWAP, EMAs, ATR, RSI, opening range, ...),
  * the account context (capital, risk budget, fee model, position cap),
  * the hard risk rules it must respect.

It must return raw JSON:
  {"action":"BUY"|"PASS", "entry":float, "stop":float, "target":float,
   "confidence_pct":int, "reasoning":str}

Safety rails (the AI proposes, the engine disposes)
---------------------------------------------------
The model is *not* trusted with the risk limits. Whatever it returns is
validated before it can become a trade:
  * action must be BUY (long-only intraday; SELL/short is rejected),
  * entry must be within a sane band of the current price,
  * stop must be below entry and within the max stop distance,
  * target must be above entry and clear round-trip fees by a required
    multiple (the same fee-awareness that fixed the deterministic edge),
  * the resulting risk-per-share must be positive and the fee-to-risk ratio
    must be acceptable.
If any rail fails, the proposal is rejected and no trade is taken. This keeps
the AI's freedom (it chooses direction, entry, stop, target) while making it
impossible for a bad model answer to blow through the account's risk limits.

Cost accounting: every call is charged against the result as AI cost, exactly
like the veto path, so an AI arm is compared to the deterministic arm on a
net-of-AI-cost basis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from app.config import settings, create_genai_client
from app.system.tokens import token_tracker
from app.trading.costs import calculate_buy_fee, calculate_sell_fee

logger = logging.getLogger("app.trading.ai_decision")


# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------
SYSTEM_PROMPT = """You are an autonomous intraday trader for the Indian NSE market (long-only, cash equity).
You decide the ENTIRE trade: whether to trade, the entry price, the stop loss, and the target.

You are given the recent bars and computed indicators for ONE symbol at ONE moment in the session.
Decide whether to open a long position RIGHT NOW.

HARD RULES (a proposal that breaks any of these is rejected by the risk engine):
  1. LONG ONLY. action must be "BUY" or "PASS". Never short.
  2. entry must be within 0.5% of the current price (you are filled at the next bar open).
  3. stop must be BELOW entry, and (entry - stop) must be between 0.15% and 2.5% of entry.
  4. target must be ABOVE entry, and (target - entry) must be at least 1.2x (entry - stop).
  5. The target must clear round-trip transaction fees (about 0.11% of position value) by at least 2x.
  6. Intraday only: the position is squared off at 15:20 IST. Do not propose a target that needs more than the rest of the session to reach.

TRADING LOGIC:
  * Prefer momentum continuation: a symbol trending up on the day, breaking above its opening range or VWAP, with volume support.
  * Do NOT chase a move that has already run far from VWAP (poor risk/reward).
  * Place the stop at a level that would genuinely invalidate your idea (below the opening-range midpoint, below VWAP, or below the recent swing low), not an arbitrary tight number.
  * Place the target where the move realistically stalls (recent high, round number, or a measured move), not at an unreachable price.
  * If the setup is unclear, choppy, or the risk/reward is poor, answer PASS. Passing is free; a bad trade is not.

Return RAW JSON ONLY, no markdown, no commentary:
{"action":"BUY" or "PASS","entry":<float>,"stop":<float>,"target":<float>,"confidence_pct":<50-95>,"reasoning":"one sentence"}
For PASS, set entry/stop/target to 0.
Do not invent data that is not in the input."""


class AIDecisionEngine:
    """AI-decides strategy: the model proposes the full trade geometry."""

    STRATEGY_ID = "ai_decides"
    NAME = "AI-Decides Intraday"

    def __init__(
        self,
        max_stop_pct: float = 0.025,
        min_stop_pct: float = 0.0015,
        min_rr: float = 1.2,
        min_edge_to_fee: float = 2.0,
        max_entry_deviation_pct: float = 0.005,
        est_position_inr: float = 3000.0,
        timeout_s: float = 30.0,
        max_calls: int = 400,
    ) -> None:
        self.max_stop_pct = max_stop_pct
        self.min_stop_pct = min_stop_pct
        self.min_rr = min_rr
        self.min_edge_to_fee = min_edge_to_fee
        self.max_entry_deviation_pct = max_entry_deviation_pct
        self.est_position_inr = est_position_inr
        self.timeout_s = timeout_s
        # Hard ceiling on model calls per run, so a research run cannot burn
        # unbounded spend if the candidate surface is larger than expected.
        self.max_calls = max_calls
        # Observability: how many proposals were made / accepted / rejected and why.
        self.stats: Dict[str, Any] = {
            "calls": 0,
            "proposed_buy": 0,
            "proposed_pass": 0,
            "accepted": 0,
            "rejected": 0,
            "reject_reasons": {},
            "errors": 0,
            "cost_inr": 0.0,
        }

    # ------------------------------------------------------------------
    def reset_stats(self) -> None:
        self.stats = {
            "calls": 0,
            "proposed_buy": 0,
            "proposed_pass": 0,
            "accepted": 0,
            "rejected": 0,
            "reject_reasons": {},
            "errors": 0,
            "cost_inr": 0.0,
        }

    def _reject(self, reason: str) -> None:
        self.stats["rejected"] += 1
        bucket = self.stats["reject_reasons"]
        bucket[reason] = bucket.get(reason, 0) + 1

    # ------------------------------------------------------------------
    def _build_context(
        self,
        symbol: str,
        df: pd.DataFrame,
        capital_inr: float,
    ) -> Dict[str, Any]:
        """Compact, model-friendly snapshot of the current bar and indicators."""
        cur = df.iloc[-1]
        price = float(cur["close"])
        day_open = float(df.iloc[0]["open"])
        or_df = df.iloc[:3]
        or_high = float(or_df["high"].max())
        or_low = float(or_df["low"].min())

        def _f(row, key, default=0.0):
            try:
                v = row.get(key, default)
                return round(float(v), 4) if v == v else default  # NaN guard
            except Exception:
                return default

        recent = []
        for _, r in df.tail(8).iterrows():
            recent.append({
                "t": str(r.get("time", "")),
                "o": round(float(r["open"]), 2),
                "h": round(float(r["high"]), 2),
                "l": round(float(r["low"]), 2),
                "c": round(float(r["close"]), 2),
                "v": int(r.get("volume", 0) or 0),
            })

        return {
            "symbol": symbol,
            "current_time": str(cur.get("time", "")),
            "current_price": round(price, 2),
            "day_open": round(day_open, 2),
            "day_return_pct": round((price / day_open - 1) * 100, 3) if day_open else 0.0,
            "opening_range_high": round(or_high, 2),
            "opening_range_low": round(or_low, 2),
            "opening_range_mid": round((or_high + or_low) / 2, 2),
            "vwap": _f(cur, "vwap"),
            "ema_9": _f(cur, "ema_9"),
            "ema_20": _f(cur, "ema_20"),
            "ema_50": _f(cur, "ema_50"),
            "atr_14": _f(cur, "atr_14"),
            "rsi_14": _f(cur, "rsi_14"),
            "supertrend": _f(cur, "supertrend"),
            "volume": int(cur.get("volume", 0) or 0),
            "recent_bars": recent,
            "account": {
                "capital_inr": round(capital_inr, 2),
                "risk_per_trade_pct": settings.RISK_PER_TRADE_PCT,
                "max_position_pct": settings.MAX_POSITION_PCT,
                "round_trip_fee_pct": 0.106,
            },
        }

    # ------------------------------------------------------------------
    async def decide(
        self,
        symbol: str,
        df: pd.DataFrame,
        capital_inr: float,
    ) -> Dict[str, Any]:
        """Ask the model for a full trade proposal. Returns a raw decision dict."""
        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_"):
            return {
                "action": "PASS",
                "entry": 0.0, "stop": 0.0, "target": 0.0,
                "confidence_pct": 0,
                "reasoning": "DeepSeek not configured.",
                "cost_inr": 0.0,
            }
        if self.stats["calls"] >= self.max_calls:
            return {
                "action": "PASS",
                "entry": 0.0, "stop": 0.0, "target": 0.0,
                "confidence_pct": 0,
                "reasoning": "Call budget exhausted.",
                "cost_inr": 0.0,
            }

        context = self._build_context(symbol, df, capital_inr)
        prompt = f"{SYSTEM_PROMPT}\n\nMARKET SNAPSHOT:\n{json.dumps(context, default=str)}"

        self.stats["calls"] += 1
        try:
            client = create_genai_client(api_key, location="global")
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=settings.DEEPSEEK_MODEL,
                    contents=prompt,
                ),
                timeout=self.timeout_s,
            )
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            decision = json.loads(match.group(0) if match else text)
            usage = getattr(resp, "usage_metadata", None)
            p_toks = getattr(usage, "prompt_token_count", 700) if usage else 700
            c_toks = getattr(usage, "candidates_token_count", 120) if usage else 120
            cost = token_tracker.calculate_cost_inr(settings.DEEPSEEK_MODEL, p_toks, c_toks)
            decision["cost_inr"] = cost
            self.stats["cost_inr"] = round(self.stats["cost_inr"] + cost, 4)
            return decision
        except Exception as exc:
            logger.warning("AI decision call failed for %s: %s", symbol, exc)
            self.stats["errors"] += 1
            return {
                "action": "PASS",
                "entry": 0.0, "stop": 0.0, "target": 0.0,
                "confidence_pct": 0,
                "reasoning": f"AI unavailable: {exc}",
                "cost_inr": 0.0,
            }

    # ------------------------------------------------------------------
    def validate(
        self,
        decision: Dict[str, Any],
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """Apply the safety rails. Returns a clean setup dict, or None to reject."""
        action = str(decision.get("action", "PASS")).upper()
        if action != "BUY":
            self.stats["proposed_pass"] += 1
            return None
        self.stats["proposed_buy"] += 1

        try:
            entry = float(decision.get("entry") or 0.0)
            stop = float(decision.get("stop") or 0.0)
            target = float(decision.get("target") or 0.0)
        except (TypeError, ValueError):
            self._reject("non_numeric_geometry")
            return None

        if current_price <= 0:
            self._reject("invalid_price")
            return None

        # Rail 2: entry must be near the current price (filled at next bar open).
        if entry <= 0 or abs(entry - current_price) / current_price > self.max_entry_deviation_pct:
            self._reject("entry_too_far_from_price")
            return None

        # Rail 3: stop below entry, within the allowed distance band.
        if stop <= 0 or stop >= entry:
            self._reject("stop_not_below_entry")
            return None
        stop_pct = (entry - stop) / entry
        if stop_pct < self.min_stop_pct:
            self._reject("stop_too_tight")
            return None
        if stop_pct > self.max_stop_pct:
            self._reject("stop_too_wide")
            return None

        # Rail 4: target above entry with an acceptable reward/risk.
        if target <= entry:
            self._reject("target_not_above_entry")
            return None
        risk = entry - stop
        reward = target - entry
        if reward < risk * self.min_rr:
            self._reject("reward_risk_too_low")
            return None

        # Rail 5: the target must clear round-trip fees by the required multiple.
        est_qty = max(1, int(self.est_position_inr / entry))
        gross_edge = reward * est_qty
        round_trip = calculate_buy_fee(entry * est_qty) + calculate_sell_fee(target * est_qty)
        edge_to_fee = (gross_edge / round_trip) if round_trip > 0 else float("inf")
        if edge_to_fee < self.min_edge_to_fee:
            self._reject("edge_below_fee_multiple")
            return None

        self.stats["accepted"] += 1
        return {
            "active": True,
            "strategy_id": self.STRATEGY_ID,
            "name": self.NAME,
            "signal": "BUY",
            "entry_price": round(entry, 2),
            "stop_loss": round(stop, 2),
            "target_1": round(target, 2),
            "hold_to_close": False,
            "confidence": int(decision.get("confidence_pct", 70) or 70),
            "edge_to_fee_ratio": round(edge_to_fee, 2),
            "ai_reasoning": str(decision.get("reasoning", ""))[:300],
            "rationale": f"AI-decides: {str(decision.get('reasoning', ''))[:200]}",
        }


ai_decision_engine = AIDecisionEngine()
