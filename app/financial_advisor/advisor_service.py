"""
Financial Advisor Multi-Agent Coordination Service for the Indian Stock Market.
Uses Google Gemini models with live NSE/BSE market intelligence and web search grounding.
"""

import datetime
from typing import Dict, Any, Optional
from google.genai import types

from app.config import settings, create_genai_client
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)

INDIAN_COORDINATOR_SYSTEM_PROMPT = """You are an elite Indian Financial Advisory Coordinator and Quantitative Strategist.
You specialize in the Indian Stock Market (NSE - National Stock Exchange and BSE - Bombay Stock Exchange).
You guide investors and traders by synthesizing real-time data, technicals, execution strategies, and risk profiles in Indian Rupees (INR / ₹).

DISCLAIMER TO KEEP IN MIND:
All outputs are for educational and analytical purposes under SEBI guidelines. Never guarantee returns.

When generating a comprehensive financial advisory report for an Indian security:
1. Ground your analysis strictly on the provided live NSE/BSE quote, 52-week range, Day high/low, and current news.
2. Structure the final advisory plan with clear sections:
   - **Executive Summary & Macro Context** (Nifty 50, Sensex, India VIX impact)
   - **Data Analyst Insights** (Valuation, 52-week position, volume, quarterly earnings context)
   - **Trading Analyst Strategies** (Specific entry zone in ₹, target 1 & target 2 in ₹, stop-loss in ₹, risk-reward ratio)
   - **Execution Analyst Plan** (Order type on NSE/BSE, market hours 09:15 - 15:30 IST, liquidity & slippage considerations)
   - **Risk Analyst Assessment** (Position sizing in ₹ based on portfolio capital, maximum drawdown risk, volatility assessment)
"""

async def run_financial_advisor_pipeline(
    symbol: str,
    risk_attitude: str = "moderate",
    investment_period: str = "medium-term",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute the multi-agent financial advisor pipeline for any Indian stock.
    Gathers live NSE/BSE quote, benchmark indices, and real-time news,
    then prompts Gemini to deliver a comprehensive, structured advisory plan.
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        return {
            "error": "GEMINI_API_KEY is not configured. Please set your API key in .env to use the Financial Advisor.",
            "success": False
        }

    selected_model = model or settings.GEMINI_AGENT_MODEL

    # Step 1: Fetch Live Indian Market Quote
    quote = get_indian_stock_quote(symbol)
    if not quote.get("success"):
        return {
            "error": f"Failed to retrieve Indian market data for '{symbol}': {quote.get('error')}",
            "success": False
        }

    # Step 2: Fetch Live Benchmark Indices
    indices = get_indian_market_indices()

    # Step 3: Fetch Real-Time News via Live Search
    news = search_indian_market_news(f"{symbol} quarterly results stock news NSE", max_results=4)

    # Step 4: Synthesize through Gemini Model
    client = create_genai_client(api_key)

    user_prompt = f"""
Please generate a comprehensive Indian Financial Advisory Report for the following stock:

### LIVE MARKET DATA:
- Symbol: {quote['symbol']} ({quote['ticker']})
- Exchange: {quote['exchange']}
- Current Price: ₹ {quote['current_price']}
- Change: {quote['change_inr']} ({quote['change_percent']}%)
- Day Range: Low ₹ {quote['day_range']['low']} - High ₹ {quote['day_range']['high']}
- 52-Week Range: Low ₹ {quote['year_52w_range']['low']} - High ₹ {quote['year_52w_range']['high']}
- Market Capitalization: {quote['market_cap']}
- Recent 5-Day Trend: {quote['recent_5d_history']}

### BENCHMARK CONTEXT:
- Indian Market Indices: {indices.get('indices')}

### RECENT NEWS & CATALYSTS:
- Search Findings: {news.get('articles')}

### INVESTOR PROFILE:
- Stated Risk Attitude: {risk_attitude}
- Investment Horizon: {investment_period}

Please coordinate the 4 expert sub-agent analyses (Data Analyst, Trading Analyst, Execution Analyst, Risk Analyst) into a cohesive, professional markdown advisory report.
"""

    config = types.GenerateContentConfig(
        system_instruction=INDIAN_COORDINATOR_SYSTEM_PROMPT,
        temperature=0.3,
    )

    try:
        response = await client.aio.models.generate_content(
            model=selected_model,
            contents=user_prompt,
            config=config,
        )

        report_markdown = response.text or "No report content generated."

        return {
            "symbol": quote["symbol"],
            "ticker": quote["ticker"],
            "quote": quote,
            "indices": indices.get("indices"),
            "news": news.get("articles"),
            "risk_attitude": risk_attitude,
            "investment_period": investment_period,
            "report_markdown": report_markdown,
            "model_used": selected_model,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "success": True,
        }
    except Exception as e:
        return {
            "symbol": symbol,
            "error": f"Financial advisor generation failed: {str(e)}",
            "success": False
        }
