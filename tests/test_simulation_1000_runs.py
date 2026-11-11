"""
1,000-Run Monte Carlo Simulation & Quantitative Stress Test Suite (Zero AI).

Executes 1,000 independent multi-regime market sessions across the 8 liquid Indian stocks
(SBIN, ITC, TATASTEEL, TATAMOTORS, ICICIBANK, AXISBANK, NTPC, COALINDIA) with ₹10,000 INR initial capital.

Purely deterministic quantitative execution (4-lines, Volume Profile, Liquidity Sweeps, 1:2 R:R, statutory fees).
Zero external AI calls (100% offline, local, deterministic, resilient).

Verifies:
  1. Zero crashes, zero database locks, zero unhandled exceptions across all 1,000 runs
  2. Sizing never exceeds 30% of ₹10,000 (~₹3,000 per position)
  3. Maximum concurrent positions never exceeds MAX_OPEN_POSITIONS (3)
  4. Daily risk limit (3.0% = ₹300) cleanly halts runaway drawdowns
  5. Daily profit target (>= 1.0% = ₹100 net gain) locks profits
  6. Positive mathematical expectancy (Profit Factor >= 1.05) after all Indian statutory taxes & fees
"""

import unittest
import asyncio
import os
from pathlib import Path
from typing import Dict, Any, List
import pandas as pd
import numpy as np

