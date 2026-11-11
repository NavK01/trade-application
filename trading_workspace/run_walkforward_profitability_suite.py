"""Phase 3 walk-forward profitability suite for the ₹10,000 NSE model.

Runs the plan.md Roadmap arms and applies the Phase 4 promotion gates:

  Arm 1  Intraday long + short + RS/RVOL filter, 1Y/1h broad universe (Zero AI)
  Arm 1b Intraday long + short, 1mo/5m broad universe (Zero AI), if 5m data exists
  Arm 2  Same as Arm 1b but with the DeepSeek V4.1 Flash veto (only when a real
         API key is configured; otherwise recorded as explicitly skipped)
  Arm 3  Multi-day swing portfolio (RSI2OversoldSwing + NR7SwingBreakout),
         5Y daily in 4 sequential folds + 1Y daily (Zero AI)

Data policy: cached pickles under /tmp/opencode are used when present so the
suite is reproducible and does not depend on network access. Arm 1b falls back
to a live fetch only when its cache is missing, and is skipped (not faked) when
neither is available.

Outputs:
  * trading_workspace/iteration115_results.json
  * an appended section in trading_workspace/INTRADAY_STRATEGY_LEARNING_LOG.md
"""

from __future__ import annotations

import asyncio
import datetime
import json
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

sys.path.insert(0, ".")

from app.config import settings
from app.trading.backtester import backtest_engine
from app.trading.fee_aware_selector import FeeAwareSelector
from app.trading.swing_strategies import SwingBacktestEngine

BROAD = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "MARUTI", "SUNPHARMA",
    "ASIANPAINT", "WIPRO", "HCLTECH", "TITAN", "ULTRACEMCO", "POWERGRID",
    "ONGC", "BAJFINANCE", "KOTAKBANK", "LT", "HINDUNILVR", "ADANIENT",
    "JSWSTEEL", "GRASIM", "TECHM", "DRREDDY", "CIPLA", "BPCL", "IOC",
    "HINDALCO", "EICHERMOT", "NESTLEIND", "DIVISLAB", "COALINDIA",
]

CACHE_1Y_1H = "/tmp/opencode/broad_1y_1h.pkl"
CACHE_1MO_5M = "/tmp/opencode/broad_1mo_5m.pkl"
CACHE_5Y_1D = "/tmp/opencode/broad_5y_1d.pkl"

RESULTS_PATH = Path("trading_workspace/iteration115_results.json")
LOG_PATH = Path("trading_workspace/INTRADAY_STRATEGY_LEARNING_LOG.md")

# Phase 4 promotion gates.
GATES = {
    "min_folds_positive": 3,
    "min_profit_factor": 1.35,
    "max_drawdown_pct": 8.0,
}


def load_cache(path: str) -> Optional[Dict[str, pd.DataFrame]]:
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "rb") as fh:
        return pickle.load(fh)


def save_cache(path: str, data: Mapping[str, pd.DataFrame]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        pickle.dump(dict(data), fh)


def summarize_backtest(tag: str, res: Dict[str, Any]) -> Dict[str, Any]:
    if not res or res.get("error"):
        return {"tag": tag, "error": str(res.get("error") if res else "no result")}
    return {
        "tag": tag,
        "total_trades": res.get("total_trades", 0),
        "win_rate_pct": res.get("win_rate_pct", 0.0),
        "gross_pnl_inr": res.get("gross_pnl_inr", 0.0),
        "fees_inr": res.get("fees_inr", 0.0),
        "net_pnl_inr": res.get("net_pnl_before_ai_inr", 0.0),
        "profit_factor": res.get("profit_factor", 0.0),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0.0),
        "interval": res.get("interval"),
        "intraday_leverage_mult": res.get("intraday_leverage_mult"),
    }


async def run_intraday_arm(
    tag: str,
    data: Mapping[str, pd.DataFrame],
    interval: str,
    period: str,
    use_deepseek: bool,
    leverage: float,
    max_deepseek_calls: int = 15,
) -> Dict[str, Any]:
    selector = FeeAwareSelector(data, interval=interval)
    res = await backtest_engine.run_backtest(
        symbols=list(data),
        period=period,
        interval=interval,
        use_deepseek=use_deepseek,
        max_deepseek_calls=max_deepseek_calls,
        strategy_override=selector.override,
        intraday_leverage_mult=leverage,
        preloaded_data=dict(data),
    )
    return summarize_backtest(tag, res)


