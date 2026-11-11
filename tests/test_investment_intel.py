"""Tests for the Investment Intelligence agent: calls, permanence, scoring."""

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
from app.trading.market_intel import add_business_days, classify_direction
from app.trading.investment_intel import (
    InvestmentIntelAgent,
    classify_move_permanence,
    classify_who_is_trading,
    delivery_round_trip_cost_pct,
    expected_direction_for_verdict,
)

IST = market_hours.IST
# Thursday 2026-11-05, after the close.
FIXED_NOW = datetime.datetime(2026, 11, 5, 16, 30, tzinfo=IST)
HORIZON = 20


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


def _accumulate_json():
    return (
        '{"verdict":"ACCUMULATE","confidence_pct":68,"band_low_pct":-2.0,'
        '"band_high_pct":8.0,"who_is_trading":"institutions accumulating",'
        '"move_permanence":"structural_up","crash_risk":"low","crash_reason":"",'
        '"why":["steady uptrend","institutions adding","earnings steady"],'
        '"reasoning":"Durable demand."}'
    )


class IntelTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(Path(self.tmpdir.name) / "test.db")
        self.closes = [100.0 + i * 0.5 for i in range(70)]
        self.data = lambda sym: make_df(self.closes)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _agent(self, universe=None, model_text=None, data=None, max_symbols=30,
               ownership=None, ipo=None):
        async def _model(prompt):
            return {
                "text": model_text if model_text is not None else _accumulate_json(),
                "prompt_tokens": 1500,
                "completion_tokens": 120,
            }

        return InvestmentIntelAgent(
            universe=universe if universe is not None else ["RELIANCE", "TCS"],
            data_provider=data or self.data,
            news_provider=lambda sym: [{"title": "headline", "snippet": "text"}],
            ownership_provider=lambda sym: (ownership if ownership is not None else {}),
            ipo_provider=lambda: (ipo if ipo is not None else []),
            model_fn=_model,
            now_provider=_now,
            database=self.db,
            max_symbols=max_symbols,
            horizon_days=HORIZON,
        )


class TestPureHelpers(unittest.TestCase):
    def test_expected_direction_for_verdict(self):
        self.assertEqual(expected_direction_for_verdict("ACCUMULATE"), "UP")
        self.assertEqual(expected_direction_for_verdict("AVOID"), "DOWN")
        self.assertEqual(expected_direction_for_verdict("HOLD"), "FLAT")
        self.assertEqual(expected_direction_for_verdict("WATCH"), "FLAT")
        self.assertIsNone(expected_direction_for_verdict("nonsense"))

    def test_permanence_structural_up(self):
        out = classify_move_permanence({
            "ret_20d_pct": 8.0, "ret_60d_pct": 12.0,
            "pct_from_sma50": 4.0, "pct_from_sma200": 9.0,
            "position_in_52w_range_pct": 88.0,
        })
        self.assertEqual(out["label"], "structural_up")
        self.assertGreaterEqual(out["confidence_pct"], 58)
        self.assertTrue(out["reasons"])

    def test_permanence_structural_down(self):
        out = classify_move_permanence({
            "ret_20d_pct": -9.0, "ret_60d_pct": -14.0,
            "pct_from_sma50": -6.0, "pct_from_sma200": -10.0,
            "position_in_52w_range_pct": 10.0,
        })
        self.assertEqual(out["label"], "structural_down")

    def test_permanence_temporary_up(self):
        out = classify_move_permanence({
            "ret_20d_pct": 6.0, "ret_60d_pct": 1.0,
            "pct_from_sma50": 1.0, "pct_from_sma200": 0.5,
            "position_in_52w_range_pct": 55.0,
        })
        self.assertEqual(out["label"], "temporary_up")

    def test_permanence_range_when_flat(self):
        out = classify_move_permanence({
            "ret_20d_pct": 1.0, "ret_60d_pct": 0.5,
            "pct_from_sma50": 0.2, "pct_from_sma200": 0.1,
            "position_in_52w_range_pct": 50.0,
        })
        self.assertEqual(out["label"], "range")

    def test_who_is_trading_insiders_selling(self):
        out = classify_who_is_trading({
            "insider_net_shares": -5000, "insider_buy_count": 0,
            "insider_sell_count": 3, "institutional_pct": 25.0,
        })
        self.assertEqual(out["label"], "insiders_distributing")
        self.assertTrue(out["evidence"])

    def test_who_is_trading_insiders_buying(self):
        out = classify_who_is_trading({
            "insider_net_shares": 4000, "insider_buy_count": 2,
            "insider_sell_count": 0,
        })
        self.assertEqual(out["label"], "insiders_accumulating")

    def test_who_is_trading_unknown_without_data(self):
        out = classify_who_is_trading({})
        self.assertEqual(out["label"], "unknown")

    def test_delivery_cost_pct_is_positive_and_small(self):
        cost = delivery_round_trip_cost_pct(5000.0)
        self.assertGreater(cost, 0.0)
        self.assertLess(cost, 2.0)


