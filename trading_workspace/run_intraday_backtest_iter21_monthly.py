"""Monthly P&L breakdown of the 1-year hourly backtest (Iteration 21).

Runs the shipped default config (MAX_FEE_TO_RISK_PCT=25) on 1y/1h and groups
trades by calendar month so the regime dependence is visible month by month.
"""

import asyncio
import json
import logging
from collections import defaultdict

from app.config import settings
from app.trading.backtester import backtest_engine

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UNIVERSE = [
    "^BSESN", "^NSEI", "^NSEBANK",
    "GOLDBEES.NS", "SBIN.NS", "TATASTEEL.NS", "TATAPOWER.NS", "RELIANCE.NS",
]


def _month(date_str):
    return date_str[:7]  # YYYY-MM


async def main():
    print(f"Shipped default MAX_FEE_TO_RISK_PCT={settings.MAX_FEE_TO_RISK_PCT}")
    res = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period="1y", interval="1h",
        use_deepseek=False, time_filter=True, primary_only=True,
    )
    if res.get("error"):
        print("ERROR:", res["error"])
        return

    trades = res["trades"]
    months = defaultdict(lambda: {"n": 0, "w": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "syms": defaultdict(float)})
    for t in trades:
        m = months[_month(t["date"])]
        m["n"] += 1
        m["w"] += 1 if t["won"] else 0
        m["gross"] += t["gross_pnl"]
        m["fees"] += t["fees_inr"]
        m["net"] += t["net_pnl"]
        m["syms"][t["symbol"]] += t["net_pnl"]

    print()
    print(f"{'Month':<9} {'Trades':>6} {'Win%':>6} {'Gross':>10} {'Fees':>9} {'Net':>10} {'CumNet':>10}")
    print("-" * 66)
    cum = 0.0
    rows = []
    for mon in sorted(months):
        m = months[mon]
        cum += m["net"]
        wr = round(m["w"] / m["n"] * 100, 1) if m["n"] else 0.0
        print(f"{mon:<9} {m['n']:>6} {wr:>5}% {m['gross']:>10.2f} {m['fees']:>9.2f} {m['net']:>10.2f} {cum:>10.2f}")
        rows.append({
            "month": mon, "trades": m["n"], "win_rate": wr,
            "gross": round(m["gross"], 2), "fees": round(m["fees"], 2),
            "net": round(m["net"], 2), "cum_net": round(cum, 2),
            "per_symbol": {s: round(v, 2) for s, v in sorted(m["syms"].items())},
        })

    print("-" * 66)
    print(f"{'TOTAL':<9} {res['total_trades']:>6} {res['win_rate_pct']:>5}% "
          f"{res['gross_pnl_inr']:>10.2f} {res['fees_inr']:>9.2f} {res['net_pnl_before_ai_inr']:>10.2f}")

    pos = sum(1 for r in rows if r["net"] > 0)
    neg = sum(1 for r in rows if r["net"] < 0)
    print(f"\nMonths: {len(rows)} total, {pos} positive, {neg} negative")

    with open("trading_workspace/iteration21_monthly.json", "w") as f:
        json.dump({"summary": {
            "trades": res["total_trades"], "win_rate": res["win_rate_pct"],
            "gross": res["gross_pnl_inr"], "fees": res["fees_inr"],
            "net": res["net_pnl_before_ai_inr"], "pf": res["profit_factor"],
            "max_dd": res["max_drawdown_pct"], "ending_capital": res["ending_capital_inr"],
        }, "months": rows}, f, indent=2)
    print("Saved trading_workspace/iteration21_monthly.json")


if __name__ == "__main__":
    asyncio.run(main())
