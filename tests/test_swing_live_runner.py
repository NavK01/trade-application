"""Tests for the live multi-day swing paper-trading runner and delivery-hold plumbing."""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app.config import ACCOUNT_IDS
from app.storage.db import DatabaseManager
from app.trading import market_hours
from app.trading.broker import PaperBroker
from app.trading.swing_live_runner import SwingLiveRunner

IST = market_hours.IST
FIXED_NOW = datetime.datetime(2026, 11, 5, 10, 0, tzinfo=IST)  # Thursday, entry window


def _now() -> datetime.datetime:
    return FIXED_NOW


def _signal_df() -> pd.DataFrame:
    """A daily frame whose last completed bar is RSI-2 oversold with an uptick."""
    closes = [100.0] * 59 + [96.0, 88.0, 88.5]
    return pd.DataFrame({
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    })


class _FakePosition:
    def __init__(self, delivery_hold: bool):
        self.delivery_hold = delivery_hold


class _FakeAccount:
    def __init__(self, positions):
        self.positions = positions


class _FakeEngine:
    def __init__(self, accounts):
        self.accounts = accounts
        self.closed = []

    def get_account(self, account_id):
        return self.accounts.get(account_id)

    async def close_position(self, symbol, account_id=None, reason=None):
        acc = self.accounts.get(account_id)
        if acc and symbol in acc.positions:
            del acc.positions[symbol]
            self.closed.append((account_id, symbol, reason))
            return {"status": "FILLED", "symbol": symbol, "exit_reason": reason}
        return None


class SwingRunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")
        for acc_id in ACCOUNT_IDS:
            self.db.save_account_state(acc_id, 10_000.0, 0.0, auto_execute=False)
        self.broker = PaperBroker(
            database=self.db,
            price_lookup=lambda sym: 88.0,
            enforce_market_hours=False,
            now_provider=_now,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def _scan(self, runner, now=FIXED_NOW):
        import asyncio
        return asyncio.run(runner.run_daily_scan(now))

    def _enable(self, account_id: str):
        self.db.save_account_state(account_id, 10_000.0, 0.0, auto_execute=True)

    def _runner(self, universe, provider=None):
        return SwingLiveRunner(
            universe=universe,
            data_provider=provider or (lambda sym: _signal_df()),
            now_provider=_now,
            price_provider=lambda sym: 88.0,
            broker_engine=self.broker,
            database=self.db,
        )


class TestSwingEntry(SwingRunnerTestBase):
    def test_daily_scan_opens_delivery_hold_position(self):
        self._enable("deepseek_flash")
        runner = self._runner(["RELIANCE"])
        res = self._scan(runner)

        self.assertEqual(res["status"], "ok")
        self.assertIn("RELIANCE", res["entered"])
        acc = self.broker.get_account("deepseek_flash")
        self.assertIn("RELIANCE", acc.positions)
        pos = acc.positions["RELIANCE"]
        self.assertTrue(pos.delivery_hold)
        self.assertLess(pos.stop_loss, pos.avg_price)
        self.assertGreater(pos.target_price, pos.avg_price)

        # Persisted with the delivery_hold flag so EOD square-off skips it.
        rows = self.db.get_open_positions("deepseek_flash")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["delivery_hold"]), 1)

        trades = self.db.get_trades("deepseek_flash", limit=5)
        buys = [t for t in trades if t["action"] == "BUY"]
        self.assertEqual(buys[0]["strategy_name"], "rsi2_oversold_swing")

    def test_daily_scan_respects_concurrency_cap(self):
        self._enable("deepseek_flash")
        runner = self._runner(["RELIANCE", "TCS", "INFY", "SBIN"])
        res = self._scan(runner)
        acc = self.broker.get_account("deepseek_flash")
        self.assertEqual(len(acc.positions), 2)
        self.assertEqual(len(res["entered"]), 2)

    def test_daily_scan_is_idempotent_for_the_same_day(self):
        self._enable("deepseek_flash")
        runner = self._runner(["RELIANCE"])
        self._scan(runner)
        second = self._scan(runner)
        self.assertEqual(second["status"], "already_scanned")
        self.assertEqual(len(self.broker.get_account("deepseek_flash").positions), 1)

    def test_daily_scan_noop_when_auto_execute_off(self):
        runner = self._runner(["RELIANCE"])
        res = self._scan(runner)
        self.assertEqual(res["entered"], [])
        self.assertEqual(len(self.broker.get_account("deepseek_flash").positions), 0)

    def test_daily_scan_skips_outside_entry_window(self):
        self._enable("deepseek_flash")
        runner = self._runner(["RELIANCE"])
        late = datetime.datetime(2026, 11, 5, 15, 25, tzinfo=IST)
        res = self._scan(runner, now=late)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(len(self.broker.get_account("deepseek_flash").positions), 0)

    def test_dropped_today_bar_is_not_used_for_signal(self):
        self._enable("deepseek_flash")

        def with_today(sym):
            df = _signal_df()
            df["date"] = ["2026-01-01"] * (len(df) - 1) + ["2026-11-05"]
            return df

        runner = self._runner(["RELIANCE"], provider=with_today)
        res = self._scan(runner)
        # The last (today) bar is dropped, so the uptick signal is gone.
        self.assertEqual(res["entered"], [])


