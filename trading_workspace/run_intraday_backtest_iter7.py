"""
Intraday Backtesting Runner with DeepSeek Flash MaaS (Iteration 7 - Full Multi-Strategy Suite).
Tests:
- ORB-15m Undefeated Breakout Engine
- True Camarilla H4 Breakout & Pivot Engine (using exact previous day H/L/C)
- 9/20/50 EMA Ribbon Pullback Scalper
- Quadruple-Filtered VWAP Institutional Bounce
- Dynamic SuperTrend Trend Following
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
    print(" INTRADAY STRATEGY BACKTEST: ITERATION 7 (FULL MULTI-STRATEGY SUITE)")
    print("=" * 65)

    test_symbols = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
    print(f"Testing Universe: {test_symbols}")
    print(f"Time Horizon: Past 5 Trading Sessions (5-Minute Candles)")
    print(f"Model: deepseek-ai/deepseek-v4.1-flash-maas")
    print(f"Initial Account Capital: ₹10,000.00")
    print("-" * 65)

    print("\n>>> RUNNING ITERATION 7: Testing Full Multi-Strategy Suite with DeepSeek Validation...")
    res_iter7 = await backtest_engine.run_backtest(
        symbols=test_symbols,
        period="5d",
        use_deepseek=True,
        max_deepseek_calls=15,
        time_filter=True,
    )

    print("\n--- ITERATION 7 RESULTS ---")
    print(f"Total Trades Executed: {res_iter7.get('total_trades')}")
    print(f"Winning Trades:        {res_iter7.get('winning_trades')} / {res_iter7.get('total_trades')} ({res_iter7.get('win_rate_pct')}%)")
    print(f"Net Realized P&L:      ₹{res_iter7.get('net_pnl_inr'):+,.2f} ({res_iter7.get('net_return_pct'):+}%)")
    print(f"Profit Factor:         {res_iter7.get('profit_factor')}")
    print(f"Max Drawdown:          {res_iter7.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        ₹{res_iter7.get('ending_capital_inr'):,.2f}")
    print(f"DeepSeek Queries:      {res_iter7.get('deepseek_queries_count')}")

    print("\nStrategy Breakdown:")
    for strat, data in res_iter7.get("strategy_breakdown", {}).items():
        print(f"  * {strat}: {data.get('trades')} trades, {data.get('wins')} wins ({data.get('win_rate_pct')}%), P&L: ₹{data.get('pnl'):+,.2f}")

    # Save results to JSON
    with open("trading_workspace/iteration7_results.json", "w") as f:
        json.dump(res_iter7, f, indent=2)
    print("\nIteration 7 results saved to trading_workspace/iteration7_results.json")

    # Record learning insight to SQLite
    db.record_learning(
        category="BACKTEST_ITERATION_7",
        insight=(
            f"Iteration 7 (Full Multi-Strategy Suite with Camarilla H4, 9/20/50 EMA Ribbon, refined SuperTrend, "
            f"and Quadruple VWAP) yielded {res_iter7.get('total_trades')} trades, {res_iter7.get('win_rate_pct')}% win rate, "
            f"₹{res_iter7.get('net_pnl_inr'):+,.2f} net P&L, {res_iter7.get('profit_factor')} profit factor, "
            f"and {res_iter7.get('max_drawdown_pct')}% max drawdown."
        ),
        strategy_ref="confluence_quant_engine",
        symbol_ref="MULTI_EQUITY_NSE",
        impact_pnl=res_iter7.get("net_pnl_inr", 0.0),
        account_id="deepseek_flash",
    )
    print("Logged to SQLite learning table.")


if __name__ == "__main__":
    asyncio.run(main())

