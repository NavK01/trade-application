"""Tests for the market-intelligence agent: forecast creation and scoring."""

import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app.config import settings
from app.storage.db import DatabaseManager
from app.trading import market_hours
from app.trading.market_intel import (
    MarketIntelAgent,
    add_business_days,
    classify_direction,
    delivery_round_trip_cost_pct,
)

IST = market_hours.IST
# Thursday 2026-11-05, after the close.
FIXED_NOW = datetime.datetime(2026, 11, 5, 16, 30, tzinfo=IST)


def _now() -> datetime.datetime:
    return FIXED_NOW


def _business_dates(end: datetime.date, n: int):
    dates = []
    cursor = end
    while len(dates) < n:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor -= datetime.timedelta(days=1)
    return list(reversed(dates))


def make_df(closes, end=datetime.date(2026, 11, 4)):
    """Ascending lowercase OHLCV daily frame ending on ``end``."""
    dates = _business_dates(end, len(closes))
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    })


def _up_forecast_json():
    return '{"direction":"UP","expected_move_pct":3.5,"confidence_pct":62,"catalysts":"order win","reasoning":"trend up"}'


class IntelTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")
        self.closes = [100.0 + i * 0.5 for i in range(70)]
        self.data = lambda sym: make_df(self.closes)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _agent(self, universe=None, model_text=None, data=None, max_symbols=30):
        async def _model(prompt):
            return {"text": model_text if model_text is not None else _up_forecast_json(),
                    "prompt_tokens": 1200, "completion_tokens": 80}

        return MarketIntelAgent(
            universe=universe if universe is not None else ["RELIANCE", "TCS"],
            data_provider=data or self.data,
            news_provider=lambda sym: [{"title": "headline", "snippet": "text"}],
            model_fn=_model,
            now_provider=_now,
            database=self.db,
            max_symbols=max_symbols,
        )


class TestPureHelpers(unittest.TestCase):
    def test_add_business_days_skips_weekend(self):
        wed = datetime.date(2026, 11, 4)
        self.assertEqual(add_business_days(wed, 5), datetime.date(2026, 11, 11))
        self.assertEqual(add_business_days(wed, 0), wed)

    def test_classify_direction_uses_flat_band(self):
        self.assertEqual(classify_direction(5.0, 1.0), "UP")
        self.assertEqual(classify_direction(-5.0, 1.0), "DOWN")
        self.assertEqual(classify_direction(0.4, 1.0), "FLAT")
        self.assertEqual(classify_direction(-0.9, 1.0), "FLAT")

    def test_delivery_cost_pct_is_positive_and_small(self):
        cost = delivery_round_trip_cost_pct(5000.0)
        self.assertGreater(cost, 0.0)
        self.assertLess(cost, 2.0)


class TestForecastCycle(IntelTestBase):
    def test_cycle_persists_forecast_with_reference_price_and_horizon(self):
        agent = self._agent(universe=["RELIANCE"])
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["forecasted"], ["RELIANCE"])

        rows = self.db.get_forecasts(status="open")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "RELIANCE")
        self.assertEqual(round(row["reference_price"], 2), round(self.closes[-1], 2))
        self.assertEqual(row["forecast_date"], "2026-11-04")
        self.assertEqual(row["horizon_date"], "2026-11-11")
        self.assertEqual(row["direction"], "UP")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["run_date"], "2026-11-05")

    def test_cycle_is_idempotent_for_the_same_day(self):
        agent = self._agent(universe=["RELIANCE"])
        asyncio.run(agent.run_cycle(FIXED_NOW))
        second = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(second["status"], "already_ran")
        self.assertEqual(len(self.db.get_forecasts()), 1)

    def test_cycle_respects_max_symbols(self):
        agent = self._agent(universe=["RELIANCE", "TCS", "INFY", "SBIN", "ITC"], max_symbols=2)
        asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(len(self.db.get_forecasts()), 2)

    def test_cycle_drops_todays_forming_bar(self):
        closes = [100.0 + i for i in range(70)]
        today_frame = make_df(closes, end=datetime.date(2026, 11, 5))  # last bar is today
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: today_frame)
        asyncio.run(agent.run_cycle(FIXED_NOW))
        row = self.db.get_forecasts()[0]
        # Reference must be the last COMPLETED bar, i.e. closes[-2].
        self.assertEqual(round(row["reference_price"], 2), round(closes[-2], 2))
        self.assertEqual(row["forecast_date"], "2026-11-04")

    def test_invalid_model_output_is_discarded(self):
        agent = self._agent(universe=["RELIANCE"], model_text="not json at all")
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["forecasted"], [])
        self.assertGreaterEqual(res["errors"], 1)
        self.assertEqual(len(self.db.get_forecasts()), 0)

    def test_no_api_key_skips_cycle(self):
        agent = self._agent(universe=["RELIANCE"])
        with mock.patch.object(settings, "DEEPSEEK_API_KEY", ""):
            res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["status"], "no_api_key")
        self.assertEqual(len(self.db.get_forecasts()), 0)

    def test_insufficient_bars_are_skipped(self):
        short = make_df([100.0] * 10)
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: short)
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["forecasted"], [])
        self.assertEqual(len(self.db.get_forecasts()), 0)

    def test_default_cap_covers_index_context(self):
        """The default universe includes NIFTY/BANKNIFTY and the cap must not
        silently truncate them (SWING_UNIVERSE is exactly the equity count)."""
        agent = MarketIntelAgent(
            data_provider=self.data,
            news_provider=lambda sym: [],
            model_fn=lambda prompt: None,  # never invoked here
            now_provider=_now,
            database=self.db,
        )
        self.assertIn("^NSEI", agent.universe)
        self.assertIn("^NSEBANK", agent.universe)
        self.assertGreaterEqual(agent.max_symbols, len(agent.universe))
        self.assertGreaterEqual(settings.MARKET_INTEL_MAX_SYMBOLS, len(agent.universe))


