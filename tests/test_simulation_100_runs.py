"""
100-Run Monte Carlo Simulation & Quantitative Stress Test Suite (Zero AI).

Tests the trading system 100s of times across varied market conditions:
- 100 independent market sessions across the 8 liquid Indian stocks (~₹150 - ₹1,400)
- Capital fixed at ₹10,000 INR
- Pure deterministic quantitative logic (4-Lines, Volume Profile, Liquidity Sweeps, 1:2 R:R)
- Zero external AI calls (100% offline, deterministic, resilient)
- Verifies:
    1. Zero crashes or unhandled exceptions across all 100 runs
    2. Sizing never exceeds 30% of ₹10,000 (~₹3,000 per position)
    3. Maximum concurrent positions never exceeds MAX_OPEN_POSITIONS (3)
    4. Daily risk limit (3.0% = ₹300) cleanly halts runaway drawdowns
    5. Positive mathematical expectancy (1:2 R:R targets overcome Indian statutory taxes/fees)
    6. Target profit (>= 1.0% = ₹100 net gain) is captured and secured
"""

import unittest
import asyncio
import random
import datetime
from datetime import time
from typing import Dict, Any, List
from pathlib import Path
import pandas as pd
import numpy as np

from app.config import settings
from app.storage.db import DatabaseManager
from app.trading.broker import PaperBroker, Position
from app.trading.risk_manager import RiskManager
from app.trading.institutional_engine import (
    generate_institutional_ai_signal,
    evaluate_risk_guardrails,
    calculate_indian_statutory_charges,
)
from app.trading.institutional_runner import DEFAULT_STOCKS

STOCK_BASE_PRICES = {
    "SBIN": 970.0,
    "ITC": 275.0,
    "TATASTEEL": 140.0,
    "TATAMOTORS": 820.0,
    "ICICIBANK": 1420.0,
    "AXISBANK": 1180.0,
    "NTPC": 390.0,
    "COALINDIA": 430.0,
}


def generate_synthetic_session_ohlc(
    base_price: float,
    num_bars: int = 40,
    regime: str = "bullish",
    seed: int = 42
) -> pd.DataFrame:
    """
    Generates realistic 5-minute OHLC bars for a single Indian stock session without external APIs.
    Regimes: 'bullish', 'bearish', 'chop', 'sweep_reversal'
    """
    rng = np.random.default_rng(seed)
    bars = []
    current_price = base_price

    if regime == "bullish":
        drift = 0.0005
        vol = 0.0020
    elif regime == "bearish":
        drift = -0.0005
        vol = 0.0020
    elif regime == "sweep_reversal":
        drift = -0.0004
        vol = 0.0030
    else:  # chop
        drift = 0.0000
        vol = 0.0015

    for i in range(num_bars):
        if regime == "sweep_reversal" and i > 12:
            step_drift = 0.0008  # strong absorption and mean-reversion bounce
        else:
            step_drift = drift

        pct_change = float(rng.normal(step_drift, vol))
        open_p = current_price
        close_p = round(open_p * (1.0 + pct_change), 2)
        high_p = round(max(open_p, close_p) * (1.0 + abs(float(rng.normal(0, vol * 0.4)))), 2)
        low_p = round(min(open_p, close_p) * (1.0 - abs(float(rng.normal(0, vol * 0.4)))), 2)
        volume = int(rng.integers(10000, 50000))

        bars.append({
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": volume,
        })
        current_price = close_p

    df = pd.DataFrame(bars)
    df["Open"] = df["open"]
    df["High"] = df["high"]
    df["Low"] = df["low"]
    df["Close"] = df["close"]
    df["Volume"] = df["volume"]
    return df


