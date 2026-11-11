"""Regression test: the main app's auto-execute toggle must not touch the
legacy Gemini-4 intraday dashboard account (gemini_optimized).

If it did, restarting gemini-agent would re-enable the account behind the
dashboard's back, defeating the gate that stops the intraday churner.
"""

import sys
import types
import unittest
from unittest import mock

from app.trading import strategy_manager as sm_mod


class TestAutoExecuteScope(unittest.TestCase):
    def test_managed_accounts_exclude_legacy_dashboard(self):
        self.assertNotIn("gemini_optimized", sm_mod.MANAGED_ACCOUNT_IDS)
        self.assertIn("deepseek_flash", sm_mod.MANAGED_ACCOUNT_IDS)
        self.assertIn("opus_flash", sm_mod.MANAGED_ACCOUNT_IDS)

    def test_set_auto_execute_does_not_touch_legacy_account(self):
        calls = []
        fake_db = mock.MagicMock()
        fake_db.get_account_state.return_value = {
            "available_cash": 10000.0,
            "total_realized_pnl": 0.0,
        }
        fake_db.save_account_state.side_effect = lambda *a, **k: calls.append((a, k))

        fake_predictor = types.ModuleType("app.trading.predictor")
        fake_predictor.argon_predictor = mock.MagicMock()

        with mock.patch.object(sm_mod, "db", fake_db), \
                mock.patch.dict(sys.modules, {"app.trading.predictor": fake_predictor}):
            sm_mod.StrategyManager().set_auto_execute(True)

        touched = [a[0] for a, _ in calls]
        self.assertNotIn("gemini_optimized", touched)
        self.assertEqual(set(touched), set(sm_mod.MANAGED_ACCOUNT_IDS))

    def test_explicit_legacy_account_still_targetable(self):
        calls = []
        fake_db = mock.MagicMock()
        fake_db.get_account_state.return_value = {"available_cash": 1.0, "total_realized_pnl": 0.0}
        fake_db.save_account_state.side_effect = lambda *a, **k: calls.append(a)
        with mock.patch.object(sm_mod, "db", fake_db):
            sm_mod.StrategyManager().set_auto_execute(False, account_id="gemini_optimized")
        self.assertEqual([a[0] for a in calls], ["gemini_optimized"])


if __name__ == "__main__":
    unittest.main()
