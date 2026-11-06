"""Tests for the fee-aware high-win-rate strategies and the backtester's
profit-lock ratchet / prior-day mapping.
"""

import unittest
from unittest.mock import patch

import pandas as pd

from app.trading.fee_aware_strategies import (
    FeeAwareOpeningPullback,
    LiquiditySweepLong,
    LiquiditySweepShort,
    RelativeStrengthLeaderBreakout,
    compute_cross_sectional_metrics,
    compute_features,
    _ratchet_levels,
)
from app.trading.swing_strategies import (
    RSI2OversoldSwing,
    NR7SwingBreakout,
    SwingBacktestEngine,
    compute_swing_features,
)
from app.trading.backtester import (
    HistoricalBacktestEngine,
    edge_to_fee_ratio,
    fee_to_risk_pct,
)
from app.trading.fee_aware_selector import (
    FeeAwareSelector,
    select_fee_aware_setup,
)


def _feature_row(**overrides):
    base = {
        "time": "09:15",
        "open": 120.0,
        "high": 120.6,
        "low": 120.1,
        "close": 120.5,
        "ema_20": 120.0,
        "ema_50": 118.0,
        "ema50_slope": 0.06,
        "atr_pct": 0.80,
        "body_pct": 0.50,
        "lower_wick_ratio": 0.10,
        "upper_wick_ratio": 0.05,
    }
    base.update(overrides)
    # Two rows: a filler row plus the signal row, both carrying feature fields.
    return pd.DataFrame([{**base, "time": "09:00"}, base])


