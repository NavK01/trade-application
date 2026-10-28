"""
Real-Time Market Data Streamer for Indian Equities and Indices.
Streams 100% verified live prices, percentage changes, and tick updates to WebSockets.
Never generates synthetic or simulated ticks.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import logging
from typing import Dict, Any, List, Optional

from app.financial_advisor.india_market import get_indian_stock_quote, get_indian_market_indices
from app.trading.execution_engine import execution_engine
from app.storage.cache import cache

logger = logging.getLogger("app.trading.market_feed")

CORE_WATCHLIST_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "SBIN", "INFY", "ICICIBANK", "ITC", "TATAMOTORS"
]


class MarketDataFeed:
    """
    Broadcasts strictly verified real-time market data directly from Indian exchanges (NSE/BSE).
    Dynamically tracks the core watchlist, open agent positions, and scanned breakout stocks.
    """

    def __init__(self):
        self.cached_quotes: Dict[str, Dict[str, Any]] = {}
        self.subscribers: List[asyncio.Queue] = []
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="live_feed")

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

    def start_feed(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._feed_loop())
            logger.info("Real-time live market feed started.")

    def stop_feed(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Real-time live market feed stopped.")

    def get_tracked_symbols(self) -> List[str]:
        """Aggregate core watchlist, active portfolio holdings, and scanned candidates."""
        symbols = set(CORE_WATCHLIST_SYMBOLS)

        # Include open positions for both trading agents
        try:
            for acc_id in ["gemini_flash", "deepseek_flash"]:
                summary = execution_engine.get_portfolio_summary(acc_id)
                for pos in summary.get("positions", []):
                    sym = pos.get("symbol")
                    if sym:
                        symbols.add(sym.upper())
        except Exception:
            pass

        # Include scanner candidates
        try:
            from app.trading.scanner import scanner
            if scanner.top_candidates:
                symbols.update(scanner.top_candidates[:5])
        except Exception:
            pass

        return list(symbols)

    async def _fetch_all_quotes(self):
        """Polls verified real-time quotes from NSE/BSE in parallel."""
        loop = asyncio.get_event_loop()
        symbols = self.get_tracked_symbols()

        def _fetch_single(sym: str) -> Optional[Dict[str, Any]]:
            try:
                q = get_indian_stock_quote(sym)
                if q.get("success") and q.get("current_price", 0) > 0:
                    return q
            except Exception as e:
                logger.debug(f"Feed quote lookup failed for {sym}: {e}")
            return None

        tasks = [loop.run_in_executor(self._executor, _fetch_single, sym) for sym in symbols]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
        except Exception as e:
            logger.debug(f"Feed parallel fetch timed out: {e}")
            results = []

        for res in results:
            if isinstance(res, dict) and res.get("success"):
                sym = res["symbol"].upper()
                price = res["current_price"]
                self.cached_quotes[sym] = res
                cache.set_price(sym, price)

    async def _feed_loop(self):
        # Initial snapshot
        await self._fetch_all_quotes()

        while self.is_running:
            try:
                # 1. Fetch genuine live quotes from exchange
                await self._fetch_all_quotes()

                # 2. Update execution engine positions with verified live prices
                price_map = {sym: q["current_price"] for sym, q in self.cached_quotes.items() if q.get("current_price", 0) > 0}
                if price_map:
                    execution_engine.update_live_prices(price_map)

                # 3. Fetch indices
                indices = get_indian_market_indices()
                from app.trading.strategy_manager import strategy_manager

                # 4. Broadcast live market tick
                self.broadcast({
                    "type": "market_tick",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "watchlist": list(self.cached_quotes.values()),
                    "indices": indices.get("indices", {}),
                    "portfolio": execution_engine.get_portfolio_summary(),
                    "market_status": strategy_manager.get_market_status(),
                })

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in live market feed loop: {e}")

            # Polling cadence: 8 seconds for fresh real market ticks
            await asyncio.sleep(8.0)


market_feed = MarketDataFeed()
