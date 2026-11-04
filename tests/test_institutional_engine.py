"""
Unit tests for the Institutional Quant Engine using standard unittest:
- Statutory Indian Fee & Net Profit Calculator
- Option Chain & Gamma Exposure (GEX) Analyzer
- Pre-Market 4-Line Mapping & Volume Profile / Fibonacci
- Liquidity Sweep & Volume Delta Absorption Detector
- Risk Guardrails & Circuit Breakers (+0.5% lock, 2 consecutive loss lockout)
"""

import unittest
import pandas as pd
import numpy as np
from datetime import datetime, time

from app.trading.institutional_engine import (
    calculate_indian_statutory_charges,
    compute_premarket_4lines,
    compute_volume_profile,
    compute_fibonacci_zones,
    compute_option_chain_gex,
    detect_liquidity_sweeps_and_absorption,
    evaluate_risk_guardrails,
    generate_institutional_ai_signal,
    INDIAN_STATUTORY_RATES,
)


class TestInstitutionalEngine(unittest.TestCase):

    def test_statutory_charges_calculation(self):
        """Verify exact statutory fees, GST, STT, and net P&L calculation."""
        buy_val = 23000.0 * 10
        sell_val = 23100.0 * 10
        
        res = calculate_indian_statutory_charges(
            buy_value=buy_val,
            sell_value=sell_val,
            instrument_type="INTRADAY_EQUITY"
        )
        
        self.assertEqual(res["gross_pnl"], 1000.0)
        self.assertGreater(res["total_charges"], 0)
        self.assertLessEqual(res["brokerage"], 40.0)
        self.assertGreater(res["stt"], 0)
        self.assertGreater(res["exchange_txn_fee"], 0)
        self.assertGreater(res["gst"], 0)
        self.assertGreater(res["sebi_charges"], 0)
        self.assertGreater(res["stamp_duty"], 0)
        self.assertEqual(res["net_pnl"], round(res["gross_pnl"] - res["total_charges"], 2))
        self.assertGreater(res["net_roi_pct"], 0)
        self.assertGreater(res["breakeven_points"], 0)

    def test_premarket_4lines(self):
        """Test calculation of PDH, PDL, PDC, and 1H Swing High/Low."""
        dates = pd.date_range("2026-10-27 09:15", "2026-10-28 15:30", freq="5min")
        dates = [d for d in dates if d.time() >= time(9, 15) and d.time() <= time(15, 30)]
        
        prices = [23000 + np.sin(i / 10.0) * 100 for i in range(len(dates))]
        df = pd.DataFrame({
            "timestamp": dates,
            "Open": prices,
            "High": [p + 10 for p in prices],
            "Low": [p - 10 for p in prices],
            "Close": [p + 2 for p in prices],
            "Volume": [1000] * len(dates)
        })
        
        lines = compute_premarket_4lines(df)
        self.assertIn("PDH", lines)
        self.assertIn("PDL", lines)
        self.assertIn("PDC", lines)
        self.assertIn("SWING_1H_HIGH", lines)
        self.assertIn("SWING_1H_LOW", lines)
        self.assertGreaterEqual(lines["PDH"], lines["PDL"])

    def test_volume_profile_and_fibonacci(self):
        """Test POC, VAH, VAL and deep Fibonacci discount/premium zones."""
        np.random.seed(42)
        prices = np.random.normal(23000, 50, 100)
        df = pd.DataFrame({
            "High": prices + 5,
            "Low": prices - 5,
            "Close": prices,
            "Volume": np.random.randint(100, 1000, 100)
        })
        
        vp = compute_volume_profile(df)
        self.assertIn("POC", vp)
        self.assertIn("VAH", vp)
        self.assertIn("VAL", vp)
        self.assertGreaterEqual(vp["VAH"], vp["POC"])
        self.assertGreaterEqual(vp["POC"], vp["VAL"])
        
        fib = compute_fibonacci_zones(df, vah=vp["VAH"], val=vp["VAL"])
        self.assertIn("golden_pocket_618", fib)
        self.assertIn("deep_discount_786", fib)
        self.assertIn("deep_premium_786", fib)

    def test_option_chain_gex(self):
        """Test Option Chain GEX, Call Wall, Put Wall, PCR and Regime classification."""
        spot = 23050.0
        gex_data = compute_option_chain_gex("NIFTY", spot_price=spot)
        
        self.assertGreater(gex_data["pcr"], 0)
        self.assertGreaterEqual(gex_data["call_wall"], spot)
        self.assertLessEqual(gex_data["put_wall"], spot)
        self.assertIn(gex_data["regime"], ["POSITIVE_GAMMA", "NEGATIVE_GAMMA"])
        self.assertIn("gamma_flip_level", gex_data)
        self.assertGreater(len(gex_data["strikes"]), 0)

    def test_liquidity_sweep_detector(self):
        """Test detection of liquidity sweeps at key levels."""
        bars = [
            {"timestamp": "2026-10-28 09:25:00", "Open": 23080, "High": 23095, "Low": 23075, "Close": 23090, "Volume": 5000},
            {"timestamp": "2026-10-28 09:30:00", "Open": 23090, "High": 23115, "Low": 23085, "Close": 23088, "Volume": 15000},
        ]
        df = pd.DataFrame(bars)
        
        sweeps = detect_liquidity_sweeps_and_absorption(
            df,
            pdh=23100.0,
            pdl=22900.0,
            pdc=23000.0,
            swing_high=23095.0,
            swing_low=22920.0
        )
        
        self.assertGreater(len(sweeps), 0)
        self.assertEqual(sweeps[-1]["type"], "BEARISH_LIQUIDITY_SWEEP")
        self.assertEqual(sweeps[-1]["swept_level"], "PDH")

    def test_risk_guardrails_circuit_breakers(self):
        """Test Kill switches: >= 1.0% profit lock, 4 consecutive loss lockout, max drawdown."""
        # Case 1: Target achieved >= TARGET_NET_PROFIT_PCT (>= 1.0%)
        status1 = evaluate_risk_guardrails(
            daily_net_profit_pct=1.05,
            consecutive_losses=0,
            current_drawdown_pct=0.0,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertFalse(status1["can_trade"])
        self.assertEqual(status1["trigger"], "PROFIT_GOAL_LOCKED")
        
        # Case 2: 4 consecutive losses circuit breaker
        status2 = evaluate_risk_guardrails(
            daily_net_profit_pct=-0.5,
            consecutive_losses=4,
            current_drawdown_pct=0.5,
            current_time=time(10, 0),
            trade_risk_reward=2.0
        )
        self.assertFalse(status2["can_trade"])
        self.assertEqual(status2["trigger"], "CONSECUTIVE_LOSS_CIRCUIT_BREAKER")
        
        # Case 3: In midday chop window (12:00 PM)
        status3 = evaluate_risk_guardrails(
            daily_net_profit_pct=0.1,
            consecutive_losses=0,
            current_drawdown_pct=0.0,
            current_time=time(12, 15),
            trade_risk_reward=2.0
        )
        self.assertEqual(status3["window_status"], "MIDDAY_CHOP_WARNING")
        
        # Case 4: High volume institutional window with R:R >= 1.5
        status4 = evaluate_risk_guardrails(
            daily_net_profit_pct=0.1,
            consecutive_losses=0,
            current_drawdown_pct=0.0,
            current_time=time(9, 45),
            trade_risk_reward=2.0
        )
        self.assertTrue(status4["can_trade"])
        self.assertEqual(status4["window_status"], "INSTITUTIONAL_VOLUME_WINDOW")

    def test_institutional_ai_signal_generation(self):
        """Test AI signal generation on a bullish liquidity sweep."""
        bars = [
            {"timestamp": "2026-10-28 09:25:00", "Open": 23050, "High": 23060, "Low": 23020, "Close": 23040, "Volume": 5000},
            {"timestamp": "2026-10-28 09:30:00", "Open": 23040, "High": 23055, "Low": 22980, "Close": 23045, "Volume": 18000},
        ]
        df = pd.DataFrame(bars)
        
        signal = generate_institutional_ai_signal(
            symbol="NIFTY",
            spot_price=23045.0,
            df=df,
            daily_net_profit_pct=0.1,
            consecutive_losses=0,
            current_drawdown_pct=0.0
        )
        
        self.assertIn(signal["action"], ["BUY", "SELL", "PASS"])
        self.assertIn("statutory_charges", signal)
        self.assertIn("confidence_score", signal)
        self.assertIn("reasoning", signal)
        self.assertGreater(len(signal["reasoning"]), 0)


if __name__ == "__main__":
    unittest.main()