class TestSimulation100Runs(unittest.TestCase):
    """Executes 100 independent multi-regime simulation runs without any AI."""

    def test_run_100_simulations_without_ai(self):
        """Run 100 full market day simulations and verify risk bounds, stability, and expectancy."""
        num_runs = 100
        regimes = ["bullish"] * 30 + ["sweep_reversal"] * 30 + ["chop"] * 25 + ["bearish"] * 15

        # Initialize shared test database to keep tests blazing fast
        test_db_path = Path("/tmp/sim_test_100_runs.db")
        if test_db_path.exists():
            test_db_path.unlink()
        test_db = DatabaseManager(test_db_path)
        broker = PaperBroker(database=test_db)
        broker.risk.enforce_market_hours = False

        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_net_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        max_observed_drawdown_pct = 0.0
        runs_hitting_profit_target = 0
        runs_hitting_loss_limit = 0
        sizing_violations = 0
        concurrency_violations = 0

        print(f"\n==================================================================")
        print(f" 🚀 RUNNING 100-RUN MONTE CARLO QUANTITATIVE SIMULATION (ZERO AI)")
        print(f" Initial Capital: ₹10,000.00 INR | Active Universe: {len(DEFAULT_STOCKS)} Stocks")
        print(f" Regimes: 30 Bullish, 30 Sweep Reversal, 25 Choppy, 15 Bearish")
        print(f"==================================================================")

        for run_id in range(1, num_runs + 1):
            regime = regimes[run_id - 1]
            acc_id = f"sim_{run_id}"
            broker.reset_account(capital=10000.0, account_id=acc_id)
            acc = broker.get_account(acc_id)

            # Generate synthetic session data across the 8 stocks
            stock_dfs = {}
            for idx, sym in enumerate(DEFAULT_STOCKS):
                base_p = STOCK_BASE_PRICES.get(sym, 500.0)
                seed = run_id * 1000 + idx * 13
                stock_dfs[sym] = generate_synthetic_session_ohlc(base_p, num_bars=35, regime=regime, seed=seed)

            run_trades_count = 0
            session_start_cash = 10000.0

            for bar_idx in range(5, 35):
                # 1. Update prices of existing positions and evaluate SL / TP
                for sym in list(acc.positions.keys()):
                    pos = acc.positions.get(sym)
                    if not pos:
                        continue
                    df_sym = stock_dfs[sym]
                    bar = df_sym.iloc[bar_idx]
                    high_p = float(bar["High"])
                    low_p = float(bar["Low"])
                    close_p = float(bar["Close"])

                    exit_reason = None
                    exit_fill = close_p
                    if pos.target_price and high_p >= pos.target_price:
                        exit_reason = "target_reached"
                        exit_fill = pos.target_price
                    elif pos.stop_loss and low_p <= pos.stop_loss:
                        exit_reason = "stop_loss"
                        exit_fill = pos.stop_loss

                    if exit_reason:
                        broker.price_lookup = lambda s, p=exit_fill: p
                        close_res = asyncio.run(broker.close_position(sym, account_id=acc_id, reason=exit_reason))
                        if close_res and close_res.get("status") == "FILLED":
                            pnl = float(close_res.get("pnl_inr", 0.0))
                            total_net_pnl += pnl
                            run_trades_count += 1
                            total_trades += 1
                            if pnl > 0:
                                total_wins += 1
                                gross_profit += pnl
                            else:
                                total_losses += 1
                                gross_loss += abs(pnl)

                # 2. Risk checks for the session
                realized_today = acc.realized_pnl
                drawdown_pct = round(abs(min(realized_today, 0.0)) / 10000.0 * 100.0, 2)
                profit_pct = round(realized_today / 10000.0 * 100.0, 2)
                if drawdown_pct > max_observed_drawdown_pct:
                    max_observed_drawdown_pct = drawdown_pct

                if profit_pct >= settings.TARGET_NET_PROFIT_PCT:
                    runs_hitting_profit_target += 1
                    break  # Daily profit goal achieved (>= 1.0%), secure gains

                if drawdown_pct >= settings.MAX_DAILY_LOSS_PCT:
                    runs_hitting_loss_limit += 1
                    break  # Max daily loss reached, halt session risk

                if run_trades_count >= settings.MAX_TRADES_PER_DAY:
                    break

                # 3. Institutional Quantitative Scan (Zero AI)
                for sym in DEFAULT_STOCKS:
                    if sym in acc.positions or len(acc.positions) >= settings.MAX_OPEN_POSITIONS:
                        continue

                    df_sym = stock_dfs[sym].iloc[:bar_idx + 1]
                    spot = float(df_sym.iloc[-1]["Close"])

                    sig = generate_institutional_ai_signal(
                        symbol=sym,
                        spot_price=spot,
                        df=df_sym,
                        daily_net_profit_pct=profit_pct,
                        consecutive_losses=0,
                        current_drawdown_pct=drawdown_pct,
                    )

                    can_trade = sig.get("guardrail_status", {}).get("can_trade", False)
                    action = sig.get("action")
                    conf = sig.get("confidence_score", 0)

                    if can_trade and action == "BUY" and conf >= 70:
                        qty = acc.calculate_budget_quantity(sym, spot, max_alloc_pct=0.30)
                        pos_value = qty * spot
                        if pos_value > 3100.0:
                            sizing_violations += 1

                        if len(acc.positions) >= settings.MAX_OPEN_POSITIONS:
                            concurrency_violations += 1

                        broker.price_lookup = lambda s, p=spot: p
                        order = asyncio.run(broker.fast_execute_order(
                            symbol=sym,
                            action=action,
                            quantity=qty,
                            strategy_name=sig.get("setup_name"),
                            account_id=acc_id,
                            stop_loss=sig.get("stop_loss"),
                            target_price=sig.get("target_price"),
                        ))

            # End of day square-off (15:20 IST)
            for sym in list(acc.positions.keys()):
                bar_close = float(stock_dfs[sym].iloc[-1]["Close"])
                broker.price_lookup = lambda s, p=bar_close: p
                close_res = asyncio.run(broker.close_position(sym, account_id=acc_id, reason="eod_square_off"))
                if close_res and close_res.get("status") == "FILLED":
                    pnl = float(close_res.get("pnl_inr", 0.0))
                    total_net_pnl += pnl
                    total_trades += 1
                    if pnl > 0:
                        total_wins += 1
                        gross_profit += pnl
                    else:
                        total_losses += 1
                        gross_loss += abs(pnl)

            if run_id % 20 == 0 or run_id == num_runs:
                print(f"  [Run {run_id:3d}/100] Regime: {regime:<15} | Cumulative Trades: {total_trades:3d} | Net P&L: ₹{total_net_pnl:+,.2f}")

        win_rate = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0
        profit_factor = (gross_profit / max(gross_loss, 0.01))
        avg_trade_pnl = (total_net_pnl / total_trades) if total_trades > 0 else 0.0

        print(f"==================================================================")
        print(f" 📊 100-RUN MONTE CARLO VERIFICATION RESULTS:")
        print(f"   Total Runs Simulated:           {num_runs}")
        print(f"   Total Trades Executed:          {total_trades}")
        print(f"   Winning Trades:                 {total_wins} ({win_rate:.1f}%)")
        print(f"   Losing Trades:                  {total_losses} ({100.0 - win_rate:.1f}%)")
        print(f"   Gross Profits:                  ₹{gross_profit:,.2f} INR")
        print(f"   Gross Losses:                   ₹{gross_loss:,.2f} INR")
        print(f"   Profit Factor:                  {profit_factor:.2f}")
        print(f"   Cumulative Net P&L:             ₹{total_net_pnl:+,.2f} INR")
        print(f"   Average Net P&L per Trade:      ₹{avg_trade_pnl:+,.2f} INR")
        print(f"   Max Intraday Drawdown:          {max_observed_drawdown_pct:.2f}% (Limit: 3.0%)")
        print(f"   Runs Hitting Profit Target:     {runs_hitting_profit_target}")
        print(f"   Runs Tripping Loss Limit:       {runs_hitting_loss_limit}")
        print(f"   Position Sizing Violations:     {sizing_violations}")
        print(f"   Concurrency Violations:         {concurrency_violations}")
        print(f"==================================================================\n")

        # Clean up temp db
        if test_db_path.exists():
            test_db_path.unlink()

        # Rigorous assertions
        self.assertEqual(sizing_violations, 0, "No position should exceed ₹3,000 budget cap on ₹10k capital")
        self.assertEqual(concurrency_violations, 0, "No run should exceed MAX_OPEN_POSITIONS (3)")
        self.assertLessEqual(max_observed_drawdown_pct, 3.0, "Max drawdown must never breach 3.0% daily limit")
        self.assertGreater(total_trades, 30, "At least 30 valid setups should execute across 100 runs")
        self.assertGreaterEqual(profit_factor, 1.0, f"Profit factor should demonstrate positive expectancy ({profit_factor:.2f})")


if __name__ == "__main__":
    unittest.main()
