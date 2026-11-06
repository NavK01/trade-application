"""One-month real-data evaluation with the DeepSeek V4.1 Flash veto.

Runs the exact live execution machinery (next-bar fills, slippage, statutory
fees, daily locks) over real NSE 5-minute bars for the last ~1 month across a
30-symbol Nifty universe, in two arms:

  * baseline   - fee-aware setups, zero AI (deterministic selection only)
  * deepseek   - same setups with the DeepSeek V4.1 Flash veto-only risk filter

The output makes the result auditable per trade and per day so we can see
whether the AI veto improves or degrades the deterministic edge after costs.

Outputs:
  * trading_workspace/deepseek_1mo_eval.json
  * a printed per-day and per-trade report
"""

from __future__ import annotations

import asyncio
import datetime
import json
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, ".")

from app.config import settings
from app.trading.backtester import backtest_engine
from app.trading.fee_aware_selector import FeeAwareSelector

CACHE_1MO_5M = "/tmp/opencode/broad_1mo_5m.pkl"
OUT_PATH = Path("trading_workspace/deepseek_1mo_eval.json")
MAX_DEEPSEEK_CALLS = 15


def load_cache(path: str) -> Optional[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "rb") as fh:
        return pickle.load(fh)


async def run_arm(
    tag: str,
    data: Mapping[str, Any],
    use_deepseek: bool,
) -> Dict[str, Any]:
    selector = FeeAwareSelector(data, interval="5m")
    res = await backtest_engine.run_backtest(
        symbols=list(data),
        period="1mo",
        interval="5m",
        use_deepseek=use_deepseek,
        max_deepseek_calls=MAX_DEEPSEEK_CALLS,
        strategy_override=selector.override,
        intraday_leverage_mult=1.0,
        preloaded_data=dict(data),
    )
    if not res or res.get("error"):
        return {"tag": tag, "error": str(res.get("error") if res else "no result")}
    return {
        "tag": tag,
        "total_trades": res.get("total_trades", 0),
        "win_rate_pct": res.get("win_rate_pct", 0.0),
        "gross_pnl_inr": res.get("gross_pnl_inr", 0.0),
        "fees_inr": res.get("fees_inr", 0.0),
        "ai_cost_inr": res.get("ai_cost_inr", 0.0),
        "net_pnl_before_ai_inr": res.get("net_pnl_before_ai_inr", 0.0),
        "net_pnl_after_ai_inr": res.get("net_pnl_after_ai_inr", 0.0),
        "net_return_after_ai_pct": res.get("net_return_after_ai_pct", 0.0),
        "profit_factor": res.get("profit_factor", 0.0),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0.0),
        "deepseek_queries_count": res.get("deepseek_queries_count", 0),
        "strategy_breakdown": res.get("strategy_breakdown", {}),
        "trades": [
            {
                "date": t.get("date"),
                "symbol": t.get("symbol"),
                "strategy": t.get("strategy"),
                "action": t.get("action"),
                "entry_time": t.get("entry_time"),
                "entry_price": t.get("entry_price"),
                "exit_time": t.get("exit_time"),
                "exit_price": t.get("exit_price"),
                "exit_reason": t.get("exit_reason"),
                "quantity": t.get("quantity"),
                "gross_pnl_inr": t.get("gross_pnl"),
                "fees_inr": t.get("fees_inr"),
                "net_pnl_inr": t.get("net_pnl"),
                "return_pct": t.get("return_pct"),
                "validated_by_deepseek": t.get("validated_by_deepseek"),
            }
            for t in res.get("trades", [])
        ],
        "daily_results": res.get("daily_results", []),
    }