class TestSwingExits(SwingRunnerTestBase):
    async def _enter(self, symbol="RELIANCE") -> None:
        await self.broker.fast_execute_order(
            symbol=symbol,
            action="BUY",
            quantity=10,
            strategy_name="rsi2_oversold_swing",
            account_id="deepseek_flash",
            stop_loss=80.0,
            target_price=100.0,
            delivery_hold=True,
        )

    def test_manage_positions_time_exit_closes_old_hold(self):
        import asyncio

        async def scenario():
            await self._enter()
            acc = self.broker.get_account("deepseek_flash")
            oldest = (FIXED_NOW - datetime.timedelta(days=90)).astimezone(datetime.timezone.utc)
            acc.positions["RELIANCE"].created_at = oldest.isoformat()
            runner = self._runner(["RELIANCE"])
            closed = await runner.manage_positions()
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["exit_reason"], "time_exit")
            self.assertNotIn("RELIANCE", acc.positions)

        asyncio.run(scenario())

    def test_manage_positions_keeps_young_hold(self):
        import asyncio

        async def scenario():
            await self._enter()
            runner = self._runner(["RELIANCE"])
            closed = await runner.manage_positions()
            self.assertEqual(closed, [])
            self.assertIn("RELIANCE", self.broker.get_account("deepseek_flash").positions)

        asyncio.run(scenario())


class TestEodSquareOffSkipsDeliveryHold(unittest.TestCase):
    def test_square_off_leaves_delivery_holds_alone(self):
        from app.trading import strategy_manager as sm_module
        from app.trading.strategy_manager import StrategyManager

        accounts = {
            acc_id: _FakeAccount({})
            for acc_id in ACCOUNT_IDS
        }
        accounts["deepseek_flash"].positions = {
            "SWING": _FakePosition(delivery_hold=True),
            "INTRADAY": _FakePosition(delivery_hold=False),
        }
        engine = _FakeEngine(accounts)

        async def _noop(self):
            return []

        import asyncio

        with mock.patch.object(sm_module, "execution_engine", engine), \
             mock.patch.object(StrategyManager, "record_daily_snapshots", _noop):
            manager = StrategyManager()
            asyncio.run(manager.auto_square_off_all_positions("eod_square_off"))

        self.assertIn("SWING", accounts["deepseek_flash"].positions)
        self.assertNotIn("INTRADAY", accounts["deepseek_flash"].positions)
        self.assertIn(("deepseek_flash", "INTRADAY", "eod_square_off"), engine.closed)


class TestDeliveryHoldPersistence(unittest.TestCase):
    def test_round_trip_through_broker_and_db(self):
        tmpdir = tempfile.TemporaryDirectory()
        try:
            db = DatabaseManager(Path(tmpdir.name) / "test.db")
            broker = PaperBroker(
                database=db,
                price_lookup=lambda sym: 100.0,
                enforce_market_hours=False,
                now_provider=_now,
            )
            import asyncio

            async def scenario():
                await broker.fast_execute_order(
                    symbol="SBIN",
                    action="BUY",
                    quantity=5,
                    strategy_name="rsi2_oversold_swing",
                    account_id="deepseek_flash",
                    stop_loss=90.0,
                    target_price=110.0,
                    delivery_hold=True,
                )

            asyncio.run(scenario())
            rows = db.get_open_positions("deepseek_flash")
            self.assertEqual(int(rows[0]["delivery_hold"]), 1)

            # A fresh broker (simulated restart) reloads the flag from SQLite.
            reloaded = PaperBroker(
                database=db,
                price_lookup=lambda sym: 100.0,
                enforce_market_hours=False,
                now_provider=_now,
            )
            self.assertTrue(reloaded.get_account("deepseek_flash").positions["SBIN"].delivery_hold)
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
