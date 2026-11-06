import asyncio, json, sys
from typing import Any, Dict
import pandas as pd

sys.path.insert(0, ".")

from app.trading.backtester import backtest_engine
from app.trading.fee_aware_strategies import (
    FeeAwareOpeningPullback, LiquiditySweepLong, compute_features,
)

BROAD = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "MARUTI", "SUNPHARMA",
    "ASIANPAINT", "WIPRO", "HCLTECH", "TITAN", "ULTRACEMCO", "POWERGRID",
    "ONGC", "BAJFINANCE", "KOTAKBANK", "LT", "HINDUNILVR", "ADANIENT",
    "JSWSTEEL", "GRASIM", "TECHM", "DRREDDY", "CIPLA", "BPCL", "IOC",
    "HINDALCO", "EICHERMOT", "NESTLEIND", "DIVISLAB", "COALINDIA",
]


def build(symbols, period, interval):
    data = backtest_engine.fetch_historical_bars(symbols, period=period, interval=interval)
    frames, index = {}, {}
    for sym, df in data.items():
        feat = compute_features(df).reset_index(drop=True)
        feat["symbol"] = sym
        frames[sym] = feat
        for i, row in feat.iterrows():
            index[(sym, row["date"], row["time"])] = i
    return frames, index


def make_override(frames, index, kind):
    def override(history, prev_day_ohlc, interval):
        rec = history[-1]
        i = index.get((rec.get("symbol"), rec.get("date"), rec.get("time")))
        if i is None:
            return {"active": False}
        window = frames[rec["symbol"]].iloc[max(0, i - 1): i + 1]
        if kind == "opening":
            return FeeAwareOpeningPullback.evaluate(window, interval=interval)
        return LiquiditySweepLong.evaluate(window, prev_day_ohlc=prev_day_ohlc, interval=interval)
    return override


def show(tag, r):
    if not r or r.get("error"):
        print(tag, "ERROR", r); return r
    print(f"{tag}: {r['total_trades']} trades | win {r['win_rate_pct']}% | gross ₹{r['gross_pnl_inr']} "
          f"| fees ₹{r['fees_inr']} | net ₹{r['net_pnl_after_ai_inr']} | PF {r['profit_factor']} | DD {r['max_drawdown_pct']}%")
    return r


async def main():
    out = {}
    f1, i1 = build(BROAD, "1y", "1h")
    print("broad 1h symbols:", len(f1))
    out["broad_1y_1h_opening"] = show("broad 1y/1h opening", await backtest_engine.run_backtest(
        symbols=list(f1), period="1y", interval="1h", use_deepseek=False,
        strategy_override=make_override(f1, i1, "opening")))
    f5, i5 = build(BROAD, "1mo", "5m")
    print("broad 5m symbols:", len(f5))
    out["broad_1mo_5m_sweep"] = show("broad 1mo/5m sweep", await backtest_engine.run_backtest(
        symbols=list(f5), period="1mo", interval="5m", use_deepseek=False,
        strategy_override=make_override(f5, i5, "sweep")))
    with open("trading_workspace/iteration110_results.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("saved iteration110_results.json")


if __name__ == "__main__":
    asyncio.run(main())