class TestComputeFeatures(unittest.TestCase):
    def test_body_and_wicks(self):
        df = pd.DataFrame([
            {"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0},
            {"open": 105.0, "high": 107.0, "low": 104.0, "close": 104.0},
        ])
        out = compute_features(df)
        self.assertAlmostEqual(out.iloc[0]["body_pct"], 5.0, places=4)
        # lower wick = min(o,c)-low = 100-90 = 10 ; range = 20 -> 0.5
        self.assertAlmostEqual(out.iloc[0]["lower_wick_ratio"], 0.5, places=4)
        # upper wick = high - max(o,c) = 110-105 = 5 ; 5/20 = 0.25
        self.assertAlmostEqual(out.iloc[0]["upper_wick_ratio"], 0.25, places=4)
        self.assertAlmostEqual(out.iloc[1]["body_pct"], (104.0 - 105.0) / 105.0 * 100, places=4)

    def test_ema_columns_present(self):
        df = pd.DataFrame([
            {"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i}
            for i in range(30)
        ])
        out = compute_features(df)
        for col in ("ema_20", "ema_50", "ema50_slope", "atr_pct"):
            self.assertIn(col, out.columns)


class TestOpeningPullback(unittest.TestCase):
    def test_happy_path(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(), interval="1h")
        self.assertTrue(res["active"])
        self.assertEqual(res["signal"], "BUY")
        self.assertEqual(res["strategy_id"], "opening_ema_pullback")
        self.assertEqual(res["stop_pct"], 0.95)
        self.assertEqual(res["target_pct"], 0.90)
        self.assertEqual(res["ratchet_levels"], [{"trigger_pct": 0.28, "lock_pct": 0.16}])
        self.assertFalse(res["hold_to_close"])

    def test_insufficient_bars(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row().iloc[:1], interval="1h")
        self.assertFalse(res["active"])

    def test_wrong_session_bar(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(time="10:15"), interval="1h")
        self.assertFalse(res["active"])

    def test_no_uptrend(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(ema_20=117.0, ema_50=118.0))
        self.assertFalse(res["active"])

    def test_low_volatility(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(atr_pct=0.20))
        self.assertFalse(res["active"])

    def test_close_below_ema(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(close=119.5))
        self.assertFalse(res["active"])

    def test_body_out_of_range(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(body_pct=3.0))
        self.assertFalse(res["active"])

    def test_upper_wick_too_large(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(upper_wick_ratio=0.9))
        self.assertFalse(res["active"])

    def test_flat_slope(self):
        res = FeeAwareOpeningPullback.evaluate(_feature_row(ema50_slope=0.0))
        self.assertFalse(res["active"])


class TestLiquiditySweep(unittest.TestCase):
    def _sweep_row(self, **overrides):
        base = {
            "time": "10:00",
            "open": 99.0, "high": 101.0, "low": 95.0, "close": 100.5,
            "atr_pct": 0.25, "body_pct": 1.5, "lower_wick_ratio": 0.6,
        }
        base.update(overrides)
        return pd.DataFrame([{**base, "time": "09:55"}, base])

    def test_happy_path(self):
        res = LiquiditySweepLong.evaluate(
            self._sweep_row(), prev_day_ohlc={"low": 96.0, "high": 105.0, "close": 100.0}
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["strategy_id"], "pdl_liquidity_sweep")
        self.assertEqual(res["stop_pct"], 0.60)
        self.assertEqual(len(res["ratchet_levels"]), 3)

    def test_missing_prior_day(self):
        self.assertFalse(LiquiditySweepLong.evaluate(self._sweep_row(), prev_day_ohlc=None)["active"])

    def test_no_sweep(self):
        res = LiquiditySweepLong.evaluate(
            self._sweep_row(low=99.0), prev_day_ohlc={"low": 96.0}
        )
        self.assertFalse(res["active"])

    def test_midday_lull_blocked(self):
        res = LiquiditySweepLong.evaluate(
            self._sweep_row(time="12:00"), prev_day_ohlc={"low": 96.0}
        )
        self.assertFalse(res["active"])

    def test_outside_window(self):
        res = LiquiditySweepLong.evaluate(
            self._sweep_row(time="14:30"), prev_day_ohlc={"low": 96.0}
        )
        self.assertFalse(res["active"])

    def test_low_volatility(self):
        res = LiquiditySweepLong.evaluate(
            self._sweep_row(atr_pct=0.05), prev_day_ohlc={"low": 96.0}
        )
        self.assertFalse(res["active"])


class TestRatchetValidation(unittest.TestCase):
    def test_lock_above_trigger_rejected(self):
        with self.assertRaises(ValueError):
            _ratchet_levels([{"trigger_pct": 0.2, "lock_pct": 0.3}])

    def test_non_positive_rejected(self):
        with self.assertRaises(ValueError):
            _ratchet_levels([{"trigger_pct": 0.0, "lock_pct": 0.1}])

    def test_sorted(self):
        out = _ratchet_levels([
            {"trigger_pct": 0.5, "lock_pct": 0.3},
            {"trigger_pct": 0.2, "lock_pct": 0.1},
        ])
        self.assertEqual([l["trigger_pct"] for l in out], [0.2, 0.5])


class TestBacktesterRatchet(unittest.IsolatedAsyncioTestCase):
    """Integration test: the ratchet must raise the stop to a locked profit."""

    async def _run(self, bars):
        engine = HistoricalBacktestEngine(initial_capital=10000.0)
        synthetic = {
            "RATCHET": pd.DataFrame(bars),
        }
        with patch.object(engine, "fetch_historical_bars", return_value=synthetic):
            def override(history, prev_day_ohlc, interval):
                last = history[-1]
                if last["time"] != "09:15":
                    return {"active": False}
                return {
                    "active": True,
                    "strategy_id": "test_ratchet",
                    "name": "Test Ratchet",
                    "signal": "BUY",
                    "entry_price": float(last["close"]),
                    "stop_pct": 1.0,
                    "target_pct": 5.0,
                    "ratchet_levels": [{"trigger_pct": 0.2, "lock_pct": 0.1}],
                    "hold_to_close": False,
                    "confidence": 90,
                }

            return await engine.run_backtest(
                symbols=["RATCHET"],
                period="1y",
                interval="5m",
                use_deepseek=False,
                strategy_override=override,
            )

    async def test_ratchet_locks_a_profit(self):
        bars = [
            {"timestamp": "2026-01-02 09:15", "time": "09:15", "date": "2026-01-02",
             "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.1, "volume": 1000},
            {"timestamp": "2026-01-02 09:20", "time": "09:20", "date": "2026-01-02",
             "open": 100.0, "high": 101.0, "low": 99.95, "close": 100.5, "volume": 1000},
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 100.5, "high": 101.2, "low": 100.4, "close": 100.8, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 100.8, "high": 100.9, "low": 99.0, "close": 99.2, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 99.2, "high": 99.5, "low": 99.0, "close": 99.4, "volume": 1000},
        ]
        res = await self._run(bars)
        self.assertEqual(res["total_trades"], 1)
        trade = res["trades"][0]
        entry = trade["entry_price"]
        # Ratchet lock is +0.1% above entry; the stop must have been raised there.
        expected_stop = round(entry * 1.001, 2)
        self.assertAlmostEqual(trade["stop_loss"], expected_stop, delta=0.01)
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["exit_price"], expected_stop, delta=0.01)
        # A raised stop turns the would-be losses into a locked small win.
        self.assertGreater(trade["net_pnl"], -10.0)

    async def test_updates_prior_day(self):
        engine = HistoricalBacktestEngine(initial_capital=10000.0)
        day1 = [
            {"timestamp": f"2026-01-02 09:{15 + 5 * i}", "time": f"09:{15 + 5 * i}",
             "date": "2026-01-02", "open": 100.0, "high": 110.0, "low": 90.0,
             "close": 105.0, "volume": 1000}
            for i in range(5)
        ]
        day2 = [
            {"timestamp": f"2026-01-03 09:{15 + 5 * i}", "time": f"09:{15 + 5 * i}",
             "date": "2026-01-03", "open": 105.0, "high": 106.0, "low": 104.0,
             "close": 105.0, "volume": 1000}
            for i in range(5)
        ]
        seen = {}

        def override(history, prev_day_ohlc, interval):
            seen[history[0]["date"]] = prev_day_ohlc
            return {"active": False}

        synthetic = {"TWO": pd.DataFrame(day1 + day2)}
        with patch.object(engine, "fetch_historical_bars", return_value=synthetic):
            await engine.run_backtest(
                symbols=["TWO"], period="1y", interval="5m",
                use_deepseek=False, strategy_override=override,
            )
        # Day 2 must receive day 1's true low/high, not its own range.
        self.assertEqual(seen["2026-01-03"]["low"], 90.0)
        self.assertEqual(seen["2026-01-03"]["high"], 110.0)


class TestRSI2OversoldSwing(unittest.TestCase):
    def _build_daily_df(self):
        rows = []
        # 20 steady bars around 500 with ATR ~10, then 2 sharp down bars, then a slight green turn
        for i in range(20):
            rows.append({
                "date": f"2026-01-{i + 1:02d}",
                "open": 500.0,
                "high": 506.0,
                "low": 494.0,
                "close": 500.0,
                "volume": 10000,
            })
        rows.append({"date": "2026-01-21", "open": 500.0, "high": 501.0, "low": 478.0, "close": 480.0, "volume": 15000})
        rows.append({"date": "2026-01-22", "open": 480.0, "high": 481.0, "low": 458.0, "close": 460.0, "volume": 18000})
        rows.append({"date": "2026-01-23", "open": 460.0, "high": 465.0, "low": 459.0, "close": 461.0, "volume": 20000})
        # Subsequent rally bar that hits the 4x ATR target
        rows.append({"date": "2026-01-24", "open": 462.0, "high": 530.0, "low": 460.0, "close": 525.0, "volume": 25000})
        return pd.DataFrame(rows)

    def test_rsi2_evaluate_happy_path_and_rejection(self):
        df = self._build_daily_df()
        # On bar index 22 (2026-01-23), 2-day RSI is deeply oversold (<10) and close (461) > prev close (460)
        sig = RSI2OversoldSwing.evaluate(df.iloc[:23])
        self.assertTrue(sig["active"])
        self.assertEqual(sig["signal"], "BUY")
        self.assertGreater(sig["edge_to_fee_ratio"], 2.0)

        # On bar index 21 (2026-01-22), close (460) < prev close (480) -> rejected by uptick rule
        sig_down = RSI2OversoldSwing.evaluate(df.iloc[:22])
        self.assertFalse(sig_down["active"])

    def test_swing_backtest_engine_charges_delivery_fees(self):
        df = self._build_daily_df()
        engine = SwingBacktestEngine(initial_capital=10000.0)
        res = engine.run_symbol_backtest("SBIN", df)
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["winning_trades"], 1)
        self.assertGreater(res["fees_inr"], 13.5)  # includes DP charge + delivery STT
        self.assertGreater(res["net_pnl_inr"], 0.0)


