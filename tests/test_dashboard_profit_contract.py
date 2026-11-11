"""Contract tests for the dashboard per-trade and per-day profit display.

These tests guard the frontend <-> API contract that regressed silently: the
broker and SQLite layer expose rupee-suffixed keys (``total_portfolio_value_inr``,
``pnl_inr``, ``fee_inr``) while the dashboard JavaScript must read exactly those
names, and the Daily P&L tab must be wired to ``/api/performance/daily``. A
mismatch makes every trade/day render as ₹0.00 even when the backend is correct.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.db import DatabaseManager
from app.trading.broker import PaperBroker

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


class TestPortfolioProfitContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")
        self.broker = PaperBroker(database=self.db)
        # Bypass the session clock so the contract test is deterministic.
        self.broker.risk.enforce_market_hours = False

    def tearDown(self):
        self.tmpdir.cleanup()

    async def _round_trip(self):
        """BUY 10 @ ₹100 then SELL 10 @ ₹105 (net positive after fees)."""
        self.broker.price_lookup = lambda sym: 100.0
        buy = await self.broker.fast_execute_order(
            symbol="SBIN", action="BUY", quantity=10,
            account_id="deepseek_flash", strategy_name="contract_test",
        )
        self.assertEqual(buy["status"], "FILLED", buy)
        self.broker.price_lookup = lambda sym: 105.0
        sell = await self.broker.fast_execute_order(
            symbol="SBIN", action="SELL", quantity=10,
            account_id="deepseek_flash", strategy_name="contract_test",
            exit_reason="target",
        )
        self.assertEqual(sell["status"], "FILLED", sell)
        return buy, sell

    async def test_summary_exposes_dashboard_keys(self):
        await self._round_trip()
        summary = self.broker.get_portfolio_summary("deepseek_flash")
        for key in (
            "initial_capital_inr",
            "available_cash_inr",
            "total_portfolio_value_inr",
            "realized_pnl_inr",
            "unrealized_pnl_inr",
            "total_fees_paid_inr",
            "net_pnl_after_ai_inr",
            "total_orders_executed",
            "positions",
        ):
            self.assertIn(key, summary, f"portfolio summary missing {key}")
        self.assertGreater(summary["realized_pnl_inr"], 0.0)

    async def test_trade_rows_expose_per_trade_net_profit(self):
        await self._round_trip()
        trades = self.db.get_trades("deepseek_flash", limit=10)
        sells = [t for t in trades if t["action"] == "SELL"]
        self.assertTrue(sells, "expected a completed SELL row")
        row = sells[0]
        for key in ("pnl_inr", "gross_pnl_inr", "fee_inr", "exit_reason"):
            self.assertIn(key, row, f"trade row missing {key}")
        self.assertGreater(row["pnl_inr"], 0.0)
        self.assertEqual(row["exit_reason"], "target")

    async def test_daily_row_exposes_per_day_profit(self):
        await self._round_trip()
        self.db.record_daily_snapshot(
            date_str="2026-11-06",
            starting_balance=10000.0,
            ending_balance=10045.0,
            realized_pnl=45.0,
            trades_count=1,
            win_rate_pct=100.0,
            total_tokens=0,
            ai_cost_inr=0.0,
            account_id="deepseek_flash",
            fees_inr=5.0,
        )
        history = self.db.get_daily_performance("deepseek_flash", limit=5)
        self.assertTrue(history, "expected a daily performance row")
        for key in (
            "date",
            "realized_pnl",
            "fees_inr",
            "trades_count",
            "win_rate_pct",
            "net_profit_before_ai",
            "net_return_pct",
        ):
            self.assertIn(key, history[0], f"daily row missing {key}")
        self.assertGreater(history[0]["net_profit_before_ai"], 0.0)

    async def test_daily_trade_pnl_aggregates_by_ist_date(self):
        await self._round_trip()
        days = self.db.get_daily_trade_pnl("deepseek_flash", limit=5)
        self.assertEqual(len(days), 1, days)
        day = days[0]
        self.assertEqual(day["trades_count"], 1)
        self.assertGreater(day["net_pnl"], 0.0)
        self.assertGreater(day["fees_inr"], 0.0)
        self.assertGreater(day["gross_pnl"], day["net_pnl"])
        self.assertEqual(day["win_rate_pct"], 100.0)
        # A date is required for the dashboard row, in ISO YYYY-MM-DD form.
        self.assertRegex(day["date"], r"^\d{4}-\d{2}-\d{2}$")

    async def test_daily_endpoint_merges_trades_with_net_return_pct(self):
        """The /api/performance/daily handler must surface a complete row for a
        day that has trades but no EOD snapshot, including a net return percent so
        the Daily P&L tab never shows a blank Net % cell while real P&L exists."""
        from app import main as app_main

        await self._round_trip()
        with mock.patch.object(app_main, "db", self.db):
            res = await app_main.get_daily_performance(
                account_id="deepseek_flash", limit=5, auth_user={"username": "test"}
            )
        history = res["daily_history"]
        self.assertTrue(history, "expected a merged daily row")
        row = history[0]
        self.assertEqual(row["source"], "trades")
        self.assertEqual(row["trades_count"], 1)
        self.assertGreater(row["net_profit_before_ai"], 0.0)
        self.assertIsNotNone(row["net_return_pct"])
        self.assertGreater(row["net_return_pct"], 0.0)


class TestDashboardStaticContract(unittest.TestCase):
    def _js(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_ledger_reads_rupee_suffixed_fields(self):
        js = self._js()
        self.assertIn("pnl_inr", js)
        self.assertIn("fee_inr", js)

    def test_portfolio_reads_rupee_suffixed_fields(self):
        js = self._js()
        self.assertIn("total_portfolio_value_inr", js)
        self.assertIn("net_pnl_after_ai_inr", js)

    def test_daily_tab_wired_to_performance_endpoint(self):
        self.assertIn("/api/performance/daily", self._js())
        self.assertIn("/api/trading/risk-state", self._js())
        self.assertIn('id="tab-btn-daily"', self._html())
        self.assertIn('id="daily-tbody"', self._html())

    def test_no_call_to_undefined_signal_loader(self):
        # Regression: the refresh loop called loadInstitutionalSignal(), which was
        # never defined, throwing a ReferenceError every cycle.
        self.assertNotIn("loadInstitutionalSignal", self._js())


if __name__ == "__main__":
    unittest.main()
