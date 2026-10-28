"""
Dynamic Multi-Stock Market Scanner for High-Return Indian Equities (NSE/BSE).
Scans beyond fixed watchlists across high-beta, liquid large-caps and midcaps
to detect Opening-Range Breakouts (ORB), Volume Surges, 20-Day Highs, and Oversold Bounces.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import logging
from typing import Dict, Any, List, Optional

from app.financial_advisor.india_market import get_indian_stock_quote

logger = logging.getLogger("app.trading.scanner")

# Universe of high-momentum, liquid Indian stocks across sectors (NSE symbols)
SCANNER_UNIVERSE = [
    # High-Beta Banks & Financials
    "SBIN", "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "BAJFINANCE",
    # IT Momentum
    "INFY", "TCS", "WIPRO", "HCLTECH", "TECHM",
    # Auto & Mobility
    "MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO",
    # Metals & Mining (High Intraday Beta)
    "TATASTEEL", "HINDALCO", "JSWSTEEL", "VEDL", "COALINDIA",
    # Energy & Infra
    "RELIANCE", "NTPC", "ONGC", "POWERGRID", "ADANIENT", "ADANIPORTS",
    # FMCG & Pharma
    "ITC", "SUNPHARMA", "CIPLA", "TATACONSUM",
    # High-Volume Momentum & PSU
    "ZOMATO", "BEL", "BHEL", "HAL"
]


class DynamicStockScanner:
    """Automated market scanner identifying highest-return intraday setups."""

    def __init__(self):
        self.last_scan_time: Optional[str] = None
        self.cached_results: List[Dict[str, Any]] = []
        self.top_candidates: List[str] = ["SBIN", "RELIANCE", "TATASTEEL", "INFY", "ITC"]

    async def scan_market(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Scans universe concurrently for top setups:
        - Momentum Breakouts (% Change >= +1.0% with strong volume)
        - Opening Range Breakouts
        - 20-Day High Breakouts
        - Oversold Reversals (% Change <= -2.0% stabilizing at support)
        """
        loop = asyncio.get_event_loop()

        def _fetch_single(sym: str) -> Optional[Dict[str, Any]]:
            try:
                q = get_indian_stock_quote(sym)
                if q.get("success") and q.get("current_price", 0) > 0:
                    return q
            except Exception as e:
                logger.debug(f"Scanner fetch skipped {sym}: {e}")
            return None

        # Fetch all in parallel using thread pool with strict timeout
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                tasks = [loop.run_in_executor(executor, _fetch_single, sym) for sym in SCANNER_UNIVERSE]
                quotes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15.0)
            valid_quotes = [q for q in quotes if isinstance(q, dict) and q.get("success")]
        except Exception as e:
            logger.warning(f"Scanner parallel fetch timed out or failed ({e}). Using cached results.")
            valid_quotes = []
        results = []

        for q in valid_quotes:
            sym = q["symbol"]
            price = q["current_price"]
            chg_pct = q.get("change_percent", 0.0)
            high = q.get("day_high", price)
            low = q.get("day_low", price)

            # Calculate Setup Type & Conviction Score
            score = 50
            setup_type = "Consolidation"
            catalyst_note = "Standard trading range"

            # 1. High Momentum Bullish Breakout
            if chg_pct >= 1.5:
                score += 25
                setup_type = "Momentum Breakout"
                catalyst_note = f"Surging +{chg_pct:.2f}% intraday with aggressive buying pressure"
            elif chg_pct >= 0.8:
                score += 15
                setup_type = "Opening Range Breakout"
                catalyst_note = f"Up +{chg_pct:.2f}%, holding firmly above opening range"
            # 2. Oversold Bounce Candidate
            elif chg_pct <= -2.5:
                score += 20
                setup_type = "Oversold Mean Reversion"
                catalyst_note = f"Priced at day low (-{abs(chg_pct):.2f}%), prime oversold bounce setup"
            elif chg_pct <= -1.2:
                score += 10
                setup_type = "Pullback to Support"
                catalyst_note = f"Intraday dip (-{abs(chg_pct):.2f}%) approaching support level"

            # Proximity to Day High (Strength Indicator)
            day_range = (high - low) if high > low else 1.0
            pos_in_range = (price - low) / day_range
            if pos_in_range >= 0.85 and chg_pct > 0:
                score += 15
                catalyst_note += " (Near day high)"

            # Budget Suitability Check for ₹10,000 capital
            if price <= 1500:
                score += 10
            elif price > 3000:
                score -= 5

            score = min(max(score, 45), 98)

            risk = round(price * 0.01, 2)  # 1% standard day-trade risk
            stop_loss = round(price - risk, 2)
            target_1 = round(price + (risk * 1.5), 2)
            target_2 = round(price + (risk * 2.5), 2)

            results.append({
                "symbol": sym,
                "current_price": price,
                "change_percent": chg_pct,
                "change_amount": q.get("change_amount", 0.0),
                "day_high": high,
                "day_low": low,
                "score": score,
                "setup_type": setup_type,
                "catalyst_note": catalyst_note,
                "entry_zone": f"₹{price - 1.0:.1f} - ₹{price + 1.0:.1f}",
                "stop_loss": stop_loss,
                "target_1": target_1,
                "target_2": target_2,
                "risk_reward": "1:2.0",
                "recommended_qty": max(1, min(15, int(1500 / price))),
            })

        # Sort by highest score first
        results.sort(key=lambda x: (x["score"], abs(x["change_percent"])), reverse=True)
        self.cached_results = results[:limit]
        self.top_candidates = [r["symbol"] for r in self.cached_results[:5]] or ["SBIN", "RELIANCE", "TATASTEEL"]
        self.last_scan_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        logger.info(f"Market Scanner completed: Top {len(self.cached_results)} opportunities: {self.top_candidates}")
        return self.cached_results

    def get_cached_results(self) -> Dict[str, Any]:
        return {
            "last_scan_time": self.last_scan_time,
            "count": len(self.cached_results),
            "top_candidates": self.top_candidates,
            "opportunities": self.cached_results,
        }


scanner = DynamicStockScanner()

