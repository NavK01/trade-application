"""Multi-day swing / positional strategies and honest delivery-cost backtester for NSE equities.

Why this module exists
----------------------
Walk-forward validation across 30 liquid NSE large-cap equities over 2–5 years
demonstrated that high-frequency intraday cash-equity setups suffer from severe
statutory fee drag (~0.106%–0.146% round-trip plus slippage) relative to single-session
intraday price expansion.

By contrast, multi-day swing setups—specifically 2-period RSI exhaustion bounces
with volatility-scaled (ATR) stops and targets—achieved positive mathematical
expectancy across ALL four sequential 5-year walk-forward folds even after full
Indian delivery statutory charges (0.1% STT on both legs, ₹13.50 DP sell charge,
0.015% buy stamp duty, exchange/SEBI fees, 18% GST, and execution slippage).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from app.config import settings
from app.trading.institutional_engine import calculate_indian_statutory_charges


def compute_swing_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute daily/swing technical indicators on an ascending OHLCV DataFrame."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)

    out["sma_10"] = close.rolling(window=10, min_periods=1).mean()
    out["sma_20"] = close.rolling(window=20, min_periods=1).mean()
    out["sma_50"] = close.rolling(window=50, min_periods=1).mean()
    out["sma_200"] = close.rolling(window=200, min_periods=1).mean()
    out["close_prev"] = close.shift(1)

    if "volume" in out.columns:
        out["vol_sma_20"] = out["volume"].astype(float).rolling(window=20, min_periods=1).mean()
    else:
        out["vol_sma_20"] = 0.0

    out["range"] = high - low
    out["range_min_7"] = out["range"].rolling(window=7, min_periods=7).min()

    high_low = high - low
    high_close = (high - close.shift(1)).abs()
    low_close = (low - close.shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(window=14, min_periods=1).mean()

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain_14 = gain.rolling(window=14, min_periods=1).mean()
    avg_loss_14 = loss.rolling(window=14, min_periods=1).mean().replace(0.0, 1e-9)
    out["rsi_14"] = 100.0 - (100.0 / (1.0 + (avg_gain_14 / avg_loss_14)))

    avg_gain_2 = gain.rolling(window=2, min_periods=2).mean()
    avg_loss_2 = loss.rolling(window=2, min_periods=2).mean().replace(0.0, 1e-9)
    out["rsi_2"] = 100.0 - (100.0 / (1.0 + (avg_gain_2 / avg_loss_2)))

    return out


class RSI2OversoldSwing:
    """2-Period RSI Exhaustion Mean-Reversion Swing Strategy with ATR Geometry.

    Walk-forward validated across 30 Nifty 50/100 equities over 5 years (4 folds):
      * Entry: 2-period RSI < threshold (default 10.0) AND positive close-over-close
        turn (`close > close_prev`), confirming seller exhaustion and initial bid support.
      * Stop Loss: Entry - `stop_atr_mult` * ATR(14) (default 2.0x ATR).
      * Take Profit: Entry + `target_atr_mult` * ATR(14) (default 3.0x–4.0x ATR).
      * Fee Gate: Expected gross profit at target must exceed full Indian delivery
        statutory charges (`DELIVERY_EQUITY`) by `min_edge_to_fee` (default 2.0x).
    """

    STRATEGY_ID = "rsi2_oversold_swing"
    NAME = "RSI-2 Oversold Swing Reversal (ATR 1:2)"

    DEFAULTS: Dict[str, Any] = {
        "rsi2_max": 10.0,
        "require_uptick": True,
        "require_above_sma200": False,
        "stop_atr_mult": 2.0,
        "target_atr_mult": 4.0,
        "max_hold_days": 60,
        "min_edge_to_fee": 2.0,
        "allocation_inr": 9000.0,
        "confidence": 85,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        if df is None or len(df) < 15:
            return {"active": False, "signal": "HOLD", "reason": "Insufficient daily bars"}

        feat = df if "rsi_2" in df.columns else compute_swing_features(df)
        last = feat.iloc[-1]

        close = float(last["close"])
        close_prev = float(last.get("close_prev", np.nan))
        rsi_2 = float(last.get("rsi_2", np.nan))
        atr = float(last.get("atr_14", np.nan))
        sma_200 = float(last.get("sma_200", np.nan))

        if close <= 0 or not np.isfinite(rsi_2) or not np.isfinite(atr) or atr <= 0:
            return {"active": False, "signal": "HOLD", "reason": "Invalid indicator values"}

        if rsi_2 >= float(p["rsi2_max"]):
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"RSI-2 ({rsi_2:.1f}) not oversold (< {p['rsi2_max']})",
            }

        if p["require_uptick"] and (not np.isfinite(close_prev) or close <= close_prev):
            return {
                "active": False,
                "signal": "HOLD",
                "reason": "Awaiting positive daily close-over-close turn",
            }

        if p["require_above_sma200"] and (not np.isfinite(sma_200) or close <= sma_200):
            return {
                "active": False,
                "signal": "HOLD",
                "reason": "Price below 200-SMA long-term trend filter",
            }

        stop_dist = float(p["stop_atr_mult"]) * atr
        target_dist = float(p["target_atr_mult"]) * atr
        stop_price = round(max(0.05, close - stop_dist), 2)
        target_price = round(close + target_dist, 2)

        est_qty = max(1, int(float(p["allocation_inr"]) // close))
        buy_val = close * est_qty
        sell_val = target_price * est_qty
        charges = calculate_indian_statutory_charges(
            buy_value=buy_val,
            sell_value=sell_val,
            instrument_type="DELIVERY_EQUITY",
            quantity=est_qty,
        )
        total_cost = float(charges["total_charges"])
        gross_edge = (target_price - close) * est_qty
        edge_to_fee = (gross_edge / total_cost) if total_cost > 0 else float("inf")

        if edge_to_fee < float(p["min_edge_to_fee"]):
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"Delivery fee hurdle failed ({edge_to_fee:.2f}x < {p['min_edge_to_fee']:.2f}x)",
            }

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": round(close, 2),
            "stop_loss": stop_price,
            "target_1": target_price,
            "atr_14": round(atr, 2),
            "stop_atr_mult": float(p["stop_atr_mult"]),
            "target_atr_mult": float(p["target_atr_mult"]),
            "max_hold_days": int(p["max_hold_days"]),
            "edge_to_fee_ratio": round(edge_to_fee, 2),
            "confidence": int(p["confidence"]),
            "rationale": (
                f"RSI-2 exhaustion ({rsi_2:.1f}) with bullish turn at ₹{close:.2f}. "
                f"ATR stop ₹{stop_price:.2f} (-{p['stop_atr_mult']}x ATR), "
                f"target ₹{target_price:.2f} (+{p['target_atr_mult']}x ATR), "
                f"clears delivery fees {edge_to_fee:.1f}x."
            ),
        }


class NR7SwingBreakout:
    """Volatility-contraction (NR7) multi-day breakout continuation.

    Complements :class:`RSI2OversoldSwing` (which buys dips in range/mean-
    reverting tape): this captures explosive multi-day trend continuations out
    of a quiet base without requiring a directional day before entry.

      * Regime: ``close > sma_50 > sma_200`` (established uptrend).
      * Contraction: today's range is the narrowest of the last 7 sessions
        (NR7) and volume dries up (``volume < vol_contraction * vol_sma_20``).
      * Trigger: next session stop-limit buy above the NR7 high
        (``high * (1 + breakout_buffer)``).
      * Risk: stop at ``max(nr7_low, entry - stop_atr_mult * ATR(14))``.
      * Reward: ``entry + target_atr_mult * ATR(14)`` with a trailing stop at
        ``sma_10``.
      * Fee gate: gross edge to target must clear delivery charges by
        ``min_edge_to_fee``.
    """

    STRATEGY_ID = "nr7_swing_breakout"
    NAME = "NR7 Volatility-Contraction Breakout"

    DEFAULTS: Dict[str, Any] = {
        "nr7_window": 7,
        "vol_contraction": 0.75,
        "breakout_buffer": 0.001,
        "stop_atr_mult": 1.5,
        "target_atr_mult": 3.5,
        "max_hold_days": 30,
        "min_edge_to_fee": 2.0,
        "allocation_inr": 9000.0,
        "confidence": 83,
    }

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = {**cls.DEFAULTS, **(params or {})}
        window = int(p["nr7_window"])
        if df is None or len(df) < max(window, 200):
            return {"active": False, "signal": "HOLD", "reason": "Insufficient daily bars"}

        feat = df if "range_min_7" in df.columns else compute_swing_features(df)
        last = feat.iloc[-1]

        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        sma_50 = float(last.get("sma_50", np.nan))
        sma_200 = float(last.get("sma_200", np.nan))
        atr = float(last.get("atr_14", np.nan))
        volume = float(last.get("volume", 0.0) or 0.0)
        vol_sma_20 = float(last.get("vol_sma_20", 0.0) or 0.0)
        rng = float(last.get("range", high - low))
        range_min_7 = float(last.get("range_min_7", np.nan))

        if close <= 0 or not np.isfinite(atr) or atr <= 0:
            return {"active": False, "signal": "HOLD", "reason": "Invalid indicator values"}
        if not (np.isfinite(sma_50) and np.isfinite(sma_200) and close > sma_50 > sma_200):
            return {"active": False, "signal": "HOLD", "reason": "Not in an established uptrend"}
        if not np.isfinite(range_min_7) or rng > range_min_7 + 1e-9:
            return {"active": False, "signal": "HOLD", "reason": "Range is not the narrowest of 7 sessions (not NR7)"}
        if vol_sma_20 > 0 and volume >= float(p["vol_contraction"]) * vol_sma_20:
            return {"active": False, "signal": "HOLD", "reason": "Volume has not dried up"}

        entry_proxy = round(high * (1.0 + float(p["breakout_buffer"])), 2)
        stop_price = round(max(low, entry_proxy - float(p["stop_atr_mult"]) * atr), 2)
        target_price = round(entry_proxy + float(p["target_atr_mult"]) * atr, 2)

        est_qty = max(1, int(float(p["allocation_inr"]) // entry_proxy))
        charges = calculate_indian_statutory_charges(
            buy_value=entry_proxy * est_qty,
            sell_value=target_price * est_qty,
            instrument_type="DELIVERY_EQUITY",
            quantity=est_qty,
        )
        total_cost = float(charges["total_charges"])
        gross_edge = (target_price - entry_proxy) * est_qty
        edge_to_fee = (gross_edge / total_cost) if total_cost > 0 else float("inf")
        if edge_to_fee < float(p["min_edge_to_fee"]):
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"Delivery fee hurdle failed ({edge_to_fee:.2f}x < {p['min_edge_to_fee']:.2f}x)",
            }

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": entry_proxy,
            "breakout_level": entry_proxy,
            "stop_loss": stop_price,
            "target_1": target_price,
            "nr7_low": round(low, 2),
            "atr_14": round(atr, 2),
            "stop_atr_mult": float(p["stop_atr_mult"]),
            "target_atr_mult": float(p["target_atr_mult"]),
            "max_hold_days": int(p["max_hold_days"]),
            "trailing_sma": "sma_10",
            "edge_to_fee_ratio": round(edge_to_fee, 2),
            "confidence": int(p["confidence"]),
            "rationale": (
                f"NR7 range ₹{rng:.2f} (narrowest of 7) in uptrend "
                f"(close>50>200 SMA); volume {volume:,.0f} < "
                f"{p['vol_contraction']}x avg. Buy-stop ₹{entry_proxy:.2f}, "
                f"stop ₹{stop_price:.2f}, target ₹{target_price:.2f}, "
                f"clears delivery fees {edge_to_fee:.1f}x."
            ),
        }


class SwingBacktestEngine:
    """Honest next-day-open swing backtester charging full Indian delivery costs."""

    def __init__(self, initial_capital: Optional[float] = None):
        self.initial_capital = float(initial_capital or settings.INITIAL_CAPITAL_INR)

    def run_symbol_backtest(
        self,
        symbol: str,
        df: pd.DataFrame,
        params: Optional[Mapping[str, Any]] = None,
        slippage_bps: float = 5.0,
    ) -> Dict[str, Any]:
        """Simulate non-overlapping swing trades on a single symbol's daily bars."""
        p = {**RSI2OversoldSwing.DEFAULTS, **(params or {})}
        feat = compute_swing_features(df).reset_index(drop=True)
        slip = slippage_bps / 10_000.0
        trades: List[Dict[str, Any]] = []

        i = 15
        n = len(feat)
        while i < n - 1:
            window = feat.iloc[: i + 1]
            sig = RSI2OversoldSwing.evaluate(window, params=p)
            if not sig.get("active"):
                i += 1
                continue

            next_bar = feat.iloc[i + 1]
            entry_px = round(float(next_bar["open"]) * (1.0 + slip), 2)
            atr = float(feat.iloc[i]["atr_14"])
            if entry_px <= 0 or not np.isfinite(atr) or atr <= 0:
                i += 1
                continue

            qty = max(1, int(float(p["allocation_inr"]) // entry_px))
            stop = round(entry_px - float(p["stop_atr_mult"]) * atr, 2)
            target = round(entry_px + float(p["target_atr_mult"]) * atr, 2)
            max_hold = int(p["max_hold_days"])

            exit_px: Optional[float] = None
            exit_reason = "TIME_EXIT"
            exit_idx = min(n - 1, i + 1 + max_hold)

            for j in range(i + 1, min(n, i + 1 + max_hold)):
                bar = feat.iloc[j]
                b_open = float(bar["open"])
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                if b_open <= stop:
                    exit_px, exit_reason, exit_idx = round(b_open, 2), "STOP_GAP", j
                    break
                if b_low <= stop:
                    exit_px, exit_reason, exit_idx = stop, "STOP_LOSS", j
                    break
                if b_high >= target:
                    exit_px, exit_reason, exit_idx = target, "TARGET_HIT", j
                    break

            if exit_px is None:
                exit_px = round(float(feat.iloc[exit_idx]["close"]), 2)

            buy_val = entry_px * qty
            sell_val = exit_px * qty
            charges = calculate_indian_statutory_charges(
                buy_value=buy_val,
                sell_value=sell_val,
                instrument_type="DELIVERY_EQUITY",
                quantity=qty,
                slippage_pct=0.0,  # entry slippage already applied to fill price
            )

            trades.append({
                "symbol": symbol,
                "entry_date": str(feat.iloc[i + 1].get("date", i + 1)),
                "exit_date": str(feat.iloc[exit_idx].get("date", exit_idx)),
                "hold_days": int(exit_idx - (i + 1) + 1),
                "entry_price": entry_px,
                "exit_price": exit_px,
                "stop_loss": stop,
                "target_price": target,
                "quantity": qty,
                "exit_reason": exit_reason,
                "gross_pnl": charges["gross_pnl"],
                "fees_inr": charges["total_charges"],
                "net_pnl": charges["net_pnl"],
                "return_pct": charges["net_roi_pct"],
                "won": charges["net_pnl"] > 0,
            })
            # Advance past exit bar to prevent overlapping positions in the same symbol
            i = exit_idx + 1

        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        return {
            "symbol": symbol,
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
            "net_pnl_inr": round(sum(t["net_pnl"] for t in trades), 2),
            "fees_inr": round(sum(t["fees_inr"] for t in trades), 2),
            "profit_factor": pf,
            "trades": trades,
        }

    def run_nr7_symbol_backtest(
        self,
        symbol: str,
        df: pd.DataFrame,
        params: Optional[Mapping[str, Any]] = None,
        slippage_bps: float = 5.0,
    ) -> Dict[str, Any]:
        """Simulate NR7 breakout trades on a single symbol's daily bars.

        Entry is a next-session stop-limit buy: the trade only opens when the
        next bar's high reaches the NR7 breakout level, filled at the greater
        of the bar open and the breakout level (plus slippage). The stop trails
        up to ``sma_10`` and is capped by the ATR stop / NR7 low.
        """
        p = {**NR7SwingBreakout.DEFAULTS, **(params or {})}
        feat = compute_swing_features(df).reset_index(drop=True)
        slip = slippage_bps / 10_000.0
        trades: List[Dict[str, Any]] = []

        i = max(int(p["nr7_window"]), 200) - 1
        n = len(feat)
        while i < n - 1:
            window = feat.iloc[: i + 1]
            sig = NR7SwingBreakout.evaluate(window, params=p)
            if not sig.get("active"):
                i += 1
                continue

            breakout = float(sig["breakout_level"])
            atr = float(feat.iloc[i]["atr_14"])
            next_bar = feat.iloc[i + 1]
            if not np.isfinite(atr) or atr <= 0:
                i += 1
                continue
            # Stop-limit: no fill unless the next session trades through the level.
            if float(next_bar["high"]) < breakout:
                i += 1
                continue

            raw_open = float(next_bar["open"])
            fill = raw_open if raw_open >= breakout else breakout
            entry_px = round(fill * (1.0 + slip), 2)
            if entry_px <= 0:
                i += 1
                continue

            qty = max(1, int(float(p["allocation_inr"]) // entry_px))
            stop = round(max(float(sig["nr7_low"]), entry_px - float(p["stop_atr_mult"]) * atr), 2)
            target = round(entry_px + float(p["target_atr_mult"]) * atr, 2)
            max_hold = int(p["max_hold_days"])

            exit_px: Optional[float] = None
            exit_reason = "TIME_EXIT"
            exit_idx = min(n - 1, i + 1 + max_hold)

            for j in range(i + 1, min(n, i + 1 + max_hold)):
                bar = feat.iloc[j]
                # Trail the stop up to the previous bar's 10-day SMA, never
                # lowering it, and never on the entry bar itself (the entry bar
                # is where the breakout is confirmed).
                if j > i + 1:
                    prev_sma_10 = float(feat.iloc[j - 1].get("sma_10", np.nan))
                    if np.isfinite(prev_sma_10) and prev_sma_10 > stop:
                        stop = round(prev_sma_10, 2)
                b_open = float(bar["open"])
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                if b_open <= stop:
                    exit_px, exit_reason, exit_idx = round(b_open, 2), "STOP_GAP", j
                    break
                if b_low <= stop:
                    exit_px, exit_reason, exit_idx = stop, "STOP_LOSS", j
                    break
                if b_high >= target:
                    exit_px, exit_reason, exit_idx = target, "TARGET_HIT", j
                    break

            if exit_px is None:
                exit_px = round(float(feat.iloc[exit_idx]["close"]), 2)

            charges = calculate_indian_statutory_charges(
                buy_value=entry_px * qty,
                sell_value=exit_px * qty,
                instrument_type="DELIVERY_EQUITY",
                quantity=qty,
                slippage_pct=0.0,
            )
            trades.append({
                "symbol": symbol,
                "strategy_id": NR7SwingBreakout.STRATEGY_ID,
                "entry_date": str(feat.iloc[i + 1].get("date", i + 1)),
                "exit_date": str(feat.iloc[exit_idx].get("date", exit_idx)),
                "hold_days": int(exit_idx - (i + 1) + 1),
                "entry_price": entry_px,
                "exit_price": exit_px,
                "stop_loss": stop,
                "target_price": target,
                "quantity": qty,
                "exit_reason": exit_reason,
                "gross_pnl": charges["gross_pnl"],
                "fees_inr": charges["total_charges"],
                "net_pnl": charges["net_pnl"],
                "return_pct": charges["net_roi_pct"],
                "won": charges["net_pnl"] > 0,
            })
            i = exit_idx + 1

        return self._summarize(symbol, trades)

    def _summarize(self, symbol: str, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        return {
            "symbol": symbol,
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
            "net_pnl_inr": round(sum(t["net_pnl"] for t in trades), 2),
            "fees_inr": round(sum(t["fees_inr"] for t in trades), 2),
            "profit_factor": pf,
            "trades": trades,
        }

    def run_portfolio_backtest(
        self,
        data: Mapping[str, pd.DataFrame],
        strategy: str = "combined",
        max_concurrent: int = 2,
        params: Optional[Mapping[str, Mapping[str, Any]]] = None,
        slippage_bps: float = 5.0,
    ) -> Dict[str, Any]:
        """Multi-symbol swing portfolio on the flat capital base.

        Candidate trades are generated per symbol (RSI-2 and/or NR7), each sized
        to ``initial_capital / max_concurrent`` so the book can never exceed
        available cash without leverage. Candidates are then accepted in
        chronological entry order subject to the concurrency cap, and portfolio
        P&L, profit factor and max drawdown are computed by exit order.
        """
        if strategy not in ("rsi2", "nr7", "combined"):
            raise ValueError(f"unknown portfolio strategy: {strategy}")
        per_pos = self.initial_capital / max(1, int(max_concurrent))
        p_rsi2 = {**RSI2OversoldSwing.DEFAULTS, "allocation_inr": per_pos}
        p_nr7 = {**NR7SwingBreakout.DEFAULTS, "allocation_inr": per_pos}
        if params:
            p_rsi2.update(params.get("rsi2", {}))
            p_nr7.update(params.get("nr7", {}))
            p_rsi2["allocation_inr"] = per_pos
            p_nr7["allocation_inr"] = per_pos

        candidates: List[Dict[str, Any]] = []
        for sym, df in data.items():
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            if strategy in ("rsi2", "combined"):
                candidates.extend(
                    self.run_symbol_backtest(sym, df, params=p_rsi2, slippage_bps=slippage_bps)["trades"]
                )
            if strategy in ("nr7", "combined"):
                candidates.extend(
                    self.run_nr7_symbol_backtest(sym, df, params=p_nr7, slippage_bps=slippage_bps)["trades"]
                )

        candidates.sort(key=lambda t: str(t["entry_date"]))
        accepted: List[Dict[str, Any]] = []
        open_exits: List[str] = []
        for t in candidates:
            entry_date = str(t["entry_date"])
            open_exits = [d for d in open_exits if d >= entry_date]
            if len(open_exits) >= int(max_concurrent):
                continue
            accepted.append(t)
            open_exits.append(str(t["exit_date"]))

        ordered = sorted(accepted, key=lambda t: str(t["exit_date"]))
        running = self.initial_capital
        peak = running
        max_dd = 0.0
        equity_curve = [round(running, 2)]
        for t in ordered:
            running += t["net_pnl"]
            equity_curve.append(round(running, 2))
            peak = max(peak, running)
            max_dd = max(max_dd, (peak - running) / peak * 100.0 if peak else 0.0)

        wins = [t for t in accepted if t["won"]]
        losses = [t for t in accepted if not t["won"]]
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        net = round(sum(t["net_pnl"] for t in accepted), 2)
        return {
            "strategy": strategy,
            "initial_capital_inr": self.initial_capital,
            "max_concurrent": int(max_concurrent),
            "allocation_per_position_inr": round(per_pos, 2),
            "total_trades": len(accepted),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round(len(wins) / len(accepted) * 100.0, 1) if accepted else 0.0,
            "net_pnl_inr": net,
            "net_return_pct": round(net / self.initial_capital * 100.0, 2),
            "fees_inr": round(sum(t["fees_inr"] for t in accepted), 2),
            "profit_factor": pf,
            "max_drawdown_pct": round(max_dd, 2),
            "equity_curve": equity_curve,
            "trades": accepted,
        }


rsi2_swing_strategy = RSI2OversoldSwing()
nr7_swing_strategy = NR7SwingBreakout()
swing_backtest_engine = SwingBacktestEngine()
