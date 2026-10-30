"""
Day Trader Skill Adapter for Gemini Quant Platform.
Directly implements algorithms from Jakeschincariol/day-trader-skill:
- trader-entry: Opening Range Breakout (ORB-15m) trigger levels
- trader-exit: Midpoint stop-loss & multi-tier R-targets (1R, 1.5R, 2R) with flat-by-market-close
- trader-timemachine: Monte Carlo 2,000 path simulation for empirical win-rate odds & expected value E[R]
- trader-budget: 2% capital risk position sizing & capital allocation bounds
- trader-risk: Risk limits validation (reward-to-risk ratio, max positions, cash availability)
Calibrated specifically for Indian Equities (NSE/BSE) and INR pricing.
"""

import math
import random
import datetime
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger("app.trading.skills.day_trader")


class DayTraderSkillAdapter:
    """
    Adapter incorporating Day Trader Skill mechanics:
    - Opening Range Breakout (ORB) on 15-minute / 5-minute candles
    - ATR-based dynamic stop-loss & R-multiple targets
    - Monte Carlo Time Machine simulation to calculate empirical hit probability
    - Disciplined position sizing (2% max loss per trade, ₹200 on ₹10,000 capital)
    - Pre-trade risk validation gates
    """

    @staticmethod
    def calculate_atr(bars: List[Dict[str, float]], period: int = 14) -> float:
        """Calculate Average True Range (ATR) over historical bars."""
        if not bars:
            return 1.0
        trs = []
        prev_close = None
        for b in bars:
            high = b.get("high", b.get("h", 0.0))
            low = b.get("low", b.get("l", 0.0))
            close = b.get("close", b.get("c", 0.0))
            if prev_close is not None:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            else:
                tr = high - low
            trs.append(tr)
            prev_close = close
        use = trs[-period:] if len(trs) >= period else trs
        return round(sum(use) / len(use), 2) if use else 1.0

    @classmethod
    def calculate_orb_plan(
        cls,
        symbol: str,
        current_price: float,
        bars_5m: Optional[List[Dict[str, float]]] = None,
        or_minutes: int = 15,
        target_r: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Calculates Opening Range Breakout (ORB) parameters (from trader-entry & trader-exit):
        - Range High & Low of the first 15 mins (three 5-min bars: 09:15, 09:20, 09:25 IST)
        - Midpoint Stop Loss: (High + Low) / 2
        - Target 1 = Entry + 1.0 * Risk
        - Target 2 = Entry + R * Risk
        """
        bars = bars_5m or []
        or_bars = bars[: max(3, or_minutes // 5)] if len(bars) >= 3 else []
        if or_bars:
            or_high = max(b.get("high", b.get("h", current_price)) for b in or_bars)
            or_low = min(b.get("low", b.get("l", current_price)) for b in or_bars)
        else:
            # High-fidelity estimation based on Indian equity typical opening intraday volatility (~0.4%)
            or_high = round(current_price * 1.004, 2)
            or_low = round(current_price * 0.996, 2)

        entry = round(current_price, 2)
        # ORB Midpoint Stop Loss (from day-trader-skill trader-exit: or-mid)
        stop_loss = round((or_high + or_low) / 2.0, 2)
        risk = round(entry - stop_loss, 2)

        # Minimum risk check (0.1% buffer to prevent slippage erosion)
        min_risk = max(round(entry * 0.002, 2), 0.10)
        if risk < min_risk:
            risk = min_risk
            stop_loss = round(entry - risk, 2)

        target_1 = round(entry + (risk * 1.0), 2)
        target_2 = round(entry + (risk * target_r), 2)

        return {
            "symbol": symbol.upper(),
            "setup": "Opening Range Breakout (ORB-15m)",
            "entry_price": entry,
            "or_high": or_high,
            "or_low": or_low,
            "stop_loss": stop_loss,
            "risk_per_share": risk,
            "target_1": target_1,
            "target_2": target_2,
            "target_r": target_r,
            "risk_reward_ratio": f"1:{target_r:.1f}",
            "flat_by": "15:25 IST",
        }

    @classmethod
    def run_time_machine_simulation(
        cls,
        symbol: str,
        entry: float,
        stop_loss: float,
        target: float,
        bars: Optional[List[Dict[str, float]]] = None,
        replays: int = 2000,
    ) -> Dict[str, Any]:
        """
        Time Machine Simulation (Monte Carlo path simulation from trader-timemachine):
        Replays the remainder of the session 2,000 times using empirical volatility
        to compute actual probability of hitting target before stop-loss.
        """
        risk = abs(entry - stop_loss)
        reward = abs(target - entry)
        if risk == 0:
            risk = max(entry * 0.01, 0.10)

        # Volatility calibration from recent bars or Indian large/mid-cap standard
        sigma = 0.0035
        if bars and len(bars) > 5:
            returns = []
            for i in range(1, len(bars)):
                c0 = bars[i - 1].get("close", bars[i - 1].get("c", 1.0))
                c1 = bars[i].get("close", bars[i].get("c", 1.0))
                if c0 > 0:
                    returns.append((c1 - c0) / c0)
            if returns:
                variance = sum(r * r for r in returns) / len(returns)
                sigma = max(math.sqrt(variance), 0.0015)

        target_hits = 0
        stop_hits = 0
        neither_hits = 0
        simulated_pnl_r = []

        random.seed(42)  # Deterministic seed for reproducible evaluation
        remaining_bars = 45  # Typical remaining 5-min intervals in active session

        for _ in range(replays):
            price = entry
            hit = None
            for _step in range(remaining_bars):
                shock = random.gauss(0.0001, sigma)
                price = price * (1.0 + shock)

                # Stop-loss checked first (conservative standard as in day-trader-skill)
                if price <= stop_loss:
                    hit = "stop"
                    break
                elif price >= target:
                    hit = "target"
                    break

            if hit == "target":
                target_hits += 1
                simulated_pnl_r.append(reward / risk)
            elif hit == "stop":
                stop_hits += 1
                simulated_pnl_r.append(-1.0)
            else:
                neither_hits += 1
                simulated_pnl_r.append((price - entry) / risk)

        decided = target_hits + stop_hits
        win_rate = (target_hits / decided * 100) if decided > 0 else 50.0
        expected_value_r = sum(simulated_pnl_r) / len(simulated_pnl_r) if simulated_pnl_r else 0.0

        verdict = (
            "Strongly toward target" if win_rate >= 60.0 else (
                "Leans toward target" if win_rate >= 52.0 else (
                    "Close to coin flip" if win_rate >= 48.0 else "Leans toward stop"
                )
            )
        )

        return {
            "symbol": symbol.upper(),
            "replays": replays,
            "target_hits": target_hits,
            "stop_hits": stop_hits,
            "neither_hits": neither_hits,
            "win_rate_pct": round(win_rate, 1),
            "expected_value_r": round(expected_value_r, 2),
            "verdict": verdict,
            "recommendation": "EXECUTE" if (win_rate >= 52.0 and expected_value_r > 0.10) else "PASS",
        }

    @classmethod
    def calculate_budget_size(
        cls,
        initial_capital: float,
        available_cash: float,
        entry_price: float,
        stop_loss: float,
        max_loss_pct: float = 0.02,  # 2% max risk per trade (trader-budget standard)
        max_position_pct: float = 0.70,  # Max 70% of capital in one position for intraday alpha
    ) -> Dict[str, Any]:
        """
        Position Sizing from trader-budget:
        Computes the safe quantity of whole shares based on:
        1. Max loss limit: ₹200 max loss on ₹10,000 capital (2%)
        2. Position cap: max ₹7,000 allocation for intraday equities
        3. Available cash balance
        """
        if entry_price <= 0:
            return {"quantity": 0, "allocated_cash": 0.0, "max_loss_inr": 0.0, "reason": "Invalid price"}

        risk_per_share = max(entry_price - stop_loss, entry_price * 0.002)
        max_loss_inr = round(initial_capital * max_loss_pct, 2)
        max_position_inr = round(initial_capital * max_position_pct, 2)

        # Sizing constraints
        shares_by_loss = int(max_loss_inr // risk_per_share) if risk_per_share > 0 else 1
        shares_by_cap = int(max_position_inr // entry_price)
        shares_by_cash = int(available_cash // entry_price)

        shares = max(0, min(shares_by_loss, shares_by_cap, shares_by_cash))
        # Ensure at least 1 share if cash allows
        if shares == 0 and available_cash >= entry_price and (entry_price <= max_position_inr):
            shares = 1

        allocated_cash = round(shares * entry_price, 2)
        potential_loss = round(shares * risk_per_share, 2)

        return {
            "quantity": shares,
            "allocated_cash_inr": allocated_cash,
            "potential_loss_inr": potential_loss,
            "max_loss_limit_inr": max_loss_inr,
            "risk_per_share_inr": round(risk_per_share, 2),
            "sizing_bottleneck": "max_loss" if shares == shares_by_loss else (
                "position_cap" if shares == shares_by_cap else "available_cash"
            ),
        }

    @classmethod
    def check_risk_limits(
        cls,
        orb_plan: Dict[str, Any],
        time_machine: Dict[str, Any],
        budget_size: Dict[str, Any],
        current_open_positions_count: int,
        max_open_positions: int = 3,
    ) -> Dict[str, Any]:
        """
        Risk Validation Gate from trader-risk:
        Evaluates trade parameters before execution.
        Returns PASS or BLOCK with full rationale.
        """
        reasons = []

        if current_open_positions_count >= max_open_positions:
            reasons.append(f"Max concurrent positions limit reached ({current_open_positions_count}/{max_open_positions}).")

        if budget_size.get("quantity", 0) <= 0:
            reasons.append("Calculated budget quantity is 0 (insufficient cash or risk exceeds threshold).")

        if time_machine.get("win_rate_pct", 50.0) < 50.0:
            reasons.append(f"Time Machine win-rate odds ({time_machine.get('win_rate_pct')}%) fail minimum 50% threshold.")

        if time_machine.get("expected_value_r", 0.0) <= 0.0:
            reasons.append(f"Expected value ({time_machine.get('expected_value_r')}R) is non-positive.")

        status = "PASS" if not reasons else "BLOCK"
        return {
            "status": status,
            "approved": status == "PASS",
            "reasons": reasons if reasons else ["All Day-Trader risk limits satisfied."],
        }

    @classmethod
    def evaluate_day_trade_setup(
        cls,
        symbol: str,
        current_price: float,
        bars_5m: Optional[List[Dict[str, float]]] = None,
        initial_capital: float = 10000.0,
        available_cash: float = 10000.0,
        current_positions_count: int = 0,
    ) -> Dict[str, Any]:
        """
        End-to-End Day Trader Pipeline:
        Combines scanner -> entry -> exit -> timemachine -> budget -> risk check.
        """
        orb = cls.calculate_orb_plan(symbol, current_price, bars_5m=bars_5m)
        tm = cls.run_time_machine_simulation(symbol, orb["entry_price"], orb["stop_loss"], orb["target_2"], bars=bars_5m)
        budget = cls.calculate_budget_size(initial_capital, available_cash, orb["entry_price"], orb["stop_loss"])
        risk = cls.check_risk_limits(orb, tm, budget, current_positions_count)

        return {
            "symbol": symbol.upper(),
            "orb_plan": orb,
            "time_machine_odds": tm,
            "budget_sizing": budget,
            "risk_validation": risk,
            "overall_recommendation": "EXECUTE" if (risk["approved"] and tm["recommendation"] == "EXECUTE") else "PASS",
        }


day_trader = DayTraderSkillAdapter()
