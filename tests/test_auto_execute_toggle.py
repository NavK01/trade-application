"""Regression tests for the auto-execute toggle path.

Two latent bugs are guarded here:
  * ``DatabaseManager.save_account_state(account_id, auto_execute=...)`` used to
    raise ``IndexError`` because it required positional cash arguments; the
    account_id-only form must instead preserve the stored cash/P&L.
  * ``/api/trading/auto-toggle`` used to call that broken signature and crash.

The frozen-baseline paper-trade programme depends on being able to disable
unvalidated auto-execution without a 500, so this path is contract-critical.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.db import DatabaseManager


class TestSaveAccountStateSignature(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_account_id_only_preserves_cash_and_sets_flag(self):
        before = self.db.get_account_state("deepseek_flash")
        self.db.save_account_state("deepseek_flash", auto_execute=False)
        after = self.db.get_account_state("deepseek_flash")
        self.assertEqual(int(after["auto_execute"]), 0)
        # Cash/P&L must be untouched by an auto_execute-only update.
        self.assertEqual(float(after["available_cash"]), float(before["available_cash"]))
        self.assertEqual(float(after["total_realized_pnl"]), float(before["total_realized_pnl"]))

    def test_account_id_only_can_enable_flag(self):
        self.db.save_account_state("deepseek_flash", auto_execute=True)
        self.assertEqual(int(self.db.get_account_state("deepseek_flash")["auto_execute"]), 1)

    def test_legacy_positional_form_still_works(self):
        self.db.save_account_state("deepseek_flash", 9500.0, -500.0, auto_execute=False)
        state = self.db.get_account_state("deepseek_flash")
        self.assertEqual(round(float(state["available_cash"]), 2), 9500.0)
        self.assertEqual(round(float(state["total_realized_pnl"]), 2), -500.0)
        self.assertEqual(int(state["auto_execute"]), 0)


class TestAutoToggleEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_disable_does_not_crash_and_reports_state(self):
        from app import main as app_main
        from app.trading import strategy_manager as sm_mod

        with mock.patch.object(
            sm_mod.strategy_manager, "set_auto_execute", return_value=False
        ) as setter:
            res = await app_main.toggle_auto_execution(
                enabled=False, account_id="deepseek_flash", auth_user={"username": "test"}
            )

        self.assertFalse(res["auto_execute"])
        setter.assert_called_once_with(False, account_id="deepseek_flash")

    async def test_enable_runs_swing_only_and_keeps_legacy_off(self):
        from app import main as app_main
        from app.trading import strategy_manager as sm_mod
        from app.trading import swing_live_runner as sw_mod
        from app.trading import predictor as pred_mod
        from app.trading import institutional_runner as inst_mod

        saved = (
            sw_mod.swing_live_runner.auto_execute,
            pred_mod.argon_predictor.auto_execute,
            inst_mod.institutional_runner.auto_execute,
        )
        try:
            with mock.patch.object(
                sm_mod.strategy_manager, "set_auto_execute", return_value=True
            ):
                res = await app_main.toggle_auto_execution(
                    enabled=True, account_id="deepseek_flash", auth_user={"username": "test"}
                )
            self.assertTrue(res["auto_execute"])
            self.assertTrue(sw_mod.swing_live_runner.auto_execute)
            # Legacy index/intraday engines must remain off by default.
            self.assertFalse(res["legacy_intraday_engines_enabled"])
            self.assertFalse(pred_mod.argon_predictor.auto_execute)
            self.assertFalse(inst_mod.institutional_runner.auto_execute)
        finally:
            (
                sw_mod.swing_live_runner.auto_execute,
                pred_mod.argon_predictor.auto_execute,
                inst_mod.institutional_runner.auto_execute,
            ) = saved


if __name__ == "__main__":
    unittest.main()
