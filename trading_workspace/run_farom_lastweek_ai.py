"""FAROM (Fee-Aware Opening-Range Momentum) on LAST WEEK's data, with AI veto.

Last week = Mon 2026-10-26 .. Fri 2026-10-30 (5 sessions, 5-minute bars).

Runs three arms on the identical window and universe so the comparison is
apples-to-apples:
  A. Shipped primary edge (momentum_continuation), no AI  -> control
  B. Shipped primary edge (momentum_continuation), with AI veto
  C. New FAROM strategy, with AI veto

Universe is the restricted, liquid Indian set agreed earlier:
  Sensex, Nifty, Bank Nifty, Gold ETF, SBIN, TATA group, RELIANCE.
"""

import asyncio
import json
import logging

from app.trading.backtester import backtest_engine
from app.trading.farom_strategy import farom_engine
from app.storage.db import db

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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

PERIOD = "5d"
INTERVAL = "5m"


def farom_override(history, prev_day_ohlc, interval):
    """Adapter: evaluate FAROM on the bar history and return a setup dict."""
    from app.trading.intraday_strategies import IntradayStrategySuite
    df = IntradayStrategySuite.prepare_dataframe(history)
    if len(df) < 5:
        return None
    df = IntradayStrategySuite.compute_technical_indicators(df)
    return farom_engine.evaluate(df, or_bars_count=3, min_h1_bars=5)


def show(tag, res):
    if res.get("error"):
        print(f"\n[{tag}] ERROR: {res['error']}")
        return
    print(f"\n--- {tag} ---")
    print(f"  Trades:        {res.get('total_trades')}")
    print(f"  Win rate:      {res.get('win_rate_pct')}% ({res.get('winning_trades')}W / {res.get('losing_trades')}L)")
    print(f"  Gross P&L:     \u20b9{res.get('gross_pnl_inr'):+,.2f}")
    print(f"  Fees:          \u20b9{res.get('fees_inr'):,.2f}")
    print(f"  AI cost:       \u20b9{res.get('ai_cost_inr'):,.2f} ({res.get('deepseek_queries_count')} calls)")
    print(f"  Net (pre-AI):  \u20b9{res.get('net_pnl_before_ai_inr'):+,.2f} ({res.get('net_return_before_ai_pct'):+}%)")
    print(f"  Net (post-AI): \u20b9{res.get('net_pnl_after_ai_inr'):+,.2f} ({res.get('net_return_after_ai_pct'):+}%)")
    print(f"  Profit factor: {res.get('profit_factor')}")
    print(f"  Max drawdown:  {res.get('max_drawdown_pct')}%")
    print(f"  Ending:        \u20b9{res.get('ending_capital_inr'):,.2f}")
    for t in res.get("trades", []):
        print(f"    {t['date']} {t['entry_time']:>5} {t['symbol']:<12} {t['strategy_id']:<26} "
              f"qty={t['quantity']:<4} {t['exit_reason']:<15} net=\u20b9{t['net_pnl']:+.2f}")


async def main():
    print("=" * 78)
    print("  FAROM vs SHIPPED EDGE \u2014 LAST WEEK (2026-10-26 .. 2026-10-30), 5m, WITH AI")
    print("=" * 78)
    print(f"Universe: {UNIVERSE}")
    print("Capital: \u20b910,000 | Risk 0.75%/trade | 90% position cap | max 3 trades/day")

    results = {}

    print("\n>>> ARM A: shipped momentum_continuation, NO AI (control)")
    results["A_shipped_no_ai"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=False, time_filter=True, primary_only=True,
    )
    show("A: shipped, no AI", results["A_shipped_no_ai"])

    print("\n>>> ARM B: shipped momentum_continuation, WITH AI veto")
    results["B_shipped_ai"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=True, max_deepseek_calls=15, time_filter=True, primary_only=True,
    )
    show("B: shipped, AI", results["B_shipped_ai"])

    print("\n>>> ARM C: NEW FAROM strategy, WITH AI veto")
    results["C_farom_ai"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=True, max_deepseek_calls=15, time_filter=True,
        primary_only=False, strategy_override=farom_override,
    )
    show("C: FAROM, AI", results["C_farom_ai"])

    print("\n" + "=" * 78)
    print("  SUMMARY (last week, 5m)")
    print("=" * 78)
    print(f"  {'Arm':<26}{'Trades':>7}{'Win%':>7}{'Gross':>11}{'Fees':>10}{'NetPreAI':>11}{'PF':>7}")
    for k, r in results.items():
        if r.get("error"):
            continue
        print(f"  {k:<26}{r['total_trades']:>7}{r['win_rate_pct']:>7}{r['gross_pnl_inr']:>11,.2f}"
              f"{r['fees_inr']:>10,.2f}{r['net_pnl_before_ai_inr']:>11,.2f}{r['profit_factor']:>7}")

    with open("trading_workspace/farom_lastweek_ai.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved to trading_workspace/farom_lastweek_ai.json")

    c = results.get("C_farom_ai", {})
    if not c.get("error"):
        db.record_learning(
            category="FAROM_LASTWEEK_AI",
            insight=(
                f"FAROM (fee-aware OR momentum, fee-clearing target) on last week 5m with AI: "
                f"{c.get('total_trades')} trades, {c.get('win_rate_pct')}% win, "
                f"net \u20b9{c.get('net_pnl_before_ai_inr'):+,.2f}, PF {c.get('profit_factor')}, "
                f"DD {c.get('max_drawdown_pct')}%."
            ),
            strategy_ref="farom_fee_aware_momentum",
            symbol_ref="RESTRICTED_UNIVERSE_LASTWEEK_5M",
            impact_pnl=c.get("net_pnl_before_ai_inr", 0.0),
            account_id="deepseek_flash",
        )
        print("Logged to SQLite learning table.")


if __name__ == "__main__":
    asyncio.run(main())
