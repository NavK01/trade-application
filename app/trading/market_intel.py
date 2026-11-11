"""Market Intelligence Agent: scored, multi-day directional forecasts.

What this is (and is not)
-------------------------
This is *not* a promise that an LLM can predict the market. It is an honest
research instrument: once per trading day it builds an evidence pack for each
symbol (real daily bars/technicals, real news headlines, live quote context),
asks DeepSeek for a directional view over a fixed multi-day horizon, and
**persists every forecast with the reference price and horizon date**. Later,
a resolution pass scores each forecast against the price that actually
printed, so the system can measure whether the analysis adds any real skill
over time.

Guarantees
----------
* Analysis only. It never places, sizes, or closes a trade.
* Deterministic evidence: signals are computed on *completed* daily bars only
  (today's still-forming bar is dropped).
* No claimed edge without measurement: the report returns an
  ``insufficient_sample`` verdict until enough forecasts have resolved.
* External inputs are validated at the boundary; a malformed model answer is
  discarded, never stored.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pandas as pd

from app.config import settings, create_genai_client
from app.storage.db import db
from app.trading import market_hours
from app.trading.institutional_engine import calculate_indian_statutory_charges
from app.trading.swing_live_runner import (
    SWING_UNIVERSE,
    _normalize_daily,
    fetch_daily_bars,
)
from app.trading.swing_strategies import compute_swing_features

logger = logging.getLogger("app.trading.market_intel")

# Index tickers appended to the equity universe. Stored by Yahoo ticker; the
# report maps them back to friendly labels.
INDEX_TICKERS: List[str] = ["^NSEI", "^NSEBANK"]
SYMBOL_LABELS: Dict[str, str] = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY"}

ALLOWED_DIRECTIONS = {"UP", "DOWN", "FLAT"}

SYSTEM_PROMPT = """You are a disciplined equity research analyst covering the Indian NSE market.
You are given an evidence pack for ONE symbol. Forecast where its price will be after {horizon} trading days relative to the last completed close.

WEIGH, IN ORDER OF RELIABILITY:
  1. Multi-day trend and momentum (position vs 20/50/200-day averages, 5/20/60-day returns).
  2. Mean reversion: an extreme 2-day RSI after a sustained fall can snap back; an extreme run-up can stall.
  3. Volatility: scale your expected move to the ATR%, not to a fixed number.
  4. News catalysts in the pack, but treat headlines as weak/tentative evidence.
  5. Valuation/52-week position as slow context only.

RULES:
  * direction must be exactly one of "UP", "DOWN", "FLAT".
  * "FLAT" means you expect the absolute move to stay within {band}% over the horizon.
  * expected_move_pct is your signed point estimate (+ for UP, - for DOWN, near 0 for FLAT).
  * confidence_pct is 0-100. Be honest: use 50-60 when the evidence is mixed, not 90.
  * Use ONLY the supplied evidence. Do not invent prices, dates, or facts.

