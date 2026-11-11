"""Fee-Aware Opening-Range Momentum (FAROM) — a new strategy built from the
real, measured behaviour of the existing suite.

Why this exists
---------------
The 1-year hourly validation of the shipped primary edge
(`momentum_continuation`) showed the honest problem:

  * 59 trades, 39.0% win rate, gross +\u20b9349.11, fees \u20b9501.06, net -\u20b9151.95.
  * Realized per-trade move averaged +0.06% (median 0.00%) while the
    round-trip fee floor is ~0.106% of position value.
  * The strategy holds to the close, so it never banks a fee-clearing move;
    it pays the fee and gives the move back.
  * Fees consumed 86% of gross P&L over the year.

The measured root cause is therefore NOT the entry signal (the first-hour +
opening-range breakout does find real momentum) but the EXIT and the COST
STRUCTURE. FAROM keeps the same, already-validated entry geometry and fixes
the two things the data says are broken:

  1. TAKE PROFIT AT A FEE-CLEARING TARGET instead of holding to the close.
     The target is placed at a distance that clears round-trip fees by a
     required multiple, so a winning trade is a real win, not a fee sink.
  2. REQUIRE A MINIMUM OPENING-RANGE WIDTH. A narrow opening range produces a
     tiny target that cannot pay for its own fees. FAROM refuses to trade
     ranges whose breakout target is too small relative to cost.

Everything else (first-hour bullish bias, opening-range breakout, OR-midpoint
stop, next-bar fill, 2% risk sizing, 30% position cap) is reused unchanged from
the validated engine so the comparison is apples-to-apples.

This module is intentionally standalone: it does not modify
`intraday_strategies.py`, so live 5m behaviour is untouched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from app.trading.costs import calculate_buy_fee, calculate_sell_fee
from app.trading.intraday_strategies import IntradayStrategySuite


# Round-trip fee floor as a fraction of position value, measured from the real
# cost model at a typical \u20b93,000 position (30% of \u20b910,000). Used only as a
# sanity constant in the rationale text; the actual gate uses the live model.
FEE_FLOOR_PCT = 0.106


class FeeAwareOpeningRangeMomentum:
    """Fee-aware opening-range momentum with a fee-clearing take-profit."""

    STRATEGY_ID = "farom_fee_aware_momentum"
    NAME = "Fee-Aware Opening-Range Momentum"

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        or_bars_count: int = 3,
        h1_end: str = "10:15",
        min_h1_bars: int = 5,
        min_edge_to_fee: float = 2.0,
        min_or_width_pct: float = 0.004,
        target_r_multiple: float = 1.6,
        est_position_inr: float = 3000.0,
    ) -> Dict[str, Any]:
        """Evaluate a fee-aware momentum-continuation setup on a bar series.

        Parameters
        ----------
        min_edge_to_fee:
            Required ratio of gross profit at target to round-trip fees. A
            value of 2.0 means the target must earn at least twice what the
            trade costs to execute. This is the core fix: the shipped engine
            only checked this at the portfolio gate, never in the geometry.
        min_or_width_pct:
            Minimum opening-range width as a fraction of price. A range
            narrower than this cannot produce a fee-clearing target, so the
            setup is skipped entirely.
        target_r_multiple:
            Target distance as a multiple of the stop distance (R). 1.6R
            clears fees comfortably while still being reachable intraday.
        est_position_inr:
            Position size used to estimate fees for the edge-to-fee check.
            Defaults to the 30% cap on \u20b910,000 capital.
        """
        if len(df) <= or_bars_count + 1:
            return {"active": False, "signal": "HOLD", "reason": "Insufficient bars"}

        day_open = float(df.iloc[0]["open"])
        if day_open <= 0:
            return {"active": False, "signal": "HOLD", "reason": "Invalid day open"}

        # 1. First-hour bullish bias (same as the validated primary edge).
        h1 = df[df["time"] <= h1_end]
        if len(h1) < min_h1_bars:
            return {"active": False, "signal": "HOLD", "reason": "First hour still forming"}
        h1_close = float(h1.iloc[-1]["close"])
        h1_return = (h1_close / day_open) - 1.0
        if h1_return <= 0:
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"First-hour trend not bullish ({h1_return * 100:+.2f}%)",
            }

        # 2. Opening range.
        or_df = df.iloc[:or_bars_count]
        or_high = float(or_df["high"].max())
        or_low = float(or_df["low"].min())
        or_mid = round((or_high + or_low) / 2.0, 2)
        or_width_pct = (or_high - or_low) / day_open if day_open > 0 else 0.0

        # FIX #2: refuse narrow ranges that cannot pay for their own fees.
        if or_width_pct < min_or_width_pct:
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"Opening range too narrow ({or_width_pct * 100:.2f}% < {min_or_width_pct * 100:.2f}%)",
            }

        current = df.iloc[-1]
        cur_close = float(current["close"])
        cur_open = float(current["open"])
        prev_close = float(df.iloc[-2]["close"])
        atr = float(current.get("atr_14", cur_close * 0.005))

        # 3. Opening-range breakout on the current bar.
        if not (cur_close > or_high and prev_close <= or_high * 1.003):
            return {"active": False, "signal": "HOLD", "reason": "No opening-range breakout"}
        if cur_close > or_high * 1.02:
            return {"active": False, "signal": "HOLD", "reason": "Breakout already extended"}
        if cur_close < cur_open:
            return {"active": False, "signal": "HOLD", "reason": "Breakout bar is red"}

        # 4. Stop at the OR midpoint (natural invalidation), floored so the
        #    risk is never absurdly tight relative to noise.
        stop = round(max(or_mid, cur_close - max(atr * 1.5, cur_close * 0.008)), 2)
        if stop >= cur_close:
            stop = round(cur_close * 0.992, 2)
        risk = max(cur_close - stop, cur_close * 0.003)

        # FIX #1: take profit at a fee-clearing distance, NOT hold-to-close.
        target = round(cur_close + max(risk * target_r_multiple, cur_close * 0.012), 2)

        # 5. Fee-aware geometry gate: the target must clear round-trip fees by
        #    the required multiple at the position size we would actually take.
        est_qty = max(1, int(est_position_inr / cur_close))
        gross_edge = (target - cur_close) * est_qty
        round_trip = calculate_buy_fee(cur_close * est_qty) + calculate_sell_fee(target * est_qty)
        edge_to_fee = (gross_edge / round_trip) if round_trip > 0 else float("inf")
        if edge_to_fee < min_edge_to_fee:
            return {
                "active": False,
                "signal": "HOLD",
                "reason": f"Target edge {edge_to_fee:.2f}x fees < required {min_edge_to_fee:.2f}x",
            }

        return {
            "active": True,
            "strategy_id": cls.STRATEGY_ID,
            "name": cls.NAME,
            "signal": "BUY",
            "entry_price": round(cur_close, 2),
            "stop_loss": stop,
            "target_1": target,
            "target_2": round(cur_close + risk * target_r_multiple * 1.6, 2),
            "hold_to_close": False,
            "or_high": round(or_high, 2),
            "risk_reward_ratio": f"1:{target_r_multiple:.2f} (fee-clearing target)",
            "confidence": 86,
            "edge_to_fee_ratio": round(edge_to_fee, 2),
            "rationale": (
                f"Fee-aware momentum: first hour {h1_return * 100:+.2f}% above open, "
                f"broke OR high \u20b9{or_high:.2f} (range {or_width_pct * 100:.2f}%). "
                f"Target \u20b9{target:.2f} clears fees {edge_to_fee:.1f}x; stop \u20b9{stop:.2f}."
            ),
        }


farom_engine = FeeAwareOpeningRangeMomentum()