def _day_rows(arm: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Group the arm's trades by date for a per-day net view."""
    by_day: Dict[str, Dict[str, Any]] = {}
    for t in arm.get("trades", []):
        d = str(t.get("date"))
        row = by_day.setdefault(d, {"date": d, "trades": 0, "wins": 0, "net": 0.0, "fees": 0.0})
        row["trades"] += 1
        row["net"] = round(row["net"] + float(t.get("net_pnl_inr") or 0.0), 2)
        row["fees"] = round(row["fees"] + float(t.get("fees_inr") or 0.0), 2)
        if float(t.get("net_pnl_inr") or 0.0) > 0:
            row["wins"] += 1
    rows = sorted(by_day.values(), key=lambda r: r["date"])
    for r in rows:
        r["win_rate_pct"] = round(r["wins"] / r["trades"] * 100, 1) if r["trades"] else 0.0
    return rows


def print_arm(arm: Mapping[str, Any]) -> None:
    print("=" * 78)
    print(f"ARM: {arm['tag']}")
    print("=" * 78)
    if "error" in arm:
        print("  ERROR:", arm["error"])
        return
    print(
        f"  trades {arm['total_trades']} | win {arm['win_rate_pct']}% | "
        f"gross ₹{arm['gross_pnl_inr']} | fees ₹{arm['fees_inr']} | AI ₹{arm['ai_cost_inr']}"
    )
    print(
        f"  net before AI ₹{arm['net_pnl_before_ai_inr']} | net after AI ₹{arm['net_pnl_after_ai_inr']} "
        f"({arm['net_return_after_ai_pct']}%) | PF {arm['profit_factor']} | maxDD {arm['max_drawdown_pct']}%"
    )
    print(f"  DeepSeek calls: {arm['deepseek_queries_count']}")
    print("  strategy breakdown:")
    for name, b in (arm.get("strategy_breakdown") or {}).items():
        print(f"    - {name}: {b.get('trades')} trades | win {b.get('win_rate_pct')}% | net ₹{b.get('pnl')}")
    print("  per-day net:")
    for r in _day_rows(arm):
        print(
            f"    {r['date']} | {r['trades']} trades | win {r['win_rate_pct']}% | "
            f"fees ₹{r['fees']} | net ₹{r['net']}"
        )
    print("  per-trade:")
    for t in arm.get("trades", []):
        sign = "+" if float(t.get("net_pnl_inr") or 0.0) >= 0 else ""
        print(
            f"    {t['date']} {t['entry_time']} {t['action']:4} {t['symbol']:<11} "
            f"qty {t['quantity']:<4} {t['exit_reason']:<14} net {sign}₹{t['net_pnl_inr']} "
            f"({t['return_pct']}%)"
        )


async def main() -> None:
    data = load_cache(CACHE_1MO_5M)
    if not data:
        print(f"ERROR: cache missing: {CACHE_1MO_5M}")
        return

    dates = sorted({d for df in data.values() for d in df["date"].unique()})
    results: Dict[str, Any] = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "capital_inr": settings.INITIAL_CAPITAL_INR,
        "interval": "5m",
        "period": "1mo",
        "symbols": len(data),
        "window_start": dates[0],
        "window_end": dates[-1],
        "max_deepseek_calls": MAX_DEEPSEEK_CALLS,
    }
    print(
        f"Real 1-month NSE 5m universe: {len(data)} symbols, {len(dates)} trading days "
        f"({dates[0]} -> {dates[-1]})"
    )

    results["baseline_zero_ai"] = await run_arm("baseline_zero_ai", data, use_deepseek=False)
    results["deepseek_veto"] = await run_arm("deepseek_veto", data, use_deepseek=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)

    print_arm(results["baseline_zero_ai"])
    print_arm(results["deepseek_veto"])

    a = results["baseline_zero_ai"]
    b = results["deepseek_veto"]
    if "error" not in a and "error" not in b:
        print("=" * 78)
        print("DELTA (DeepSeek vs baseline, net after AI):")
        print(
            f"  net ₹{a['net_pnl_after_ai_inr']} -> ₹{b['net_pnl_after_ai_inr']} "
            f"(Δ ₹{round(b['net_pnl_after_ai_inr'] - a['net_pnl_after_ai_inr'], 2)})"
        )
        print(f"  trades {a['total_trades']} -> {b['total_trades']}")
        print(f"  win% {a['win_rate_pct']} -> {b['win_rate_pct']} | PF {a['profit_factor']} -> {b['profit_factor']}")
        print(f"  AI cost incurred: ₹{b['ai_cost_inr']}")
    print(f"Saved -> {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