Return RAW JSON ONLY (no markdown, no commentary):
{{"direction":"UP"|"DOWN"|"FLAT","expected_move_pct":<float>,"confidence_pct":<int>,"catalysts":"one short phrase or empty","reasoning":"one or two sentences"}}"""


# ----------------------------------------------------------------------
# Pure helpers (unit-testable, no I/O)
# ----------------------------------------------------------------------
def add_business_days(start: datetime.date, days: int) -> datetime.date:
    """Add ``days`` weekdays to ``start`` (holidays handled at resolution)."""
    cursor = start
    remaining = max(0, int(days))
    while remaining > 0:
        cursor += datetime.timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def classify_direction(move_pct: float, flat_band_pct: float) -> str:
    """Map a realized/expected percentage move to UP/DOWN/FLAT."""
    if move_pct > flat_band_pct:
        return "UP"
    if move_pct < -flat_band_pct:
        return "DOWN"
    return "FLAT"


def delivery_round_trip_cost_pct(notional_inr: float) -> float:
    """Round-trip Indian delivery cost as a percentage of notional."""
    if notional_inr <= 0:
        return 0.0
    charges = calculate_indian_statutory_charges(
        buy_value=notional_inr,
        sell_value=notional_inr,
        instrument_type="DELIVERY_EQUITY",
        quantity=1,
        slippage_pct=0.0,
    )
    return round(float(charges["total_charges"]) / notional_inr * 100.0, 4)


def _parse_iso_date(value: Any) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
def _default_model_fn(prompt: str) -> Dict[str, Any]:
    """Blocking DeepSeek call. Returns {text, prompt_tokens, completion_tokens}."""
    client = create_genai_client(settings.DEEPSEEK_API_KEY, location="global")
    resp = client.models.generate_content(model=settings.DEEPSEEK_MODEL, contents=prompt)
    usage = getattr(resp, "usage_metadata", None)
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    return {
        "text": (getattr(resp, "text", "") or "").strip(),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


class MarketIntelAgent:
    """Daily scored directional forecasting over a multi-day horizon."""

    def __init__(
        self,
        universe: Optional[List[str]] = None,
        data_provider: Optional[Callable[[str], pd.DataFrame]] = None,
        news_provider: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        model_fn: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None,
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
        database: Any = None,
        horizon_days: Optional[int] = None,
        flat_band_pct: Optional[float] = None,
        max_symbols: Optional[int] = None,
        notional_inr: Optional[float] = None,
        timeout_s: float = 30.0,
    ):
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.interval_seconds = 300
        base_universe = list(universe) if universe is not None else list(SWING_UNIVERSE) + INDEX_TICKERS
        self.universe = base_universe
        self._data_provider = data_provider or fetch_daily_bars
        self._news_provider = news_provider or self._default_news
        self._model_fn = model_fn or (lambda p: asyncio.to_thread(_default_model_fn, p))
        self._now_provider = now_provider or market_hours.now_ist
        self.db = database or db
        self.horizon_days = int(horizon_days or settings.MARKET_INTEL_HORIZON_DAYS)
        self.flat_band_pct = float(flat_band_pct if flat_band_pct is not None else settings.MARKET_INTEL_FLAT_BAND_PCT)
        self.max_symbols = int(max_symbols if max_symbols is not None else settings.MARKET_INTEL_MAX_SYMBOLS)
        self.notional_inr = float(notional_inr if notional_inr is not None else settings.MARKET_INTEL_NOTIONAL_INR)
        self.timeout_s = float(timeout_s)
        self._cost_pct = delivery_round_trip_cost_pct(self.notional_inr)
        self._last_cycle_date: Optional[str] = None
        self.stats: Dict[str, Any] = {"cycles": 0, "forecasts": 0, "errors": 0, "last_cycle_iso": None}

    # ------------------------------------------------------------------
    def _now(self) -> datetime.datetime:
        return market_hours.as_ist(self._now_provider())

    def _label(self, symbol: str) -> str:
        return SYMBOL_LABELS.get(symbol.upper(), symbol.upper())

    def _default_news(self, symbol: str) -> List[Dict[str, Any]]:
        """Real news headlines via web search; never raises."""
        try:
            from app.financial_advisor.india_market import search_indian_market_news

            res = search_indian_market_news(
                f"{self._label(symbol)} share price outlook NSE India",
                max_results=int(settings.MARKET_INTEL_NEWS_MAX_RESULTS),
            )
            articles = res.get("articles", []) if isinstance(res, dict) else []
            return [
                {"title": str(a.get("title", ""))[:180], "snippet": str(a.get("snippet", ""))[:240]}
                for a in articles[: int(settings.MARKET_INTEL_NEWS_MAX_RESULTS)]
            ]
        except Exception as exc:
            logger.debug("News fetch failed for %s: %s", symbol, exc)
            return []

    # ------------------------------------------------------------------
    def _completed_bars(self, symbol: str, period: str = "1y") -> pd.DataFrame:
        """Daily bars with today's still-forming bar removed."""
        raw = self._data_provider(symbol)
        df = _normalize_daily(raw)
        if df.empty:
            return df
        today_str = market_hours.today_ist_str(self._now_provider())
        if str(df["date"].iloc[-1])[:10] == today_str:
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    @staticmethod
    def _build_technicals(df: pd.DataFrame) -> Dict[str, Any]:
        feat = compute_swing_features(df)
        if feat.empty:
            return {}
        last = feat.iloc[-1]
        closes = df["close"].astype(float).reset_index(drop=True)
        close = float(last["close"])

        def _ret(days: int) -> Optional[float]:
            if len(closes) <= days:
                return None
            prev = float(closes.iloc[-1 - days])
            return round((close / prev - 1.0) * 100.0, 2) if prev > 0 else None

        window = closes.tail(252)
        hi, lo = float(window.max()), float(window.min())
        pos_52w = round((close - lo) / (hi - lo) * 100.0, 1) if hi > lo else 50.0

        daily_returns = closes.pct_change().dropna()
        realized_vol = round(float(daily_returns.tail(20).std() or 0.0) * math.sqrt(252) * 100.0, 1)

        atr = float(last.get("atr_14", 0.0) or 0.0)
        sma_20 = float(last.get("sma_20", float("nan")))
        sma_50 = float(last.get("sma_50", float("nan")))
        sma_200 = float(last.get("sma_200", float("nan")))
        return {
            "close": round(close, 2),
            "rsi_2": round(float(last.get("rsi_2", float("nan"))), 1),
            "rsi_14": round(float(last.get("rsi_14", float("nan"))), 1),
            "atr_pct": round(atr / close * 100.0, 2) if close > 0 else 0.0,
            "ret_5d_pct": _ret(5),
            "ret_20d_pct": _ret(20),
            "ret_60d_pct": _ret(60),
            "pct_from_sma20": round((close / sma_20 - 1.0) * 100.0, 2) if sma_20 == sma_20 and sma_20 else None,
            "pct_from_sma50": round((close / sma_50 - 1.0) * 100.0, 2) if sma_50 == sma_50 and sma_50 else None,
            "pct_from_sma200": round((close / sma_200 - 1.0) * 100.0, 2) if sma_200 == sma_200 and sma_200 else None,
            "position_in_52w_range_pct": pos_52w,
            "realized_vol_20d_ann_pct": realized_vol,
        }

    def _build_evidence(self, symbol: str, df: pd.DataFrame, news: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "symbol": self._label(symbol),
            "as_of": str(df["date"].iloc[-1])[:10],
            "horizon_days": self.horizon_days,
            "technicals": self._build_technicals(df),
            "news": news,
        }

    # ------------------------------------------------------------------
    def _parse_forecast(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            payload = json.loads(match.group(0) if match else text)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        direction = str(payload.get("direction", "")).strip().upper()
        if direction not in ALLOWED_DIRECTIONS:
            return None
        try:
            expected = float(payload.get("expected_move_pct", 0.0))
            confidence = int(round(float(payload.get("confidence_pct", 50))))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(expected):
            return None
        confidence = max(0, min(100, confidence))
        return {
            "direction": direction,
            "expected_move_pct": round(expected, 2),
            "confidence_pct": confidence,
            "catalysts": str(payload.get("catalysts", ""))[:200],
            "reasoning": str(payload.get("reasoning", ""))[:400],
        }

    async def _forecast_symbol(self, symbol: str, run_date: str) -> Optional[Dict[str, Any]]:
        df = await asyncio.to_thread(self._completed_bars, symbol)
        if df.empty or len(df) < 60:
            logger.debug("Market intel: insufficient bars for %s", symbol)
            return None
        news = await asyncio.to_thread(self._news_provider, symbol)
        evidence = self._build_evidence(symbol, df, news)
        prompt = (
            SYSTEM_PROMPT.format(horizon=self.horizon_days, band=self.flat_band_pct)
            + "\n\nEVIDENCE PACK:\n"
            + json.dumps(evidence, default=str)
        )
        result = await asyncio.wait_for(self._model_fn(prompt), timeout=self.timeout_s)
        parsed = self._parse_forecast(result.get("text", ""))
        if parsed is None:
            self.stats["errors"] += 1
            logger.warning("Market intel: unparseable forecast for %s", symbol)
            return None

        prompt_tokens = int(result.get("prompt_tokens", 0) or 0)
        completion_tokens = int(result.get("completion_tokens", 0) or 0)
        from app.system.tokens import token_tracker

        cost = token_tracker.log_usage(
            module="market_intel",
            model=settings.DEEPSEEK_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            account_id="analysis",
        )

        forecast_date = evidence["as_of"]
        horizon_date = add_business_days(
            _parse_iso_date(forecast_date) or self._now().date(), self.horizon_days
        ).isoformat()
        return {
            "symbol": symbol.upper(),
            "run_date": run_date,
            "forecast_date": forecast_date,
            "horizon_days": self.horizon_days,
            "horizon_date": horizon_date,
            "reference_price": float(evidence["technicals"]["close"]),
            "direction": parsed["direction"],
            "expected_move_pct": parsed["expected_move_pct"],
            "confidence_pct": parsed["confidence_pct"],
            "flat_band_pct": self.flat_band_pct,
            "catalysts": parsed["catalysts"],
            "reasoning": parsed["reasoning"],
            "model": settings.DEEPSEEK_MODEL,
            "tokens_used": prompt_tokens + completion_tokens,
            "cost_inr": cost,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    async def run_cycle(self, now: Optional[datetime.datetime] = None, force: bool = False) -> Dict[str, Any]:
        """Generate and persist one forecast per symbol, at most once per day."""
        current = market_hours.as_ist(now or self._now_provider())
        run_date = current.date().isoformat()
        result: Dict[str, Any] = {"status": "ok", "run_date": run_date, "forecasted": [], "skipped": [], "errors": 0}

        if not force and self._last_cycle_date == run_date:
            result["status"] = "already_ran"
            return result

        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_"):
            result["status"] = "no_api_key"
            return result

        symbols = self.universe[: self.max_symbols]
        max_calls = min(int(settings.MARKET_INTEL_MAX_AI_CALLS), len(symbols))
        for symbol in symbols:
            if result["errors"] >= 5 and len(result["forecasted"]) == 0:
                # Abort early if every call is failing (likely a config outage).
                result["status"] = "aborted"
                break
            if len(result["forecasted"]) >= max_calls:
                break
            try:
                if not force and self.db.has_forecast_for(symbol, run_date):
                    result["skipped"].append(symbol)
                    continue
                forecast = await self._forecast_symbol(symbol, run_date)
                if forecast is None:
                    result["errors"] += 1
                    continue
                self.db.record_forecast(forecast)
                self.stats["forecasts"] += 1
                result["forecasted"].append(self._label(symbol))
            except Exception as exc:
                result["errors"] += 1
                logger.error("Market intel forecast failed for %s: %s", symbol, exc)

        self._last_cycle_date = run_date
        self.stats["cycles"] += 1
        self.stats["last_cycle_iso"] = current.isoformat()
        return result

    # ------------------------------------------------------------------
    async def resolve_due_forecasts(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """Score every open forecast whose horizon has a completed bar."""
        self._now()  # touch now_provider for testability/consistency
        open_rows = self.db.get_open_forecasts()
        resolved: List[Dict[str, Any]] = []
        result: Dict[str, Any] = {"status": "ok", "resolved": resolved, "pending": 0, "errors": 0}
        for row in open_rows:
            try:
                outcome = await self._resolve_one(row)
            except Exception as exc:
                result["errors"] += 1
                logger.error("Market intel resolution failed for %s: %s", row.get("symbol"), exc)
                continue
            if outcome is None:
                result["pending"] += 1
                continue
            self.db.resolve_forecast(int(row["id"]), **outcome)
            resolved.append({
                "symbol": self._label(str(row["symbol"])),
                "direction": row["direction"],
                "actual_direction": outcome["actual_direction"],
                "correct": bool(outcome["direction_correct"]),
                "actual_move_pct": outcome["actual_move_pct"],
            })
        return result

    async def _resolve_one(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        horizon_date = _parse_iso_date(row.get("horizon_date"))
        if horizon_date is None:
            return None
        df = await asyncio.to_thread(self._completed_bars, str(row["symbol"]), "3mo")
        if df.empty:
            return None
        dates = [str(d)[:10] for d in df["date"].tolist()]
        horizon_str = horizon_date.isoformat()
        idx = next((i for i, d in enumerate(dates) if d >= horizon_str), None)
        if idx is None:
            return None  # not enough completed bars yet
        resolved_price = float(df["close"].iloc[idx])
        ref = float(row["reference_price"])
        if ref <= 0:
            return None
        actual_move_pct = round((resolved_price / ref - 1.0) * 100.0, 2)
        flat_band = float(row.get("flat_band_pct", self.flat_band_pct) or self.flat_band_pct)
        actual_direction = classify_direction(actual_move_pct, flat_band)
        forecast_direction = str(row["direction"]).upper()
        correct = forecast_direction == actual_direction

        signed = actual_move_pct if forecast_direction == "UP" else (-actual_move_pct if forecast_direction == "DOWN" else 0.0)
        realized_net_pct = round(signed - self._cost_pct, 2)
        expected = float(row.get("expected_move_pct", 0.0) or 0.0)
        return {
            "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "resolved_price": round(resolved_price, 2),
            "actual_direction": actual_direction,
            "actual_move_pct": actual_move_pct,
            "direction_correct": 1 if correct else 0,
            "abs_error_pct": round(abs(expected - actual_move_pct), 2),
            "realized_net_pct": realized_net_pct,
        }

    # ------------------------------------------------------------------
    def build_report(self, limit: int = 200) -> Dict[str, Any]:
        rows = self.db.get_forecasts(limit=limit)
        resolved = [r for r in rows if str(r.get("status")) == "resolved"]
        open_rows = [r for r in rows if str(r.get("status")) == "open"]
        n = len(resolved)

        stats: Dict[str, Any] = {
            "closed_forecasts": n,
            "open_forecasts": len(open_rows),
            "min_closed_for_verdict": int(settings.MARKET_INTEL_MIN_CLOSED_FOR_VERDICT),
            "is_conclusive": n >= int(settings.MARKET_INTEL_MIN_CLOSED_FOR_VERDICT),
            "horizon_days": self.horizon_days,
            "flat_band_pct": self.flat_band_pct,
            "round_trip_cost_pct": self._cost_pct,
        }

        if n > 0:
            correct = sum(int(r.get("direction_correct") or 0) for r in resolved)
            moves = [float(r.get("actual_move_pct") or 0.0) for r in resolved]
            nets = [float(r.get("realized_net_pct") or 0.0) for r in resolved]
            errs = [float(r.get("abs_error_pct") or 0.0) for r in resolved]
            brier_sum = 0.0
            for r in resolved:
                p = max(0.0, min(1.0, float(r.get("confidence_pct") or 50) / 100.0))
                outcome = 1.0 if int(r.get("direction_correct") or 0) else 0.0
                brier_sum += (p - outcome) ** 2
            nets_sum = sum(nets)
            stats.update({
                "direction_hit_rate_pct": round(correct / n * 100.0, 1),
                "mean_abs_error_pct": round(sum(errs) / n, 2),
                "brier_score": round(brier_sum / n, 4),
                "avg_realized_net_pct": round(nets_sum / n, 3),
                "sum_realized_net_pct": round(nets_sum, 2),
                "avg_actual_move_pct": round(sum(moves) / n, 3),
            })

        # Per-symbol breakdown over resolved forecasts.
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for r in resolved:
            sym = self._label(str(r.get("symbol")))
            bucket = per_symbol.setdefault(sym, {"forecasts": 0, "correct": 0, "net_pct_sum": 0.0})
            bucket["forecasts"] += 1
            bucket["correct"] += int(r.get("direction_correct") or 0)
            bucket["net_pct_sum"] += float(r.get("realized_net_pct") or 0.0)
        for bucket in per_symbol.values():
            bucket["hit_rate_pct"] = round(bucket["correct"] / bucket["forecasts"] * 100.0, 1) if bucket["forecasts"] else 0.0
            bucket["avg_net_pct"] = round(bucket["net_pct_sum"] / bucket["forecasts"], 3) if bucket["forecasts"] else 0.0

        if n < int(settings.MARKET_INTEL_MIN_CLOSED_FOR_VERDICT):
            verdict = "insufficient_sample"
        elif stats.get("avg_realized_net_pct", 0.0) > 0 and stats.get("direction_hit_rate_pct", 0.0) >= 50.0:
            verdict = "on_track"
        else:
            verdict = "underperforming"

        return {
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": settings.DEEPSEEK_MODEL,
            "statistics": stats,
            "verdict": verdict,
            "per_symbol": per_symbol,
            "recent": [
                {
                    "id": r["id"],
                    "symbol": self._label(str(r["symbol"])),
                    "run_date": r.get("run_date"),
                    "forecast_date": r.get("forecast_date"),
                    "horizon_date": r.get("horizon_date"),
                    "reference_price": r.get("reference_price"),
                    "direction": r.get("direction"),
                    "expected_move_pct": r.get("expected_move_pct"),
                    "confidence_pct": r.get("confidence_pct"),
                    "status": r.get("status"),
                    "actual_direction": r.get("actual_direction"),
                    "actual_move_pct": r.get("actual_move_pct"),
                    "direction_correct": r.get("direction_correct"),
                    "realized_net_pct": r.get("realized_net_pct"),
                    "catalysts": r.get("catalysts"),
                    "reasoning": r.get("reasoning"),
                }
                for r in rows[:60]
            ],
        }

    # ------------------------------------------------------------------
    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._loop())
            logger.info("Started market intelligence agent (scored multi-day forecasts).")

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("Stopped market intelligence agent.")

    async def _loop(self):
        while self.is_running:
            try:
                now = self._now()
                if market_hours.is_trading_day(now) and now.hour >= int(settings.MARKET_INTEL_RUN_HOUR_IST):
                    await self.resolve_due_forecasts(now)
                    await self.run_cycle(now)
            except Exception as exc:
                logger.error("Market intelligence loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self.interval_seconds)


market_intel = MarketIntelAgent()
