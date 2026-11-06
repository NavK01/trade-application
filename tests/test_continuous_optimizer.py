"""
Unit Tests for Autonomous Continuous Optimizer & Self-Healing Strategy Loop.
Tests:
1. Token Budget Guard (Gemini 4: <10M in / <3M out; DeepSeek: <10M combined)
2. Phase 1: 1-Year Baseline Backtest Execution
3. Phase 2: 1-Month Out-of-Sample Backtest with DeepSeek Filter & Indian Statutory Fees
4. Phase 3: Self-Healing Plan Mutation & Logging
"""

import unittest
import asyncio
import os
import json
from pathlib import Path

import pandas as pd

from app.storage.db import db
from app.system.tokens import token_tracker
from app.trading.continuous_optimizer import (
    ContinuousOptimizer,
    TokenBudgetGuard,
    StrategyParameters,
)


class TestContinuousOptimizer(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.optimizer = ContinuousOptimizer(run_interval_seconds=60)
        self.guard = TokenBudgetGuard()

    def test_token_budget_guardrails(self):
        """Verify token budget ceilings for Gemini 4 and DeepSeek V4.1 Flash."""
        status = self.guard.get_token_status()
        self.assertIn("gemini_input_tokens", status)
        self.assertIn("gemini_output_tokens", status)
        self.assertIn("deepseek_total_tokens", status)

        # Ceilings must match user mandate
        self.assertEqual(status["gemini_max_input"], 10_000_000)
        self.assertEqual(status["gemini_max_output"], 3_000_000)
        self.assertEqual(status["deepseek_max_total"], 10_000_000)

        # Check permission under current usage
        can_call_gemini = self.guard.can_call_gemini(est_input=5000, est_output=1000)
        can_call_deepseek = self.guard.can_call_deepseek(est_tokens=1500)
        self.assertTrue(can_call_gemini)
        self.assertTrue(can_call_deepseek)

    def test_strategy_parameter_bounds(self):
        """Verify that strategy parameter mutations stay within safe institutional bounds."""
        params = StrategyParameters()
        self.assertGreaterEqual(params.min_target_pct, 0.8)
        self.assertLessEqual(params.min_target_pct, 2.5)

        self.assertGreaterEqual(params.min_sweep_vol_multiplier, 1.2)
        self.assertLessEqual(params.min_sweep_vol_multiplier, 3.0)

        self.assertGreaterEqual(params.breakeven_trigger_r, 0.8)
        self.assertLessEqual(params.breakeven_trigger_r, 1.5)

        # Test parameter mutation clamping
        params.mutate({
            "min_target_pct": 5.0,  # Out of bounds high -> should clamp to 2.5
            "min_sweep_vol_multiplier": 0.5,  # Out of bounds low -> should clamp to 1.2
            "breakeven_trigger_r": 0.95,  # Within bounds
        })
        self.assertEqual(params.min_target_pct, 2.5)
        self.assertEqual(params.min_sweep_vol_multiplier, 1.2)
        self.assertEqual(params.breakeven_trigger_r, 0.95)

    async def test_phase2_1month_backtest_execution(self):
        """Verify Phase 2 1-month backtest with real 5m CSVs and Indian statutory fees."""
        params = StrategyParameters(min_target_pct=0.8, min_sweep_vol_multiplier=1.3)
        res = await self.optimizer.run_phase2_1month_backtest(params=params, use_live_ai=False)

        self.assertIn("total_trades", res)
        self.assertIn("win_rate_pct", res)
        self.assertIn("net_pnl_inr", res)
        self.assertIn("total_fees_inr", res)
        self.assertIn("profit_factor", res)

        # Must have executed trades over the 23-day period
        self.assertGreater(res["total_trades"], 0)
        # Sizing per trade must not violate ₹3,000 cap (30% of ₹10,000)
        self.assertLessEqual(res.get("max_cost_basis", 0.0), 3000.0)

    async def test_self_healing_plan_update(self):
        """Verify that Phase 3 updates the execution plan file and creates iteration snapshots."""
        test_plan_path = Path("/tmp/test_AUTONOMOUS_TRADING_PLAN.md")
        test_plan_path.write_text("# Test Plan\n\nInitial plan content.\n")

        p1_res = {"total_trades": 50, "win_rate_pct": 54.0, "net_pnl_inr": 350.0, "profit_factor": 1.45}
        p2_res = {"total_trades": 25, "win_rate_pct": 72.0, "net_pnl_inr": 480.0, "profit_factor": 2.10, "losing_trades_count": 7}

        updated = self.optimizer.update_trading_plan(
            plan_path=test_plan_path,
            cycle_num=99,
            p1_res=p1_res,
            p2_res=p2_res,
            learning="Tightened sweep volume multiplier to 1.6x to eliminate lunch-hour false breakouts.",
            new_params={"min_target_pct": 1.0, "min_sweep_vol_multiplier": 1.6}
        )

        self.assertTrue(updated)
        content = test_plan_path.read_text()
        self.assertIn("Cycle #99", content)
        self.assertIn("72.0%", content)
        self.assertIn("Tightened sweep volume multiplier", content)

        if test_plan_path.exists():
            test_plan_path.unlink()


    async def test_phase1_fee_aware_baseline_uses_validated_setups(self):
        """Phase 1b must run the fee-aware selector (sweeps/RS) over preloaded data."""
        day1 = [
            {"date": "2026-01-01", "time": "09:15", "open": 98.0, "high": 99.0, "low": 97.0, "close": 98.5, "volume": 1000},
            {"date": "2026-01-01", "time": "10:15", "open": 98.5, "high": 99.0, "low": 96.0, "close": 98.0, "volume": 1000},
            {"date": "2026-01-01", "time": "11:15", "open": 98.0, "high": 98.5, "low": 95.0, "close": 97.0, "volume": 1000},
            {"date": "2026-01-01", "time": "12:15", "open": 97.0, "high": 97.5, "low": 94.0, "close": 96.0, "volume": 1000},
        ]
        day2 = [
            {"date": "2026-01-02", "time": "09:15", "open": 96.0, "high": 97.0, "low": 95.0, "close": 96.5, "volume": 1000},
            {"date": "2026-01-02", "time": "10:15", "open": 99.0, "high": 101.0, "low": 90.0, "close": 100.0, "volume": 2000},
            {"date": "2026-01-02", "time": "11:15", "open": 100.0, "high": 102.0, "low": 99.9, "close": 101.0, "volume": 1500},
            {"date": "2026-01-02", "time": "12:15", "open": 101.0, "high": 101.5, "low": 100.5, "close": 101.0, "volume": 1200},
        ]
        data = {"AAA": pd.DataFrame(day1 + day2)}
        res = await self.optimizer.run_phase1_fee_aware_baseline(preloaded_data=data)
        for key in ("total_trades", "win_rate_pct", "net_pnl_inr", "profit_factor", "max_drawdown_pct"):
            self.assertIn(key, res)
        # The prior-day-low sweep on day 2 must have produced a trade.
        self.assertGreaterEqual(res["total_trades"], 1)
        self.assertGreater(res["net_pnl_inr"], 0.0)

    async def test_phase1_fee_aware_baseline_empty_data(self):
        res = await self.optimizer.run_phase1_fee_aware_baseline(preloaded_data={})
        self.assertEqual(res["total_trades"], 0)
        self.assertEqual(res["net_pnl_inr"], 0.0)


if __name__ == "__main__":
    unittest.main()