class TestShortSellingBacktester(unittest.IsolatedAsyncioTestCase):
    """Phase 1: bidirectional execution must handle SELL (short) geometry."""

    def _bars(self, extra):
        base = [
            {"timestamp": "2026-01-02 09:15", "time": "09:15", "date": "2026-01-02",
             "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:20", "time": "09:20", "date": "2026-01-02",
             "open": 100.0, "high": 100.5, "low": 99.5, "close": 99.8, "volume": 1000},
        ]
        return base + extra

    async def _run(self, bars, setup, leverage=1.0):
        engine = HistoricalBacktestEngine(initial_capital=10000.0)
        synthetic = {"SHORT": pd.DataFrame(bars)}

        def override(history, prev_day_ohlc, interval):
            last = history[-1]
            if last["time"] != "09:15":
                return {"active": False}
            return dict(setup)

        with patch.object(engine, "fetch_historical_bars", return_value=synthetic):
            return await engine.run_backtest(
                symbols=["SHORT"], period="1y", interval="5m",
                use_deepseek=False, strategy_override=override,
                intraday_leverage_mult=leverage,
            )

    def _short_setup(self, **overrides):
        base = {
            "active": True,
            "strategy_id": "test_short",
            "name": "Test Short",
            "signal": "SELL",
            "entry_price": 100.0,
            "stop_pct": 1.0,
            "target_pct": 2.0,
            "hold_to_close": False,
            "confidence": 90,
        }
        base.update(overrides)
        return base

    async def test_short_target_hit_is_profitable(self):
        bars = self._bars([
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 99.8, "high": 99.9, "low": 95.0, "close": 96.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 96.0, "high": 96.5, "low": 95.5, "close": 96.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 96.0, "high": 96.2, "low": 95.8, "close": 96.0, "volume": 1000},
        ])
        res = await self._run(bars, self._short_setup())
        self.assertEqual(res["total_trades"], 1)
        trade = res["trades"][0]
        self.assertEqual(trade["action"], "SELL")
        self.assertEqual(trade["exit_reason"], "TARGET_HIT")
        self.assertLess(trade["exit_price"], trade["entry_price"])
        self.assertGreater(trade["net_pnl"], 0.0)
        self.assertGreater(trade["return_pct"], 0.0)
        self.assertTrue(trade["won"])

    async def test_short_gap_through_stop_is_capped(self):
        bars = self._bars([
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 102.0, "high": 102.5, "low": 101.8, "close": 102.2, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 102.2, "high": 102.5, "low": 102.0, "close": 102.2, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 102.2, "high": 102.3, "low": 102.0, "close": 102.1, "volume": 1000},
        ])
        res = await self._run(bars, self._short_setup())
        trade = res["trades"][0]
        self.assertEqual(trade["exit_reason"], "STOP_GAP")
        # Gap fill is the bar open (102.0), not the stop (100.95).
        self.assertAlmostEqual(trade["exit_price"], 102.0, delta=0.01)
        self.assertLess(trade["net_pnl"], 0.0)
        self.assertFalse(trade["won"])

    async def test_short_ratchet_locks_profit(self):
        bars = self._bars([
            # 09:25 dips 1%+ below entry -> triggers the ratchet.
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 99.8, "high": 99.9, "low": 98.9, "close": 99.0, "volume": 1000},
            # 09:30 rallies back to the locked stop.
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 99.2, "high": 100.0, "low": 99.0, "close": 99.8, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 99.8, "high": 99.9, "low": 99.5, "close": 99.6, "volume": 1000},
        ])
        setup = self._short_setup(
            stop_pct=1.0, target_pct=10.0,
            ratchet_levels=[{"trigger_pct": 1.0, "lock_pct": 0.5}],
        )
        res = await self._run(bars, setup)
        trade = res["trades"][0]
        entry = trade["entry_price"]
        expected_lock = round(entry * 0.995, 2)
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["stop_loss"], expected_lock, delta=0.01)
        self.assertAlmostEqual(trade["exit_price"], expected_lock, delta=0.01)
        self.assertGreater(trade["net_pnl"], 0.0)

    async def test_short_geometry_rejected_when_wrong_way(self):
        # An inverted short geometry (target ABOVE entry, stop BELOW entry) must
        # be rejected before any position is opened.
        bars = self._bars([
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 99.8, "high": 99.9, "low": 95.0, "close": 96.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 96.0, "high": 96.2, "low": 95.8, "close": 96.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 96.0, "high": 96.2, "low": 95.8, "close": 96.0, "volume": 1000},
        ])
        setup = {
            "active": True, "strategy_id": "test_bad_short", "name": "Bad Short",
            "signal": "SELL", "entry_price": 100.0,
            "stop_loss": 95.0, "target_1": 105.0,
            "hold_to_close": False, "confidence": 90,
        }
        res = await self._run(bars, setup)
        self.assertEqual(res["total_trades"], 0)


