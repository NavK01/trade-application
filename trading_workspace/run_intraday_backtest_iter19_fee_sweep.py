"""Fee-to-risk filter sweep (Iteration 19).

Re-runs the 1-year hourly backtest on the restricted universe at several
MAX_FEE_TO_RISK_PCT values. The gate rejects candidates whose round-trip
fee exceeds that percentage of the rupee risk budget (entry-stop)*qty.
100.0 is the iter18 baseline (filter off).
"""

import asyncio
import json
import logging

from app.config import settings
from app.trading.backtester import backtest_engine

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UNIVERSE = [
    "^BSESN", "^NSEI", "^NSEBANK",
    "GOLDBEES.NS", "SBIN.NS", "TATASTEEL.NS", "TATAPOWER.NS", "RELIANCE.NS",
]

THRESHOLDS = [100.0, 40.0, 30.0, 25.0, 20.0, 15.0, 10.0]


def _per_symbol(trades):
    out = {}
    for t in trades:
        b = out.setdefault(t["symbol"], {"n": 0, "w": 0, "net": 0.0})
        b["n"] += 1
        b["w"] += 1 if t["won"] else 0
        b["net"] = round(b["net"] + t["net_pnl"], 2)
    return out


async def main():
    rows = []
    for thr in THRESHOLDS:
        settings.MAX_FEE_TO_RISK_PCT = thr
        res = await backtest_engine.run_backtest(
            symbols=UNIVERSE, period="1y", interval="1h",
            use_deepseek=False, time_filter=True, primary_only=True,
        )
        if res.get("error"):
            print(f"thr={thr}: ERROR {res['error']}")
            continue
        row = {
            "max_fee_to_risk_pct": thr,
            "trades": res["total_trades"],
            "win_rate": res["win_rate_pct"],
            "gross": res["gross_pnl_inr"],
            "fees": res["fees_inr"],
            "net": res["net_pnl_before_ai_inr"],
            "net_pct": res["net_return_before_ai_pct"],
            "pf": res["profit_factor"],
            "max_dd": res["max_drawdown_pct"],
            "per_symbol": _per_symbol(res["trades"]),
        }
        rows.append(row)
        print(
            f"thr={thr:<6} trades={row['trades']:>3} win={row['win_rate']:>5}% "
            f"gross={row['gross']:>9.2f} fees={row['fees']:>8.2f} net={row['net']:>9.2f} "
            f"({row['net_pct']:>6}%) pf={row['pf']:>5} dd={row['max_dd']:>5}%"
        )

    with open("trading_workspace/iteration19_fee_sweep.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("\nSaved trading_workspace/iteration19_fee_sweep.json")

    print("\nPer-symbol net P&L by threshold:")
    for row in rows:
        syms = ", ".join(
            f"{s}:{b['net']:+.0f}"
            for s, b in sorted(row["per_symbol"].items(), key=lambda kv: kv[1]["net"])
        )
        print(f"  thr={row['max_fee_to_risk_pct']:<6} {syms}")


if __name__ == "__main__":
    asyncio.run(main())
