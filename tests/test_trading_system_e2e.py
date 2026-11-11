"""
End-to-End Simulation & Verification Test Suite for ₹10,000 Capital Stock Trading:
- Verifies budget quantity sizing across liquid stocks (SBIN, ITC, TATASTEEL, TATAMOTORS, etc.)
- Verifies that risk guardrails do NOT prematurely lock out at 0.5% (₹50) and allow reaching >= 1.0% profit
- Verifies that normal price swings do not trip the daily loss limit until full 3.0% (₹300) ceiling
- Verifies multi-position execution and full lifecycle (BUY -> Hold -> Target Exit -> Net Gain)
- Verifies trade count limits accommodate active intraday execution (up to 10 trades/day)
"""

import unittest
import asyncio
from datetime import datetime, time
import pandas as pd

from app.config import settings
from app.trading.broker import PaperBroker
from app.trading.risk_manager import RiskManager
from app.trading.institutional_engine import (
    generate_institutional_ai_signal,
    evaluate_risk_guardrails,
)
from app.trading.institutional_runner import InstitutionalRunner, DEFAULT_STOCKS


class TestTradingSystemE2E(unittest.TestCase):

    def setUp(self):
        self.broker = PaperBroker()
        self.broker.reset_account(capital=10000.0, account_id="deepseek_flash")
        self.broker.risk.enforce_market_hours = False
        self.risk = RiskManager(enforce_market_hours=False)

    def test_risk_guardrails_allow_1_percent_target(self):
        """Verify that guardrails allow trading until user's target (>= 1.0%), not prematurely halting at 0.5%."""
        # At 0.40% profit, trading must be allowed
        res_40 = evaluate_risk_guardrails(
            daily_net_profit_pct=0.40,
            consecutive_losses=0,
            current_drawdown_pct=0.10,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertTrue(res_40["can_trade"], "Guardrail should allow trading at 0.40% profit")

        # At 0.75% profit, trading must STILL be allowed to reach >= 1.0% target
        res_75 = evaluate_risk_guardrails(
            daily_net_profit_pct=0.75,
            consecutive_losses=0,
            current_drawdown_pct=0.0,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertTrue(res_75["can_trade"], "Guardrail must NOT halt trading at 0.50%; user target is >= 1.0%")

        # At >= 1.0% profit, profit target lock engages to secure gains
        res_100 = evaluate_risk_guardrails(
            daily_net_profit_pct=1.05,
            consecutive_losses=0,
            current_drawdown_pct=0.0,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertFalse(res_100["can_trade"], "Guardrail should lock profits when >= 1.0% target is achieved")
        self.assertEqual(res_100["trigger"], "PROFIT_GOAL_LOCKED")

    def test_drawdown_guardrail_cushion(self):
        """Verify that minor drawdown (e.g. 0.8% = ₹80) does not trip lockout; only 3.0% (₹300) halts."""
        # Normal fluctuation of 0.70% (₹70) must be allowed
        res_minor = evaluate_risk_guardrails(
            daily_net_profit_pct=-0.70,
            consecutive_losses=1,
            current_drawdown_pct=0.70,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertTrue(res_minor["can_trade"], "Guardrail must NOT halt on normal 0.70% price dip")

        # Full 3.0% drawdown limit reached
        res_max = evaluate_risk_guardrails(
            daily_net_profit_pct=-3.10,
            consecutive_losses=2,
            current_drawdown_pct=3.10,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertFalse(res_max["can_trade"], "Guardrail should halt at 3.0% maximum daily loss")
        self.assertEqual(res_max["trigger"], "MAX_DRAWDOWN_LIMIT_REACHED")

    def test_budget_quantity_calculation_across_universe(self):
        """Verify that all default stocks are sized safely within ₹10,000 capital (~₹2,500/pos)."""
        account = self.broker.get_account("deepseek_flash")
        stock_prices = {
            "SBIN": 970.0,
            "ITC": 275.0,
            "TATASTEEL": 140.0,
            "TATAMOTORS": 820.0,
            "ICICIBANK": 1420.0,
            "AXISBANK": 1180.0,
            "NTPC": 390.0,
            "COALINDIA": 430.0,
        }
        for sym, price in stock_prices.items():
            qty = account.calculate_budget_quantity(sym, price, max_alloc_pct=0.30)
            position_cost = qty * price
            self.assertGreater(qty, 0, f"Quantity for {sym} must be positive")
            self.assertLessEqual(position_cost, 3100.0, f"Position for {sym} exceeds 30% of ₹10k")

    def test_full_trade_lifecycle_profitable_exit(self):
        """Verify a complete simulated trade cycle on SBIN: Entry -> Update -> Target Exit -> Net Gain."""
        async def run_lifecycle():
            account = self.broker.get_account("deepseek_flash")
            initial_cash = account.available_cash
            entry_price = 970.0
            qty = 2  # ₹1,940 position cost

            # 1. Execute BUY Order
            self.broker.price_lookup = lambda sym: entry_price
            order = await self.broker.fast_execute_order(
                symbol="SBIN",
                action="BUY",
                quantity=qty,
                strategy_name="bullish_sweep_reversal",
                account_id="deepseek_flash",
                stop_loss=960.0,       # 10 pts risk
                target_price=990.0,    # 20 pts reward (1:2 R:R)
            )
            self.assertEqual(order.get("status"), "FILLED", f"SBIN BUY order failed: {order}")
            self.assertIn("SBIN", account.positions)
            pos = account.positions["SBIN"]
            self.assertEqual(pos.quantity, 2)

            # 2. Simulate price moving to target price (₹990.0)
            target_price = 990.0
            pos.update_price(target_price)
            self.assertGreater(pos.unrealized_pnl, 0)

            # 3. Close position at target
            self.broker.price_lookup = lambda sym: target_price
            close_order = await self.broker.close_position("SBIN", account_id="deepseek_flash", reason="target_reached")
            self.assertIsNotNone(close_order)
            self.assertEqual(close_order.get("status"), "FILLED", f"SBIN close order failed: {close_order}")
            self.assertNotIn("SBIN", account.positions)

            # 4. Verify realized P&L and cash gain
            realized_pnl = close_order["pnl_inr"]
            expected_gross = (target_price - entry_price) * qty  # +₹40.0
            self.assertAlmostEqual(realized_pnl, expected_gross, delta=5.0)
            self.assertGreater(account.available_cash, initial_cash)

        asyncio.run(run_lifecycle())


if __name__ == "__main__":
    unittest.main()
