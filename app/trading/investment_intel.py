"""Investment Intelligence Agent: long-horizon, explainable, scored advice.

What this is (and is not)
-------------------------
This is *not* a promise that an LLM can beat the market. It is an honest
"investment committee" instrument. Once per trading day it builds a rich
evidence pack for each symbol:

* real completed daily bars and technicals (trend, trend persistence, ATR,
  drawdown from the 52-week high, position in the 52-week range);
* a rule-based reading of *who* is trading (institutional accumulation vs
  distribution, insider net buying vs selling, or retail-dominated flow);
* a rule-based **permanence** classifier that separates *structural* moves
  from *temporary* noise;
* real news headlines.

It then asks DeepSeek for a plain-English verdict (ACCUMULATE / HOLD / AVOID /
WATCH) over a multi-week horizon, with a confidence level, an expected price
band, a crash-risk flag, and 3-4 "why" bullets a non-trader can follow.

Crucially, **every call is persisted** with its reference price and horizon,
then resolved against the price that actually printed, so the system can prove
whether the advice adds value (rather than assuming it does).

Guarantees
----------
* Analysis only. It never places, sizes, or closes a trade.
* Deterministic evidence: signals are computed on *completed* daily bars only.
* No claimed edge without measurement: the report returns an
  ``insufficient_sample`` verdict until enough calls have resolved.
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
from app.trading.market_intel import (
    INDEX_TICKERS,
    SYMBOL_LABELS,
    add_business_days,
    classify_direction,
)
from app.trading.swing_live_runner import (
    SWING_UNIVERSE,
    _normalize_daily,
    fetch_daily_bars,
)
from app.trading.swing_strategies import compute_swing_features

logger = logging.getLogger("app.trading.investment_intel")

ALLOWED_VERDICTS = {"ACCUMULATE", "HOLD", "AVOID", "WATCH"}
ALLOWED_PERMANENCE = {
    "structural_up",
    "structural_down",
    "temporary_up",
    "temporary_down",
    "range",
}
VALUED_VERDICTS = {"ACCUMULATE", "AVOID"}

# Expected realized direction encoded by each verdict; used for scoring.
VERDICT_DIRECTION: Dict[str, str] = {
    "ACCUMULATE": "UP",
    "AVOID": "DOWN",
    "HOLD": "FLAT",
    "WATCH": "FLAT",
}

SYSTEM_PROMPT = """You are the chair of a disciplined India (NSE) equity investment committee.
You are given an evidence pack for ONE stock and must give a plain-English, multi-week investment call.
Your horizon is about {horizon} trading days (~{weeks} weeks). The reference price is the last completed close.

WEIGH, IN ORDER OF RELIABILITY:
  1. Multi-day trend and its PERSISTENCE (position vs 20/50/200-day averages, 5/20/60-day returns).
  2. Who is trading: institutional accumulation/distribution and insider net buying/selling. Treat these as strong evidence of permanence, but never invent flow that is not in the pack.
  3. Whether the recent move looks STRUCTURAL (supported by trend + flow) or TEMPORARY (a spike/overshoot with no flow support).
  4. Volatility (ATR%) and drawdown from the 52-week high to size the expected band and crash risk.
  5. News catalysts in the pack, treated as weak/tentative evidence.

VERDICT MEANINGS:
  * "ACCUMULATE": you expect a genuine positive move beyond {band}% over the horizon; a stock worth buying and holding.
  * "HOLD": you expect the price to stay broadly intact (within ~{band}%); no fresh action.
  * "AVOID": you expect a negative move beyond {band}%; do not own it.
  * "WATCH": genuinely two-sided / not enough conviction; do not act yet.

RULES:
  * verdict must be exactly one of "ACCUMULATE", "HOLD", "AVOID", "WATCH".
  * band_low_pct / band_high_pct are your expected price range as signed % vs the reference close (low <= high).
  * confidence_pct is 0-100. Be honest: 50-60 when mixed, never 95.
  * crash_risk must be "low", "medium", or "high"; crash_reason one short phrase or empty.
  * who_is_trading: one short phrase (e.g. "institutions accumulating, insiders selling").
  * move_permanence must be exactly one of "structural_up", "structural_down", "temporary_up", "temporary_down", "range".
  * why must be 3-4 short bullets in plain language a non-investor can understand.
  * Use ONLY the supplied evidence. Do not invent prices, dates, flows, or facts.

