"""Frozen-baseline paper-trading truth report.

Answers the only question that matters once strategy work is frozen: does the
live paper account behave like the walk-forward backtest said it should?

It compares the realized closed-trade statistics of a paper account against the
frozen expectation in ``trading_workspace/FROZEN_BASELINE.json`` and returns a
verdict. It deliberately refuses to judge before a minimum sample size, because
short samples are noise. This is a measurement tool -- it never places a trade.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import BASE_DIR

logger = logging.getLogger("app.trading.paper_truth")

FROZEN_BASELINE_PATH = BASE_DIR / "trading_workspace" / "FROZEN_BASELINE.json"


def load_frozen_baseline(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the frozen baseline spec. Raises if it is missing (it is required)."""
    p = Path(path) if path else FROZEN_BASELINE_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _profit_factor(win_sum: float, loss_sum: float) -> float:
    if loss_sum > 0:
        return round(win_sum / loss_sum, 2)
    return 99.0 if win_sum > 0 else 0.0


def _max_drawdown_pct(net_pnls: List[float], initial_capital: float) -> float:
    running = float(initial_capital)
    peak = running
    max_dd = 0.0
    for net in net_pnls:
        running += float(net)
        peak = max(peak, running)
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak * 100.0)
    return round(max_dd, 2)


def build_truth_report(
    database: Any,
    account_id: str,
    baseline: Optional[Dict[str, Any]] = None,
    limit: int = 1000,
) -> Dict[str, Any]:
    """Compare a paper account's realized closed trades to the frozen baseline.

    ``database`` only needs the ``get_trades``, ``get_daily_trade_pnl`` and
    ``get_account_state`` methods (i.e. ``app.storage.db.DatabaseManager``).
    """
    baseline = baseline or load_frozen_baseline()
    expectation = baseline["expectation"]
    agg = expectation["aggregate"]
    band = expectation.get("acceptance_bands", {})
    min_trades = int(band.get("min_closed_trades_for_verdict", 30))
    wr_tol = float(band.get("win_rate_tolerance_pct", 0.90))
    pf_tol = float(band.get("profit_factor_tolerance", 0.90))

    # get_trades returns newest-first; reverse to chronological for the curve.
    trades = database.get_trades(account_id=account_id, limit=limit)
    chrono = list(reversed(trades))
    closed = [t for t in chrono if str(t.get("action", "")).upper() == "SELL"]

    nets = [float(t.get("pnl_inr") or 0.0) for t in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gross = round(sum(float(t.get("gross_pnl_inr") or 0.0) for t in closed), 2)
    fees = round(
        sum(float(t.get("fee_inr") or 0.0) + float(t.get("entry_fee_inr") or 0.0) for t in closed),
        2,
    )
    net_total = round(sum(nets), 2)

    state = database.get_account_state(account_id) or {}
    initial = float(state.get("initial_capital") or 10000.0)

    realized = {
        "closed_trades": len(closed),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "profit_factor": _profit_factor(round(sum(wins), 2), round(abs(sum(losses)), 2)),
        "avg_net_inr": round(net_total / len(closed), 2) if closed else 0.0,
        "gross_pnl_inr": gross,
        "fees_inr": fees,
        "net_pnl_inr": net_total,
        "net_return_pct": round(net_total / initial * 100.0, 2) if initial else 0.0,
        "max_drawdown_pct": _max_drawdown_pct(nets, initial),
    }

    expected = {
        "win_rate_pct": agg["win_rate_pct"],
        "profit_factor": agg["profit_factor"],
        "expectancy_pct_per_trade": agg["expectancy_pct_per_trade"],
        "positive_folds": f"{agg['positive_folds']}/{agg['total_folds']}",
    }

    conclusive = len(closed) >= min_trades
    if not conclusive:
        verdict = "insufficient_sample"
        reasons = [
            f"{len(closed)}/{min_trades} closed trades -- sample too small to judge."
        ]
    else:
        checks = {
            "net_positive": net_total > 0,
            "win_rate_ok": realized["win_rate_pct"] >= wr_tol * expected["win_rate_pct"],
            "profit_factor_ok": realized["profit_factor"] >= pf_tol * expected["profit_factor"],
        }
        failed = [name for name, ok in checks.items() if not ok]
        verdict = "on_track" if not failed else "underperforming"
        reasons = failed or ["all checks passed"]

    daily = database.get_daily_trade_pnl(account_id, limit=60)

    return {
        "account_id": account_id,
        "baseline_id": baseline.get("baseline_id"),
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sample": {
            "closed_trades": len(closed),
            "min_closed_trades_for_verdict": min_trades,
            "is_conclusive": conclusive,
        },
        "realized": realized,
        "expected": expected,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "daily": daily,
    }


__all__ = ["FROZEN_BASELINE_PATH", "load_frozen_baseline", "build_truth_report"]
