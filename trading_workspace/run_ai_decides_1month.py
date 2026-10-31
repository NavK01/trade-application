"""AI-DECIDES on ONE MONTH of data — does letting the model own the trade beat
 the deterministic engine?

Window: 2026-10-01 .. 2026-10-30 (one month, 5-minute bars).
Universe: the restricted, liquid Indian set agreed earlier:
  Sensex, Nifty, Bank Nifty, Gold ETF, SBIN, TATA group, RELIANCE.

Three arms on the identical window and universe so the comparison is
apples-to-apples:
  A. Shipped primary edge (momentum_continuation), NO AI      -> control
  B. AI-DECIDES: the model proposes the whole trade geometry
     (action / entry / stop / target), the engine validates it against hard
     safety rails and rejects anything that breaks them.
  C. AI-DECIDES with a stricter reward/risk rail (1.5R instead of 1.2R),
     to see whether demanding more reward per unit of risk helps.

Every AI arm is charged its own model cost, so the comparison is net of AI
spend. The question the user asked is simple: does the AI increase P&L?
"""

import asyncio
import json
import logging

from app.trading.backtester import backtest_engine
from app.trading.ai_decision import ai_decision_engine, AIDecisionEngine
from app.trading.intraday_strategies import IntradayStrategySuite
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

PERIOD = "1mo"
INTERVAL = "5m"
CAPITAL = 10_000.0


def make_ai_override(engine: AIDecisionEngine):
    """Async override that lets the AI own the whole trade geometry.

    The backtester's strategy_override signature is (history, prev_day_ohlc,
    interval) and does NOT include the symbol, so the backtester tags each
    history record with its symbol; we read it back here.
    """

    async def _override(history, prev_day_ohlc, interval):
        if not history:
            return None
        symbol = history[-1].get("symbol") or "UNKNOWN"
        df = IntradayStrategySuite.prepare_dataframe(history)
        if len(df) < 5:
            return None
        df = IntradayStrategySuite.compute_technical_indicators(df)
        current_price = float(df.iloc[-1]["close"])
        decision = await engine.decide(symbol, df, CAPITAL)
        return engine.validate(decision, current_price)

    return _override


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
    print("  AI-DECIDES vs DETERMINISTIC \u2014 ONE MONTH (2026-10-01 .. 2026-10-30), 5m")
    print("=" * 78)
    print(f"Universe: {UNIVERSE}")
    print("Capital: \u20b910,000 | Risk 0.75%/trade | 90% position cap | max 3 trades/day")

    results = {}

    print("\n>>> ARM A: shipped momentum_continuation, NO AI (control)")
    results["A_deterministic_no_ai"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=False, time_filter=True, primary_only=True,
    )
    show("A: deterministic, no AI", results["A_deterministic_no_ai"])

    print("\n>>> ARM B: AI-DECIDES (model owns entry/stop/target), rails 1.2R")
    ai_decision_engine.reset_stats()
    results["B_ai_decides"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=False, time_filter=True, primary_only=False,
        strategy_override=make_ai_override(ai_decision_engine),
        ai_cost_hook=lambda: ai_decision_engine.stats["cost_inr"],
    )
    results["B_ai_decides"]["ai_decision_stats"] = dict(ai_decision_engine.stats)
    show("B: AI-decides", results["B_ai_decides"])

    print("\n>>> ARM C: AI-DECIDES with stricter reward/risk rail (1.5R)")
    strict_engine = AIDecisionEngine(min_rr=1.5)
    results["C_ai_decides_strict_rr"] = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period=PERIOD, interval=INTERVAL,
        use_deepseek=False, time_filter=True, primary_only=False,
        strategy_override=make_ai_override(strict_engine),
        ai_cost_hook=lambda: strict_engine.stats["cost_inr"],
    )
    results["C_ai_decides_strict_rr"]["ai_decision_stats"] = dict(strict_engine.stats)
    show("C: AI-decides (1.5R)", results["C_ai_decides_strict_rr"])

    print("\n" + "=" * 78)
    print("  SUMMARY (one month, 5m)")
    print("=" * 78)
    print(f"  {'Arm':<26}{'Trades':>7}{'Win%':>7}{'Gross':>11}{'Fees':>10}{'NetPostAI':>11}{'PF':>7}{'DD%':>7}")
    for k, r in results.items():
        if r.get("error"):
            continue
        print(f"  {k:<26}{r['total_trades']:>7}{r['win_rate_pct']:>7}{r['gross_pnl_inr']:>11,.2f}"
              f"{r['fees_inr']:>10,.2f}{r['net_pnl_after_ai_inr']:>11,.2f}"
              f"{r['profit_factor']:>7}{r['max_drawdown_pct']:>7}")

    print("\n  AI proposal funnel (arm B):")
    st = results.get("B_ai_decides", {}).get("ai_decision_stats", {})
    print(f"    calls={st.get('calls')} proposed_buy={st.get('proposed_buy')} "
          f"proposed_pass={st.get('proposed_pass')} accepted={st.get('accepted')} "
          f"rejected={st.get('rejected')} errors={st.get('errors')}")
    print(f"    reject_reasons={st.get('reject_reasons')}")

    with open("trading_workspace/ai_decides_1month.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved to trading_workspace/ai_decides_1month.json")

    a = results.get("A_deterministic_no_ai", {})
    b = results.get("B_ai_decides", {})
    if not a.get("error") and not b.get("error"):
        delta = b.get("net_pnl_after_ai_inr", 0.0) - a.get("net_pnl_after_ai_inr", 0.0)
        db.record_learning(
            category="AI_DECIDES_1MONTH",
            insight=(
                f"AI-decides vs deterministic on 1 month 5m: deterministic "
                f"\u20b9{a.get('net_pnl_after_ai_inr'):+,.2f} ({a.get('total_trades')} trades, "
                f"{a.get('win_rate_pct')}% win, PF {a.get('profit_factor')}); "
                f"AI-decides \u20b9{b.get('net_pnl_after_ai_inr'):+,.2f} ({b.get('total_trades')} trades, "
                f"{b.get('win_rate_pct')}% win, PF {b.get('profit_factor')}); "
                f"delta \u20b9{delta:+,.2f}. AI cost \u20b9{b.get('ai_cost_inr'):,.2f}."
            ),
            strategy_ref="ai_decides_full_geometry",
            symbol_ref="RESTRICTED_UNIVERSE_1MONTH_5M",
            impact_pnl=delta,
            account_id="deepseek_flash",
        )
        print("Logged to SQLite learning table.")


if __name__ == "__main__":
    asyncio.run(main())
