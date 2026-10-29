import asyncio
import json
from app.trading.backtester import backtest_engine

async def test_ds_eval():
    setup = {
        "strategy_id": "vwap_institutional_bounce",
        "name": "VWAP Institutional Bounce",
        "signal": "BUY",
        "entry_price": 2495.0,
        "stop_loss": 2483.5,
        "target_1": 2510.0,
        "target_2": 2518.0,
        "risk_reward_ratio": "1:1.30 (Primary Target 1) / 1:1.60 (Momentum Ceiling Target 2)",
        "confidence": 92,
        "rationale": "Strong rejection and bounce off dynamic VWAP (₹2486.00) with green expansion bar reclaiming 20-EMA. Institutional volume surge >= 1.25x SMA20, RSI at 54.5 in bullish expansion zone.",
    }
    indicators = {
        "price": 2495.0,
        "vwap": 2486.0,
        "ema_9": 2494.0,
        "ema_20": 2492.0,
        "supertrend": 2486.0,
        "supertrend_dir": 1,
        "rsi_14": 54.5,
    }
    recent_bars = [
        {"open": 2487.0, "high": 2490.0, "low": 2486.0, "close": 2489.0, "volume": 15000},
        {"open": 2489.0, "high": 2492.0, "low": 2486.5, "close": 2491.0, "volume": 18000},
        {"open": 2491.0, "high": 2496.0, "low": 2490.5, "close": 2495.0, "volume": 25000},
    ]
    res = await backtest_engine._query_deepseek_decision("TCS", setup, indicators, recent_bars)
    print("Test result:", json.dumps(res, indent=2))

if __name__ == "__main__":
    asyncio.run(test_ds_eval())