class TestCallCycle(IntelTestBase):
    def test_cycle_persists_call_with_reference_price_and_horizon(self):
        agent = self._agent(universe=["RELIANCE"])
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["called"], ["RELIANCE"])

        rows = self.db.get_investment_calls(status="open")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "RELIANCE")
        self.assertEqual(round(row["reference_price"], 2), round(self.closes[-1], 2))
        self.assertEqual(row["call_date"], "2026-11-04")
        self.assertEqual(
            row["horizon_date"],
            add_business_days(datetime.date(2026, 11, 4), HORIZON).isoformat(),
        )
        self.assertEqual(row["verdict"], "ACCUMULATE")
        self.assertEqual(row["move_permanence"], "structural_up")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["run_date"], "2026-11-05")

    def test_cycle_is_idempotent_for_the_same_day(self):
        agent = self._agent(universe=["RELIANCE"])
        asyncio.run(agent.run_cycle(FIXED_NOW))
        second = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(second["status"], "already_ran")
        self.assertEqual(len(self.db.get_investment_calls()), 1)

    def test_cycle_respects_max_symbols(self):
        agent = self._agent(universe=["RELIANCE", "TCS", "INFY", "SBIN", "ITC"], max_symbols=2)
        asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(len(self.db.get_investment_calls()), 2)

    def test_cycle_drops_todays_forming_bar(self):
        closes = [100.0 + i for i in range(70)]
        today_frame = make_df(closes, end=datetime.date(2026, 11, 5))
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: today_frame)
        asyncio.run(agent.run_cycle(FIXED_NOW))
        row = self.db.get_investment_calls()[0]
        self.assertEqual(round(row["reference_price"], 2), round(closes[-2], 2))
        self.assertEqual(row["call_date"], "2026-11-04")

    def test_invalid_model_output_is_discarded(self):
        agent = self._agent(universe=["RELIANCE"], model_text="not json at all")
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["called"], [])
        self.assertGreaterEqual(res["errors"], 1)
        self.assertEqual(len(self.db.get_investment_calls()), 0)

    def test_verdict_outside_allowed_set_is_discarded(self):
        bad = '{"verdict":"MOON","confidence_pct":90,"band_low_pct":0,"band_high_pct":5}'
        agent = self._agent(universe=["RELIANCE"], model_text=bad)
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["called"], [])
        self.assertEqual(len(self.db.get_investment_calls()), 0)

    def test_no_api_key_skips_cycle(self):
        agent = self._agent(universe=["RELIANCE"])
        with mock.patch.object(settings, "DEEPSEEK_API_KEY", ""):
            res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["status"], "no_api_key")
        self.assertEqual(len(self.db.get_investment_calls()), 0)

    def test_insufficient_bars_are_skipped(self):
        short = make_df([100.0] * 10)
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: short)
        res = asyncio.run(agent.run_cycle(FIXED_NOW))
        self.assertEqual(res["called"], [])
        self.assertEqual(len(self.db.get_investment_calls()), 0)

    def test_why_bullets_are_persisted_as_list(self):
        agent = self._agent(universe=["RELIANCE"])
        asyncio.run(agent.run_cycle(FIXED_NOW))
        report = agent.build_report()
        self.assertEqual(report["open_calls"][0]["why"], [
            "steady uptrend", "institutions adding", "earnings steady",
        ])

    def test_ipo_watch_uses_injected_provider(self):
        agent = self._agent(
            universe=["RELIANCE"],
            ipo=[{"title": "New IPO", "url": "http://x", "snippet": "s"}],
        )
        self.assertEqual(agent.ipo_watch()[0]["title"], "New IPO")


