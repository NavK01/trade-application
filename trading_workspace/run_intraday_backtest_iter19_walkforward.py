"""Walk-forward validation of the fee-to-risk gate (Iteration 19b).

Splits the 1-year hourly sample into two halves and compares the baseline
(filter off) against the candidate threshold (25) in each half. A gate that
only helps in one half is an overfit; one that helps in both is credible.
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


def _slice(res, lo, hi):
    """Recompute summary metrics for trades whose date falls in [lo, hi)."""
    trades = [t for t in res["trades"] if lo <= t["date"] < hi]
    gross = round(sum(t["gross_pnl"] for t in trades), 2)
    fees = round(sum(t["fees_inr"] for t in trades), 2)
    net = round(gross - fees, 2)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gp = sum(t["net_pnl"] for t in wins)
    gl = abs(sum(t["net_pnl"] for t in losses))
    pf = round(gp / gl, 2) if gl else (99.0 if gp else 0.0)
    wr = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    return {"trades": len(trades), "win_rate": wr, "gross": gross, "fees": fees, "net": net, "pf": pf}


async def main():
    results = {}
    for label, thr in (("baseline_off", 100.0), ("gate_25", 25.0)):
        settings.MAX_FEE_TO_RISK_PCT = thr
        res = await backtest_engine.run_backtest(
            symbols=UNIVERSE, period="1y", interval="1h",
            use_deepseek=False, time_filter=True, primary_only=True,
        )
        results[label] = res

    dates = sorted({t["date"] for t in results["baseline_off"]["trades"]})
    mid = dates[len(dates) // 2]
    print(f"Split date (second half starts): {mid}")
    print(f"Full range: {dates[0]} .. {dates[-1]}")
    print()

    out = {"split_date": mid, "halves": {}}
    for half, (lo, hi) in (("H1", ("0000", mid)), ("H2", (mid, "9999"))):
        print(f"--- {half} ({lo} .. {hi}) ---")
        out["halves"][half] = {}
        for label in ("baseline_off", "gate_25"):
            m = _slice(results[label], lo, hi)
            out["halves"][half][label] = m
            print(
                f"  {label:14s} trades={m['trades']:>3} win={m['win_rate']:>5}% "
                f"gross={m['gross']:>9.2f} fees={m['fees']:>8.2f} net={m['net']:>9.2f} pf={m['pf']:>5}"
            )
        print()

    with open("trading_workspace/iteration19_walkforward.json", "w") as f:
        json.dump(out, f, indent=2)
    print("Saved trading_workspace/iteration19_walkforward.json")


if __name__ == "__main__":
    asyncio.run(main())
