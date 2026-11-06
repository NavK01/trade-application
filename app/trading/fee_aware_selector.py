"""Reusable fee-aware intraday setup selection for live and research paths.

Why this module exists
----------------------
The intraday backtester evaluates a ``strategy_override`` with DAY-ONLY history
for one symbol at a time. The fee-aware setups, however, need two extra things
the per-symbol day slice cannot provide:

  1. Continuous (non-session-reset) indicator context so the 20/50 EMAs, ATR
     and RSI are defined on the very first bar of a session; and
  2. Cross-sectional relative-strength / RVOL metrics for the RS leader setup,
     which by definition depend on the whole universe for that session.

Previously each research harness re-implemented this bookkeeping, and the
window convention drifted (the RS leader silently never fired because it was
handed a two-bar slice). This module owns the bookkeeping exactly once so the
walk-forward suite, the continuous optimizer, and the live strategy manager all
select setups through the same code path and the same window semantics.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from app.trading.fee_aware_strategies import (
    FeeAwareOpeningPullback,
    LiquiditySweepLong,
    LiquiditySweepShort,
    RelativeStrengthLeaderBreakout,
    compute_cross_sectional_metrics,
    compute_features,
)

# Evaluation order for a single bar. Highest-conviction, most specific setups
# are checked first so a scripted bar is attributed to the strongest signal.
DEFAULT_STRATEGY_ORDER: Tuple[str, ...] = (
    "opening_pullback",
    "liquidity_sweep_long",
    "liquidity_sweep_short",
    "rs_leader",
)


def _inactive(reason: str) -> Dict[str, Any]:
    return {"active": False, "signal": "HOLD", "reason": reason}


def select_fee_aware_setup(
    session_bars: pd.DataFrame,
    prev_day_ohlc: Optional[Mapping[str, float]] = None,
    interval: str = "5m",
    metrics: Optional[Mapping[str, float]] = None,
    feature_frame: Optional[pd.DataFrame] = None,
    strategy_order: Tuple[str, ...] = DEFAULT_STRATEGY_ORDER,
) -> Dict[str, Any]:
    """Return the best active fee-aware setup for one bar, or an inactive dict.

    Parameters
    ----------
    session_bars:
        The current session's bars up to and including the bar being evaluated
        (ascending). Used by the RS leader, which needs the session's opening
        range, VWAP and EMA-9. Must carry ``open``/``high``/``low``/``close``/
        ``volume`` and ``time`` (and ``date`` when spanning a session).
    prev_day_ohlc:
        ``{"high": .., "low": .., "close": ..}`` for the PREVIOUS trading day.
    metrics:
        Optional ``{"rs_30m_pct": .., "rvol": .., "rs_rank_pct": ..}`` for the
        RS leader. When omitted the RS leader is skipped.
    feature_frame:
        Continuous feature frame (from :func:`compute_features`) whose last row
        is the bar being evaluated. Supplying it avoids recomputing ATR/EMA/RSI
        on a truncated slice. When None, features are computed from
        ``session_bars``.
    strategy_order:
        Which setups to try, in priority order.
    """
    if session_bars is None or len(session_bars) == 0:
        return _inactive("Empty session window")

    if feature_frame is not None and len(feature_frame) > 0:
        tail = feature_frame.tail(2)
    else:
        tail = compute_features(session_bars).tail(2)
    last = tail.iloc[-1]
    bar_time = str(last.get("time", ""))

    if "opening_pullback" in strategy_order:
        if bar_time == str(FeeAwareOpeningPullback.DEFAULTS["entry_time"]):
            sig = FeeAwareOpeningPullback.evaluate(tail, interval=interval)
            if sig.get("active"):
                return sig

    if "liquidity_sweep_long" in strategy_order:
        sig = LiquiditySweepLong.evaluate(tail, prev_day_ohlc=prev_day_ohlc, interval=interval)
        if sig.get("active"):
            return sig

    if "liquidity_sweep_short" in strategy_order:
        sig = LiquiditySweepShort.evaluate(tail, prev_day_ohlc=prev_day_ohlc, interval=interval)
        if sig.get("active"):
            return sig

    if "rs_leader" in strategy_order and metrics:
        sig = RelativeStrengthLeaderBreakout.evaluate(
            session_bars,
            rs_30m_pct=float(metrics.get("rs_30m_pct", 0.0)),
            rvol=float(metrics.get("rvol", 0.0)),
            rs_rank_pct=metrics.get("rs_rank_pct"),
        )
        if sig.get("active"):
            return sig

    return _inactive("No fee-aware setup")


class FeeAwareSelector:
    """Precomputed fee-aware selector over an intraday universe.

    Build it once from the raw universe frames; it then answers
    ``override(history, prev_day_ohlc, interval)`` for the backtester and
    ``select(symbol, date, time, prev_day_ohlc)`` for ad-hoc/live use. Feature
    frames, the ``(symbol, date, time)`` position index, and per-session
    cross-sectional metrics are computed once and reused.
    """

    def __init__(
        self,
        universe: Mapping[str, pd.DataFrame],
        interval: str = "5m",
        strategy_order: Tuple[str, ...] = DEFAULT_STRATEGY_ORDER,
    ):
        self.interval = interval
        self.strategy_order = strategy_order
        self.frames: Dict[str, pd.DataFrame] = {}
        self.index: Dict[Tuple[str, str, str], int] = {}
        for sym, df in universe.items():
            if df is None or len(df) == 0:
                continue
            feat = compute_features(df).reset_index(drop=True)
            feat["symbol"] = sym
            self.frames[sym] = feat
            for i, row in feat.iterrows():
                self.index[(str(sym), str(row["date"]), str(row["time"]))] = int(i)
        self.rs_metrics = self._build_rs_metrics()

    def _build_rs_metrics(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        dates = sorted({str(d) for f in self.frames.values() for d in f["date"].unique()})
        return {d: compute_cross_sectional_metrics(self.frames, session_date=d) for d in dates}

    def metrics_for(self, symbol: str, date: str) -> Optional[Dict[str, float]]:
        return self.rs_metrics.get(str(date), {}).get(str(symbol))

    def select(
        self,
        symbol: str,
        date: str,
        time: str,
        prev_day_ohlc: Optional[Mapping[str, float]] = None,
    ) -> Dict[str, Any]:
        """Select the best setup at a known (symbol, date, time) bar."""
        frame = self.frames.get(str(symbol))
        if frame is None:
            return _inactive(f"Unknown symbol {symbol}")
        i = self.index.get((str(symbol), str(date), str(time)))
        if i is None:
            return _inactive("Bar not present in universe")
        date_str = str(date)
        session_mask = (frame["date"].astype(str) == date_str) & (frame.index <= i)
        session_bars = frame[session_mask]
        feature_frame = frame.iloc[max(0, i - 1): i + 1]
        return select_fee_aware_setup(
            session_bars,
            prev_day_ohlc=prev_day_ohlc,
            interval=self.interval,
            metrics=self.metrics_for(symbol, date_str),
            feature_frame=feature_frame,
            strategy_order=self.strategy_order,
        )

    def override(self, history: List[Dict[str, Any]], prev_day_ohlc: Any, interval: str) -> Dict[str, Any]:
        """Backtester ``strategy_override`` adapter.

        ``history`` is the day-only slice the backtester hands over; the symbol
        is read from the (tagged) last record and the universe index is used to
        recover the continuous feature context.
        """
        if not history:
            return _inactive("Empty history")
        rec = history[-1]
        sym = str(rec.get("symbol", ""))
        date = str(rec.get("date", ""))
        time = str(rec.get("time", ""))
        return self.select(sym, date, time, prev_day_ohlc=prev_day_ohlc)


__all__ = [
    "DEFAULT_STRATEGY_ORDER",
    "FeeAwareSelector",
    "select_fee_aware_setup",
]