class TestResolution(IntelTestBase):
    def _seed_open(self, verdict="ACCUMULATE", reference=100.0, horizon="2026-11-04",
                   call_date="2026-10-27", flat_band=2.0):
        return self.db.record_investment_call({
            "symbol": "RELIANCE",
            "run_date": "2026-10-27",
            "call_date": call_date,
            "horizon_days": HORIZON,
            "horizon_date": horizon,
            "reference_price": reference,
            "verdict": verdict,
            "confidence_pct": 65,
            "flat_band_pct": flat_band,
            "band_low_pct": -2.0,
            "band_high_pct": 8.0,
            "who_is_trading": "institutions accumulating",
            "move_permanence": "structural_up",
            "crash_risk": "low",
            "why_bullets": ["a", "b"],
            "model": "test",
        })

    def test_due_accumulate_is_scored_correct(self):
        self._seed_open(verdict="ACCUMULATE", reference=100.0, horizon="2026-11-04")
        closes = [100.0] * 60 + [110.0]
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: make_df(closes))
        res = asyncio.run(agent.resolve_due_calls(FIXED_NOW))
        self.assertEqual(len(res["resolved"]), 1)
        self.assertTrue(res["resolved"][0]["correct"])

        row = self.db.get_investment_calls(status="resolved")[0]
        self.assertEqual(row["verdict_correct"], 1)
        self.assertAlmostEqual(row["actual_move_pct"], 10.0, delta=0.5)
        # Net is the move minus the round-trip delivery cost.
        self.assertLess(row["realized_net_pct"], 10.0)
        self.assertGreater(row["realized_net_pct"], 9.0)
        self.assertGreaterEqual(row["max_runup_pct"], row["actual_move_pct"])
        self.assertLessEqual(row["max_drawdown_pct"], 0.0)

    def test_avoid_correct_when_price_falls(self):
        self._seed_open(verdict="AVOID", reference=100.0, horizon="2026-11-04")
        closes = [100.0] * 60 + [90.0]
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: make_df(closes))
        asyncio.run(agent.resolve_due_calls(FIXED_NOW))
        row = self.db.get_investment_calls(status="resolved")[0]
        self.assertEqual(row["verdict_correct"], 1)
        self.assertLess(row["actual_move_pct"], 0.0)
        # AVOID does not put on a delivery position, so realized P&L is zero.
        self.assertEqual(row["realized_net_pct"], 0.0)

    def test_accumulate_incorrect_when_price_falls(self):
        self._seed_open(verdict="ACCUMULATE", reference=100.0, horizon="2026-11-04")
        closes = [100.0] * 60 + [90.0]
        agent = self._agent(universe=["RELIANCE"], data=lambda sym: make_df(closes))
        asyncio.run(agent.resolve_due_calls(FIXED_NOW))
        row = self.db.get_investment_calls(status="resolved")[0]
        self.assertEqual(row["verdict_correct"], 0)
        self.assertLess(row["realized_net_pct"], 0.0)

    def test_immature_call_stays_open(self):
        self._seed_open(horizon="2026-12-31")
        agent = self._agent(universe=["RELIANCE"])
        res = asyncio.run(agent.resolve_due_calls(FIXED_NOW))
        self.assertEqual(res["resolved"], [])
        self.assertEqual(res["pending"], 1)
        self.assertEqual(len(self.db.get_open_investment_calls()), 1)


