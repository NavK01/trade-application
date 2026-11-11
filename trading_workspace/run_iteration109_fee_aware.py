"""Iteration 109: fee-aware high-win-rate strategies through the REAL backtester.

Runs:
  A. 1-year / 1h, no AI   -> FeeAwareOpeningPullback (walk-forward validated)
  B. 1-month / 5m, no AI  -> LiquiditySweepLong (PDL sweep)
  C. 1-month / 5m, AI veto-> LiquiditySweepLong + DeepSeek veto filter

Results are stored as trading_workspace/iteration109_results.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict

import pandas as pd

sys.path.insert(0, ".")

from app.trading.backtester import backtest_engine
from app.trading.fee_aware_strategies import (
    FeeAwareOpeningPullback,
    LiquiditySweepLong,
    compute_features,
)

UNIVERSE = ["SBIN", "ITC", "TATASTEEL", "INFY", "ICICIBANK", "AXISBANK", "NTPC", "COALINDIA"]


def build_feature_index(symbols, period, interval):
    """Fetch continuous per-symbol bars and expose them as feature frames."""
    data = backtest_engine.fetch_historical_bars(symbols, period=period, interval=interval)
    frames: Dict[str, pd.DataFrame] = {}
    index: Dict[Any, int] = {}
    for sym, df in data.items():
        feat = compute_features(df)
        feat = feat.reset_index(drop=True)
        feat["symbol"] = sym
        frames[sym] = feat
        for i, row in feat.iterrows():
            index[(sym, row["date"], row["time"])] = i
    return frames, index


def make_override(frames, index, evaluator):
    def override(history, prev_day_ohlc, interval):
        rec = history[-1]
        sym = rec.get("symbol")
        key = (sym, rec.get("date"), rec.get("time"))
        i = index.get(key)
        if i is None:
            return {"active": False}
        window = frames[sym].iloc[max(0, i - 1): i + 1]
        return evaluator(window, prev_day_ohlc, interval)
    return override


def sweep_eval(window, prev_day_ohlc, interval):
    return LiquiditySweepLong.evaluate(window, prev_day_ohlc=prev_day_ohlc, interval=interval)


def opening_eval(window, prev_day_ohlc, interval):
    return FeeAwareOpeningPullback.evaluate(window, interval=interval)


def summarize(tag, res):
    if not res or res.get("error"):
        print(f"{tag}: ERROR {res}")
        return res
    print(
        f"{tag}: {res['total_trades']} trades | win {res['win_rate_pct']}% | "
        f"gross ₹{res['gross_pnl_inr']} | fees ₹{res['fees_inr']} | "
        f"AI ₹{res['ai_cost_inr']} | net ₹{res['net_pnl_after_ai_inr']} "
        f"| PF {res['profit_factor']} | DD {res['max_drawdown_pct']}%"
    )
    return res


async def main():
    out: Dict[str, Any] = {}

    # A: 1-year hourly opening pullback, no AI.
    frames, index = build_feature_index(UNIVERSE, period="1y", interval="1h")
    print(f"1h feature frames: {len(frames)} symbols")
    res_a = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period="1y", interval="1h", use_deepseek=False,
        strategy_override=make_override(frames, index, opening_eval),
    )
    out["A_1y_1h_opening_pullback"] = summarize("A 1y/1h opening-pullback", res_a)

    # B: 1-month 5m PDL sweep, no AI.
    frames5, index5 = build_feature_index(UNIVERSE, period="1mo", interval="5m")
    print(f"5m feature frames: {len(frames5)} symbols")
    res_b = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period="1mo", interval="5m", use_deepseek=False,
        strategy_override=make_override(frames5, index5, sweep_eval),
    )
    out["B_1mo_5m_pdl_sweep"] = summarize("B 1mo/5m PDL-sweep", res_b)

    # C: 1-month 5m PDL sweep with the DeepSeek veto filter.
    res_c = await backtest_engine.run_backtest(
        symbols=UNIVERSE, period="1mo", interval="5m", use_deepseek=True,
        max_deepseek_calls=15,
        strategy_override=make_override(frames5, index5, sweep_eval),
    )
    out["C_1mo_5m_pdl_sweep_ai"] = summarize("C 1mo/5m PDL-sweep + AI", res_c)

    with open("trading_workspace/iteration109_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("Saved trading_workspace/iteration109_results.json")


if __name__ == "__main__":
    asyncio.run(main())
