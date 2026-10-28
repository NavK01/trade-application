"""
Intraday Backtesting Runner with DeepSeek Flash MaaS (Iteration 4).
Tests the High-Alpha Refined Confluence Engine:
- Undefeated ORB-15m
- Trend-Aligned 9/20 EMA Pullback (Price >= VWAP)
- Inverted Bollinger Band (Mean Reversion off Lower Band)
- DeepSeek Flash MaaS Institutional Quality Validation
"""

import asyncio
import json
import logging
from app.trading.backtester import backtest_engine
from app.storage.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main():
    print("=" * 65)
    print(" INTRADAY STRATEGY BACKTEST: ITERATION 4 (HIGH-ALPHA OPTIMIZED)")
    print("=" * 65)

    test_symbols = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
    print(f"Testing Universe: {test_symbols}")
    print(f"Time Horizon: Past 5 Trading Sessions (5-Minute Candles)")
    print(f"Model: deepseek-ai/deepseek-v4.1-flash-maas")
    print(f"Initial Account Capital: ₹10,000.00")
    print(f"Enhancements Applied:")
    print(f"  1. High-Priority ORB-15m Breakout Engine")
    print(f"  2. Trend-Backed 9/20 EMA Pullback (Price >= VWAP)")
    print(f"  3. Inverted Bollinger Lower Band Mean Reversion")
    print(f"  4. DeepSeek Flash MaaS Quality Gate")
    print("-" * 65)

    print("\n>>> RUNNING ITERATION 4: Testing High-Alpha Engine with DeepSeek Validation...")
    res_iter4 = await backtest_engine.run_backtest(
        symbols=test_symbols,
        period="5d",
        use_deepseek=True,
        max_deepseek_calls=12,
        time_filter=True,
    )

    print("\n--- ITERATION 4 RESULTS ---")
    print(f"Total Trades Executed: {res_iter4.get('total_trades')}")
    print(f"Winning Trades:        {res_iter4.get('winning_trades')} / {res_iter4.get('total_trades')} ({res_iter4.get('win_rate_pct')}%)")
    print(f"Net Realized P&L:      ₹{res_iter4.get('net_pnl_inr'):+,.2f} ({res_iter4.get('net_return_pct'):+}%)")
    print(f"Profit Factor:         {res_iter4.get('profit_factor')}")
    print(f"Max Drawdown:          {res_iter4.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        ₹{res_iter4.get('ending_capital_inr'):,.2f}")
    print(f"DeepSeek Queries:      {res_iter4.get('deepseek_queries_count')}")

    print("\nStrategy Breakdown:")
    for strat, data in res_iter4.get("strategy_breakdown", {}).items():
        print(f"  • {strat:35s}: {data['wins']}/{data['trades']} wins ({data['win_rate_pct']}%) | P&L: ₹{data['pnl']:+,.2f}")

    # Record learnings to SQLite
    db.record_learning(
        account_id="deepseek_flash",
        category="Intraday Backtest Iteration 4",
        insight=f"Iteration 4 High-Alpha achieved {res_iter4.get('win_rate_pct')}% win rate with ₹{res_iter4.get('net_pnl_inr'):+,.2f} Net P&L and Profit Factor {res_iter4.get('profit_factor')}.",
        strategy_ref="intraday_high_alpha_v4",
        impact_pnl=res_iter4.get("net_pnl_inr", 0.0),
    )

    with open("trading_workspace/iteration4_results.json", "w") as f:
        json.dump(res_iter4, f, indent=2)

    return res_iter4


if __name__ == "__main__":
    asyncio.run(main())