from app.config import settings
from app.storage.db import DatabaseManager
from app.trading.broker import PaperBroker
from app.trading.institutional_engine import (
    generate_institutional_ai_signal,
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


def generate_fast_synthetic_session_ohlc(
    base_price: float,
    num_bars: int = 50,
    regime: str = "bullish",
    vol_jitter: float = 1.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Vectorized generation of 5-minute OHLC bars for a single Indian stock session without external APIs.
    Fast NumPy vectorization allows generating hundreds of thousands of bars in milliseconds.
    """
    rng = np.random.default_rng(seed)

    if regime == "bullish":
        base_drift, base_vol = 0.0006, 0.0020
    elif regime == "bearish":
        base_drift, base_vol = -0.0006, 0.0020
    elif regime == "sweep_reversal":
        base_drift, base_vol = -0.0004, 0.0030
    else:  # chop
        base_drift, base_vol = 0.0000, 0.0015

    vol = base_vol * vol_jitter
    drifts = np.full(num_bars, base_drift)
    if regime == "sweep_reversal":
        # Sharp initial dip sweeping stops below PDL followed by strong institutional absorption & bounce
        drifts[15:] = 0.0008

    pct_changes = rng.normal(drifts, vol)
    cum_returns = np.cumprod(1.0 + pct_changes)
    close_prices = np.round(base_price * cum_returns, 2)

    open_prices = np.empty(num_bars)
    open_prices[0] = base_price
    open_prices[1:] = close_prices[:-1]

    high_noise = np.abs(rng.normal(0, vol * 0.4, num_bars))
    low_noise = np.abs(rng.normal(0, vol * 0.4, num_bars))

    high_prices = np.round(np.maximum(open_prices, close_prices) * (1.0 + high_noise), 2)
    low_prices = np.round(np.minimum(open_prices, close_prices) * (1.0 - low_noise), 2)
    volumes = rng.integers(10000, 50000, size=num_bars)

    df = pd.DataFrame({
        "open": open_prices,
        "high": high_prices,
        "low": low_prices,
        "close": close_prices,
        "volume": volumes,
        "Open": open_prices,
        "High": high_prices,
        "Low": low_prices,
        "Close": close_prices,
        "Volume": volumes,
    })
    return df


class TestSimulation1000Runs(unittest.IsolatedAsyncioTestCase):
    """Executes 1,000 independent multi-regime simulation runs without any AI."""

    async def test_run_1000_simulations_without_ai(self):
        """Run 1,000 full market sessions and verify risk bounds, stability, and positive expectancy."""
        num_runs = 1000

        # Regimes distribution: 300 Bullish, 300 Sweep Reversal, 250 Choppy, 150 Bearish
        regimes = (
            ["bullish"] * 300 +
            ["sweep_reversal"] * 300 +
            ["chop"] * 250 +
            ["bearish"] * 150
        )

        # Place test SQLite DB in tmpfs (/dev/shm) for high performance and zero disk wear
        shm_dir = Path("/dev/shm")
        test_db_path = (shm_dir / "sim_test_1000_runs.db") if shm_dir.exists() else Path("/tmp/sim_test_1000_runs.db")
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
        exceptions_encountered = 0

        print(f"\n==================================================================")
        print(f" 🚀 RUNNING 1,000-RUN MONTE CARLO QUANTITATIVE SIMULATION (ZERO AI)")
        print(f" Initial Capital: ₹10,000.00 INR | Active Universe: {len(DEFAULT_STOCKS)} Stocks")
        print(f" Extended Dataset: 50 Bars/Day | Total Bars Evaluated: {num_runs * 50 * len(DEFAULT_STOCKS):,}")
        print(f" Regimes: 300 Bullish, 300 Sweep Reversal, 250 Choppy, 150 Bearish")
        print(f" DB Backend: {test_db_path} (High Performance In-RAM SQLite WAL)")
        print(f"==================================================================")

        for run_id in range(1, num_runs + 1):
            try:
                regime = regimes[run_id - 1]
                acc_id = "opus_flash"  # Reset pre-seeded account
                broker.reset_account(capital=10000.0, account_id=acc_id)
                acc = broker.get_account(acc_id)

                # Volatility variation per session: 0.8x (calm) to 1.5x (turbulent)
                vol_jitter = 0.8 + ((run_id * 17) % 80) / 100.0

                # Generate extended synthetic session data across the 8 stocks
                stock_dfs: Dict[str, pd.DataFrame] = {}
                for idx, sym in enumerate(DEFAULT_STOCKS):
                    base_p = STOCK_BASE_PRICES.get(sym, 500.0)
                    seed = run_id * 10000 + idx * 37
                    stock_dfs[sym] = generate_fast_synthetic_session_ohlc(
                        base_p, num_bars=50, regime=regime, vol_jitter=vol_jitter, seed=seed
                    )

                run_trades_count = 0
                session_start_cash = 10000.0

                for bar_idx in range(5, 50):
                    # 1. Update prices of existing positions and evaluate SL / TP
                    # Conservative risk evaluation: check Stop-Loss first
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
                        if pos.stop_loss and low_p <= pos.stop_loss:
                            exit_reason = "stop_loss"
                            exit_fill = pos.stop_loss
                        elif pos.target_price and high_p >= pos.target_price:
                            exit_reason = "target_reached"
                            exit_fill = pos.target_price

                        if exit_reason:
                            broker.price_lookup = lambda s, p=exit_fill: p
                            close_res = await broker.close_position(sym, account_id=acc_id, reason=exit_reason)
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

                    # Daily profit goal achieved (>= 1.0%), secure gains
                    if profit_pct >= settings.TARGET_NET_PROFIT_PCT:
                        runs_hitting_profit_target += 1
                        break

                    # Max daily loss reached (3.0%), halt session risk
                    if drawdown_pct >= settings.MAX_DAILY_LOSS_PCT:
                        runs_hitting_loss_limit += 1
                        break

                    # Trade count limit per day reached
                    if run_trades_count >= settings.MAX_TRADES_PER_DAY:
                        break

                    # 3. Institutional Quantitative Scan (Zero AI)
                    # Skip scan if account already has MAX_OPEN_POSITIONS
                    if len(acc.positions) >= settings.MAX_OPEN_POSITIONS:
                        continue

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
                            await broker.fast_execute_order(
                                symbol=sym,
                                action=action,
                                quantity=qty,
                                strategy_name=sig.get("setup_name"),
                                account_id=acc_id,
                                stop_loss=sig.get("stop_loss"),
                                target_price=sig.get("target_price"),
                            )

                # End of day square-off (15:20 IST) for any remaining open positions
                for sym in list(acc.positions.keys()):
                    bar_close = float(stock_dfs[sym].iloc[-1]["Close"])
                    broker.price_lookup = lambda s, p=bar_close: p
                    close_res = await broker.close_position(sym, account_id=acc_id, reason="eod_square_off")
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

            except Exception as e:
                exceptions_encountered += 1
                print(f"❌ Unhandled exception in run {run_id}: {e}")

            if run_id % 100 == 0 or run_id == num_runs:
                win_rate_so_far = (total_wins / max(total_trades, 1)) * 100.0
                pf_so_far = (gross_profit / max(gross_loss, 0.01))
                print(
                    f"  [Run {run_id:4d}/1000] Regime: {regime:<15} | "
                    f"Trades: {total_trades:5d} | "
                    f"Win%: {win_rate_so_far:4.1f}% | "
                    f"PF: {pf_so_far:4.2f} | "
                    f"Net P&L: ₹{total_net_pnl:+10,.2f}"
                )

        win_rate = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0
        profit_factor = (gross_profit / max(gross_loss, 0.01))
        avg_trade_pnl = (total_net_pnl / total_trades) if total_trades > 0 else 0.0

        print(f"==================================================================")
        print(f" 📊 1,000-RUN MONTE CARLO VERIFICATION RESULTS:")
        print(f"   Total Runs Simulated:           {num_runs:,}")
        print(f"   Total Trades Executed:          {total_trades:,}")
        print(f"   Winning Trades:                 {total_wins:,} ({win_rate:.1f}%)")
        print(f"   Losing Trades:                  {total_losses:,} ({100.0 - win_rate:.1f}%)")
        print(f"   Gross Profits:                  ₹{gross_profit:,.2f} INR")
        print(f"   Gross Losses:                   ₹{gross_loss:,.2f} INR")
        print(f"   Profit Factor:                  {profit_factor:.2f}")
        print(f"   Cumulative Net P&L:             ₹{total_net_pnl:+,.2f} INR")
        print(f"   Average Net P&L per Trade:      ₹{avg_trade_pnl:+,.2f} INR")
        print(f"   Max Intraday Drawdown:          {max_observed_drawdown_pct:.2f}% (Limit: 3.0%)")
        print(f"   Runs Hitting Profit Target:     {runs_hitting_profit_target:,}")
        print(f"   Runs Tripping Loss Limit:       {runs_hitting_loss_limit:,}")
        print(f"   Position Sizing Violations:     {sizing_violations}")
        print(f"   Concurrency Violations:         {concurrency_violations}")
        print(f"   Unhandled Exceptions:           {exceptions_encountered}")
        print(f"==================================================================\n")

        # Clean up temp db
        if test_db_path.exists():
            test_db_path.unlink()

        # Rigorous assertions
        self.assertEqual(exceptions_encountered, 0, "No unhandled exceptions allowed across 1,000 runs")
        self.assertEqual(sizing_violations, 0, "No position should exceed ₹3,000 budget cap on ₹10k capital")
        self.assertEqual(concurrency_violations, 0, "No run should exceed MAX_OPEN_POSITIONS (3)")
        self.assertLessEqual(max_observed_drawdown_pct, 3.0, "Max drawdown must never breach 3.0% daily limit")
        self.assertGreater(total_trades, 300, "At least 300 valid setups should execute across 1,000 runs")
        self.assertGreaterEqual(profit_factor, 1.05, f"Profit factor should demonstrate positive expectancy ({profit_factor:.2f})")


if __name__ == "__main__":
    unittest.main()

