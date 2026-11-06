"""Tests for the frozen-baseline paper-trading truth report."""

import tempfile
import unittest
from pathlib import Path

from app.storage.db import DatabaseManager
from app.trading.paper_truth import (
    FROZEN_BASELINE_PATH,
    build_truth_report,
    load_frozen_baseline,
)


def _baseline(min_trades: int = 5) -> dict:
    return {
        "baseline_id": "test_baseline",
        "expectation": {
            "aggregate": {
                "win_rate_pct": 50.0,
                "profit_factor": 1.30,
                "expectancy_pct_per_trade": 0.65,
                "positive_folds": 4,
                "total_folds": 4,
            },
            "acceptance_bands": {
                "min_closed_trades_for_verdict": min_trades,
                "win_rate_tolerance_pct": 0.90,
                "profit_factor_tolerance": 0.90,
            },
        },
    }


class TestFrozenBaselineFile(unittest.TestCase):
    def test_baseline_loads_and_documents_positive_folds(self):
        self.assertTrue(FROZEN_BASELINE_PATH.exists(), "FROZEN_BASELINE.json is required")
        baseline = load_frozen_baseline()
        self.assertEqual(baseline["baseline_id"], "rsi2_swing_v1")
        agg = baseline["expectation"]["aggregate"]
        self.assertEqual(agg["positive_folds"], agg["total_folds"])
        self.assertGreater(agg["profit_factor"], 1.0)
        self.assertIn("primary_strategy", baseline)


class TestBuildTruthReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")
        self.account = "deepseek_flash"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _closed_trade(self, idx: int, pnl: float, fee: float = 1.0, ts: str = None):
        symbol = f"SYM{idx}"
        self.db.record_trade({
            "order_id": f"ord-{idx}",
            "account_id": self.account,
            "symbol": symbol,
            "action": "SELL",
            "quantity": 10,
            "order_type": "MARKET",
            "fill_price": 100.0,
            "total_value_inr": 1000.0,
            "fee_inr": fee,
            "entry_fee_inr": fee,
            "status": "FILLED",
            "pnl_inr": pnl,
            "gross_pnl_inr": round(pnl + 2 * fee, 2),
            "exit_reason": "target",
            "timestamp": ts or "2026-11-05T10:05:00+00:00",
        })

    async def test_insufficient_sample_is_not_judged(self):
        self._closed_trade(1, 50.0)
        self._closed_trade(2, -20.0)
        report = build_truth_report(self.db, self.account, baseline=_baseline(min_trades=5))
        self.assertEqual(report["verdict"], "insufficient_sample")
        self.assertFalse(report["sample"]["is_conclusive"])
        self.assertEqual(report["sample"]["closed_trades"], 2)

    async def test_on_track_when_matching_expectation(self):
        for i in range(8):
            self._closed_trade(i, 60.0)
        report = build_truth_report(self.db, self.account, baseline=_baseline())
        self.assertEqual(report["sample"]["closed_trades"], 8)
        self.assertEqual(report["realized"]["win_rate_pct"], 100.0)
        self.assertGreater(report["realized"]["net_pnl_inr"], 0)
        self.assertEqual(report["verdict"], "on_track")

    async def test_underperforming_when_losing(self):
        for i in range(6):
            self._closed_trade(i, -50.0)
        for i in range(6, 8):
            self._closed_trade(i, 10.0)
        report = build_truth_report(self.db, self.account, baseline=_baseline())
        self.assertEqual(report["verdict"], "underperforming")
        self.assertIn("net_positive", report["verdict_reasons"])
        self.assertLess(report["realized"]["net_pnl_inr"], 0)

    async def test_report_exposes_daily_and_expected(self):
        for i in range(6):
            self._closed_trade(i, 40.0)
        report = build_truth_report(self.db, self.account, baseline=_baseline())
        self.assertEqual(report["expected"]["win_rate_pct"], 50.0)
        self.assertEqual(report["baseline_id"], "test_baseline")
        self.assertEqual(len(report["daily"]), 1)
        self.assertEqual(report["daily"][0]["trades_count"], 6)
        self.assertGreater(report["daily"][0]["net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
