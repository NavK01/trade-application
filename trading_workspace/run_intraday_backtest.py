"""
Intraday Backtesting Runner with DeepSeek Flash MaaS (Iteration 2).
Executes backtest with enhanced strategy filters, time-of-day regime gates,
and breakeven trailing stop protection.
"""

import asyncio
import json
import logging
from app.trading.backtester import backtest_engine
from app.storage.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main():
    print("=" * 65)
    print(" INTRADAY STRATEGY BACKTEST: ITERATION 2 (OPTIMIZED)")
    print("=" * 65)

    test_symbols = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
    print(f"Testing Universe: {test_symbols}")
    print(f"Time Horizon: Past 5 Trading Sessions (5-Minute Candles)")
    print(f"Model: deepseek-ai/deepseek-v4.1-flash-maas")
    print(f"Initial Account Capital: ₹10,000.00")
    print(f"Enhancements Applied:")
    print(f"  1. Trend-Aligned RSI (Oversold bounce blocked below 50-EMA / VWAP)")
    print(f"  2. Time-of-Day Regime Gate (No new breakouts 11:30 - 13:15 IST)")
    print(f"  3. Break-Even Trailing Stop (+0.8R trigger locks in entry price)")
    print(f"  4. Volume Expansion Threshold (>= 1.30x SMA on breakouts)")
    print("-" * 65)

    print("\n>>> RUNNING ITERATION 2: Testing Enhanced Rules with DeepSeek Validation...")
    res_iter2 = await backtest_engine.run_backtest(
        symbols=test_symbols,
        period="5d",
        use_deepseek=True,
        max_deepseek_calls=12,
        time_filter=True,
    )

    print("\n--- ITERATION 2 RESULTS ---")
    print(f"Total Trades Executed: {res_iter2.get('total_trades')}")
    print(f"Winning Trades:        {res_iter2.get('winning_trades')} / {res_iter2.get('total_trades')} ({res_iter2.get('win_rate_pct')}%)")
    print(f"Net Realized P&L:      ₹{res_iter2.get('net_pnl_inr'):+,.2f} ({res_iter2.get('net_return_pct'):+}%)")
    print(f"Profit Factor:         {res_iter2.get('profit_factor')}")
    print(f"Max Drawdown:          {res_iter2.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        ₹{res_iter2.get('ending_capital_inr'):,.2f}")
    print(f"DeepSeek Queries:      {res_iter2.get('deepseek_queries_count')}")

    print("\nStrategy Breakdown:")
    for strat, data in res_iter2.get("strategy_breakdown", {}).items():
        print(f"  • {strat:35s}: {data['wins']}/{data['trades']} wins ({data['win_rate_pct']}%) | P&L: ₹{data['pnl']:+,.2f}")

    # Record learnings to SQLite
    db.record_learning(
        account_id="deepseek_flash",
        category="Intraday Backtest Iteration 2",
        insight=f"Iteration 2 achieved {res_iter2.get('win_rate_pct')}% win rate (+{(res_iter2.get('win_rate_pct', 0) - 54.2):.1f}% improvement) with ₹{res_iter2.get('net_pnl_inr'):+,.2f} Net P&L and Profit Factor {res_iter2.get('profit_factor')}.",
        strategy_ref="intraday_multi_strategy_v2",
        impact_pnl=res_iter2.get("net_pnl_inr", 0.0),
    )

    with open("trading_workspace/iteration2_results.json", "w") as f:
        json.dump(res_iter2, f, indent=2)

    return res_iter2


if __name__ == "__main__":
    asyncio.run(main())
