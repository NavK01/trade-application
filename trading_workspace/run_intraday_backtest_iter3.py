"""
Intraday Backtesting Runner with DeepSeek Flash MaaS (Iteration 3).
Executes with Strategy Confluence Gate + Undefeated ORB-15m + Natural Technical Stops.
"""

import asyncio
import json
import logging
from app.trading.backtester import backtest_engine
from app.storage.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main():
    print("=" * 65)
    print(" INTRADAY STRATEGY BACKTEST: ITERATION 3 (CONFLUENCE ENGINE)")
    print("=" * 65)

    test_symbols = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
    print(f"Testing Universe: {test_symbols}")
    print(f"Time Horizon: Past 5 Trading Sessions (5-Minute Candles)")
    print(f"Model: deepseek-ai/deepseek-v4.1-flash-maas")
    print(f"Initial Account Capital: ₹10,000.00")
    print(f"Enhancements Applied:")
    print(f"  1. Multi-Strategy Confluence Gate (Requires >= 2 agreeing strategies or ORB)")
    print(f"  2. Trend-Backed 9/20 EMA Pullback (Requires Price >= VWAP)")
    print(f"  3. Natural Technical Stops (Removed premature breakeven choking)")
    print(f"  4. DeepSeek Flash MaaS Quality Filter (Evaluates institutional momentum)")
    print("-" * 65)

    print("\n>>> RUNNING ITERATION 3: Testing Confluence Engine with DeepSeek Validation...")
    res_iter3 = await backtest_engine.run_backtest(
        symbols=test_symbols,
        period="5d",
        use_deepseek=True,
        max_deepseek_calls=12,
        time_filter=True,
    )

    print("\n--- ITERATION 3 RESULTS ---")
    print(f"Total Trades Executed: {res_iter3.get('total_trades')}")
    print(f"Winning Trades:        {res_iter3.get('winning_trades')} / {res_iter3.get('total_trades')} ({res_iter3.get('win_rate_pct')}%)")
    print(f"Net Realized P&L:      ₹{res_iter3.get('net_pnl_inr'):+,.2f} ({res_iter3.get('net_return_pct'):+}%)")
    print(f"Profit Factor:         {res_iter3.get('profit_factor')}")
    print(f"Max Drawdown:          {res_iter3.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        ₹{res_iter3.get('ending_capital_inr'):,.2f}")
    print(f"DeepSeek Queries:      {res_iter3.get('deepseek_queries_count')}")

    print("\nStrategy Breakdown:")
    for strat, data in res_iter3.get("strategy_breakdown", {}).items():
        print(f"  • {strat:35s}: {data['wins']}/{data['trades']} wins ({data['win_rate_pct']}%) | P&L: ₹{data['pnl']:+,.2f}")

    # Record learnings to SQLite
    db.record_learning(
        account_id="deepseek_flash",
        category="Intraday Backtest Iteration 3",
        insight=f"Iteration 3 Confluence Engine produced {res_iter3.get('win_rate_pct')}% win rate with ₹{res_iter3.get('net_pnl_inr'):+,.2f} Net P&L and Profit Factor {res_iter3.get('profit_factor')}.",
        strategy_ref="intraday_confluence_v3",
        impact_pnl=res_iter3.get("net_pnl_inr", 0.0),
    )

    with open("trading_workspace/iteration3_results.json", "w") as f:
        json.dump(res_iter3, f, indent=2)

    return res_iter3


if __name__ == "__main__":
    asyncio.run(main())