class TestLeverageSizing(unittest.IsolatedAsyncioTestCase):
    """Phase 1: MIS leverage must scale quantity without changing % risk."""

    async def _run(self, leverage):
        engine = HistoricalBacktestEngine(initial_capital=10000.0)
        bars = [
            {"timestamp": "2026-01-02 09:15", "time": "09:15", "date": "2026-01-02",
             "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:20", "time": "09:20", "date": "2026-01-02",
             "open": 100.0, "high": 100.5, "low": 99.5, "close": 99.8, "volume": 1000},
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 99.8, "high": 99.9, "low": 99.0, "close": 99.2, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 99.2, "high": 99.4, "low": 99.0, "close": 99.2, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 99.2, "high": 99.4, "low": 99.0, "close": 99.2, "volume": 1000},
        ]
        synthetic = {"LEV": pd.DataFrame(bars)}

        def override(history, prev_day_ohlc, interval):
            if history[-1]["time"] != "09:15":
                return {"active": False}
            return {
                "active": True, "strategy_id": "test_lev", "name": "Test Lev",
                "signal": "BUY", "entry_price": 100.0,
                "stop_pct": 0.6, "target_pct": 5.0, "hold_to_close": False,
                "confidence": 90,
            }

        with patch.object(engine, "fetch_historical_bars", return_value=synthetic):
            return await engine.run_backtest(
                symbols=["LEV"], period="1y", interval="5m",
                use_deepseek=False, strategy_override=override,
                intraday_leverage_mult=leverage,
            )

    async def test_leverage_increases_position_size(self):
        base = await self._run(1.0)
        levered = await self._run(2.5)
        self.assertEqual(base["total_trades"], 1)
        self.assertEqual(levered["total_trades"], 1)
        qty_base = base["trades"][0]["quantity"]
        qty_lev = levered["trades"][0]["quantity"]
        self.assertGreater(qty_lev, qty_base)
        self.assertEqual(levered["intraday_leverage_mult"], 2.5)
        # Hard rupee risk must stay capped near RISK_PER_TRADE_PCT of equity
        # even as notional grows (that is the point of risk-based sizing).
        from app.config import settings as _s
        max_risk = 10000.0 * _s.RISK_PER_TRADE_PCT / 100.0
        for res in (base, levered):
            t = res["trades"][0]
            self.assertLessEqual(abs(t["entry_price"] - t["stop_loss"]) * t["quantity"], max_risk + 1.0)


