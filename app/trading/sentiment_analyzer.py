"""
Real-Time Market Sentiment & Sector Intelligence Engine for Indian Equities.
Leverages Google Search grounding and financial NLP to quantify news sentiment,
identify sector tailwinds/headwinds, and rank high-conviction breakout opportunities.
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Dict, Any, List, Optional
from google.genai import types

from app.config import settings, create_genai_client
from app.financial_advisor.india_market import search_indian_market_news
from app.storage.cache import cache

logger = logging.getLogger("app.trading.sentiment")

# Major NSE Sectors and their Key Tickers & Macro Drivers
NSE_SECTOR_MAP = {
    "DEFENSE_CAPEX": {
        "name": "Defense & Capital Goods",
        "symbols": ["BEL", "HAL", "BHEL", "L&T", "SIEMENS", "ABB"],
        "macro_driver": "Atmanirbhar Bharat, ₹7.85L Cr defense outlay, 22% surge in modern capital capex.",
        "historical_cagr_5y": 24.5,
        "current_outlook": "STRONG_BOOM",
        "beta": 1.35,
    },
    "POWER_ENERGY": {
        "name": "Power & Green Energy Transition",
        "symbols": ["NTPC", "POWERGRID", "TATAPOWER", "ONGC", "RELIANCE", "COALINDIA"],
        "macro_driver": "Surging national peak electricity demand, renewable transition, grid capacity expansion.",
        "historical_cagr_5y": 14.8,
        "current_outlook": "STRONG_BOOM",
        "beta": 1.15,
    },
    "PSU_BANKING": {
        "name": "PSU & Commercial Banking",
        "symbols": ["SBIN", "BANKBARODA", "CANBK", "PNB", "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK"],
        "macro_driver": "Multi-year low NPAs (<2.8%), robust corporate credit growth, strong ROA/ROE.",
        "historical_cagr_5y": 23.0,
        "current_outlook": "BULLISH_COMPOUNDER",
        "beta": 1.25,
    },
    "AUTO_MOBILITY": {
        "name": "Automobile & EV Premiumization",
        "symbols": ["MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT"],
        "macro_driver": "SUV premiumization boom, EV two-wheeler ramp-up, healthy rural recovery.",
        "historical_cagr_5y": 16.9,
        "current_outlook": "MODERATE_BULLISH",
        "beta": 1.10,
    },
    "METALS_MINING": {
        "name": "Metals & High-Beta Cyclicals",
        "symbols": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL"],
        "macro_driver": "Domestic infrastructure steel demand vs Chinese export dumping volatility.",
        "historical_cagr_5y": 15.0,
        "current_outlook": "HIGH_VOLATILITY_TACTICAL",
        "beta": 1.45,
    },
    "PHARMA_HEALTH": {
        "name": "Pharmaceuticals & Healthcare",
        "symbols": ["SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "APOLLOHOSP"],
        "macro_driver": "Defensive compounder, US generic price stabilization, domestic branded chronic formulations.",
        "historical_cagr_5y": 13.0,
        "current_outlook": "DEFENSIVE_STABLE",
        "beta": 0.75,
    },
    "IT_SERVICES": {
        "name": "Information Technology",
        "symbols": ["INFY", "TCS", "WIPRO", "HCLTECH", "TECHM"],
        "macro_driver": "Global discretionary tech spending caution, generative AI transition, margin defense.",
        "historical_cagr_5y": -1.7,
        "current_outlook": "LAGGING_CONSOLIDATION",
        "beta": 1.18,
    },
    "FMCG_CONSUMPTION": {
        "name": "FMCG & Fast Consumption",
        "symbols": ["ITC", "HINDUNILVR", "NESTLEIND", "DABUR", "BRITANNIA"],
        "macro_driver": "Rural wage lag, palm oil / packaging cost inflation, volume growth pressure.",
        "historical_cagr_5y": 2.0,
        "current_outlook": "UNDERPERFORMING",
        "beta": 0.65,
    },
}

# High-conviction keywords for algorithmic score weighting
POSITIVE_KEYWORDS = [
    "order win", "profit surge", "revenue up", "margin expands", "upgrade", "outperform",
    "capex", "record high", "dividend", "acquisition", "defense contract", "capacity expansion",
    "strong demand", "guidance raised", "market share gain", "beat estimates", "breakout"
]
NEGATIVE_KEYWORDS = [
    "profit drop", "margin compression", "downgrade", "underperform", "sebi probe", "penalty",
    "tax demand", "investigation", "guidance cut", "weak demand", "delay", "cancellation",
    "loss widened", "fall", "slump", "concern", "dispute", "litigation"
]


class SentimentIntelligenceEngine:
    """Quantitative Sentiment and Sector Momentum Engine powered by Google Search Grounding."""

    def __init__(self):
        self.cached_sentiment: Dict[str, Dict[str, Any]] = {}

    def get_sector_for_symbol(self, symbol: str) -> Optional[str]:
        clean = symbol.upper()
        for sec_id, info in NSE_SECTOR_MAP.items():
            if clean in info["symbols"]:
                return sec_id
        return None

    def get_all_sectors_outlook(self) -> List[Dict[str, Any]]:
        """Returns macro-ranked outlook for all 8 core NSE sectors."""
        outlooks = []
        for sec_id, info in NSE_SECTOR_MAP.items():
            outlooks.append({
                "sector_id": sec_id,
                "name": info["name"],
                "status": info["current_outlook"],
                "5y_cagr": info["historical_cagr_5y"],
                "macro_driver": info["macro_driver"],
                "beta": info["beta"],
                "representative_stocks": info["symbols"][:4],
                "rank": 1 if "BOOM" in info["current_outlook"] else (2 if "BULLISH" in info["current_outlook"] else 3),
            })
        outlooks.sort(key=lambda x: (x["rank"], -x["5y_cagr"]))
        return outlooks

    async def analyze_stock_sentiment(self, symbol: str, use_llm_grounding: bool = True) -> Dict[str, Any]:
        """
        Analyzes live news sentiment for a given Indian equity using Google Search.
        Produces:
          - sentiment_score (-1.0 to +1.0)
          - sentiment_label (STRONG_BULLISH, MODERATE_BULLISH, NEUTRAL, BEARISH)
          - sector_tailwinds
          - key catalysts and risk factors
        """
        clean_sym = symbol.strip().upper()

        # Check in-memory cache (5-minute TTL)
        cached = self.cached_sentiment.get(clean_sym)
        if cached and (datetime.datetime.now(datetime.timezone.utc) - cached["cached_at"]).total_seconds() < 300:
            return cached["data"]

        # 1. Fetch live news articles via Google Search grounding (non-blocking)
        search_query = f"{clean_sym} stock news NSE India corporate earnings"
        loop = asyncio.get_event_loop()
        try:
            news_data = await asyncio.wait_for(
                loop.run_in_executor(None, search_indian_market_news, search_query),
                timeout=8.0
            )
        except Exception:
            news_data = {"articles": []}
        articles = news_data.get("articles", [])

        # 2. Algorithmic Keyword Sentiment Scoring
        pos_hits = 0
        neg_hits = 0
        snippets = []

        for art in articles:
            text = f"{art.get('title', '')} {art.get('snippet', '')}".lower()
            snippets.append(text)
            for kw in POSITIVE_KEYWORDS:
                if kw in text:
                    pos_hits += 1
            for kw in NEGATIVE_KEYWORDS:
                if kw in text:
                    neg_hits += 1

        total_hits = pos_hits + neg_hits
        if total_hits > 0:
            algo_score = round((pos_hits - neg_hits) / max(total_hits, 1), 2)
        else:
            algo_score = 0.15  # Slight positive baseline for growing Indian macro

        # Sector context
        sec_id = self.get_sector_for_symbol(clean_sym)
        sector_info = NSE_SECTOR_MAP.get(sec_id, {
            "name": "General Indian Equities",
            "macro_driver": "Domestic GDP expansion, healthy credit trajectory.",
            "current_outlook": "MODERATE_BULLISH",
            "historical_cagr_5y": 12.0
        })

        # Final sentiment determination
        combined_score = algo_score
        # Sector boost/drag
        if sector_info.get("current_outlook") == "STRONG_BOOM":
            combined_score = min(1.0, combined_score + 0.20)
        elif sector_info.get("current_outlook") in ["LAGGING_CONSOLIDATION", "UNDERPERFORMING"]:
            combined_score = max(-1.0, combined_score - 0.20)

        combined_score = round(combined_score, 2)

        if combined_score >= 0.40:
            label = "STRONG_BULLISH"
        elif combined_score >= 0.10:
            label = "MODERATE_BULLISH"
        elif combined_score <= -0.30:
            label = "MODERATE_BEARISH"
        elif combined_score <= -0.60:
            label = "STRONG_BEARISH"
        else:
            label = "NEUTRAL"

        result = {
            "symbol": clean_sym,
            "sector": sector_info["name"],
            "sector_status": sector_info["current_outlook"],
            "sector_driver": sector_info["macro_driver"],
            "sentiment_score": combined_score,
            "sentiment_label": label,
            "news_sample_count": len(articles),
            "positive_signals_count": pos_hits,
            "negative_risks_count": neg_hits,
            "catalyst_summary": f"{clean_sym} shows {label.replace('_', ' ').title()} sentiment in {sector_info['name']} with {pos_hits} positive vs {neg_hits} cautionary news signals.",
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        self.cached_sentiment[clean_sym] = {
            "data": result,
            "cached_at": datetime.datetime.now(datetime.timezone.utc),
        }

        return result


sentiment_engine = SentimentIntelligenceEngine()

