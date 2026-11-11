"""Fee-aware, high-win-rate intraday strategies for NSE equities.

These strategies exist because the honest 1-year/1-hour validation of the
shipped primary edge (`momentum_continuation`) is fee-negative: it holds to
the close and therefore never banks a move that clears the round-trip Indian
statutory cost floor (~0.146% of position value). The two setups here were
selected by walk-forward validation on cached real NSE bars:

  * ``FeeAwareOpeningPullback`` (1h bars). Opening-session pullback to a
    rising 20-EMA in an established uptrend, entered on the session's first
    completed bar. Walk-forward: 13 trades, 92.3% win, train 91.7% / test
    100%, net +\u20b9518 on \u20b97,895 average position. Small sample; treated as
    the primary high-conviction setup, not a proven 90%+ system.

  * ``LiquiditySweepLong`` (5m bars). Bullish sweep of the prior day's low
    followed by a reclaim, i.e. a stop-hunt reversal. 3-month sample:
    31 trades, 80.6% win, net +\u20b965 on \u20b92,800 positions.

Both strategies express their geometry as PERCENTAGES of the eventual fill
(``stop_pct`` / ``target_pct``) rather than absolute prices, because the
backtester fills on the NEXT bar's open. They also publish ``ratchet_levels``:
a ratchet that raises the stop to a fee-clearing lock once price advances,
which converts would-be scratch trades into small net winners.

This module is intentionally standalone: it does not modify
``intraday_strategies.py`` and so live 5m behaviour is unchanged unless a
caller explicitly routes through these evaluators.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd


# Round-trip Indian statutory cost floor as a percentage of position value,
# used as the minimum a profit-lock must clear to be a real (net) lock.
FEE_FLOOR_PCT = 0.146


def compute_features(df: pd.DataFrame, atr_window: int = 14) -> pd.DataFrame:
    """Add the indicator columns the fee-aware strategies need.

    ``df`` must carry ``open``/``high``/``low``/``close`` columns and be sorted
    ascending in time. Indicators are computed on the continuous series (not
    per session) so the EMAs and ATR are well defined on the first bar of a
    session.
    """
    out = df.copy()
    out["ema_20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema_50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema50_slope"] = out["ema_50"].pct_change() * 100.0

    high_low = out["high"] - out["low"]
    high_close = (out["high"] - out["close"].shift(1)).abs()
    low_close = (out["low"] - out["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(window=atr_window, min_periods=1).mean()
    out["atr_pct"] = out["atr_14"] / out["close"].replace(0, np.nan) * 100.0

    out["body_pct"] = (out["close"] - out["open"]) / out["open"].replace(0, np.nan) * 100.0
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    out["lower_wick_ratio"] = (out[["open", "close"]].min(axis=1) - out["low"]) / rng
    out["upper_wick_ratio"] = (out["high"] - out[["open", "close"]].max(axis=1)) / rng

    # Wilder-style 14-period RSI on the continuous series (no session reset so
    # the first bar of a session still has an RSI estimate available).
    delta = out["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(window=14, min_periods=1).mean()
    avg_loss = loss.rolling(window=14, min_periods=1).mean().replace(0.0, 1e-9)
    out["rsi_14"] = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    return out


def _inactive(reason: str) -> Dict[str, Any]:
    return {"active": False, "signal": "HOLD", "reason": reason}


def _ratchet_levels(levels: List[Mapping[str, float]]) -> List[Dict[str, float]]:
    """Validate and normalize ratchet levels (trigger_pct asc, positive locks)."""
    clean = [
        {"trigger_pct": float(l["trigger_pct"]), "lock_pct": float(l["lock_pct"])}
        for l in levels
    ]
    clean.sort(key=lambda l: l["trigger_pct"])
    for level in clean:
        if level["trigger_pct"] <= 0 or level["lock_pct"] <= 0:
            raise ValueError(f"ratchet level must be positive: {level}")
        if level["lock_pct"] >= level["trigger_pct"]:
            raise ValueError(f"ratchet lock must be below its trigger: {level}")
    return clean


class FeeAwareOpeningPullback:
    """Opening-session pullback to a rising 20-EMA, with a fee-clearing ratchet.

    Evaluated on the session's FIRST completed bar (09:15 for hourly bars).
    The evaluator expects a continuous frame whose LAST row is that first bar,
    so the EMA/ATR context is fully populated.
    """

    STRATEGY_ID = "opening_ema_pullback"
    NAME = "Opening EMA-20 Pullback (fee-aware)"

    # Validated on 1y / 1h NSE bars (walk-forward: 13 trades, 92.3% win).
    DEFAULTS: Dict[str, Any] = {
        "entry_time": "09:15",
        "atr_pct_min": 0.75,
        "ema50_slope_min": 0.05,
        "ema_proximity": 1.002,
        "body_min": 0.30,
        "body_max": 2.20,
        "lower_wick_min": 0.035,
        "upper_wick_max": 0.15,
        "stop_pct": 0.95,
        "target_pct": 0.90,
        "ratchet_levels": [{"trigger_pct": 0.28, "lock_pct": 0.16}],
        "confidence": 88,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        interval: str = "1h",
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        if df is None or len(df) < 2:
            return _inactive("Insufficient bars")

        feat = df if "ema50_slope" in df.columns else compute_features(df)
        last = feat.iloc[-1]

        if str(last.get("time", "")) != str(p["entry_time"]):
            return _inactive("Not the session opening bar")

        ema_20 = float(last["ema_20"])
        ema_50 = float(last["ema_50"])
        close = float(last["close"])
        low = float(last["low"])
        atr_pct = float(last.get("atr_pct", 0.0) or 0.0)
        slope = float(last.get("ema50_slope", 0.0) or 0.0)
        body = float(last.get("body_pct", 0.0) or 0.0)
        lower_wick = float(last.get("lower_wick_ratio", 0.0) or 0.0)
        upper_wick = float(last.get("upper_wick_ratio", 0.0) or 0.0)

        if ema_50 <= 0 or close <= 0:
            return _inactive("Invalid price context")
        if atr_pct < p["atr_pct_min"]:
            return _inactive(f"Volatility too low (atr {atr_pct:.2f}% < {p['atr_pct_min']:.2f}%)")
        if ema_20 < ema_50:
            return _inactive("EMA-20 below EMA-50 (no uptrend)")
        if slope < p["ema50_slope_min"]:
            return _inactive(f"EMA-50 not rising (slope {slope:.3f} < {p['ema50_slope_min']:.3f})")
        if low > ema_20 * p["ema_proximity"]:
            return _inactive("Price did not pull back to the 20-EMA")
        if close < ema_20:
            return _inactive("Opening close below the 20-EMA")
        if not (p["body_min"] <= body <= p["body_max"]):
            return _inactive(f"Body {body:.2f}% outside [{p['body_min']}, {p['body_max']}]")
        if lower_wick < p["lower_wick_min"]:
            return _inactive("No supportive lower wick")
        if upper_wick > p["upper_wick_max"]:
            return _inactive("Upper rejection wick too large")

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": round(close, 2),
            "stop_pct": float(p["stop_pct"]),
            "target_pct": float(p["target_pct"]),
            "ratchet_levels": _ratchet_levels(p["ratchet_levels"]),
            "hold_to_close": False,
            "confidence": int(p["confidence"]),
            "rationale": (
                f"Opening pullback: {body:+.2f}% body holding the 20-EMA "
                f"(\u20b9{ema_20:.2f}) with EMA-50 rising {slope:.3f}% and ATR {atr_pct:.2f}%. "
                f"Fee-clearing target +{p['target_pct']:.2f}%, stop -{p['stop_pct']:.2f}%."
            ),
        }


class LiquiditySweepLong:
    """Bullish sweep of the prior day's low followed by a reclaim (stop-hunt).

    Evaluated on 5m bars inside the 09:30-14:00 window, excluding the midday
    lull. Entry is taken on the next bar's open by the executor.
    """

    STRATEGY_ID = "pdl_liquidity_sweep"
    NAME = "Prior-Day-Low Liquidity Sweep (long)"

    # Validated on 3 months of real 5m NSE bars, next-bar fills + fees.
    DEFAULTS: Dict[str, Any] = {
        "start_time": "09:30",
        "end_time": "14:00",
        "lull_start": "11:30",
        "lull_end": "13:15",
        "atr_pct_min": 0.18,
        "lower_wick_min": 0.42,
        "body_min": 0.0,
        "stop_pct": 0.60,
        "target_pct": 0.90,
        "ratchet_levels": [
            {"trigger_pct": 0.32, "lock_pct": 0.20},
            {"trigger_pct": 0.48, "lock_pct": 0.32},
            {"trigger_pct": 0.65, "lock_pct": 0.48},
        ],
        "confidence": 82,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        prev_day_ohlc: Optional[Mapping[str, float]] = None,
        interval: str = "5m",
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        if df is None or len(df) < 2:
            return _inactive("Insufficient bars")
        if not prev_day_ohlc or not prev_day_ohlc.get("low"):
            return _inactive("Prior-day low unavailable")

        feat = df if "lower_wick_ratio" in df.columns else compute_features(df)
        last = feat.iloc[-1]
        t = str(last.get("time", ""))
        if t and (t < p["start_time"] or t > p["end_time"]):
            return _inactive("Outside sweep window")
        if t and (p["lull_start"] <= t <= p["lull_end"]):
            return _inactive("Midday lull")

        pdl = float(prev_day_ohlc["low"])
        low = float(last["low"])
        close = float(last["close"])
        atr_pct = float(last.get("atr_pct", 0.0) or 0.0)
        body = float(last.get("body_pct", 0.0) or 0.0)
        lower_wick = float(last.get("lower_wick_ratio", 0.0) or 0.0)

        if pdl <= 0:
            return _inactive("Invalid prior-day low")
        if not (low < pdl and close > pdl):
            return _inactive("No prior-day-low sweep and reclaim")
        if lower_wick < p["lower_wick_min"]:
            return _inactive("Reclaim wick too small")
        if body <= p["body_min"]:
            return _inactive("Reclaim bar is not green")
        if atr_pct < p["atr_pct_min"]:
            return _inactive(f"Volatility too low (atr {atr_pct:.2f}%)")

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": round(close, 2),
            "stop_pct": float(p["stop_pct"]),
            "target_pct": float(p["target_pct"]),
            "ratchet_levels": _ratchet_levels(p["ratchet_levels"]),
            "hold_to_close": False,
            "confidence": int(p["confidence"]),
            "or_high": round(pdl, 2),
            "rationale": (
                f"Prior-day-low sweep at \u20b9{pdl:.2f} reclaimed above it "
                f"(close \u20b9{close:.2f}, lower wick {lower_wick * 100:.0f}%). "
                f"Target +{p['target_pct']:.2f}%, stop -{p['stop_pct']:.2f}%, staged ratchet."
            ),
        }


class LiquiditySweepShort:
    """Bearish sweep of the prior day's high followed by a rejection (stop-hunt).

    The mirror of ``LiquiditySweepLong``. Retail breakout buyers push price
    above the prior-day high (PDH), the move fails, and price closes back
    below the level on a long upper wick. NSE cash intraday (MIS) permits
    short-selling equities, so this unlocks profit on red/chop days where
    long-only book is idle. Evaluated on 5m bars inside 09:30-14:00 IST,
    excluding the midday lull; entry is taken on the next bar's open.
    """

    STRATEGY_ID = "pdh_liquidity_sweep_short"
    NAME = "Prior-Day-High Liquidity Sweep (short)"

    DEFAULTS: Dict[str, Any] = {
        "start_time": "09:30",
        "end_time": "14:00",
        "lull_start": "11:30",
        "lull_end": "13:15",
        "atr_pct_min": 0.18,
        "upper_wick_min": 0.42,
        "body_max": -0.025,
        "rsi14_min": 55.0,
        "strong_displacement_pct": -0.40,
        "stop_pct": 0.65,
        "target_pct": 0.95,
        "ratchet_levels": [
            {"trigger_pct": 0.32, "lock_pct": 0.20},
            {"trigger_pct": 0.48, "lock_pct": 0.32},
            {"trigger_pct": 0.65, "lock_pct": 0.48},
        ],
        "confidence": 81,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        prev_day_ohlc: Optional[Mapping[str, float]] = None,
        interval: str = "5m",
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        if df is None or len(df) < 2:
            return _inactive("Insufficient bars")
        if not prev_day_ohlc or not prev_day_ohlc.get("high"):
            return _inactive("Prior-day high unavailable")

        feat = df if "upper_wick_ratio" in df.columns else compute_features(df)
        last = feat.iloc[-1]
        t = str(last.get("time", ""))
        if t and (t < p["start_time"] or t > p["end_time"]):
            return _inactive("Outside sweep window")
        if t and (p["lull_start"] <= t <= p["lull_end"]):
            return _inactive("Midday lull")

        pdh = float(prev_day_ohlc["high"])
        high = float(last["high"])
        close = float(last["close"])
        atr_pct = float(last.get("atr_pct", 0.0) or 0.0)
        body = float(last.get("body_pct", 0.0) or 0.0)
        upper_wick = float(last.get("upper_wick_ratio", 0.0) or 0.0)
        rsi_14 = float(last.get("rsi_14", 0.0) or 0.0)

        if pdh <= 0:
            return _inactive("Invalid prior-day high")
        if not (high > pdh and close < pdh):
            return _inactive("No prior-day-high sweep and rejection")
        if upper_wick < p["upper_wick_min"]:
            return _inactive("Rejection wick too small")
        if body > p["body_max"]:
            return _inactive("Rejection bar is not red")
        # Momentum confirmation: overbought RSI OR a strong bearish displacement.
        if rsi_14 < p["rsi14_min"] and body > float(p["strong_displacement_pct"]):
            return _inactive("No overbought RSI or strong bearish displacement")
        if atr_pct < p["atr_pct_min"]:
            return _inactive(f"Volatility too low (atr {atr_pct:.2f}%)")

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "SELL",
            "entry_price": round(close, 2),
            "stop_pct": float(p["stop_pct"]),
            "target_pct": float(p["target_pct"]),
            "ratchet_levels": _ratchet_levels(p["ratchet_levels"]),
            "hold_to_close": False,
            "confidence": int(p["confidence"]),
            "or_high": round(pdh, 2),
            "rationale": (
                f"Prior-day-high sweep at \u20b9{pdh:.2f} rejected back below it "
                f"(close \u20b9{close:.2f}, upper wick {upper_wick * 100:.0f}%, "
                f"RSI-14 {rsi_14:.1f}). Target -{p['target_pct']:.2f}%, "
                f"stop +{p['stop_pct']:.2f}%, staged downward ratchet."
            ),
        }


def compute_cross_sectional_metrics(
    universe: Mapping[str, pd.DataFrame],
    index_df: Optional[pd.DataFrame] = None,
    session_date: Optional[str] = None,
    open_cutoff: str = "09:45",
    rvol_lookback_days: int = 20,
) -> Dict[str, Dict[str, float]]:
    """Rank an intraday universe by relative strength (RS) and relative volume.

    For each symbol this computes, for the requested session:

      * ``rs_30m_pct``: first-30m return minus the market's first-30m return
        (the market being ``index_df`` when supplied, else the cross-sectional
        median first-30m return).
      * ``rvol``: first-30m volume divided by the mean first-30m volume of the
        preceding ``rvol_lookback_days`` sessions.
      * ``rs_rank_pct``: percentile rank of ``rs_30m_pct`` within the universe
        (0-100, higher = stronger).

    Symbol frames need ``open``/``high``/``low``/``close``/``volume`` and a
    ``time`` column (HH:MM) plus a ``date`` column. When ``session_date`` is
    None the most recent session present in the frames is used.
    """
    metrics: Dict[str, Dict[str, float]] = {}
    first_returns: Dict[str, float] = {}

    def _session_first_return(frame: pd.DataFrame, date: str) -> Optional[float]:
        day = frame[frame["date"] == date]
        if day.empty:
            return None
        open_row = day[day["time"] <= open_cutoff]
        if open_row.empty:
            return None
        first_open = float(open_row.iloc[0]["open"])
        cutoff_close = float(open_row.iloc[-1]["close"])
        if first_open <= 0:
            return None
        return (cutoff_close / first_open - 1.0) * 100.0

    def _first_volumes(frame: pd.DataFrame) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for date, day in frame.groupby("date"):
            window = day[day["time"] <= open_cutoff]
            if not window.empty:
                out[str(date)] = float(window["volume"].sum())
        return out

    # Determine the session date if not supplied.
    if session_date is None:
        dates = {str(d) for frame in universe.values() for d in frame["date"].unique()}
        if not dates:
            return metrics
        session_date = max(dates)

    for sym, frame in universe.items():
        ret = _session_first_return(frame, session_date)
        if ret is None:
            continue
        first_returns[sym] = ret
        vols = _first_volumes(frame)
        ordered_days = sorted(vols)
        if session_date in ordered_days:
            prior = [vols[d] for d in ordered_days if d < session_date]
            prior = prior[-rvol_lookback_days:]
            avg_vol = sum(prior) / len(prior) if prior else 0.0
            rvol = (vols[session_date] / avg_vol) if avg_vol > 0 else 1.0
        else:
            rvol = 1.0
        metrics[sym] = {"rs_30m_pct": round(ret, 3), "rvol": round(rvol, 2)}

    if not metrics:
        return metrics

    # Market return: index if available, else cross-sectional median.
    if index_df is not None and "date" in index_df.columns:
        market_ret = _session_first_return(index_df, session_date)
    else:
        market_ret = None
    if market_ret is None:
        sorted_rets = sorted(first_returns.values())
        market_ret = sorted_rets[len(sorted_rets) // 2] if sorted_rets else 0.0

    # Relative strength and percentile rank.
    rs_values = {s: first_returns[s] - market_ret for s in first_returns}
    ordered = sorted(rs_values, key=lambda s: rs_values[s])
    n = len(ordered)
    for rank, sym in enumerate(ordered):
        pct = (rank / (n - 1) * 100.0) if n > 1 else 100.0
        metrics[sym]["rs_30m_pct"] = round(rs_values[sym], 3)
        metrics[sym]["rs_rank_pct"] = round(pct, 1)
    return metrics


class RelativeStrengthLeaderBreakout:
    """Cross-sectional relative-strength + RVOL intraday momentum continuation.

    Solves the Iteration-110 failure where tuning indicator thresholds on a
    fixed 8-stock list did not generalise to a broad 30-stock universe. Instead
    of trading every stock, this strategy only fires on a stock that is:

      1. An ``in-play`` leader: 30-minute relative strength vs. the market
         (``rs_30m_pct >= rs_min``) and relative volume ``rvol >= rvol_min``;
         and optionally in the top ``rs_rank_min_pct`` of the universe.
      2. Breaking the 30-minute opening range high, and
      3. Showing a first controlled pullback that holds VWAP / EMA-9, then a
         green reclaim candle on rising volume.

    Cross-sectional metrics are computed by :func:`compute_cross_sectional_metrics`
    and passed in; the evaluator itself is a pure function of the session's
    intraday bars plus those metrics, which keeps it unit-testable.
    """

    STRATEGY_ID = "rs_rvol_leader_breakout"
    NAME = "Relative-Strength + RVOL Leader Breakout"

    DEFAULTS: Dict[str, Any] = {
        "or_cutoff": "09:45",
        "rs_min_pct": 0.60,
        "rvol_min": 1.80,
        "rs_rank_min_pct": 75.0,
        "max_vwap_extension_pct": 0.60,
        "stop_pct": 0.60,
        "target_pct": 1.50,
        "ratchet_levels": [
            {"trigger_pct": 0.45, "lock_pct": 0.22},
            {"trigger_pct": 0.80, "lock_pct": 0.55},
            {"trigger_pct": 1.15, "lock_pct": 0.90},
        ],
        "confidence": 87,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        rs_30m_pct: float,
        rvol: float,
        rs_rank_pct: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        if df is None or len(df) < 3:
            return _inactive("Insufficient session bars")
        # The caller passes one session's bars (ascending). Guard the contract.
        session = df
        if "date" in session.columns:
            last_date = str(session.iloc[-1]["date"])
            session = session[session["date"] == last_date]
        if len(session) < 3:
            return _inactive("Insufficient bars in session")

        if float(rs_30m_pct) < float(p["rs_min_pct"]):
            return _inactive(f"RS {float(rs_30m_pct):.2f}% below {p['rs_min_pct']}%")
        if float(rvol) < float(p["rvol_min"]):
            return _inactive(f"RVOL {float(rvol):.2f} < {p['rvol_min']}")
        if rs_rank_pct is not None and float(rs_rank_pct) < float(p["rs_rank_min_pct"]):
            return _inactive(f"RS rank {float(rs_rank_pct):.1f} below top-quartile gate")

        session = session.reset_index(drop=True)
        opening = session[session["time"] <= p["or_cutoff"]]
        if opening.empty:
            return _inactive("No opening-range bars")
        or_high = float(opening["high"].max())

        # Session VWAP and 9-EMA computed on the bars seen so far.
        typical = (session["high"].astype(float) + session["low"].astype(float)
                   + session["close"].astype(float)) / 3.0
        vol = session["volume"].astype(float).replace(0, np.nan).fillna(0.0)
        cum_vol = vol.cumsum()
        vwap_series = (typical * vol).cumsum() / cum_vol.replace(0, np.nan)
        vwap = float(vwap_series.iloc[-1]) if np.isfinite(vwap_series.iloc[-1]) else float(session["close"].iloc[-1])
        ema9 = float(session["close"].astype(float).ewm(span=9, adjust=False).mean().iloc[-1])

        # Require a genuine breakout above the 30-minute ORH earlier in the day.
        if float(session["high"].max()) <= or_high:
            return _inactive("Opening range high never broken")

        last = session.iloc[-1]
        close = float(last["close"])
        open_px = float(last["open"])
        body = close - open_px
        last_vol = float(last["volume"])
        prev_vol = float(session.iloc[-2]["volume"])

        if close <= vwap:
            return _inactive("Reclaim close below VWAP")
        if close < ema9:
            return _inactive("Reclaim close below EMA-9")
        if body <= 0:
            return _inactive("Reclaim bar is not green")
        if last_vol < prev_vol:
            return _inactive("Reclaim bar volume not expanding")
        extension = (close / vwap - 1.0) * 100.0
        if extension > float(p["max_vwap_extension_pct"]):
            return _inactive(f"Over-extended from VWAP ({extension:.2f}%)")

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": round(close, 2),
            "stop_pct": float(p["stop_pct"]),
            "target_pct": float(p["target_pct"]),
            "ratchet_levels": _ratchet_levels(p["ratchet_levels"]),
            "hold_to_close": False,
            "confidence": int(p["confidence"]),
            "or_high": round(or_high, 2),
            "vwap": round(vwap, 2),
            "rationale": (
                f"RS leader (+{float(rs_30m_pct):.2f}% vs market), RVOL {float(rvol):.2f}x; "
                f"reclaimed VWAP \u20b9{vwap:.2f}/EMA-9 \u20b9{ema9:.2f} above ORH-30m "
                f"\u20b9{or_high:.2f} on expanding volume."
            ),
        }


fee_aware_opening_pullback = FeeAwareOpeningPullback()
fee_aware_liquidity_sweep = LiquiditySweepLong()
fee_aware_liquidity_sweep_short = LiquiditySweepShort()
rs_leader_breakout = RelativeStrengthLeaderBreakout()
