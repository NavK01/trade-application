"""
Intraday Backtesting Runner with DeepSeek Flash MaaS (Iteration 5 - Production Quant).
Tests Triple-Aligned Institutional Stack:
- Undefeated ORB-15m
- SuperTrend + VWAP + EMA Triple-Aligned Bounces
- Inverted Bollinger Lower Band Mean Reversion
- DeepSeek Flash MaaS AI Decision Gate
"""

import asyncio
import json
import logging
from app.trading.backtester import backtest_engine
from app.storage.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main():
    print("=" * 65)
    print(" INTRADAY STRATEGY BACKTEST: ITERATION 5 (PRODUCTION QUANT)")
    print("=" * 65)

    test_symbols = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
    print(f"Testing Universe: {test_symbols}")
    print(f"Time Horizon: Past 5 Trading Sessions (5-Minute Candles)")
    print(f"Model: deepseek-ai/deepseek-v4.1-flash-maas")
    print(f"Initial Account Capital: ₹10,000.00")
    print(f"Enhancements Applied:")
    print(f"  1. High-Priority ORB-15m Breakout Engine (100% win rate foundation)")
    print(f"  2. Triple-Aligned VWAP Institutional Bounce (SuperTrend + 9/20 EMA + Volume)")
    print(f"  3. Inverted Bollinger Lower Band Mean Reversion")
    print(f"  4. DeepSeek Flash MaaS Quality Gate")
    print("-" * 65)

    print("\n>>> RUNNING ITERATION 5: Testing Production Quant Stack with DeepSeek Validation...")
    res_iter5 = await backtest_engine.run_backtest(
        symbols=test_symbols,
        period="5d",
        use_deepseek=True,
        max_deepseek_calls=12,
        time_filter=True,
    )

    print("\n--- ITERATION 5 RESULTS ---")
    print(f"Total Trades Executed: {res_iter5.get('total_trades')}")
    print(f"Winning Trades:        {res_iter5.get('winning_trades')} / {res_iter5.get('total_trades')} ({res_iter5.get('win_rate_pct')}%)")
    print(f"Net Realized P&L:      ₹{res_iter5.get('net_pnl_inr'):+,.2f} ({res_iter5.get('net_return_pct'):+}%)")
    print(f"Profit Factor:         {res_iter5.get('profit_factor')}")
    print(f"Max Drawdown:          {res_iter5.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        ₹{res_iter5.get('ending_capital_inr'):,.2f}")
    print(f"DeepSeek Queries:      {res_iter5.get('deepseek_queries_count')}")

    print("\nStrategy Breakdown:")
    for strat, data in res_iter5.get("strategy_breakdown", {}).items():
        print(f"  • {strat:35s}: {data['wins']}/{data['trades']} wins ({data['win_rate_pct']}%) | P&L: ₹{data['pnl']:+,.2f}")

    # Record learnings to SQLite
    db.record_learning(
        account_id="deepseek_flash",
        category="Intraday Backtest Iteration 5",
        insight=f"Iteration 5 Production Quant achieved {res_iter5.get('win_rate_pct')}% win rate with ₹{res_iter5.get('net_pnl_inr'):+,.2f} Net P&L and Profit Factor {res_iter5.get('profit_factor')}.",
        strategy_ref="intraday_production_quant_v5",
        impact_pnl=res_iter5.get("net_pnl_inr", 0.0),
    )

    with open("trading_workspace/iteration5_results.json", "w") as f:
        json.dump(res_iter5, f, indent=2)

    return res_iter5


if __name__ == "__main__":
    asyncio.run(main())