class TestReport(IntelTestBase):
    def _seed_resolved(self, verdict, actual_move, correct, net=None):
        cid = self.db.record_investment_call({
            "symbol": "RELIANCE", "run_date": "2026-10-01", "call_date": "2026-10-01",
            "horizon_days": HORIZON, "horizon_date": "2026-10-29", "reference_price": 100.0,
            "verdict": verdict, "confidence_pct": 70, "flat_band_pct": 2.0,
            "band_low_pct": -2.0, "band_high_pct": 8.0, "who_is_trading": "x",
            "move_permanence": "structural_up", "crash_risk": "low",
            "why_bullets": ["a"], "model": "test",
        })
        if net is None:
            net = actual_move if verdict == "ACCUMULATE" else 0.0
        self.db.resolve_investment_call(
            cid, resolved_at="2026-10-29T10:00:00+00:00", resolved_price=100.0 + actual_move,
            actual_move_pct=actual_move, verdict_correct=1 if correct else 0,
            max_drawdown_pct=min(0.0, actual_move), max_runup_pct=max(0.0, actual_move),
            realized_net_pct=net,
        )

    def test_report_insufficient_sample(self):
        self._seed_resolved("ACCUMULATE", 5.0, True)
        self._seed_resolved("AVOID", -4.0, False)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["verdict"], "insufficient_sample")
        self.assertEqual(report["statistics"]["closed_calls"], 2)
        self.assertEqual(report["statistics"]["verdict_hit_rate_pct"], 50.0)
        self.assertIn("RELIANCE", report["per_symbol"])
        self.assertIn("ACCUMULATE", report["per_verdict"])

    def test_report_on_track_after_enough_samples(self):
        for _ in range(20):
            self._seed_resolved("ACCUMULATE", 5.0, True)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["statistics"]["closed_calls"], 20)
        self.assertEqual(report["statistics"]["verdict_hit_rate_pct"], 100.0)
        self.assertGreater(report["statistics"]["avg_realized_net_pct"], 0)
        self.assertEqual(report["verdict"], "on_track")

    def test_report_underperforming_when_net_negative(self):
        for _ in range(20):
            self._seed_resolved("ACCUMULATE", -5.0, False, net=-5.0)
        report = self._agent(universe=[]).build_report()
        self.assertEqual(report["verdict"], "underperforming")

    def test_actionable_only_contains_valued_verdicts(self):
        self._seed_resolved("ACCUMULATE", 5.0, True)
        # Seed an open HOLD which must NOT appear in the actionable list.
        self.db.record_investment_call({
            "symbol": "INFY", "run_date": "2026-11-04", "call_date": "2026-11-04",
            "horizon_days": HORIZON, "horizon_date": "2026-12-02", "reference_price": 100.0,
            "verdict": "HOLD", "confidence_pct": 55, "flat_band_pct": 2.0,
            "band_low_pct": -2.0, "band_high_pct": 2.0, "who_is_trading": "x",
            "move_permanence": "range", "crash_risk": "low",
            "why_bullets": ["a"], "model": "test",
        })
        report = self._agent(universe=[]).build_report()
        self.assertTrue(all(
            c["verdict"] in ("ACCUMULATE", "AVOID") for c in report["actionable"]
        ))
        self.assertTrue(all(c["plain_english"] for c in report["open_calls"]))

    def test_default_universe_covers_index_context(self):
        agent = InvestmentIntelAgent(
            data_provider=self.data,
            ownership_provider=lambda sym: {},
            news_provider=lambda sym: [],
            model_fn=lambda prompt: None,
            now_provider=_now,
            database=self.db,
        )
        self.assertIn("^NSEI", agent.universe)
        self.assertGreaterEqual(agent.max_symbols, len(agent.universe))
        self.assertGreaterEqual(settings.INVESTMENT_INTEL_MAX_SYMBOLS, len(agent.universe))


if __name__ == "__main__":
    unittest.main()