def run_swing_folds(
    data: Mapping[str, pd.DataFrame],
    capital: float = 10000.0,
    max_concurrent: int = 2,
    n_folds: int = 4,
) -> Dict[str, Any]:
    dates = sorted({str(d) for df in data.values() for d in df["date"].unique()})
    if not dates:
        return {"error": "no dates"}
    size = len(dates) // n_folds
    engine = SwingBacktestEngine(initial_capital=capital)
    folds: List[Dict[str, Any]] = []
    for k in range(n_folds):
        lo = k * size
        hi = len(dates) if k == n_folds - 1 else (k + 1) * size
        fold_dates = set(dates[lo:hi])
        fold_data = {
            sym: df[df["date"].astype(str).isin(fold_dates)].reset_index(drop=True)
            for sym, df in data.items()
        }
        fold_data = {s: d for s, d in fold_data.items() if len(d) >= 210}
        res = engine.run_portfolio_backtest(
            fold_data, strategy="combined", max_concurrent=max_concurrent
        )
        folds.append({
            "fold": k + 1,
            "start": dates[lo],
            "end": dates[hi - 1],
            "symbols": len(fold_data),
            "total_trades": res["total_trades"],
            "win_rate_pct": res["win_rate_pct"],
            "net_pnl_inr": res["net_pnl_inr"],
            "net_return_pct": res["net_return_pct"],
            "profit_factor": res["profit_factor"],
            "max_drawdown_pct": res["max_drawdown_pct"],
            "fees_inr": res["fees_inr"],
        })
    positive = [f for f in folds if f["net_pnl_inr"] > 0]
    return {
        "folds": folds,
        "positive_folds": len(positive),
        "total_folds": len(folds),
        "avg_profit_factor": round(
            sum(f["profit_factor"] for f in folds) / len(folds), 2
        ) if folds else 0.0,
    }


def gate_report(arm_results: Mapping[str, Any]) -> Dict[str, Any]:
    swing = arm_results.get("arm3_swing_portfolio_5y", {})
    folds = swing.get("folds", [])
    positive = sum(1 for f in folds if f.get("net_pnl_inr", 0) > 0)
    avg_pf = swing.get("avg_profit_factor", 0.0)
    max_dd = max((f.get("max_drawdown_pct", 0.0) for f in folds), default=0.0)
    return {
        "gate1_folds_positive": positive >= GATES["min_folds_positive"],
        "gate1_detail": f"{positive}/{len(folds)} swing folds net-positive (need >= {GATES['min_folds_positive']})",
        "gate2_profit_factor": avg_pf >= GATES["min_profit_factor"],
        "gate2_detail": f"avg swing PF {avg_pf} (need >= {GATES['min_profit_factor']})",
        "gate2_drawdown": max_dd <= GATES["max_drawdown_pct"],
        "gate2_drawdown_detail": f"max swing fold DD {round(max_dd, 2)}% (need <= {GATES['max_drawdown_pct']}%)",
    }


