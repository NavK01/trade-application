"""Confirm the shipped default (MAX_FEE_TO_RISK_PCT=25) on 1y/1h (Iteration 20).

Runs the backtest with NO override so it exercises the real settings default.
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


async def main():
    print(f"Using shipped default MAX_FEE_TO_RISK_PCT={settings.MAX_FEE_TO_RISK_PCT}")
    res = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period="1y", interval="1h",
        use_deepseek=False, time_filter=True, primary_only=True,
    )
    if res.get("error"):
        print("ERROR:", res["error"])
        return
    summary = {
        "max_fee_to_risk_pct": settings.MAX_FEE_TO_RISK_PCT,
        "trades": res["total_trades"],
        "win_rate": res["win_rate_pct"],
        "gross": res["gross_pnl_inr"],
        "fees": res["fees_inr"],
        "net": res["net_pnl_before_ai_inr"],
        "net_pct": res["net_return_before_ai_pct"],
        "pf": res["profit_factor"],
        "max_dd": res["max_drawdown_pct"],
        "ending_capital": res["ending_capital_inr"],
    }
    print(json.dumps(summary, indent=2))
    with open("trading_workspace/iteration20_default_confirm.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("Saved trading_workspace/iteration20_default_confirm.json")


if __name__ == "__main__":
    asyncio.run(main())
