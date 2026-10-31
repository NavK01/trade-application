"""Fee-aware filter sweep (Iteration 19).

Re-runs the 1-year hourly backtest on the restricted universe at several
MIN_EDGE_TO_FEE_RATIO values to find the threshold that turns the strategy
net-positive after fees. Ratio 1.0 is the iter18 baseline (filter effectively
off for any trade that at least breaks even on fees).
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

RATIOS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


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
    for ratio in RATIOS:
        settings.MIN_EDGE_TO_FEE_RATIO = ratio
        res = await backtest_engine.run_backtest(
            symbols=UNIVERSE, period="1y", interval="1h",
            use_deepseek=False, time_filter=True, primary_only=True,
        )
        if res.get("error"):
            print(f"ratio={ratio}: ERROR {res['error']}")
            continue
        row = {
            "ratio": ratio,
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
            f"ratio={ratio:<4} trades={row['trades']:>3} win={row['win_rate']:>5}% "
            f"gross={row['gross']:>9.2f} fees={row['fees']:>8.2f} net={row['net']:>9.2f} "
            f"({row['net_pct']:>6}%) pf={row['pf']:>5} dd={row['max_dd']:>5}%"
        )

    with open("trading_workspace/iteration19_fee_sweep.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("\nSaved trading_workspace/iteration19_fee_sweep.json")

    print("\nPer-symbol net P&L by ratio:")
    for row in rows:
        syms = ", ".join(f"{s}:{b['net']:+.0f}" for s, b in sorted(row["per_symbol"].items(), key=lambda kv: kv[1]["net"]))
        print(f"  ratio={row['ratio']:<4} {syms}")


if __name__ == "__main__":
    asyncio.run(main())