async def main() -> None:
    results: Dict[str, Any] = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "capital_inr": settings.INITIAL_CAPITAL_INR,
        "gates": GATES,
    }

    # ---- Arm 1: intraday 1Y/1h broad, zero AI ----
    data_1h = load_cache(CACHE_1Y_1H)
    if data_1h:
        results["arm1_intraday_1y_1h_zero_ai"] = await run_intraday_arm(
            "arm1_intraday_1y_1h", data_1h, "1h", "1y",
            use_deepseek=False, leverage=1.0,
        )
    else:
        results["arm1_intraday_1y_1h_zero_ai"] = {"error": f"cache missing: {CACHE_1Y_1H}"}

    # ---- Arm 1b: intraday 1mo/5m broad, zero AI ----
    data_5m = load_cache(CACHE_1MO_5M)
    if data_5m is None:
        try:
            fetched = backtest_engine.fetch_historical_bars(BROAD, period="1mo", interval="5m")
            data_5m = fetched or None
            if data_5m:
                save_cache(CACHE_1MO_5M, data_5m)
        except Exception as exc:  # network / rate limit
            results["arm1b_intraday_1mo_5m_zero_ai"] = {"skipped": f"5m data unavailable: {exc}"}
            data_5m = None
    if data_5m:
        results["arm1b_intraday_1mo_5m_zero_ai"] = await run_intraday_arm(
            "arm1b_intraday_1mo_5m", data_5m, "5m", "1mo",
            use_deepseek=False, leverage=1.0,
        )

    # ---- Arm 2: 1mo/5m with DeepSeek veto (only with a real key) ----
    key = settings.DEEPSEEK_API_KEY or ""
    if data_5m and key and not key.startswith("your_"):
        results["arm2_intraday_1mo_5m_deepseek"] = await run_intraday_arm(
            "arm2_intraday_1mo_5m_deepseek", data_5m, "5m", "1mo",
            use_deepseek=True, leverage=1.0, max_deepseek_calls=15,
        )
    else:
        reason = "no real DEEPSEEK_API_KEY configured" if not (key and not key.startswith("your_")) else "5m data unavailable"
        results["arm2_intraday_1mo_5m_deepseek"] = {"skipped": reason}

    # ---- Arm 3: multi-day swing portfolio (5Y daily, 4 folds) ----
    data_1d = load_cache(CACHE_5Y_1D)
    if data_1d:
        results["arm3_swing_portfolio_5y"] = run_swing_folds(data_1d, n_folds=4)
        results["arm3_swing_portfolio_1y"] = run_swing_folds(data_1d, n_folds=1)
    else:
        results["arm3_swing_portfolio_5y"] = {"error": f"cache missing: {CACHE_5Y_1D}"}

    results["promotion_gates"] = gate_report(results)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)

    _append_log(results)
    _print_summary(results)


def _append_log(results: Mapping[str, Any]) -> None:
    ts = results.get("timestamp_utc", "")
    gates = results.get("promotion_gates", {})
    swing = results.get("arm3_swing_portfolio_5y", {})
    lines = [
        "",
        "---",
        "",
        f"## Iteration 115: Walk-Forward Profitability Suite ({ts})",
        "",
        f"* **Arm 1 (1Y/1h broad, long+short+RS, Zero AI):** {_fmt(results.get('arm1_intraday_1y_1h_zero_ai'))}",
        f"* **Arm 1b (1mo/5m broad, long+short, Zero AI):** {_fmt(results.get('arm1b_intraday_1mo_5m_zero_ai'))}",
        f"* **Arm 2 (1mo/5m + DeepSeek veto):** {_fmt(results.get('arm2_intraday_1mo_5m_deepseek'))}",
        f"* **Arm 3 (5Y daily swing portfolio, 4 folds):** "
        f"{swing.get('positive_folds', '?')}/{swing.get('total_folds', '?')} folds net-positive, "
        f"avg PF {swing.get('avg_profit_factor', '?')}",
        f"* **Gates:** {json.dumps(gates)}",
    ]
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _fmt(arm: Optional[Mapping[str, Any]]) -> str:
    if arm is None:
        return "not run"
    if "skipped" in arm:
        return f"SKIPPED ({arm['skipped']})"
    if "error" in arm:
        return f"ERROR ({arm['error']})"
    return (
        f"{arm.get('total_trades')} trades | win {arm.get('win_rate_pct')}% | "
        f"net ₹{arm.get('net_pnl_inr')} | PF {arm.get('profit_factor')} | "
        f"DD {arm.get('max_drawdown_pct')}%"
    )


def _print_summary(results: Mapping[str, Any]) -> None:
    print("=" * 78)
    print("ITERATION 115 - WALK-FORWARD PROFITABILITY SUITE")
    print("=" * 78)
    for tag in (
        "arm1_intraday_1y_1h_zero_ai",
        "arm1b_intraday_1mo_5m_zero_ai",
        "arm2_intraday_1mo_5m_deepseek",
    ):
        print(f"{tag}: {_fmt(results.get(tag))}")
    swing = results.get("arm3_swing_portfolio_5y", {})
    for fold in swing.get("folds", []):
        print(
            f"  swing fold {fold['fold']} [{fold['start']}..{fold['end']}]: "
            f"{fold['total_trades']} trades | win {fold['win_rate_pct']}% | "
            f"net ₹{fold['net_pnl_inr']} | PF {fold['profit_factor']} | DD {fold['max_drawdown_pct']}%"
        )
    print("-" * 78)
    print(json.dumps(results.get("promotion_gates", {}), indent=2))
    print(f"Saved -> {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