Return RAW JSON ONLY (no markdown, no commentary):
{{"verdict":"ACCUMULATE"|"HOLD"|"AVOID"|"WATCH","confidence_pct":<int>,"band_low_pct":<float>,"band_high_pct":<float>,"who_is_trading":"<short phrase>","move_permanence":"structural_up"|"structural_down"|"temporary_up"|"temporary_down"|"range","crash_risk":"low"|"medium"|"high","crash_reason":"<short or empty>","why":["<bullet>","<bullet>","<bullet>"],"reasoning":"<two or three sentences>"}}"""


# ----------------------------------------------------------------------
# Pure helpers (unit-testable, no I/O)
# ----------------------------------------------------------------------
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


def expected_direction_for_verdict(verdict: str) -> Optional[str]:
    """Map an investment verdict to the realized direction it implies."""
    return VERDICT_DIRECTION.get(str(verdict).strip().upper())


def classify_move_permanence(technicals: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based structural-vs-temporary classifier from technicals.

    Returns ``{label, confidence_pct, reasons}``. It is deliberately simple and
    deterministic so it can be unit-tested and explained to a non-trader.
    """
    ret_20 = technicals.get("ret_20d_pct")
    ret_60 = technicals.get("ret_60d_pct")
    from_sma50 = technicals.get("pct_from_sma50")
    from_sma200 = technicals.get("pct_from_sma200")
    pos = technicals.get("position_in_52w_range_pct")
    reasons: List[str] = []

    def _num(value: Any) -> Optional[float]:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    ret_20 = _num(ret_20)
    ret_60 = _num(ret_60)
    from_sma50 = _num(from_sma50)
    from_sma200 = _num(from_sma200)
    pos = _num(pos)

    above_50 = from_sma50 is not None and from_sma50 > 0
    above_200 = from_sma200 is not None and from_sma200 > 0

    label = "range"
    confidence = 45

    uptrend = (ret_60 is not None and ret_60 > 5) and above_50
    downtrend = (ret_60 is not None and ret_60 < -5) and (from_sma50 is not None and from_sma50 < 0)

    if uptrend:
        label = "structural_up"
        confidence = 70 if above_200 else 58
        reasons.append(f"6-month trend is up ({ret_60:+.1f}%) with price above its 50-day average.")
        if above_200:
            reasons.append("Price is also above its 200-day average, a sign of durable demand.")
    elif downtrend:
        label = "structural_down"
        confidence = 70 if (from_sma200 is not None and from_sma200 < 0) else 58
        reasons.append(f"6-month trend is down ({ret_60:+.1f}%) with price below its 50-day average.")
    elif ret_20 is not None and ret_20 > 3 and ((ret_60 is None) or ret_60 <= 5):
        label = "temporary_up"
        confidence = 55
        reasons.append(f"A sharp 1-month bounce ({ret_20:+.1f}%) without a matching 6-month trend, so it may fade.")
    elif ret_20 is not None and ret_20 < -3 and not downtrend:
        label = "temporary_down"
        confidence = 55
        reasons.append(f"A sharp 1-month drop ({ret_20:+.1f}%) that is not yet a confirmed downtrend.")
    else:
        reasons.append("No clear multi-month trend; the stock is moving sideways.")

    if pos is not None:
        if pos >= 80:
            reasons.append(f"Trading near the top of its 52-week range ({pos:.0f}%).")
        elif pos <= 20:
            reasons.append(f"Trading near the bottom of its 52-week range ({pos:.0f}%).")

    return {"label": label, "confidence_pct": int(confidence), "reasons": reasons[:3]}