class TestDirectionAwareFeeHelpers(unittest.TestCase):
    def test_edge_to_fee_long_vs_short(self):
        long_ratio = edge_to_fee_ratio(100.0, 102.0, 50, direction="BUY")
        short_ratio = edge_to_fee_ratio(100.0, 98.0, 50, direction="SELL")
        self.assertGreater(long_ratio, 1.0)
        self.assertGreater(short_ratio, 1.0)
        # Symmetric distance -> nearly identical ratio (STT differs by leg).
        self.assertAlmostEqual(long_ratio, short_ratio, delta=0.5)

    def test_edge_to_fee_zero_distance_is_zero(self):
        self.assertEqual(edge_to_fee_ratio(100.0, 100.0, 50), 0.0)

    def test_fee_to_risk_short_uses_absolute_risk(self):
        pct = fee_to_risk_pct(100.0, 101.0, 100, direction="SELL")
        self.assertGreater(pct, 0.0)
        self.assertLess(pct, 100.0)

    def test_fee_to_risk_invalid_geometry(self):
        self.assertEqual(fee_to_risk_pct(100.0, 100.0, 100), 100.0)


class TestLiquiditySweepShort(unittest.TestCase):
    def _sweep_row(self, **overrides):
        base = {
            "time": "10:00",
            "open": 106.0, "high": 107.0, "low": 103.5, "close": 104.0,
            "atr_pct": 0.25, "body_pct": -0.5, "upper_wick_ratio": 0.6,
            "rsi_14": 62.0,
        }
        base.update(overrides)
        return pd.DataFrame([{**base, "time": "09:55"}, base])

    def test_happy_path(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(), prev_day_ohlc={"high": 105.0, "low": 96.0, "close": 100.0}
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["signal"], "SELL")
        self.assertEqual(res["strategy_id"], "pdh_liquidity_sweep_short")
        self.assertEqual(res["stop_pct"], 0.65)
        self.assertEqual(res["target_pct"], 0.95)
        self.assertEqual(len(res["ratchet_levels"]), 3)

    def test_missing_prior_day(self):
        self.assertFalse(LiquiditySweepShort.evaluate(self._sweep_row(), prev_day_ohlc=None)["active"])

    def test_no_sweep(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(high=104.0), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])

    def test_small_upper_wick(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(upper_wick_ratio=0.10), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])

    def test_green_rejection_bar(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(body_pct=0.5, close=104.0), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])

    def test_weak_momentum_rejected(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(rsi_14=45.0, body_pct=-0.1), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])

    def test_midday_lull_blocked(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(time="12:00"), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])

    def test_low_volatility(self):
        res = LiquiditySweepShort.evaluate(
            self._sweep_row(atr_pct=0.05), prev_day_ohlc={"high": 105.0}
        )
        self.assertFalse(res["active"])


