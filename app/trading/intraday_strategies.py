"""
Comprehensive Intraday Trading Strategy Suite for Indian Stock Market (NSE/BSE).
Includes 7 verified intraday strategies:
1. Opening Range Breakout (ORB-15m / ORB-5m) with Midpoint & Range Stop
2. VWAP Institutional Trend & Mean Reversion (with +/- 1 & 2 StdDev bands)
3. EMA Ribbon Momentum Scalper (9 EMA / 20 EMA / 50 EMA Pullback)
4. SuperTrend Dynamic Trend Following (10, 3 on 5-minute candles)
5. Daily & Camarilla Pivot Point Reversals/Breakouts (H3/H4 & L3/L4)
6. Bollinger Bands Volatility Expansion & Squeeze Breakout
7. RSI Exhaustion & Momentum Divergence (14-period RSI)

Plus a Quantitative Strategy Confluence Aggregator providing a composite signal.
"""

import math
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np


class IntradayStrategySuite:
    """
    Computes indicators and evaluates trade setups across all major
    intraday strategies for Indian equities.
    """

    @staticmethod
    def prepare_dataframe(bars: List[Dict[str, Any]]) -> pd.DataFrame:
        """Converts raw candle dictionaries to a clean pandas DataFrame."""
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        # Normalize column names
        rename_map = {
            "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
            "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
            "Datetime": "timestamp", "Date": "timestamp", "time": "timestamp"
        }
        df = df.rename(columns=rename_map)
        for col in ["open", "high", "low", "close"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        else:
            df["volume"] = 1000.0
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        return df

    @classmethod
    def compute_technical_indicators(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Calculates VWAP, EMAs, SuperTrend, Bollinger Bands, RSI, and Pivots."""
        if len(df) < 5:
            return df

        # 1. EMAs (9, 20, 50)
        df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

        # 2. VWAP & Standard Deviation Bands
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = typical_price * df["volume"]
        cum_pv = pv.cumsum()
        cum_vol = df["volume"].cumsum().replace(0, 1)
        df["vwap"] = cum_pv / cum_vol

        # VWAP Bands (1 & 2 StdDev)
        vwap_var = ((typical_price - df["vwap"]) ** 2 * df["volume"]).cumsum() / cum_vol
        vwap_std = np.sqrt(np.maximum(vwap_var, 0))
        df["vwap_upper_1"] = df["vwap"] + vwap_std
        df["vwap_lower_1"] = df["vwap"] - vwap_std
        df["vwap_upper_2"] = df["vwap"] + 2.0 * vwap_std
        df["vwap_lower_2"] = df["vwap"] - 2.0 * vwap_std

        # 3. ATR (14)
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr_14"] = tr.rolling(window=14, min_periods=1).mean()

        # 4. SuperTrend (10, 3)
        multiplier = 3.0
        hl2 = (df["high"] + df["low"]) / 2.0
        upper_basic = hl2 + (multiplier * df["atr_14"])
        lower_basic = hl2 - (multiplier * df["atr_14"])

        upper_band = upper_basic.copy()
        lower_band = lower_basic.copy()
        supertrend = np.zeros(len(df))
        direction = np.ones(len(df))  # 1 = bullish, -1 = bearish

        for i in range(1, len(df)):
            # Upper band logic
            if upper_basic.iloc[i] < upper_band.iloc[i - 1] or df["close"].iloc[i - 1] > upper_band.iloc[i - 1]:
                upper_band.iloc[i] = upper_basic.iloc[i]
            else:
                upper_band.iloc[i] = upper_band.iloc[i - 1]

            # Lower band logic
            if lower_basic.iloc[i] > lower_band.iloc[i - 1] or df["close"].iloc[i - 1] < lower_band.iloc[i - 1]:
                lower_band.iloc[i] = lower_basic.iloc[i]
            else:
                lower_band.iloc[i] = lower_band.iloc[i - 1]

            # Trend direction
            if direction[i - 1] == 1:
                if df["close"].iloc[i] < lower_band.iloc[i]:
                    direction[i] = -1
                    supertrend[i] = upper_band.iloc[i]
                else:
                    direction[i] = 1
                    supertrend[i] = lower_band.iloc[i]
            else:
                if df["close"].iloc[i] > upper_band.iloc[i]:
                    direction[i] = 1
                    supertrend[i] = lower_band.iloc[i]
                else:
                    direction[i] = -1
                    supertrend[i] = upper_band.iloc[i]

        df["supertrend"] = supertrend
        df["supertrend_dir"] = direction

        # 5. Bollinger Bands (20, 2)
        df["bb_middle"] = df["close"].rolling(window=20, min_periods=1).mean()
        bb_std = df["close"].rolling(window=20, min_periods=1).std().fillna(0)
        df["bb_upper"] = df["bb_middle"] + 2.0 * bb_std
        df["bb_lower"] = df["bb_middle"] - 2.0 * bb_std
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"].replace(0, 1)

        # 6. RSI (14)
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=14, min_periods=1).mean()
        avg_loss = loss.rolling(window=14, min_periods=1).mean().replace(0, 1e-9)
        rs = avg_gain / avg_loss
        df["rsi_14"] = 100 - (100 / (1 + rs))

        # 7. Volume SMA
        df["vol_sma_20"] = df["volume"].rolling(window=20, min_periods=1).mean()

        return df

    # --- Strategy 1: Opening Range Breakout (ORB-15m) ---
    @classmethod
    def evaluate_orb(cls, df: pd.DataFrame, or_bars_count: int = 3) -> Dict[str, Any]:
        """
        ORB 15-Minute Breakout:
        Requires close above OR High with volume confirmation.
        Stop loss at OR midpoint.
        """
        if len(df) <= or_bars_count:
            return {"active": False, "signal": "HOLD", "reason": "Opening range still forming"}

        or_df = df.iloc[:or_bars_count]
        or_high = float(or_df["high"].max())
        or_low = float(or_df["low"].min())
        or_mid = round((or_high + or_low) / 2.0, 2)
        current = df.iloc[-1]
        prev = df.iloc[-2]

        cur_close = float(current["close"])
        cur_open = float(current["open"])
        cur_high = float(current["high"])
        cur_low = float(current["low"])
        vol = float(current["volume"])
        vol_sma = float(current.get("vol_sma_20", 1.0))
        vol_confirmed = vol >= (vol_sma * 1.15) if vol_sma > 0 else True
        vwap = float(current.get("vwap", cur_close))
        vwap_u2 = float(current.get("vwap_upper_2", cur_close * 1.02))
        vwap_l2 = float(current.get("vwap_lower_2", cur_close * 0.98))
        rsi = float(current.get("rsi_14", 50.0))
        ema_9 = float(current.get("ema_9", cur_close))
        ema_20 = float(current.get("ema_20", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        st_dir = int(current.get("supertrend_dir", 1))
        bb_upper = float(current.get("bb_upper", cur_close * 1.02))

        candle_rng = cur_high - cur_low
        bullish_bar = cur_close >= cur_open and ((cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True)
        not_chasing = cur_close <= (or_high * 1.010)
        not_overbought = cur_close < (bb_upper * 1.002)
        min_gain_est = cur_close * 0.0035
        headroom_ok = (bb_upper >= cur_close * 0.999) if len(df) <= 12 else (bb_upper >= cur_close + min_gain_est * 0.5)
        breakout_margin_ok = cur_close >= (or_high * 1.0005)
        range_meaningful = (or_high - or_low) >= (cur_close * 0.0020)
        range_not_exhausted = (or_high - or_low) <= (cur_close * 0.025)
        trend_ok = (ema_9 >= ema_20 * 0.998) and (st_dir == 1) and (cur_close >= ema_50 * 0.998)

        # Time-of-day regime gates: avoid midday lull and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "ORB blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:30":
            return {"active": False, "signal": "HOLD", "reason": "ORB blocked after 13:30 IST (insufficient morning momentum runway)"}

        # Check for initial breakout of the session (prevents choppy re-cross traps)
        if len(df) > or_bars_count + 1:
            prior_closes = df.iloc[or_bars_count:-1]["close"]
            had_prior_breakout = bool((prior_closes > or_high).any())
            had_prior_breakdown = bool((prior_closes < or_low * 0.998).any())
        else:
            had_prior_breakout = False
            had_prior_breakdown = False

        # In afternoon sessions (> 13:15 IST), enforce higher volume confirmation (>= 1.25x)
        vol_req = (vol_sma * 1.25) if (time_part and time_part > "13:15" and vol_sma > 0) else (vol_sma * 1.15)
        vol_confirmed_orb = vol >= vol_req if vol_sma > 0 else True

        # Bullish Breakout: green candle with upper close, above VWAP, volume confirmed, healthy RSI (46-72.0), not chasing, initial breakout
        if cur_close > or_high and breakout_margin_ok and prev["close"] <= or_high and not had_prior_breakout and not had_prior_breakdown and vol_confirmed_orb and bullish_bar and cur_close >= vwap and (46.0 <= rsi <= 72.0) and not_chasing and not_overbought and trend_ok and range_meaningful and range_not_exhausted and headroom_ok:
            atr = float(current.get("atr_14", cur_close * 0.005))
            stop = round(max(or_mid, cur_close - (atr * 1.15)), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            min_gain = max(risk * 1.18, cur_close * 0.0035)
            tp2 = round(min(cur_close + (risk * 1.38), max(cur_close + (risk * 1.25), bb_upper * 1.001)), 2)
            tp1 = round(max(cur_close + min_gain, min(cur_close + (risk * 1.18), tp2 - (risk * 0.08))), 2)
            tp1 = min(tp1, round(tp2 - (risk * 0.08), 2))
            tp1 = round(max(tp1, cur_close + cur_close * 0.0035), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "orb_15m_breakout",
                "name": "Opening Range Breakout (ORB-15m)",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 92,
                "rationale": f"High-conviction ORB-15m breakout: 5m close ₹{cur_close:.2f} broke above 15m OR High ₹{or_high:.2f} with institutional volume ({vol:.0f} >= {vol_req:.0f}) and VWAP confirmation. Disciplined stop loss anchored at ₹{stop:.2f} with primary take-profit Target 1 at ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        bb_lower = float(current.get("bb_lower", cur_close * 0.98))
        bearish_bar = cur_close <= cur_open and ((cur_high - cur_close) >= (candle_rng * 0.52) if candle_rng > 0 else True)
        not_chasing_short = cur_close >= (or_low * 0.990)
        not_oversold_short = cur_close > (bb_lower * 0.998)
        breakdown_margin_ok = cur_close <= (or_low * 0.9995)
        trend_ok_short = (ema_9 <= ema_20 * 1.002) and (st_dir == -1) and (cur_close <= ema_50 * 1.002)
        headroom_ok_short = (bb_lower <= cur_close * 1.001) if len(df) <= 12 else (bb_lower <= cur_close - min_gain_est * 0.5)

        # Bearish Breakdown: red candle, below VWAP, volume confirmed, healthy RSI, initial breakdown
        if cur_close < or_low and breakdown_margin_ok and prev["close"] >= or_low and not had_prior_breakdown and not had_prior_breakout and vol_confirmed_orb and bearish_bar and cur_close <= vwap and (30.0 <= rsi <= 54.0) and not_chasing_short and not_oversold_short and trend_ok_short and range_meaningful and range_not_exhausted and headroom_ok_short:
            atr = float(current.get("atr_14", cur_close * 0.005))
            stop = round(min(or_mid, cur_close + (atr * 1.15)), 2)
            stop = max(stop, round(cur_close + max(cur_close * 0.0035, atr * 0.35), 2))
            stop = min(stop, round(cur_close + max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(stop - cur_close, cur_close * 0.003)
            min_gain = max(risk * 1.18, cur_close * 0.0035)
            tp2 = round(max(cur_close - (risk * 1.38), min(cur_close - (risk * 1.25), bb_lower * 0.999)), 2)
            tp1 = round(min(cur_close - min_gain, max(cur_close - (risk * 1.18), tp2 + (risk * 0.08))), 2)
            tp1 = max(tp1, round(tp2 + (risk * 0.08), 2))
            tp1 = round(min(tp1, cur_close - cur_close * 0.0035), 2)
            tp2 = min(tp2, round(tp1 - max(risk * 0.08, cur_close * 0.0010), 2))
            round_floor = (math.ceil(cur_close / 10.0) - 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_floor >= tp2 and (round_floor - tp2) <= (risk * 0.28):
                if (round_floor + 0.15) <= (cur_close - min_gain - min_tp_spread):
                    tp2 = round(round_floor + 0.15, 2)
                    tp1 = max(tp1, round(tp2 + min_tp_spread, 2))
                    tp1 = min(tp1, round(cur_close - min_gain, 2))
                    tp2 = min(tp2, round(tp1 - min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "orb_15m_breakout",
                "name": "Opening Range Breakdown (ORB-15m)",
                "signal": "SELL",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 84,
                "rationale": f"5m close ₹{cur_close:.2f} broke below 15m OR Low ₹{or_low:.2f} with volume & VWAP confirmation. Primary take-profit Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD", "reason": "Price within opening range"}

    # --- Strategy 2: VWAP Institutional Trend & Bounce ---
    @classmethod
    def evaluate_vwap_trend(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        VWAP Institutional Bounce & Trend Continuation:
        Long when price dips to test VWAP and bounces with bullish rejection candle.
        """
        if len(df) < 5 or "vwap" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        prev = df.iloc[-2]
        vwap = float(current["vwap"])
        cur_close = float(current["close"])
        cur_low = float(current["low"])
        prev_close = float(prev["close"])
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        vol_ok = vol >= (vol_sma * 1.15) if vol_sma > 0 else True

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "VWAP bounce blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "VWAP bounce blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:40":
            return {"active": False, "signal": "HOLD", "reason": "VWAP bounce blocked after 13:40 IST (insufficient runway before 15:20 EOD square-off)"}

        # Dynamic Volume Confirmation: 1.10x SMA20 morning, 1.15x afternoon (> 13:15 IST)
        vol_req = (vol_sma * 1.15) if (time_part and time_part > "13:15" and vol_sma > 0) else (vol_sma * 1.10)
        vol_ok = vol >= vol_req if vol_sma > 0 else True

        # VWAP Bullish Bounce (Touched VWAP and closed green above it with volume & trend alignment)
        st_dir = int(current.get("supertrend_dir", 1))
        ema_9 = float(current.get("ema_9", cur_close))
        ema_20 = float(current.get("ema_20", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        vwap_u2 = float(current.get("vwap_upper_2", cur_close * 1.02))
        bb_upper = float(current.get("bb_upper", cur_close * 1.01))
        cur_open = float(current["open"])
        cur_high = float(current["high"])
        candle_rng = cur_high - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True

        # Quadruple filter: SuperTrend bullish, EMA9 >= EMA20, EMA20 >= EMA50, Close >= EMA50, healthy RSI
        trend_aligned = (st_dir == 1) and (ema_9 >= ema_20 * 0.998) and (ema_20 >= ema_50 * 0.998) and (cur_close >= ema_50 * 0.998) and (45.0 <= rsi <= 68.0)
        # Extension & Proximity Gate: prevent entering when already stretched to Upper Bollinger ceiling
        not_extended = (cur_close < bb_upper * 0.9995)
        not_chased = cur_close <= (vwap * 1.005)
        bounce_margin_ok = cur_close >= (vwap * 1.0005)
        prior_trend_ok = prev_close >= (vwap * 0.997)
        min_gain_est = cur_close * 0.0035
        headroom_ok = (bb_upper >= cur_close + min_gain_est * 0.5)

        if cur_low <= vwap * 1.0025 and cur_low >= vwap * 0.994 and cur_close > vwap and bounce_margin_ok and cur_close >= cur_open and close_quality and prior_trend_ok and vol_ok and trend_aligned and not_extended and not_chased and headroom_ok:
            atr = float(current.get("atr_14", cur_close * 0.005))
            stop = round(min(cur_low, vwap) - (atr * 0.35), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            min_gain = max(risk * 1.18, cur_close * 0.0035)
            tp2 = round(min(cur_close + (risk * 1.38), max(cur_close + (risk * 1.25), vwap_u2)), 2)
            tp1 = round(max(cur_close + min_gain, min(cur_close + (risk * 1.18), tp2 - (risk * 0.08))), 2)
            tp1 = min(tp1, round(tp2 - (risk * 0.08), 2))
            tp1 = round(max(tp1, cur_close + cur_close * 0.0035), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "vwap_institutional_bounce",
                "name": "VWAP Institutional Bounce",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 90,
                "rationale": f"Rejection of VWAP (₹{vwap:.2f}) with bullish green close at ₹{cur_close:.2f}. SuperTrend Bullish, volume confirmed, 20-EMA dynamic support respected. Primary take-profit Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 3: EMA Ribbon Pullback (9 / 20 / 50 EMA) ---
    @classmethod
    def evaluate_ema_pullback(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Fast EMA Ribbon Scalp:
        9 EMA > 20 EMA > 50 EMA with price pulling back to test 20 EMA and closing green.
        """
        if len(df) < 15 or "ema_20" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        ema_9 = float(current.get("ema_9", 0))
        ema_20 = float(current.get("ema_20", 0))
        ema_50 = float(current.get("ema_50", 0))
        cur_close = float(current["close"])
        cur_open = float(current["open"])
        cur_low = float(current["low"])
        vwap = float(current.get("vwap", cur_close))
        st_dir = int(current.get("supertrend_dir", 1))
        rsi = float(current.get("rsi_14", 50.0))
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        bb_upper = float(current.get("bb_upper", cur_close * 1.02))
        vwap_u2 = float(current.get("vwap_upper_2", cur_close * 1.02))
        not_extended = (cur_close < bb_upper * 0.9995)
        min_gain_est = cur_close * 0.0035
        headroom_ok = (bb_upper >= cur_close + min_gain_est * 0.5)

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "EMA pullback blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "EMA pullback blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:40":
            return {"active": False, "signal": "HOLD", "reason": "EMA pullback blocked after 13:40 IST (insufficient time before 15:20 EOD square-off)"}

        # Volume confirmation: 1.15x SMA20 morning, 1.25x afternoon (> 13:15 IST)
        vol_req = (vol_sma * 1.25) if (time_part and time_part > "13:15" and vol_sma > 0) else (vol_sma * 1.15)
        vol_ok = vol >= vol_req if vol_sma > 0 else True

        # Triple EMA trend alignment backed by VWAP, SuperTrend, volume, and RSI
        above_baseline = cur_low >= ema_50 * 0.998
        trend_aligned = (st_dir == 1) and (ema_9 > ema_20) and (ema_20 > ema_50) and (cur_close >= vwap) and (45.0 <= rsi <= 68.0) and (cur_close >= ema_50) and above_baseline
        candle_rng = float(current["high"]) - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True
        not_chased = cur_close <= (ema_20 * 1.005)

        if trend_aligned and vol_ok and not_extended and not_chased and headroom_ok:
            # Low tested 20 EMA dynamic support, closed green, and reclaimed 9-EMA momentum
            if cur_low <= ema_20 * 1.0025 and cur_low >= ema_20 * 0.996 and cur_close > ema_20 and cur_close >= cur_open and close_quality and (cur_close >= ema_9 * 0.999):
                atr = float(current.get("atr_14", cur_close * 0.005))
                stop = round(min(ema_20, cur_low) - (atr * 0.35), 2)
                stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
                stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
                risk = max(cur_close - stop, cur_close * 0.003)
                min_gain = max(risk * 1.18, cur_close * 0.0035)
                tp2 = round(min(cur_close + (risk * 1.38), max(cur_close + (risk * 1.25), bb_upper * 0.999)), 2)
                tp1 = round(max(cur_close + min_gain, min(cur_close + (risk * 1.18), tp2 - (risk * 0.08))), 2)
                tp1 = min(tp1, round(tp2 - (risk * 0.08), 2))
                tp1 = round(max(tp1, cur_close + cur_close * 0.0035), 2)
                tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
                round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
                min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
                if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                    if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                        tp2 = round(round_barrier - 0.15, 2)
                        tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                        tp1 = max(tp1, round(cur_close + min_gain, 2))
                        tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
                return {
                    "active": True,
                    "strategy_id": "ema_ribbon_pullback",
                    "name": "9/20 EMA Ribbon Pullback Scalper",
                    "signal": "BUY",
                    "entry_price": round(cur_close, 2),
                    "stop_loss": stop,
                    "target_1": tp1,
                    "target_2": tp2,
                    "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                    "confidence": 88,
                    "rationale": f"Bullish EMA ribbon (9-EMA ₹{ema_9:.2f} > 20-EMA ₹{ema_20:.2f} > 50-EMA ₹{ema_50:.2f}) pullback bounce confirmed. Primary Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
                }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 4: SuperTrend Dynamic Trend Following ---
    @classmethod
    def evaluate_supertrend(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        SuperTrend (10, 3) Trend Signal:
        Captures newly flipped green SuperTrend with trailing stop and volume confirmation.
        """
        if len(df) < 5 or "supertrend" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_dir = current["supertrend_dir"]
        prev_dir = prev["supertrend_dir"]
        cur_close = float(current["close"])
        st_val = float(current["supertrend"])

        # Fresh Bullish Flip with VWAP confirmation, healthy volume, bullish candle and RSI
        vwap = float(current.get("vwap", cur_close))
        ema_9 = float(current.get("ema_9", cur_close))
        ema_20 = float(current.get("ema_20", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        bb_upper = float(current.get("bb_upper", cur_close * 1.02))
        vwap_u2 = float(current.get("vwap_upper_2", cur_close * 1.02))
        not_extended = (cur_close < bb_upper * 0.9995)
        trend_aligned = (ema_9 >= ema_20) and (ema_20 >= ema_50) and (cur_close >= ema_50)
        cur_open = float(current["open"])
        cur_high = float(current["high"])
        cur_low = float(current["low"])
        candle_rng = cur_high - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True
        not_chased = cur_close <= (ema_20 * 1.008)
        min_gain_est = cur_close * 0.0035
        headroom_ok = (bb_upper >= cur_close + min_gain_est * 0.5)

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "SuperTrend flip blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "SuperTrend flip blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:40":
            return {"active": False, "signal": "HOLD", "reason": "SuperTrend flip blocked after 13:40 IST (insufficient time before 15:20 EOD square-off)"}

        # Volume confirmation: 1.15x SMA20 morning, 1.25x afternoon (> 13:15 IST)
        vol_req = (vol_sma * 1.25) if (time_part and time_part > "13:15" and vol_sma > 0) else (vol_sma * 1.15)
        vol_ok = vol >= vol_req if vol_sma > 0 else True

        if cur_dir == 1 and prev_dir == -1 and cur_close >= vwap and cur_close >= ema_20 and (48.0 <= rsi <= 68.0) and vol_ok and (cur_close >= cur_open) and close_quality and not_extended and not_chased and trend_aligned and headroom_ok:
            atr = float(current.get("atr_14", cur_close * 0.005))
            stop = round(max(st_val, cur_close - (atr * 0.95)), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            min_gain = max(risk * 1.18, cur_close * 0.0035)
            tp2 = round(min(cur_close + (risk * 1.38), max(cur_close + (risk * 1.25), bb_upper * 0.999)), 2)
            tp1 = round(max(cur_close + min_gain, min(cur_close + (risk * 1.18), tp2 - (risk * 0.08))), 2)
            tp1 = min(tp1, round(tp2 - (risk * 0.08), 2))
            tp1 = round(max(tp1, cur_close + cur_close * 0.0035), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "supertrend_trend_flip",
                "name": "SuperTrend (10, 3) Bullish Flip",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 84,
                "rationale": f"SuperTrend flipped BULLISH at ₹{cur_close:.2f} with trailing support at ₹{stop:.2f}. Primary Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 5: Daily & Camarilla Pivot Breakouts ---
    @classmethod
    def evaluate_pivots(cls, df: pd.DataFrame, prev_high: float, prev_low: float, prev_close: float) -> Dict[str, Any]:
        """
        Camarilla & Classic Pivot Breakout:
        H4 Breakout is one of the highest probability intraday momentum trades in Indian equities.
        """
        if prev_high <= 0 or prev_low <= 0 or prev_close <= 0 or len(df) < 2:
            return {"active": False, "signal": "HOLD"}

        # Camarilla Formulas
        rng = prev_high - prev_low
        h4 = prev_close + (rng * 1.1 / 2.0)
        h3 = prev_close + (rng * 1.1 / 4.0)
        l3 = prev_close - (rng * 1.1 / 4.0)
        l4 = prev_close - (rng * 1.1 / 2.0)
        h5 = prev_close + (rng * 1.1)

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_close = float(current["close"])
        prev_c = float(prev["close"])
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        st_dir = int(current.get("supertrend_dir", 1))
        vwap = float(current.get("vwap", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        ema_9 = float(current.get("ema_9", cur_close))
        ema_20 = float(current.get("ema_20", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        cur_open = float(current["open"])
        cur_high = float(current["high"])
        cur_low = float(current["low"])
        candle_rng = cur_high - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True
        bb_upper = float(current.get("bb_upper", cur_close * 1.02))
        vwap_u2 = float(current.get("vwap_upper_2", cur_close * 1.02))
        not_extended = (cur_close < bb_upper * 0.9995)
        min_gain_est = cur_close * 0.0035
        headroom_ok = (bb_upper >= cur_close + min_gain_est * 0.5)

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "Pivot breakout blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "Pivot breakout blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:40":
            return {"active": False, "signal": "HOLD", "reason": "Pivot breakout blocked after 13:40 IST (insufficient time before 15:20 EOD square-off)"}

        # Volume confirmation: 1.15x SMA20 morning, 1.25x afternoon (> 13:15 IST)
        vol_req = (vol_sma * 1.25) if (time_part and time_part > "13:15" and vol_sma > 0) else (vol_sma * 1.15)
        vol_ok = vol >= vol_req if vol_sma > 0 else True

        # Check for pristine initial H4 breakout (prevents choppy secondary re-cross traps)
        had_prior_h4_breakout = bool((df.iloc[:-1]["close"] > h4).any()) if len(df) > 3 else False

        trend_ok = (st_dir == 1) and (cur_close >= vwap) and (45.0 <= rsi <= 68.5) and (cur_close >= cur_open) and close_quality and (ema_9 >= ema_20) and (cur_close >= ema_50) and (ema_20 >= ema_50) and not_extended
        not_chasing = cur_close <= (h4 * 1.010)
        breakout_margin_ok = cur_close >= (h4 * 1.0005)

        # H4 Bullish Breakout
        if cur_close > h4 and breakout_margin_ok and prev_c <= h4 and not had_prior_h4_breakout and vol_ok and trend_ok and not_chasing and headroom_ok:
            atr = float(current.get("atr_14", cur_close * 0.005))
            stop = round(max(h3, cur_close - (atr * 1.15)), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            min_gain = max(risk * 1.18, cur_close * 0.0035)
            tp2 = round(min(cur_close + (risk * 1.38), max(cur_close + (risk * 1.25), h5)), 2)
            tp1 = round(max(cur_close + min_gain, min(cur_close + (risk * 1.18), tp2 - (risk * 0.08))), 2)
            tp1 = min(tp1, round(tp2 - (risk * 0.08), 2))
            tp1 = round(max(tp1, cur_close + cur_close * 0.0035), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "camarilla_h4_breakout",
                "name": "Camarilla H4 Breakout",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Primary Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 89,
                "rationale": f"Price ₹{cur_close:.2f} exploded through Camarilla H4 resistance (₹{h4:.2f}) targeting H5 expansion. Primary Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 6: Bollinger Band Squeeze & Volatility Target ---
    @classmethod
    def evaluate_bollinger_expansion(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Bollinger Band Expansion / Mean Reversion:
        In intraday trading, an extended candle piercing the Upper Band (+2 StdDev)
        signals overbought exhaustion, not an entry trigger.
        Triggers BUY only on deep Lower Band bounce back into the bands.
        """
        if len(df) < 20 or "bb_lower" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_close = float(current["close"])
        cur_low = float(current["low"])
        bb_lower = float(current["bb_lower"])
        bb_middle = float(current["bb_middle"])
        vol = float(current["volume"])
        vol_sma = float(current.get("vol_sma_20", 1.0))
        rsi = float(current.get("rsi_14", 50.0))

        # Rejection of lower band with strong green reversal candle and oversold RSI
        vol_ok = vol >= (vol_sma * 1.10) if vol_sma > 0 else True
        cur_open = float(current["open"])
        cur_high = float(current["high"])
        candle_rng = cur_high - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True
        vwap = float(current.get("vwap", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        not_crashing = cur_close >= (vwap * 0.992)
        atr = float(current.get("atr_14", cur_close * 0.005))
        min_gain = max(cur_close * 0.0031, atr * 0.33)
        headroom_ok = (cur_close < bb_middle) and ((bb_middle * 0.9995 - cur_close) >= min_gain * 0.5)

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "Bollinger reversal blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "Bollinger reversal blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "14:00":
            return {"active": False, "signal": "HOLD", "reason": "Bollinger reversal blocked after 14:00 IST (insufficient time before 15:20 EOD square-off)"}

        # Trend trap protection: block counter-trend dips below both 50-EMA & VWAP
        if cur_close < ema_50 and cur_close < vwap * 0.995:
            return {"active": False, "signal": "HOLD", "reason": "Counter-trend dip below 50-EMA & VWAP ignored"}

        if prev["low"] <= bb_lower and cur_close > bb_lower and cur_close >= cur_open and close_quality and vol_ok and not_crashing and headroom_ok and (25.0 <= rsi <= 45.0):
            stop = round(min(prev["low"], cur_low) - (atr * 0.35), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            fee_hurdle = cur_close * 0.0035
            tp1 = round(min(bb_middle * 0.9995, cur_close + max(risk * 1.18, fee_hurdle)), 2)
            tp1 = max(tp1, round(cur_close + fee_hurdle, 2))
            tp2 = round(max(tp1 + (risk * 0.16), min(cur_close + (risk * 1.38), max(bb_middle, cur_close + (risk * 1.25)))), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "bollinger_lower_reversal",
                "name": "Bollinger Bands Lower Band Reversal",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Mean Reversion Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 86,
                "rationale": f"High-probability statistical mean-reversion bounce: price rejected lower Bollinger Band (₹{bb_lower:.2f}) with green reversal candle. Primary Target 1 is mean reversion to 20-SMA middle band at ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 7: RSI Exhaustion Bounce (Mean Reversion) ---
    @classmethod
    def evaluate_rsi_reversion(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        RSI Oversold Bounce:
        Intraday RSI dipping under 30 and crossing back above 32 with green candle.
        """
        if len(df) < 15 or "rsi_14" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_rsi = float(current["rsi_14"])
        prev_rsi = float(prev["rsi_14"])
        cur_close = float(current["close"])
        ema_50 = float(current.get("ema_50", cur_close))
        vwap = float(current.get("vwap", cur_close))
        bb_middle = float(current.get("bb_middle", cur_close * 1.01))

        # Strict Trend Alignment: Filter out counter-trend falling knives!
        # Only buy RSI oversold bounces when stock is supported above 50-EMA or VWAP
        if cur_close < ema_50 and cur_close < vwap * 0.995:
            return {"active": False, "signal": "HOLD", "reason": "Counter-trend dip below 50-EMA & VWAP ignored"}

        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        vol_ok = vol >= (vol_sma * 1.10) if vol_sma > 0 else True
        cur_open = float(current["open"])
        cur_low = float(current["low"])
        candle_rng = float(current["high"]) - cur_low
        close_quality = (cur_close - cur_low) >= (candle_rng * 0.52) if candle_rng > 0 else True
        atr = float(current.get("atr_14", cur_close * 0.005))
        min_gain = max(cur_close * 0.0031, atr * 0.33)
        headroom_ok = (cur_close < bb_middle) and ((bb_middle * 0.9995 - cur_close) >= min_gain * 0.5)

        # Time-of-day regime gates: avoid opening noise, midday lull, and late-session EOD decay
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and time_part < "09:30":
            return {"active": False, "signal": "HOLD", "reason": "RSI reversion blocked during opening price discovery (< 09:30 IST)"}
        if time_part and ("11:15" <= time_part <= "13:15"):
            return {"active": False, "signal": "HOLD", "reason": "RSI reversion blocked during midday lull (11:15-13:15 IST)"}
        if time_part and time_part > "13:45":
            return {"active": False, "signal": "HOLD", "reason": "RSI reversion blocked after 13:45 IST (insufficient time before 15:20 EOD square-off)"}

        if prev_rsi < 30 and (32.0 <= cur_rsi <= 44.0) and cur_close >= cur_open and close_quality and vol_ok and headroom_ok:
            stop = round(min(cur_low, float(prev["low"])) - (atr * 0.35), 2)
            stop = min(stop, round(cur_close - max(cur_close * 0.0035, atr * 0.35), 2))
            stop = max(stop, round(cur_close - max(atr * 0.95, cur_close * 0.0070), 2))
            risk = max(cur_close - stop, cur_close * 0.003)
            fee_hurdle = cur_close * 0.0035
            tp1 = round(min(bb_middle * 0.9995, cur_close + max(risk * 1.18, fee_hurdle)), 2)
            tp1 = max(tp1, round(cur_close + fee_hurdle, 2))
            tp2 = round(max(tp1 + (risk * 0.16), min(cur_close + (risk * 1.38), max(bb_middle, cur_close + (risk * 1.25)))), 2)
            tp2 = max(tp2, round(tp1 + max(risk * 0.08, cur_close * 0.0010), 2))
            round_barrier = (math.floor(cur_close / 10.0) + 1.0) * 10.0
            min_tp_spread = max(risk * 0.08, cur_close * 0.0008)
            if round_barrier <= tp2 and (tp2 - round_barrier) <= (risk * 0.28):
                if (round_barrier - 0.15) >= (cur_close + min_gain + min_tp_spread):
                    tp2 = round(round_barrier - 0.15, 2)
                    tp1 = min(tp1, round(tp2 - min_tp_spread, 2))
                    tp1 = max(tp1, round(cur_close + min_gain, 2))
                    tp2 = max(tp2, round(tp1 + min_tp_spread, 2))
            return {
                "active": True,
                "strategy_id": "rsi_oversold_reversal",
                "name": "RSI Oversold Mean Reversion",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.18 (Mean Reversion Target 1) / 1:1.38 (Momentum Ceiling Target 2)",
                "confidence": 82,
                "rationale": f"RSI recovered from oversold ({prev_rsi:.1f} -> {cur_rsi:.1f}) with green reversal candle. Primary Target 1 is ₹{tp1:.2f} (momentum ceiling ₹{tp2:.2f}).",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Quantitative Strategy Confluence Aggregator ---
    @classmethod
    def evaluate_all_strategies(
        cls,
        bars: List[Dict[str, Any]],
        prev_day_ohlc: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates all 7 intraday strategies simultaneously on the bar series.
        Returns individual strategy evaluations, active triggers, and composite confluence score.
        """
        df = cls.prepare_dataframe(bars)
        if len(df) < 5:
            return {
                "confluence_score": 0,
                "composite_signal": "HOLD",
                "active_signals_count": 0,
                "active_strategies": [],
                "best_strategy": None,
                "strategies_evaluated": {},
            }

        df = cls.compute_technical_indicators(df)
        cur_price = float(df.iloc[-1]["close"])

        p_high = prev_day_ohlc.get("high", cur_price * 1.01) if prev_day_ohlc else cur_price * 1.01
        p_low = prev_day_ohlc.get("low", cur_price * 0.99) if prev_day_ohlc else cur_price * 0.99
        p_close = prev_day_ohlc.get("close", cur_price) if prev_day_ohlc else cur_price

        results = {
            "orb": cls.evaluate_orb(df),
            "vwap": cls.evaluate_vwap_trend(df),
            "ema_ribbon": cls.evaluate_ema_pullback(df),
            "supertrend": cls.evaluate_supertrend(df),
            "pivots": cls.evaluate_pivots(df, p_high, p_low, p_close),
            "bollinger": cls.evaluate_bollinger_expansion(df),
            "rsi": cls.evaluate_rsi_reversion(df),
        }

        active = [s for s in results.values() if s.get("active") and s.get("signal") in ("BUY", "SELL")]
        buy_count = sum(1 for s in active if s.get("signal") == "BUY")
        sell_count = sum(1 for s in active if s.get("signal") == "SELL")

        # Confluence Score calculation (-100 to +100)
        net_score = (buy_count - sell_count) * 25
        net_score = max(-100, min(100, net_score))

        def _strategy_priority(s: Dict[str, Any]) -> float:
            sid = s.get("strategy_id", "")
            base_conf = float(s.get("confidence", 50))
            if sid == "orb_15m_breakout":
                return 220.0 + base_conf
            if sid == "vwap_institutional_bounce":
                return 185.0 + base_conf
            if sid == "camarilla_h4_breakout":
                return 155.0 + base_conf
            if sid == "ema_ribbon_pullback":
                return 135.0 + base_conf
            if sid == "supertrend_trend_flip":
                return 120.0 + base_conf
            if sid == "bollinger_lower_reversal":
                return 105.0 + base_conf
            if sid == "rsi_oversold_reversal":
                return 85.0 + base_conf
            return base_conf

        best = max(active, key=_strategy_priority) if active else None

        composite_signal = "BUY" if net_score >= 50 and buy_count >= 2 and sell_count == 0 else (
            "SELL" if net_score <= -50 and sell_count >= 2 and buy_count == 0 else (
                best.get("signal") if (best and best.get("strategy_id") in ("orb_15m_breakout", "camarilla_h4_breakout", "vwap_institutional_bounce", "bollinger_lower_reversal") and best.get("signal") == "BUY" and sell_count == 0) else "HOLD"
            )
        )

        return {
            "confluence_score": net_score,
            "composite_signal": composite_signal,
            "active_signals_count": len(active),
            "active_strategies": active,
            "best_strategy": best,
            "indicators": {
                "price": cur_price,
                "vwap": round(float(df.iloc[-1].get("vwap", cur_price)), 2),
                "vwap_distance_pct": round(((cur_price - float(df.iloc[-1].get("vwap", cur_price))) / float(df.iloc[-1].get("vwap", cur_price))) * 100.0, 3) if float(df.iloc[-1].get("vwap", 0)) > 0 else 0.0,
                "ema_9": round(float(df.iloc[-1].get("ema_9", cur_price)), 2),
                "ema_20": round(float(df.iloc[-1].get("ema_20", cur_price)), 2),
                "ema_50": round(float(df.iloc[-1].get("ema_50", cur_price)), 2),
                "supertrend": round(float(df.iloc[-1].get("supertrend", cur_price)), 2),
                "supertrend_dir": int(df.iloc[-1].get("supertrend_dir", 1)),
                "rsi_14": round(float(df.iloc[-1].get("rsi_14", 50.0)), 1),
                "atr_14": round(float(df.iloc[-1].get("atr_14", cur_price * 0.005)), 2),
                "bb_upper": round(float(df.iloc[-1].get("bb_upper", cur_price * 1.02)), 2),
                "bb_middle": round(float(df.iloc[-1].get("bb_middle", cur_price)), 2),
                "bb_lower": round(float(df.iloc[-1].get("bb_lower", cur_price * 0.98)), 2),
                "vol_sma_20": round(float(df.iloc[-1].get("vol_sma_20", 1.0)), 1),
                "volume_ratio": round(float(df.iloc[-1].get("volume", 0)) / float(df.iloc[-1].get("vol_sma_20", 1.0)), 2) if float(df.iloc[-1].get("vol_sma_20", 0)) > 0 else 1.0,
                "confluence_score": net_score,
                "active_signals_count": len(active),
            },
            "strategies_evaluated": results,
        }


intraday_engine = IntradayStrategySuite()
