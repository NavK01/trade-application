"""
1-Year Hourly Backtest Runner (Iteration 18 - Long-Horizon Validation).

Purpose: validate the primary momentum-continuation edge over a full year of
hourly candles on a restricted, liquid Indian universe:
  - Sensex (^BSESN), Nifty (^NSEI), Bank Nifty (^NSEBANK)
  - Gold ETF (GOLDBEES.NS)  [Indian crude oil is not available on Yahoo]
  - SBIN.NS, TATASTEEL.NS, TATAPOWER.NS (TATA group), RELIANCE.NS

Yahoo caps 5-minute intraday data at 60 days, so a true 1-year 5m backtest is
impossible. Hourly bars support a full year and are the closest honest proxy.
"""

import asyncio
import json
import logging
from app.trading.backtester import backtest_engine
from app.storage.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UNIVERSE = [
    "^BSESN",       # Sensex
    "^NSEI",        # Nifty 50
    "^NSEBANK",     # Bank Nifty
    "GOLDBEES.NS",  # Gold ETF (Indian crude oil unavailable on Yahoo)
    "SBIN.NS",      # State Bank of India
    "TATASTEEL.NS", # TATA group
    "TATAPOWER.NS", # TATA group
    "RELIANCE.NS",  # Reliance / Jio
]


async def main():
    print("=" * 70)
    print(" 1-YEAR HOURLY BACKTEST: ITERATION 18 (LONG-HORIZON VALIDATION)")
    print("=" * 70)
    print(f"Universe: {UNIVERSE}")
    print("Horizon:  1 year of 1-hour candles")
    print("Capital:  \u20b910,000.00 | Risk 2%/trade | 30% position cap")
    print("-" * 70)

    res = await backtest_engine.run_backtest(
        symbols=UNIVERSE,
        period="1y",
        interval="1h",
        use_deepseek=False,
        time_filter=True,
        primary_only=True,
    )

    if res.get("error"):
        print(f"\nERROR: {res['error']}")
        return

    print("\n--- ITERATION 18 RESULTS (1 YEAR, 1H) ---")
    print(f"Total Trades:          {res.get('total_trades')}")
    print(f"Winning Trades:        {res.get('winning_trades')} / {res.get('total_trades')} ({res.get('win_rate_pct')}%)")
    print(f"Losing Trades:         {res.get('losing_trades')}")
    print(f"Gross P&L:             \u20b9{res.get('gross_pnl_inr'):+,.2f}")
    print(f"Fees:                  \u20b9{res.get('fees_inr'):,.2f}")
    print(f"Net P&L (before AI):   \u20b9{res.get('net_pnl_before_ai_inr'):+,.2f} ({res.get('net_return_before_ai_pct'):+}%)")
    print(f"Profit Factor:         {res.get('profit_factor')}")
    print(f"Max Drawdown:          {res.get('max_drawdown_pct')}%")
    print(f"Ending Capital:        \u20b9{res.get('ending_capital_inr'):,.2f}")

    print("\nStrategy Breakdown:")
    for strat, data in res.get("strategy_breakdown", {}).items():
        print(f"  * {strat}: {data.get('trades')} trades, {data.get('wins')} wins "
              f"({data.get('win_rate_pct')}%), P&L: \u20b9{data.get('pnl'):+,.2f}")

    # Per-symbol breakdown from the trade log.
    per_symbol = {}
    for t in res.get("trades", []):
        b = per_symbol.setdefault(t["symbol"], {"trades": 0, "wins": 0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t["won"] else 0
        b["pnl"] = round(b["pnl"] + t["net_pnl"], 2)
    print("\nPer-Symbol Breakdown:")
    for sym, b in sorted(per_symbol.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
        wr = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
        print(f"  * {sym}: {b['trades']} trades, {b['wins']} wins ({wr}%), P&L: \u20b9{b['pnl']:+,.2f}")

    with open("trading_workspace/iteration18_results.json", "w") as f:
        json.dump(res, f, indent=2)
    print("\nResults saved to trading_workspace/iteration18_results.json")

    db.record_learning(
        category="BACKTEST_ITERATION_18",
        insight=(
            f"Iteration 18 (1-year hourly, restricted universe) yielded "
            f"{res.get('total_trades')} trades, {res.get('win_rate_pct')}% win rate, "
            f"\u20b9{res.get('net_pnl_before_ai_inr'):+,.2f} net P&L, "
            f"{res.get('profit_factor')} profit factor, "
            f"and {res.get('max_drawdown_pct')}% max drawdown."
        ),
        strategy_ref="momentum_continuation",
        symbol_ref="RESTRICTED_UNIVERSE_1Y_1H",
        impact_pnl=res.get("net_pnl_before_ai_inr", 0.0),
        account_id="deepseek_flash",
    )
    print("Logged to SQLite learning table.")


if __name__ == "__main__":
    asyncio.run(main())