def classify_who_is_trading(ownership: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based reading of *who* is buying or selling.

    Returns ``{label, summary, evidence}``. Handles missing data honestly by
    returning ``unknown`` rather than inventing a flow.
    """
    if not ownership:
        return {"label": "unknown", "summary": "No ownership or insider-flow data available.", "evidence": []}

    inst_pct = ownership.get("institutional_pct")
    insider_net = ownership.get("insider_net_shares")
    buys = int(ownership.get("insider_buy_count", 0) or 0)
    sells = int(ownership.get("insider_sell_count", 0) or 0)

    evidence: List[str] = []
    if inst_pct is not None:
        evidence.append(f"Institutional holders own about {float(inst_pct):.1f}% of the company.")
    if buys or sells:
        evidence.append(f"Recent insider transactions: {buys} buy(s) vs {sells} sell(s).")

    insider_buying = (insider_net is not None and insider_net > 0) or buys > sells
    insider_selling = (insider_net is not None and insider_net < 0) or sells > buys

    if insider_buying and not insider_selling:
        return {"label": "insiders_accumulating", "summary": "Insiders are net buyers - company leadership is putting its own money in.", "evidence": evidence}
    if insider_selling and not insider_buying:
        return {"label": "insiders_distributing", "summary": "Insiders are net sellers - company leadership is trimming its stake.", "evidence": evidence}
    if inst_pct is not None and float(inst_pct) >= 40:
        return {"label": "institutions_hold", "summary": "Large institutions hold a big share; flow is professionally driven.", "evidence": evidence}
    if evidence:
        return {"label": "mixed", "summary": "Ownership signals are mixed; no clear dominant buyer or seller.", "evidence": evidence}
    return {"label": "unknown", "summary": "No ownership or insider-flow data available.", "evidence": []}


# ----------------------------------------------------------------------
# Default external providers (best-effort, never raise)
# ----------------------------------------------------------------------
def _to_yf_symbol(symbol: str) -> str:
    clean = symbol.strip().upper()
    if "." in clean or "^" in clean:
        return clean
    return f"{clean}.NS"


def default_ownership_provider(symbol: str) -> Dict[str, Any]:
    """Best-effort institutional + insider data via yfinance. Never raises."""
    try:
        import yfinance as yf
    except Exception:
        return {}

    yf_sym = _to_yf_symbol(symbol)
    out: Dict[str, Any] = {}
    try:
        ticker = yf.Ticker(yf_sym)
    except Exception as exc:
        logger.debug("Ownership ticker failed for %s: %s", symbol, exc)
        return {}

    try:
        holders = ticker.institutional_holders
        if holders is not None and len(holders) > 0:
            out["institutional_holder_count"] = int(len(holders))
            pct_col = next((c for c in holders.columns if "%" in str(c)), None)
            if pct_col is not None:
                values = pd.to_numeric(holders[pct_col], errors="coerce").dropna()
                if len(values) > 0:
                    total = float(values.sum())
                    # yfinance reports "% Out" as a fraction (0.0748 == 7.48%).
                    if float(values.max()) <= 1.5:
                        total *= 100.0
                    out["institutional_pct"] = round(min(total, 100.0), 2)
    except Exception as exc:
        logger.debug("Institutional holders failed for %s: %s", symbol, exc)

    try:
        insiders = ticker.insider_transactions
        if insiders is not None and len(insiders) > 0:
            shares_col = next((c for c in insiders.columns if str(c).lower() == "shares"), None)
            text_col = next(
                (c for c in insiders.columns if str(c).lower() in ("text", "transaction")),
                None,
            )
            shares = pd.to_numeric(insiders[shares_col], errors="coerce").fillna(0.0) if shares_col else None
            texts = insiders[text_col].astype(str).str.lower() if text_col else pd.Series([""] * len(insiders))
            buy_mask = texts.str.contains("purchase|buy", regex=True, na=False)
            sell_mask = texts.str.contains("sale|sell", regex=True, na=False)
            out["insider_buy_count"] = int(buy_mask.sum())
            out["insider_sell_count"] = int(sell_mask.sum())
            if shares is not None:
                net = float(shares[buy_mask].sum()) - float(shares[sell_mask].sum())
                out["insider_net_shares"] = round(net, 0)
    except Exception as exc:
        logger.debug("Insider transactions failed for %s: %s", symbol, exc)

    return out


def default_ipo_provider() -> List[Dict[str, Any]]:
    """Best-effort recent/upcoming NSE IPO headlines via web search. Never raises."""
    try:
        from app.financial_advisor.india_market import search_indian_market_news

        res = search_indian_market_news(
            "upcoming NSE IPO listing 2026 India",
            max_results=int(settings.INVESTMENT_INTEL_NEWS_MAX_RESULTS),
        )
        articles = res.get("articles", []) if isinstance(res, dict) else []
        return [
            {
                "title": str(a.get("title", ""))[:180],
                "url": str(a.get("url", "")),
                "snippet": str(a.get("snippet", ""))[:240],
            }
            for a in articles
        ]
    except Exception as exc:
        logger.debug("IPO watch fetch failed: %s", exc)
        return []


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


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
class InvestmentIntelAgent:
    """Daily scored, explainable long-horizon investment calls."""

    def __init__(
        self,
        universe: Optional[List[str]] = None,
        data_provider: Optional[Callable[[str], pd.DataFrame]] = None,
        news_provider: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        ownership_provider: Optional[Callable[[str], Dict[str, Any]]] = None,
        ipo_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        model_fn: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None,
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
        database: Any = None,
        horizon_days: Optional[int] = None,
        flat_band_pct: Optional[float] = None,
        max_symbols: Optional[int] = None,
        notional_inr: Optional[float] = None,
        ipo_watch_enabled: Optional[bool] = None,
        timeout_s: float = 45.0,
    ):
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.interval_seconds = 300
        base_universe = list(universe) if universe is not None else list(SWING_UNIVERSE) + INDEX_TICKERS
        self.universe = base_universe
        self._data_provider = data_provider or fetch_daily_bars
        self._news_provider = news_provider or self._default_news
        self._ownership_provider = ownership_provider or default_ownership_provider
        self._ipo_provider = ipo_provider or default_ipo_provider
        self._model_fn = model_fn or (lambda p: asyncio.to_thread(_default_model_fn, p))
        self._now_provider = now_provider or market_hours.now_ist
        self.db = database or db
        self.horizon_days = int(horizon_days or settings.INVESTMENT_INTEL_HORIZON_DAYS)
        self.flat_band_pct = float(flat_band_pct if flat_band_pct is not None else settings.INVESTMENT_INTEL_FLAT_BAND_PCT)
        self.max_symbols = int(max_symbols if max_symbols is not None else settings.INVESTMENT_INTEL_MAX_SYMBOLS)
        self.notional_inr = float(notional_inr if notional_inr is not None else settings.INVESTMENT_INTEL_NOTIONAL_INR)
        if ipo_watch_enabled is None:
            ipo_watch_enabled = bool(getattr(settings, "INVESTMENT_INTEL_IPO_WATCH_ENABLED", True))
        self.ipo_watch_enabled = bool(ipo_watch_enabled)
        self.timeout_s = float(timeout_s)
        self._cost_pct = delivery_round_trip_cost_pct(self.notional_inr)
        self._last_cycle_date: Optional[str] = None
        self.stats: Dict[str, Any] = {"cycles": 0, "calls": 0, "errors": 0, "last_cycle_iso": None}

    # ------------------------------------------------------------------
    def _now(self) -> datetime.datetime:
        return market_hours.as_ist(self._now_provider())

    def _label(self, symbol: str) -> str:
        return SYMBOL_LABELS.get(symbol.upper(), symbol.upper())

    def _default_news(self, symbol: str) -> List[Dict[str, Any]]:
        try:
            from app.financial_advisor.india_market import search_indian_market_news

            res = search_indian_market_news(
                f"{self._label(symbol)} share price outlook NSE India",
                max_results=int(settings.INVESTMENT_INTEL_NEWS_MAX_RESULTS),
            )
            articles = res.get("articles", []) if isinstance(res, dict) else []
            return [
                {"title": str(a.get("title", ""))[:180], "snippet": str(a.get("snippet", ""))[:240]}
                for a in articles[: int(settings.INVESTMENT_INTEL_NEWS_MAX_RESULTS)]
            ]
        except Exception as exc:
            logger.debug("News fetch failed for %s: %s", symbol, exc)
            return []

    def _completed_bars(self, symbol: str, period: str = "1y") -> pd.DataFrame:
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
        dd_52w = round((close / hi - 1.0) * 100.0, 2) if hi > 0 else 0.0

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
            "drawdown_from_52w_high_pct": dd_52w,
            "realized_vol_20d_ann_pct": realized_vol,
        }

    @staticmethod
    def _sanitize_ownership(ownership: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(ownership, dict):
            return {}
        allowed = {
            "institutional_pct", "institutional_holder_count",
            "insider_buy_count", "insider_sell_count", "insider_net_shares",
        }
        out: Dict[str, Any] = {}
        for key in allowed:
            if key in ownership and ownership[key] is not None:
                try:
                    out[key] = round(float(ownership[key]), 2)
                except (TypeError, ValueError):
                    continue
        return out

    def _build_evidence(
        self,
        symbol: str,
        df: pd.DataFrame,
        news: List[Dict[str, Any]],
        ownership: Dict[str, Any],
    ) -> Dict[str, Any]:
        technicals = self._build_technicals(df)
        permanence = classify_move_permanence(technicals)
        flow = classify_who_is_trading(ownership)
        return {
            "symbol": self._label(symbol),
            "as_of": str(df["date"].iloc[-1])[:10],
            "horizon_days": self.horizon_days,
            "horizon_weeks": round(self.horizon_days / 5.0, 1),
            "technicals": technicals,
            "move_analysis": permanence,
            "ownership": ownership,
            "who_is_trading": flow,
            "news": news,
        }

    # ------------------------------------------------------------------
    def _parse_call(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            payload = json.loads(match.group(0) if match else text)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        verdict = str(payload.get("verdict", "")).strip().upper()
        if verdict not in ALLOWED_VERDICTS:
            return None

        try:
            confidence = int(round(float(payload.get("confidence_pct", 50))))
            band_low = float(payload.get("band_low_pct", -self.flat_band_pct))
            band_high = float(payload.get("band_high_pct", self.flat_band_pct))
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(band_low) and math.isfinite(band_high)):
            return None
        if band_low > band_high:
            band_low, band_high = band_high, band_low
        confidence = max(0, min(100, confidence))

        permanence = str(payload.get("move_permanence", "range")).strip().lower()
        if permanence not in ALLOWED_PERMANENCE:
            permanence = "range"

        crash_risk = str(payload.get("crash_risk", "")).strip().lower()
        if crash_risk not in {"low", "medium", "high"}:
            crash_risk = "medium"

        raw_why = payload.get("why", [])
        if isinstance(raw_why, str):
            raw_why = [raw_why]
        why = [str(x).strip()[:280] for x in raw_why if str(x).strip()][:4]

        return {
            "verdict": verdict,
            "confidence_pct": confidence,
            "band_low_pct": round(band_low, 2),
            "band_high_pct": round(band_high, 2),
            "who_is_trading": str(payload.get("who_is_trading", ""))[:200],
            "move_permanence": permanence,
            "crash_risk": crash_risk,
            "crash_reason": str(payload.get("crash_reason", ""))[:200],
            "why": why,
            "reasoning": str(payload.get("reasoning", ""))[:800],
        }

    async def _analyse_symbol(self, symbol: str, run_date: str) -> Optional[Dict[str, Any]]:
        df = await asyncio.to_thread(self._completed_bars, symbol)
        if df.empty or len(df) < 60:
            logger.debug("Investment intel: insufficient bars for %s", symbol)
            return None
        news = await asyncio.to_thread(self._news_provider, symbol)
        raw_ownership = await asyncio.to_thread(self._ownership_provider, symbol)
        ownership = self._sanitize_ownership(raw_ownership)
        evidence = self._build_evidence(symbol, df, news, ownership)
        prompt = (
            SYSTEM_PROMPT.format(
                horizon=self.horizon_days,
                weeks=round(self.horizon_days / 5.0, 1),
                band=self.flat_band_pct,
            )
            + "\n\nEVIDENCE PACK:\n"
            + json.dumps(evidence, default=str)
        )
        result = await asyncio.wait_for(self._model_fn(prompt), timeout=self.timeout_s)
        parsed = self._parse_call(result.get("text", ""))
        if parsed is None:
            self.stats["errors"] += 1
            logger.warning("Investment intel: unparseable call for %s", symbol)
            return None

        prompt_tokens = int(result.get("prompt_tokens", 0) or 0)
        completion_tokens = int(result.get("completion_tokens", 0) or 0)
        from app.system.tokens import token_tracker

        cost = token_tracker.log_usage(
            module="investment_intel",
            model=settings.DEEPSEEK_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            account_id="analysis",
        )

        call_date = evidence["as_of"]
        horizon_date = add_business_days(
            datetime.date.fromisoformat(call_date), self.horizon_days
        ).isoformat()
        return {
            "symbol": symbol.upper(),
            "run_date": run_date,
            "call_date": call_date,
            "horizon_days": self.horizon_days,
            "horizon_date": horizon_date,
            "reference_price": float(evidence["technicals"]["close"]),
            "verdict": parsed["verdict"],
            "confidence_pct": parsed["confidence_pct"],
            "flat_band_pct": self.flat_band_pct,
            "band_low_pct": parsed["band_low_pct"],
            "band_high_pct": parsed["band_high_pct"],
            "who_is_trading": parsed["who_is_trading"],
            "move_permanence": parsed["move_permanence"],
            "crash_risk": parsed["crash_risk"],
            "ownership_summary": json.dumps(evidence["ownership"], default=str)[:400],
            "insider_summary": evidence["who_is_trading"]["summary"][:400],
            "catalysts": (parsed["crash_reason"] or "")[:300],
            "reasoning": parsed["reasoning"],
            "why_bullets": parsed["why"],
            "model": settings.DEEPSEEK_MODEL,
            "tokens_used": prompt_tokens + completion_tokens,
            "cost_inr": cost,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    async def run_cycle(self, now: Optional[datetime.datetime] = None, force: bool = False) -> Dict[str, Any]:
        """Generate and persist one investment call per symbol, at most once per day."""
        current = market_hours.as_ist(now or self._now_provider())
        run_date = current.date().isoformat()
        result: Dict[str, Any] = {"status": "ok", "run_date": run_date, "called": [], "skipped": [], "errors": 0}

        if not force and self._last_cycle_date == run_date:
            result["status"] = "already_ran"
            return result

        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_"):
            result["status"] = "no_api_key"
            return result

        symbols = self.universe[: self.max_symbols]
        max_calls = min(int(settings.INVESTMENT_INTEL_MAX_AI_CALLS), len(symbols))
        for symbol in symbols:
            if result["errors"] >= 5 and len(result["called"]) == 0:
                result["status"] = "aborted"
                break
            if len(result["called"]) >= max_calls:
                break
            try:
                if not force and self.db.has_investment_call_for(symbol, run_date):
                    result["skipped"].append(symbol)
                    continue
                call = await self._analyse_symbol(symbol, run_date)
                if call is None:
                    result["errors"] += 1
                    continue
                self.db.record_investment_call(call)
                self.stats["calls"] += 1
                result["called"].append(self._label(symbol))
            except Exception as exc:
                result["errors"] += 1
                logger.error("Investment intel call failed for %s: %s", symbol, exc)

        self._last_cycle_date = run_date
        self.stats["cycles"] += 1
        self.stats["last_cycle_iso"] = current.isoformat()
        return result

    # ------------------------------------------------------------------
    async def resolve_due_calls(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """Score every open call whose horizon has a completed bar."""
        self._now()
        open_rows = self.db.get_open_investment_calls()
        resolved: List[Dict[str, Any]] = []
        result: Dict[str, Any] = {"status": "ok", "resolved": resolved, "pending": 0, "errors": 0}
        for row in open_rows:
            try:
                outcome = await self._resolve_one(row)
            except Exception as exc:
                result["errors"] += 1
                logger.error("Investment intel resolution failed for %s: %s", row.get("symbol"), exc)
                continue
            if outcome is None:
                result["pending"] += 1
                continue
            self.db.resolve_investment_call(int(row["id"]), **outcome)
            resolved.append({
                "symbol": self._label(str(row["symbol"])),
                "verdict": row["verdict"],
                "correct": bool(outcome["verdict_correct"]),
                "actual_move_pct": outcome["actual_move_pct"],
            })
        return result

    async def _resolve_one(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            horizon_date = datetime.date.fromisoformat(str(row.get("horizon_date"))[:10])
        except (ValueError, TypeError):
            return None
        df = await asyncio.to_thread(self._completed_bars, str(row["symbol"]), "3mo")
        if df.empty:
            return None
        dates = [str(d)[:10] for d in df["date"].tolist()]
        horizon_str = horizon_date.isoformat()
        idx = next((i for i, d in enumerate(dates) if d >= horizon_str), None)
        if idx is None:
            return None  # not enough completed bars yet

        closes = df["close"].astype(float).reset_index(drop=True)
        resolved_price = float(closes.iloc[idx])
        ref = float(row["reference_price"])
        if ref <= 0:
            return None
        call_date_str = str(row.get("call_date"))[:10]
        start_idx = next((i for i, d in enumerate(dates) if d > call_date_str), 0)
        window = closes.iloc[start_idx : idx + 1]
        if len(window) == 0:
            window = closes.iloc[idx : idx + 1]

        actual_move_pct = round((resolved_price / ref - 1.0) * 100.0, 2)
        max_runup_pct = round((float(window.max()) / ref - 1.0) * 100.0, 2)
        max_drawdown_pct = round((float(window.min()) / ref - 1.0) * 100.0, 2)

        flat_band = float(row.get("flat_band_pct", self.flat_band_pct) or self.flat_band_pct)
        actual_direction = classify_direction(actual_move_pct, flat_band)
        verdict = str(row["verdict"]).upper()
        expected_direction = expected_direction_for_verdict(verdict)
        correct = expected_direction == actual_direction

        # Only an ACCUMULATE implies a real delivery position (buy-and-hold).
        realized_net_pct = round(actual_move_pct - self._cost_pct, 2) if verdict == "ACCUMULATE" else 0.0
        return {
            "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "resolved_price": round(resolved_price, 2),
            "actual_move_pct": actual_move_pct,
            "verdict_correct": 1 if correct else 0,
            "max_drawdown_pct": max_drawdown_pct,
            "max_runup_pct": max_runup_pct,
            "realized_net_pct": realized_net_pct,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _decode_bullets(raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError):
            pass
        return [str(raw)]

    @staticmethod
    def _humanize(row: Dict[str, Any]) -> str:
        verdict = str(row.get("verdict", "WATCH")).upper()
        who = row.get("who_is_trading") or "flow unclear"
        permanence = str(row.get("move_permanence") or "range").replace("_", " ")
        move = PERMANENCE_TEXT.get(permanence, "the move has no clear character")
        if verdict == "ACCUMULATE":
            action = "Worth buying and holding over the next few weeks."
        elif verdict == "AVOID":
            action = "Do not own this now - a further fall is likely."
        elif verdict == "HOLD":
            action = "If you already own it, hold; do not add fresh money."
        else:
            action = "Wait and watch - the picture is genuinely two-sided."
        return f"{action} Signal: {who}; {move}."

    def build_report(self, limit: int = 200) -> Dict[str, Any]:
        rows = self.db.get_investment_calls(limit=limit)
        resolved = [r for r in rows if str(r.get("status")) == "resolved"]
        open_rows = [r for r in rows if str(r.get("status")) == "open"]
        n = len(resolved)

        stats: Dict[str, Any] = {
            "closed_calls": n,
            "open_calls": len(open_rows),
            "min_closed_for_verdict": int(settings.INVESTMENT_INTEL_MIN_CLOSED_FOR_VERDICT),
            "is_conclusive": n >= int(settings.INVESTMENT_INTEL_MIN_CLOSED_FOR_VERDICT),
            "horizon_days": self.horizon_days,
            "flat_band_pct": self.flat_band_pct,
            "round_trip_cost_pct": self._cost_pct,
        }

        if n > 0:
            correct = sum(int(r.get("verdict_correct") or 0) for r in resolved)
            moves = [float(r.get("actual_move_pct") or 0.0) for r in resolved]
            nets = [float(r.get("realized_net_pct") or 0.0) for r in resolved]
            brier_sum = 0.0
            for r in resolved:
                p = max(0.0, min(1.0, float(r.get("confidence_pct") or 50) / 100.0))
                outcome = 1.0 if int(r.get("verdict_correct") or 0) else 0.0
                brier_sum += (p - outcome) ** 2
            nets_sum = sum(nets)
            stats.update({
                "verdict_hit_rate_pct": round(correct / n * 100.0, 1),
                "brier_score": round(brier_sum / n, 4),
                "avg_realized_net_pct": round(nets_sum / n, 3),
                "sum_realized_net_pct": round(nets_sum, 2),
                "avg_actual_move_pct": round(sum(moves) / n, 3),
            })

        per_verdict: Dict[str, Dict[str, Any]] = {}
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for r in resolved:
            v = str(r.get("verdict", "WATCH")).upper()
            bucket = per_verdict.setdefault(v, {"calls": 0, "correct": 0, "net_pct_sum": 0.0})
            bucket["calls"] += 1
            bucket["correct"] += int(r.get("verdict_correct") or 0)
            bucket["net_pct_sum"] += float(r.get("realized_net_pct") or 0.0)

            sym = self._label(str(r.get("symbol")))
            sbucket = per_symbol.setdefault(sym, {"calls": 0, "correct": 0, "net_pct_sum": 0.0})
            sbucket["calls"] += 1
            sbucket["correct"] += int(r.get("verdict_correct") or 0)
            sbucket["net_pct_sum"] += float(r.get("realized_net_pct") or 0.0)
        for bucket in list(per_verdict.values()) + list(per_symbol.values()):
            calls = bucket["calls"]
            bucket["hit_rate_pct"] = round(bucket["correct"] / calls * 100.0, 1) if calls else 0.0
            bucket["avg_net_pct"] = round(bucket["net_pct_sum"] / calls, 3) if calls else 0.0

        if n < int(settings.INVESTMENT_INTEL_MIN_CLOSED_FOR_VERDICT):
            verdict = "insufficient_sample"
        elif stats.get("verdict_hit_rate_pct", 0.0) >= 55.0 and stats.get("avg_realized_net_pct", 0.0) > 0:
            verdict = "on_track"
        else:
            verdict = "underperforming"

        return {
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": settings.DEEPSEEK_MODEL,
            "statistics": stats,
            "verdict": verdict,
            "per_verdict": per_verdict,
            "per_symbol": per_symbol,
            "actionable": [self._public_call(r) for r in open_rows if str(r.get("verdict")) in VALUED_VERDICTS],
            "open_calls": [self._public_call(r) for r in open_rows[:60]],
            "recent": [
                {
                    **self._public_call(r),
                    "status": r.get("status"),
                    "actual_move_pct": r.get("actual_move_pct"),
                    "verdict_correct": r.get("verdict_correct"),
                    "realized_net_pct": r.get("realized_net_pct"),
                    "max_drawdown_pct": r.get("max_drawdown_pct"),
                    "max_runup_pct": r.get("max_runup_pct"),
                }
                for r in rows[:60]
            ],
            "ipo_watch": self.ipo_watch(),
            "disclaimer": (
                "This is decision-support research, not financial advice. Every call is scored "
                "against the price that actually printed so you can see whether it adds value. "
                "It never places a trade."
            ),
        }

    def _public_call(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": r.get("id"),
            "symbol": self._label(str(r.get("symbol"))),
            "run_date": r.get("run_date"),
            "call_date": r.get("call_date"),
            "horizon_days": r.get("horizon_days"),
            "horizon_date": r.get("horizon_date"),
            "reference_price": r.get("reference_price"),
            "verdict": r.get("verdict"),
            "confidence_pct": r.get("confidence_pct"),
            "band_low_pct": r.get("band_low_pct"),
            "band_high_pct": r.get("band_high_pct"),
            "who_is_trading": r.get("who_is_trading"),
            "move_permanence": r.get("move_permanence"),
            "crash_risk": r.get("crash_risk"),
            "why": self._decode_bullets(r.get("why_bullets")),
            "reasoning": r.get("reasoning"),
            "plain_english": self._humanize(r),
        }

    def ipo_watch(self) -> List[Dict[str, Any]]:
        if not self.ipo_watch_enabled:
            return []
        try:
            items = self._ipo_provider()
        except Exception as exc:
            logger.debug("IPO watch provider failed: %s", exc)
            return []
        return items[: int(settings.INVESTMENT_INTEL_NEWS_MAX_RESULTS)] if isinstance(items, list) else []

    # ------------------------------------------------------------------
    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._loop())
            logger.info("Started investment intelligence agent (scored long-horizon calls).")

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("Stopped investment intelligence agent.")

    async def _loop(self):
        while self.is_running:
            try:
                now = self._now()
                if market_hours.is_trading_day(now) and now.hour >= int(settings.INVESTMENT_INTEL_RUN_HOUR_IST):
                    await self.resolve_due_calls(now)
                    await self.run_cycle(now)
            except Exception as exc:
                logger.error("Investment intelligence loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self.interval_seconds)


PERMANENCE_TEXT: Dict[str, str] = {
    "structural up": "the up-move looks structural (durable)",
    "structural down": "the down-move looks structural (durable)",
    "temporary up": "the up-move looks temporary (may fade)",
    "temporary down": "the down-move looks temporary (may bounce)",
    "range": "the stock is range-bound",
}


investment_intel = InvestmentIntelAgent()