class TestResolution(IntelTestBase):
    def _seed_open(self, direction="UP", reference=100.0, horizon="2026-11-03", flat_band=1.0):
        return self.db.record_forecast({
            "symbol": "RELIANCE",
            "run_date": "2026-10-28",
            "forecast_date": "2026-10-27",
            "horizon_days": 5,
            "horizon_date": horizon,
            "reference_price": reference,
            "direction": direction,
            "expected_move_pct": 3.0,
            "confidence_pct": 60,
            "flat_band_pct": flat_band,
            "catalysts": "x",
            "reasoning": "y",
            "model": "test",
        })

    def test_due_forecast_is_scored(self):
        self._seed_open(direction="UP", reference=100.0, horizon="2026-11-04")
        closes = [100.0] * 60 + [110.0]  # last completed bar (2026-11-04) = 110
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: make_df(closes))
        res = asyncio.run(agent.resolve_due_forecasts(FIXED_NOW))
        self.assertEqual(len(res["resolved"]), 1)
        outcome = res["resolved"][0]
        self.assertEqual(outcome["actual_direction"], "UP")
        self.assertTrue(outcome["correct"])
        self.assertAlmostEqual(outcome["actual_move_pct"], 10.0, delta=0.5)

        row = self.db.get_forecasts(status="resolved")[0]
        self.assertEqual(row["direction_correct"], 1)
        self.assertAlmostEqual(row["actual_move_pct"], 10.0, delta=0.5)
        self.assertLess(row["realized_net_pct"], 10.0)  # net of delivery cost

    def test_wrong_direction_is_scored_as_incorrect(self):
        self._seed_open(direction="UP", reference=100.0, horizon="2026-11-04")
        closes = [100.0] * 60 + [90.0]
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: make_df(closes))
        asyncio.run(agent.resolve_due_forecasts(FIXED_NOW))
        row = self.db.get_forecasts(status="resolved")[0]
        self.assertEqual(row["direction_correct"], 0)
        self.assertEqual(row["actual_direction"], "DOWN")
        self.assertLess(row["realized_net_pct"], 0.0)

    def test_immature_forecast_stays_open(self):
        self._seed_open(horizon="2026-12-31")
        agent = self._agent(universe=["RELIANCE"])
        res = asyncio.run(agent.resolve_due_forecasts(FIXED_NOW))
        self.assertEqual(res["resolved"], [])
        self.assertEqual(res["pending"], 1)
        self.assertEqual(len(self.db.get_open_forecasts()), 1)


class TestReport(IntelTestBase):
    def _seed_resolved(self, direction, actual_move, correct):
        fid = self.db.record_forecast({
            "symbol": "RELIANCE", "run_date": "2026-10-01", "forecast_date": "2026-10-01",
            "horizon_days": 5, "horizon_date": "2026-10-08", "reference_price": 100.0,
            "direction": direction, "expected_move_pct": 3.0, "confidence_pct": 70,
            "flat_band_pct": 1.0, "catalysts": "x", "reasoning": "y", "model": "test",
        })
        self.db.resolve_forecast(
            fid, resolved_at="2026-10-08T10:00:00+00:00", resolved_price=100.0 + actual_move,
            actual_direction=classify_direction(actual_move, 1.0), actual_move_pct=actual_move,
            direction_correct=1 if correct else 0, abs_error_pct=abs(3.0 - actual_move),
            realized_net_pct=actual_move if direction == "UP" else -actual_move,
        )

    def test_report_insufficient_sample(self):
        self._seed_resolved("UP", 5.0, True)
        self._seed_resolved("DOWN", -4.0, False)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["verdict"], "insufficient_sample")
        self.assertEqual(report["statistics"]["closed_forecasts"], 2)
        self.assertEqual(report["statistics"]["direction_hit_rate_pct"], 50.0)
        self.assertIn("RELIANCE", report["per_symbol"])

    def test_report_on_track_after_enough_samples(self):
        for _ in range(30):
            self._seed_resolved("UP", 5.0, True)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["statistics"]["closed_forecasts"], 30)
        self.assertEqual(report["statistics"]["direction_hit_rate_pct"], 100.0)
        self.assertGreater(report["statistics"]["avg_realized_net_pct"], 0)
        self.assertEqual(report["verdict"], "on_track")

    def test_report_underperforming_when_net_negative(self):
        for _ in range(30):
            self._seed_resolved("UP", -5.0, False)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["verdict"], "underperforming")


if __name__ == "__main__":
    unittest.main()
