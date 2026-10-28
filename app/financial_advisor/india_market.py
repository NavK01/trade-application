"""
Indian Stock Market Live Data and Intelligence Module for Gemini Financial Advisor.
Supports National Stock Exchange (NSE) and Bombay Stock Exchange (BSE).
Fetches live quotes in INR, 52-week metrics, market breadth, and real-time news via Google/DuckDuckGo search.
"""

import datetime
import warnings
from typing import Dict, Any, List, Optional
import yfinance as yf
from ddgs import DDGS

# Suppress verbose yfinance / pandas deprecation warnings from cluttering system logs
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*inplace.*")

# Standard Indian index and ticker mappings
INDIAN_INDICES = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "BANK NIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "BSE SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
    "INDIA VIX": "^INDIAVIX",
    # 2025/2026 Corporate Restructuring Mappings
    "TATAMOTORS": "TMCV.NS",
    "TATA MOTORS": "TMCV.NS",
    "ZOMATO": "ETERNAL.NS",
}

def normalize_indian_symbol(symbol: str) -> str:
    """Normalize input ticker into standard Yahoo Finance NSE/BSE symbol."""
    sym = symbol.strip().upper()
    if sym in INDIAN_INDICES:
        return INDIAN_INDICES[sym]
    if sym.endswith(".NS") or sym.endswith(".BO") or sym.startswith("^"):
        return sym
    # Default to NSE (.NS) which has highest liquidity in India
    return f"{sym}.NS"

def get_indian_stock_quote(symbol: str) -> Dict[str, Any]:
    """
    Fetch real-time / intraday price information and key fundamental metrics
    for any Indian stock traded on NSE or BSE.
    
    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS, HDFCBANK, INFY, TATAMOTORS, NIFTY50)
    """
    ticker_sym = normalize_indian_symbol(symbol)
    try:
        ticker = yf.Ticker(ticker_sym)
        fast_info = ticker.fast_info
        
        last_price = getattr(fast_info, "last_price", None)
        prev_close = getattr(fast_info, "previous_close", None)

        # If primary ticker lookup fails or returns None, try BSE (.BO) fallback
        if (last_price is None or prev_close is None) and ticker_sym.endswith(".NS"):
            bse_sym = ticker_sym.replace(".NS", ".BO")
            try:
                bse_ticker = yf.Ticker(bse_sym)
                bse_fast = bse_ticker.fast_info
                b_last = getattr(bse_fast, "last_price", None)
                b_prev = getattr(bse_fast, "previous_close", None)
                if b_last is not None and b_last > 0:
                    ticker = bse_ticker
                    fast_info = bse_fast
                    ticker_sym = bse_sym
                    last_price = b_last
                    prev_close = b_prev or b_last
            except Exception:
                pass

        if last_price is None or prev_close is None:
            # Fallback to history
            hist = ticker.history(period="5d")
            if hist.empty:
                return {
                    "symbol": symbol,
                    "ticker": ticker_sym,
                    "error": f"Could not retrieve real-time market data for symbol '{symbol}'. Ensure it is a valid NSE/BSE ticker.",
                    "success": False
                }
            last_price = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_price

        change = round(last_price - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

        # Market Cap in INR Crores (1 Crore = 10 Million INR)
        mcap = getattr(fast_info, "market_cap", None)
        mcap_cr = f"₹ {round(mcap / 1e7, 2):,} Cr" if mcap else "N/A"

        # 52-week High/Low
        year_high = getattr(fast_info, "year_high", None)
        year_low = getattr(fast_info, "year_low", None)
        day_high = getattr(fast_info, "day_high", None)
        day_low = getattr(fast_info, "day_low", None)

        # Recent 5-day trend summary
        hist_5d = ticker.history(period="5d")
        recent_prices = []
        if not hist_5d.empty:
            for idx, row in hist_5d.iterrows():
                d_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                recent_prices.append({
                    "date": d_str,
                    "close_inr": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"])
                })

        return {
            "symbol": symbol.upper(),
            "ticker": ticker_sym,
            "exchange": "NSE" if ticker_sym.endswith(".NS") else ("BSE" if ticker_sym.endswith(".BO") else "Index"),
            "currency": "INR (₹)",
            "current_price": round(last_price, 2),
            "previous_close": round(prev_close, 2),
            "change_inr": change,
            "change_percent": change_pct,
            "day_range": {
                "high": round(day_high, 2) if day_high else None,
                "low": round(day_low, 2) if day_low else None
            },
            "year_52w_range": {
                "high": round(year_high, 2) if year_high else None,
                "low": round(year_low, 2) if year_low else None
            },
            "market_cap": mcap_cr,
            "recent_5d_history": recent_prices,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "success": True
        }
    except Exception as e:
        return {
            "symbol": symbol,
            "ticker": ticker_sym,
            "error": str(e),
            "success": False
        }

def get_indian_market_indices() -> Dict[str, Any]:
    """Fetch live benchmark levels for Nifty 50, Sensex, Bank Nifty, and India VIX."""
    results = {}
    indices = {
        "NIFTY_50": "^NSEI",
        "SENSEX": "^BSESN",
        "BANK_NIFTY": "^NSEBANK",
        "INDIA_VIX": "^INDIAVIX"
    }
    for name, sym in indices.items():
        try:
            t = yf.Ticker(sym)
            lp = t.fast_info.last_price
            pc = t.fast_info.previous_close
            if lp is not None and pc is not None:
                chg = round(lp - pc, 2)
                chg_p = round((chg / pc) * 100, 2)
                results[name] = {
                    "price": round(lp, 2),
                    "change": chg,
                    "change_pct": chg_p
                }
        except Exception:
            pass
    return {"indices": results, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}

def search_indian_market_news(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    Perform live web search for Indian market news, corporate announcements,
    quarterly earnings, and SEBI regulatory updates.
    
    Args:
        query: Search term (e.g. 'Reliance Industries Q3 results NSE', 'Nifty 50 outlook today')
        max_results: Number of search results to return (default 5).
    """
    clean_query = query.strip()
    if "india" not in clean_query.lower() and "nse" not in clean_query.lower() and "bse" not in clean_query.lower():
        clean_query += " India stock market NSE"
        
    articles = []
    try:
        ddgs = DDGS(timeout=7)
        for r in ddgs.text(clean_query, max_results=max_results):
            articles.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })
        return {
            "query": query,
            "results_count": len(articles),
            "articles": articles,
            "success": True
        }
    except Exception as e:
        return {
            "query": query,
            "error": str(e),
            "articles": [],
            "success": False
        }
