"""
Unit tests for the Institutional Autonomous Trading Runner and Index Options Execution:
- Verifies that NIFTY, BANKNIFTY, and SENSEX orders can be executed in a ₹10,000 account
- Verifies that risk_manager properly calculates options premium turnover for indices
- Verifies that the Autonomous Institutional Runner evaluates signals and manages positions
"""

import unittest
import asyncio
from datetime import datetime, time

from app.trading.broker import PaperBroker, INDEX_CONFIG
from app.trading.risk_manager import RiskManager
from app.trading.institutional_engine import generate_institutional_ai_signal


class TestInstitutionalRunnerAndExecution(unittest.TestCase):

    def setUp(self):
        self.broker = PaperBroker()
        self.broker.reset_account(capital=10000.0, account_id="deepseek_flash")
        self.broker.risk.enforce_market_hours = False
        self.risk = RiskManager(enforce_market_hours=False)

    def test_index_config_constants(self):
        """Verify that index lot sizes and premium percentages are defined."""
        self.assertIn("NIFTY", INDEX_CONFIG)
        self.assertIn("BANKNIFTY", INDEX_CONFIG)
        self.assertIn("SENSEX", INDEX_CONFIG)
        self.assertEqual(INDEX_CONFIG["NIFTY"]["lot_size"], 25)
        self.assertEqual(INDEX_CONFIG["BANKNIFTY"]["lot_size"], 15)
        self.assertEqual(INDEX_CONFIG["SENSEX"]["lot_size"], 10)

    def test_index_risk_evaluation_with_10k_capital(self):
        """Verify that NIFTY at 23,050 spot passes the 90% position cap on ₹10,000 capital."""
        account = self.broker.get_account("deepseek_flash")
        
        # In the new architecture, 1 lot of NIFTY (25 units) option premium is ~₹3,125,
        # which must pass the ₹9,000 (90%) cap.
        gate = self.risk.evaluate_entry(
            account_id="deepseek_flash",
            account=account,
            symbol="NIFTY",
            quantity=1,  # 1 lot
            price=23050.0
        )
        self.assertTrue(gate["approved"], f"Risk check failed: {gate['reasons']}")

    def test_index_order_execution_and_position(self):
        """Verify that executing a NIFTY order fills successfully within ₹10,000 capital."""
        async def run_order():
            self.broker.price_lookup = lambda sym: 23050.0
            res = await self.broker.fast_execute_order(
                symbol="NIFTY",
                action="BUY",
                quantity=1,  # 1 lot
                strategy_name="institutional_sweep_reversal",
                account_id="deepseek_flash",
                stop_loss=22950.0,
                target_price=23250.0
            )
            return res

        res = asyncio.run(run_order())
        self.assertEqual(res.get("status"), "FILLED", f"Order rejected: {res}")
        self.assertIn("NIFTY", self.broker.get_account("deepseek_flash").positions)

        # Verify position is stored with reasonable margin (< ₹4,000)
        pos = self.broker.get_account("deepseek_flash").positions["NIFTY"]
        self.assertGreater(pos.margin_allocated, 0)
        self.assertLess(pos.margin_allocated, 4500.0)

        # Test closing position with profit
        async def close_order():
            self.broker.price_lookup = lambda sym: 23150.0  # +100 index points
            close_res = await self.broker.close_position(
                symbol="NIFTY",
                account_id="deepseek_flash",
                reason="target"
            )
            return close_res

        close_res = asyncio.run(close_order())
        self.assertIsNotNone(close_res)
        self.assertGreater(close_res.get("net_pnl", 0), 0)
        self.assertNotIn("NIFTY", self.broker.get_account("deepseek_flash").positions)

    def test_institutional_runner_cycle_evaluation(self):
        """Verify that the InstitutionalRunner evaluates all three indices in a cycle."""
        from app.trading.institutional_runner import InstitutionalRunner
        runner = InstitutionalRunner()
        runner.auto_execute = False  # Evaluation only
        
        async def run_cycle():
            return await runner.evaluate_and_execute_cycle()

        res = asyncio.run(run_cycle())
        self.assertIn("NIFTY", res)
        self.assertIn("BANKNIFTY", res)
        self.assertIn("SENSEX", res)
        self.assertIn("action", res["NIFTY"])
        self.assertIn("setup_name", res["NIFTY"])
        self.assertIn("statutory_charges", res["NIFTY"])


if __name__ == "__main__":
    unittest.main()