class TestRelativeStrengthLeader(unittest.TestCase):
    def _session(self):
        rows = [
            {"time": "09:15", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
            {"time": "09:30", "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8, "volume": 1000},
            {"time": "09:45", "open": 100.8, "high": 101.2, "low": 100.5, "close": 101.0, "volume": 1500},
            {"time": "10:00", "open": 101.0, "high": 102.5, "low": 100.9, "close": 102.0, "volume": 2000},
            {"time": "10:15", "open": 102.0, "high": 102.2, "low": 101.5, "close": 101.6, "volume": 1200},
            {"time": "10:30", "open": 101.6, "high": 102.0, "low": 101.5, "close": 101.9, "volume": 1800},
        ]
        return pd.DataFrame(rows)

    def test_happy_path(self):
        res = RelativeStrengthLeaderBreakout.evaluate(
            self._session(), rs_30m_pct=1.0, rvol=2.0, rs_rank_pct=90.0
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["signal"], "BUY")
        self.assertEqual(res["strategy_id"], "rs_rvol_leader_breakout")
        self.assertEqual(len(res["ratchet_levels"]), 3)

    def test_low_relative_strength(self):
        res = RelativeStrengthLeaderBreakout.evaluate(self._session(), rs_30m_pct=0.1, rvol=2.0)
        self.assertFalse(res["active"])

    def test_low_rvol(self):
        res = RelativeStrengthLeaderBreakout.evaluate(self._session(), rs_30m_pct=1.0, rvol=1.0)
        self.assertFalse(res["active"])

    def test_rank_gate(self):
        res = RelativeStrengthLeaderBreakout.evaluate(
            self._session(), rs_30m_pct=1.0, rvol=2.0, rs_rank_pct=50.0
        )
        self.assertFalse(res["active"])

    def test_red_reclaim_rejected(self):
        session = self._session()
        session.loc[session.index[-1], "close"] = 101.3  # red body below open 101.6
        res = RelativeStrengthLeaderBreakout.evaluate(session, rs_30m_pct=1.0, rvol=2.0)
        self.assertFalse(res["active"])

    def test_volume_not_expanding(self):
        session = self._session()
        session.loc[session.index[-1], "volume"] = 500  # < previous 1200
        res = RelativeStrengthLeaderBreakout.evaluate(session, rs_30m_pct=1.0, rvol=2.0)
        self.assertFalse(res["active"])

    def test_no_breakout_rejected(self):
        session = self._session()
        # Flatten the middle so the ORH is never broken before the reclaim.
        for idx in (3, 4, 5):
            session.loc[idx, "high"] = 101.0
        res = RelativeStrengthLeaderBreakout.evaluate(session, rs_30m_pct=1.0, rvol=2.0)
        self.assertFalse(res["active"])


class TestCrossSectionalMetrics(unittest.TestCase):
    def _frame(self, opens, closes, date, vols):
        rows = []
        times = ["09:15", "09:30", "09:45"]
        for i, t in enumerate(times):
            rows.append({
                "date": date, "time": t, "open": opens[i], "high": max(opens[i], closes[i]) + 0.1,
                "low": min(opens[i], closes[i]) - 0.1, "close": closes[i], "volume": vols[i],
            })
        return pd.DataFrame(rows)

    def test_ranking_and_rvol(self):
        # Two sessions so RVOL has a prior day to compare against.
        d1 = self._frame([100, 100, 100], [100, 100, 100], "2026-01-01", [1000, 1000, 1000])
        d2 = self._frame([100, 100, 100], [100, 100, 100], "2026-01-02", [1000, 1000, 1000])
        strong = pd.concat([d1, d2], ignore_index=True)
        strong["close"] = strong["close"].astype(float)
        strong["volume"] = strong["volume"].astype(float)
        strong.loc[strong["date"] == "2026-01-02", "close"] = [101.0, 102.0, 103.0]
        strong.loc[strong["date"] == "2026-01-02", "volume"] = [2000.0, 2000.0, 2000.0]

        e1 = self._frame([100, 100, 100], [100, 100, 100], "2026-01-01", [1000, 1000, 1000])
        e2 = self._frame([100, 100, 100], [100, 100, 100], "2026-01-02", [1000, 1000, 1000])
        weak = pd.concat([e1, e2], ignore_index=True)
        weak["close"] = weak["close"].astype(float)
        weak.loc[weak["date"] == "2026-01-02", "close"] = [100.0, 99.5, 99.0]

        metrics = compute_cross_sectional_metrics(
            {"STRONG": strong, "WEAK": weak}, session_date="2026-01-02"
        )
        self.assertGreater(metrics["STRONG"]["rs_30m_pct"], metrics["WEAK"]["rs_30m_pct"])
        self.assertGreater(metrics["STRONG"]["rvol"], 1.5)
        self.assertGreater(metrics["STRONG"]["rs_rank_pct"], metrics["WEAK"]["rs_rank_pct"])

    def test_empty_universe(self):
        self.assertEqual(compute_cross_sectional_metrics({}), {})


class TestNR7SwingBreakout(unittest.TestCase):
    def _bars(self):
        rows = []
        for i in range(208):
            close = 300.0 + i
            rows.append({
                "date": f"d{i:03d}", "open": close, "high": close + 5,
                "low": close - 5, "close": close, "volume": 10000,
            })
        # NR7 signal bar: narrow range 2 and dried-up volume.
        rows.append({"date": "d208", "open": 500.0, "high": 501.0, "low": 499.0, "close": 500.0, "volume": 5000})
        # Breakout bar that gaps through the buy-stop and reaches the target.
        rows.append({"date": "d209", "open": 501.0, "high": 560.0, "low": 500.5, "close": 545.0, "volume": 30000})
        rows.append({"date": "d210", "open": 545.0, "high": 546.0, "low": 540.0, "close": 542.0, "volume": 12000})
        rows.append({"date": "d211", "open": 542.0, "high": 544.0, "low": 540.0, "close": 543.0, "volume": 12000})
        return pd.DataFrame(rows)

    def test_evaluate_happy_path(self):
        df = self._bars()
        mask = df.index == 208
        res = NR7SwingBreakout.evaluate(df.iloc[:209])
        self.assertTrue(res["active"])
        self.assertEqual(res["signal"], "BUY")
        self.assertGreater(res["edge_to_fee_ratio"], 2.0)
        self.assertGreater(res["breakout_level"], 501.0)

    def test_evaluate_not_nr7(self):
        df = self._bars()
        # Widen the signal bar so it is no longer the narrowest of 7.
        df.loc[208, "high"] = 560.0
        df.loc[208, "low"] = 440.0
        res = NR7SwingBreakout.evaluate(df.iloc[:209])
        self.assertFalse(res["active"])

    def test_symbol_backtest_enters_on_breakout(self):
        engine = SwingBacktestEngine(initial_capital=10000.0)
        res = engine.run_nr7_symbol_backtest("AAA", self._bars())
        self.assertEqual(res["total_trades"], 1)
        trade = res["trades"][0]
        self.assertGreaterEqual(trade["entry_price"], 501.0)
        self.assertEqual(trade["exit_reason"], "TARGET_HIT")
        self.assertGreater(trade["net_pnl"], 0.0)

    def test_no_entry_when_breakout_never_triggers(self):
        bars = self._bars()
        bars.loc[209, "high"] = 500.9  # below the buy-stop level
        engine = SwingBacktestEngine(initial_capital=10000.0)
        res = engine.run_nr7_symbol_backtest("AAA", bars)
        self.assertEqual(res["total_trades"], 0)

    def test_compute_swing_features_adds_nr7_columns(self):
        out = compute_swing_features(self._bars())
        for col in ("vol_sma_20", "range", "range_min_7"):
            self.assertIn(col, out.columns)


class TestPortfolioBacktest(unittest.TestCase):
    def _rsi2_frame(self, base_date, drop=40.0):
        rows = []
        for i in range(20):
            rows.append({"date": f"{base_date}-{i + 1:02d}", "open": 500.0, "high": 506.0,
                         "low": 494.0, "close": 500.0, "volume": 10000})
        rows.append({"date": f"{base_date}-21", "open": 500.0, "high": 501.0, "low": 478.0, "close": 480.0, "volume": 15000})
        rows.append({"date": f"{base_date}-22", "open": 480.0, "high": 481.0, "low": 458.0, "close": 460.0, "volume": 18000})
        rows.append({"date": f"{base_date}-23", "open": 460.0, "high": 465.0, "low": 459.0, "close": 461.0, "volume": 20000})
        rows.append({"date": f"{base_date}-24", "open": 462.0, "high": 530.0, "low": 460.0, "close": 525.0, "volume": 25000})
        return pd.DataFrame(rows)

    def test_portfolio_concurrency_cap(self):
        data = {"AAA": self._rsi2_frame("2026-01"), "BBB": self._rsi2_frame("2026-01")}
        engine = SwingBacktestEngine(initial_capital=10000.0)
        capped = engine.run_portfolio_backtest(data, strategy="rsi2", max_concurrent=1)
        both = engine.run_portfolio_backtest(data, strategy="rsi2", max_concurrent=2)
        self.assertEqual(capped["total_trades"], 1)
        self.assertEqual(both["total_trades"], 2)
        self.assertGreater(both["net_pnl_inr"], 0.0)
        self.assertAlmostEqual(both["allocation_per_position_inr"], 5000.0, places=2)
        self.assertIn("max_drawdown_pct", both)

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(ValueError):
            SwingBacktestEngine(initial_capital=10000.0).run_portfolio_backtest({}, strategy="nope")


class TestSelectFeeAwareSetup(unittest.TestCase):
    def _sweep_long_window(self):
        return pd.DataFrame([
            {"time": "09:55", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
            {"time": "10:00", "open": 102.0, "high": 103.0, "low": 97.0, "close": 102.5, "volume": 2000},
        ])

    def test_selects_sweep_long(self):
        res = select_fee_aware_setup(
            self._sweep_long_window(),
            prev_day_ohlc={"low": 98.0, "high": 108.0, "close": 104.0},
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["strategy_id"], "pdl_liquidity_sweep")
        self.assertEqual(res["signal"], "BUY")

    def test_inactive_without_sweep(self):
        window = self._sweep_long_window()
        window.loc[1, "low"] = 99.5  # never trades below the prior-day low
        res = select_fee_aware_setup(
            window, prev_day_ohlc={"low": 98.0, "high": 108.0, "close": 104.0}
        )
        self.assertFalse(res["active"])

    def test_empty_window_inactive(self):
        self.assertFalse(select_fee_aware_setup(pd.DataFrame())["active"])

    def test_rs_leader_uses_full_session_not_two_bar_tail(self):
        # Regression: the RS leader needs the whole session's ORH/VWAP/EMA-9. A
        # naive two-bar tail would silently never fire. The selector must pass
        # the full session window to it.
        session = pd.DataFrame([
            {"time": "09:15", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
            {"time": "09:30", "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8, "volume": 1000},
            {"time": "09:45", "open": 100.8, "high": 101.2, "low": 100.5, "close": 101.0, "volume": 1500},
            {"time": "10:00", "open": 101.0, "high": 102.5, "low": 100.9, "close": 102.0, "volume": 2000},
            {"time": "10:15", "open": 102.0, "high": 102.2, "low": 101.5, "close": 101.6, "volume": 1200},
            {"time": "10:30", "open": 101.6, "high": 102.0, "low": 101.5, "close": 101.9, "volume": 1800},
        ])
        res = select_fee_aware_setup(
            session,
            prev_day_ohlc=None,
            metrics={"rs_30m_pct": 1.0, "rvol": 2.0, "rs_rank_pct": 90.0},
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["strategy_id"], "rs_rvol_leader_breakout")

    def test_rs_leader_skipped_without_metrics(self):
        session = pd.DataFrame([
            {"time": "09:15", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
            {"time": "09:30", "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8, "volume": 1000},
            {"time": "10:30", "open": 101.6, "high": 102.0, "low": 101.5, "close": 101.9, "volume": 1800},
        ])
        res = select_fee_aware_setup(session, prev_day_ohlc=None, metrics=None)
        self.assertFalse(res["active"])


class TestFeeAwareSelector(unittest.TestCase):
    def _universe(self):
        sweep = pd.DataFrame([
            {"date": "2026-01-02", "time": "09:55", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 1000},
            {"date": "2026-01-02", "time": "10:00", "open": 102.0, "high": 103.0,
             "low": 97.0, "close": 102.5, "volume": 2000},
        ])
        return {"AAA": sweep}

    def test_override_recovers_continuous_context(self):
        selector = FeeAwareSelector(self._universe(), interval="5m")
        res = selector.override(
            [{"symbol": "AAA", "date": "2026-01-02", "time": "10:00"}],
            {"low": 98.0, "high": 108.0, "close": 104.0},
            "5m",
        )
        self.assertTrue(res["active"])
        self.assertEqual(res["strategy_id"], "pdl_liquidity_sweep")

    def test_select_matches_override(self):
        selector = FeeAwareSelector(self._universe(), interval="5m")
        prev = {"low": 98.0, "high": 108.0, "close": 104.0}
        via_select = selector.select("AAA", "2026-01-02", "10:00", prev_day_ohlc=prev)
        via_override = selector.override(
            [{"symbol": "AAA", "date": "2026-01-02", "time": "10:00"}], prev, "5m"
        )
        self.assertEqual(via_select.get("strategy_id"), via_override.get("strategy_id"))
        self.assertTrue(via_select["active"])

    def test_unknown_symbol_inactive(self):
        selector = FeeAwareSelector(self._universe(), interval="5m")
        res = selector.override(
            [{"symbol": "ZZZ", "date": "2026-01-02", "time": "10:00"}], None, "5m"
        )
        self.assertFalse(res["active"])

    def test_unknown_bar_inactive(self):
        selector = FeeAwareSelector(self._universe(), interval="5m")
        res = selector.override(
            [{"symbol": "AAA", "date": "2026-01-02", "time": "11:11"}], None, "5m"
        )
        self.assertFalse(res["active"])

    def test_empty_history_inactive(self):
        selector = FeeAwareSelector(self._universe(), interval="5m")
        self.assertFalse(selector.override([], None, "5m")["active"])


class TestPreloadedDataBacktester(unittest.IsolatedAsyncioTestCase):
    async def test_preloaded_data_bypasses_network_fetch(self):
        bars = [
            {"timestamp": "2026-01-02 09:15", "time": "09:15", "date": "2026-01-02",
             "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:20", "time": "09:20", "date": "2026-01-02",
             "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:25", "time": "09:25", "date": "2026-01-02",
             "open": 100.5, "high": 106.0, "low": 100.4, "close": 105.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:30", "time": "09:30", "date": "2026-01-02",
             "open": 105.0, "high": 105.2, "low": 104.8, "close": 105.0, "volume": 1000},
            {"timestamp": "2026-01-02 09:35", "time": "09:35", "date": "2026-01-02",
             "open": 105.0, "high": 105.3, "low": 104.9, "close": 105.1, "volume": 1000},
        ]

        def override(history, prev_day_ohlc, interval):
            if history[-1]["time"] != "09:15":
                return {"active": False}
            return {
                "active": True, "strategy_id": "test_pre", "name": "Test Pre",
                "signal": "BUY", "entry_price": 100.0,
                "stop_pct": 1.0, "target_pct": 5.0, "hold_to_close": False,
                "confidence": 90,
            }

        engine = HistoricalBacktestEngine(initial_capital=10000.0)
        data = {"PRE": pd.DataFrame(bars)}
        with patch.object(
            engine, "fetch_historical_bars",
            side_effect=AssertionError("fetch must be skipped when preloaded_data is given"),
        ):
            res = await engine.run_backtest(
                symbols=["PRE"], period="1y", interval="5m",
                use_deepseek=False, strategy_override=override,
                preloaded_data=data,
            )
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["trades"][0]["exit_reason"], "TARGET_HIT")
        self.assertGreater(res["trades"][0]["net_pnl"], 0.0)


class TestStrategyManagerFeeAwareEntry(unittest.TestCase):
    def _mgr(self):
        from app.trading.strategy_manager import StrategyManager
        return StrategyManager()

    def test_selects_sweep_long(self):
        mgr = self._mgr()
        bars = [
            {"time": "09:55", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
            {"time": "10:00", "open": 102.0, "high": 103.0, "low": 97.0, "close": 102.5, "volume": 2000},
        ]
        setup = mgr.select_fee_aware_entry(
            "AAA", bars, prev_day_ohlc={"low": 98.0, "high": 108.0, "close": 104.0}
        )
        self.assertIsNotNone(setup)
        self.assertEqual(setup["strategy_id"], "pdl_liquidity_sweep")
        self.assertEqual(setup["signal"], "BUY")

    def test_returns_none_without_setup(self):
        mgr = self._mgr()
        bars = [
            {"time": "09:55", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
            {"time": "10:00", "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8, "volume": 1000},
        ]
        setup = mgr.select_fee_aware_entry(
            "AAA", bars, prev_day_ohlc={"low": 98.0, "high": 108.0, "close": 104.0}
        )
        self.assertIsNone(setup)

    def test_empty_bars_returns_none(self):
        mgr = self._mgr()
        self.assertIsNone(mgr.select_fee_aware_entry("AAA", []))


if __name__ == "__main__":
    unittest.main()
