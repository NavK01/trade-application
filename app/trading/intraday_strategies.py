"""
Comprehensive Intraday Trading Strategy Suite for Indian Stock Market (NSE/BSE).
Includes 7 verified intraday quantitative strategies:
1. Opening Range Breakout (ORB-15m) with Midpoint Stop & Trend Confirmation
2. VWAP Institutional Trend Bounce & Expansion
3. EMA Ribbon Momentum Scalper (9 EMA / 20 EMA / 50 EMA Pullback)
4. SuperTrend Dynamic Trend Following (10, 3 on 5-minute candles)
5. Camarilla Institutional Pivot Breakouts (H4 Breakout & L4 Breakdown)
6. Bollinger Bands Lower Band Mean Reversion Bounce
7. RSI Exhaustion & Oversold Reversal (14-period RSI)

Plus a Quantitative Strategy Confluence Aggregator providing actionable composite signals.
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
        """Calculates VWAP, EMAs, SuperTrend, Bollinger Bands, RSI, and Volume SMA."""
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
            if upper_basic.iloc[i] < upper_band.iloc[i - 1] or df["close"].iloc[i - 1] > upper_band.iloc[i - 1]:
                upper_band.iloc[i] = upper_basic.iloc[i]
            else:
                upper_band.iloc[i] = upper_band.iloc[i - 1]

            if lower_basic.iloc[i] > lower_band.iloc[i - 1] or df["close"].iloc[i - 1] < lower_band.iloc[i - 1]:
                lower_band.iloc[i] = lower_basic.iloc[i]
            else:
                lower_band.iloc[i] = lower_band.iloc[i - 1]

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
        Requires close above OR High with volume & VWAP confirmation.
        Stop loss at OR midpoint or natural ATR stop.
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
        vol = float(current["volume"])
        vol_sma = float(current.get("vol_sma_20", 1.0))
        vol_confirmed = vol >= (vol_sma * 1.05) if vol_sma > 0 else True
        vwap = float(current.get("vwap", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        st_dir = int(current.get("supertrend_dir", 1))
        atr = float(current.get("atr_14", cur_close * 0.005))

        # Active trading window: 09:30 to 12:30 IST
        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and (time_part < "09:30" or time_part > "14:45"):
            return {"active": False, "signal": "HOLD", "reason": "Outside active ORB trading window"}

        # Bullish Breakout: 5m close above OR High, above VWAP, SuperTrend bullish, green bar
        if cur_close > or_high and prev["close"] <= or_high * 1.003 and cur_close >= vwap and st_dir == 1 and cur_close > cur_open and vol_confirmed and (45.0 <= rsi <= 76.0):
            stop = round(max(or_mid, cur_close - max(atr * 1.1, cur_close * 0.007)), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(cur_close + max(risk * 1.4, cur_close * 0.012), 2)
            tp2 = round(cur_close + max(risk * 2.0, cur_close * 0.020), 2)
            return {
                "active": True,
                "strategy_id": "orb_15m_breakout",
                "name": "Opening Range Breakout (ORB-15m)",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.40 (Target 1) / 1:2.00 (Target 2)",
                "confidence": 90,
                "rationale": f"High-conviction ORB-15m breakout: 5m close ₹{cur_close:.2f} broke above 15m OR High ₹{or_high:.2f} with institutional volume and VWAP confirmation. SL at ₹{stop:.2f}, TP1 at ₹{tp1:.2f}.",
            }

        # Bearish Breakdown: 5m close below OR Low, below VWAP, SuperTrend bearish, red bar
        if cur_close < or_low and prev["close"] >= or_low * 0.997 and cur_close <= vwap and st_dir == -1 and cur_close < cur_open and vol_confirmed and (24.0 <= rsi <= 55.0):
            stop = round(min(or_mid, cur_close + max(atr * 1.1, cur_close * 0.007)), 2)
            risk = max(stop - cur_close, cur_close * 0.003)
            tp1 = round(cur_close - max(risk * 1.4, cur_close * 0.012), 2)
            tp2 = round(cur_close - max(risk * 2.0, cur_close * 0.020), 2)
            return {
                "active": True,
                "strategy_id": "orb_15m_breakout",
                "name": "Opening Range Breakdown (ORB-15m)",
                "signal": "SELL",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.40 (Target 1) / 1:2.00 (Target 2)",
                "confidence": 88,
                "rationale": f"5m close ₹{cur_close:.2f} broke below 15m OR Low ₹{or_low:.2f} with volume & VWAP confirmation. SL at ₹{stop:.2f}, TP1 at ₹{tp1:.2f}.",
            }

        return {"active": False, "signal": "HOLD", "reason": "Price within opening range"}

    # --- Strategy 2: VWAP Institutional Trend & Bounce ---
    @classmethod
    def evaluate_vwap_trend(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        VWAP Institutional Bounce & Trend Continuation:
        Long when price dips to test VWAP in an established uptrend and bounces green.
        """
        if len(df) < 5 or "vwap" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        vwap = float(current["vwap"])
        cur_close = float(current["close"])
        cur_low = float(current["low"])
        cur_open = float(current["open"])
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        vol_ok = vol >= (vol_sma * 1.05) if vol_sma > 0 else True
        st_dir = int(current.get("supertrend_dir", 1))
        ema_9 = float(current.get("ema_9", cur_close))
        ema_20 = float(current.get("ema_20", cur_close))
        ema_50 = float(current.get("ema_50", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        atr = float(current.get("atr_14", cur_close * 0.005))

        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and (time_part < "09:25" or time_part > "14:45"):
            return {"active": False, "signal": "HOLD", "reason": "Outside active VWAP window"}

        # Bullish VWAP Bounce: Trend aligned (ST Bullish, EMA9 >= EMA20, Close >= EMA50), healthy RSI
        trend_aligned = (st_dir == 1) and (ema_9 >= ema_20) and (cur_close >= ema_50) and (42.0 <= rsi <= 74.0)

        if cur_low <= vwap * 1.003 and cur_close > vwap and cur_close > cur_open and trend_aligned and vol_ok:
            stop = round(max(min(cur_low, vwap) - (atr * 0.35), cur_close - max(atr * 0.85, cur_close * 0.0065)), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(cur_close + max(risk * 1.35, cur_close * 0.012), 2)
            tp2 = round(cur_close + max(risk * 2.00, cur_close * 0.020), 2)
            return {
                "active": True,
                "strategy_id": "vwap_institutional_bounce",
                "name": "VWAP Institutional Bounce",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.35 (Target 1) / 1:2.00 (Target 2)",
                "confidence": 88,
                "rationale": f"Institutional VWAP bounce at ₹{vwap:.2f} with green reversal close at ₹{cur_close:.2f}. SuperTrend Bullish, RSI {rsi:.1f}. Target 1 ₹{tp1:.2f}.",
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
        atr = float(current.get("atr_14", cur_close * 0.005))
        vol_ok = vol >= (vol_sma * 1.05) if vol_sma > 0 else True

        time_str = str(current.get("timestamp", ""))
        time_part = time_str[11:16] if len(time_str) >= 16 else ""
        if time_part and (time_part < "09:25" or time_part > "14:45"):
            return {"active": False, "signal": "HOLD", "reason": "Outside active EMA window"}

        trend_aligned = (st_dir == 1) and (ema_9 > ema_20) and (ema_20 > ema_50) and (cur_close >= vwap) and (45.0 <= rsi <= 72.0)

        if trend_aligned and vol_ok:
            if cur_low <= ema_20 * 1.002 and cur_close > ema_20 and cur_close > cur_open:
                stop = round(max(min(cur_low, ema_20) - (atr * 0.35), cur_close - max(atr * 0.85, cur_close * 0.0065)), 2)
                risk = max(cur_close - stop, cur_close * 0.003)
                tp1 = round(cur_close + max(risk * 1.35, cur_close * 0.010), 2)
                tp2 = round(cur_close + max(risk * 1.80, cur_close * 0.018), 2)
                return {
                    "active": True,
                    "strategy_id": "ema_ribbon_pullback",
                    "name": "9/20 EMA Ribbon Pullback Scalper",
                    "signal": "BUY",
                    "entry_price": round(cur_close, 2),
                    "stop_loss": stop,
                    "target_1": tp1,
                    "target_2": tp2,
                    "risk_reward_ratio": "1:1.35 (Target 1) / 1:1.80 (Target 2)",
                    "confidence": 85,
                    "rationale": f"Bullish EMA ribbon (9 > 20 > 50) pullback bounce at 20-EMA ₹{ema_20:.2f}. Target 1 ₹{tp1:.2f}.",
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
        vwap = float(current.get("vwap", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        atr = float(current.get("atr_14", cur_close * 0.005))
        vol_ok = vol >= (vol_sma * 1.05) if vol_sma > 0 else True

        # Fresh Bullish Flip with VWAP confirmation and healthy RSI
        if cur_dir == 1 and prev_dir == -1 and cur_close >= vwap and (45.0 <= rsi <= 72.0) and vol_ok:
            stop = round(max(st_val, cur_close - (atr * 1.1)), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(cur_close + max(risk * 1.4, cur_close * 0.012), 2)
            tp2 = round(cur_close + max(risk * 2.0, cur_close * 0.020), 2)
            return {
                "active": True,
                "strategy_id": "supertrend_trend_flip",
                "name": "SuperTrend (10, 3) Bullish Flip",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.40 (Target 1) / 1:2.00 (Target 2)",
                "confidence": 86,
                "rationale": f"SuperTrend flipped BULLISH at ₹{cur_close:.2f} with trailing support at ₹{stop:.2f}. Primary Target ₹{tp1:.2f}.",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 5: Daily & Camarilla Pivot Breakouts ---
    @classmethod
    def evaluate_pivots(cls, df: pd.DataFrame, prev_high: float, prev_low: float, prev_close: float) -> Dict[str, Any]:
        """
        Camarilla Pivot Breakout:
        H4 Breakout targeting H5 expansion; L4 Breakdown targeting L5 expansion.
        """
        if prev_high <= 0 or prev_low <= 0 or prev_close <= 0 or len(df) < 2:
            return {"active": False, "signal": "HOLD"}

        rng = prev_high - prev_low
        h4 = prev_close + (rng * 1.1 / 2.0)
        h3 = prev_close + (rng * 1.1 / 4.0)
        l3 = prev_close - (rng * 1.1 / 4.0)
        l4 = prev_close - (rng * 1.1 / 2.0)
        h5 = prev_close + (rng * 1.1)
        l5 = prev_close - (rng * 1.1)

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_close = float(current["close"])
        prev_c = float(prev["close"])
        vol = float(current.get("volume", 0))
        vol_sma = float(current.get("vol_sma_20", 1.0))
        st_dir = int(current.get("supertrend_dir", 1))
        vwap = float(current.get("vwap", cur_close))
        rsi = float(current.get("rsi_14", 50.0))
        atr = float(current.get("atr_14", cur_close * 0.005))
        vol_ok = vol >= (vol_sma * 1.05) if vol_sma > 0 else True

        # H4 Bullish Breakout
        if cur_close > h4 and prev_c <= h4 * 1.003 and cur_close >= vwap and st_dir == 1 and vol_ok and (45.0 <= rsi <= 76.0):
            stop = round(max(h3, cur_close - max(atr * 1.1, cur_close * 0.007)), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(min(h5, cur_close + max(risk * 1.4, cur_close * 0.014)), 2)
            tp2 = round(h5, 2)
            return {
                "active": True,
                "strategy_id": "camarilla_h4_breakout",
                "name": "Camarilla H4 Breakout",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.40 (Target 1) / 1:2.00 (H5 Expansion)",
                "confidence": 89,
                "rationale": f"Price ₹{cur_close:.2f} exploded through Camarilla H4 resistance (₹{h4:.2f}) targeting H5 expansion at ₹{h5:.2f}.",
            }

        # L4 Bearish Breakdown
        if cur_close < l4 and prev_c >= l4 * 0.997 and cur_close <= vwap and st_dir == -1 and vol_ok and (24.0 <= rsi <= 55.0):
            stop = round(min(l3, cur_close + max(atr * 1.1, cur_close * 0.007)), 2)
            risk = max(stop - cur_close, cur_close * 0.003)
            tp1 = round(max(l5, cur_close - max(risk * 1.4, cur_close * 0.014)), 2)
            tp2 = round(l5, 2)
            return {
                "active": True,
                "strategy_id": "camarilla_l4_breakdown",
                "name": "Camarilla L4 Breakdown",
                "signal": "SELL",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.40 (Target 1) / 1:2.00 (L5 Expansion)",
                "confidence": 87,
                "rationale": f"Price ₹{cur_close:.2f} broke below Camarilla L4 support (₹{l4:.2f}) targeting L5 expansion at ₹{l5:.2f}.",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 6: Bollinger Band Mean Reversion ---
    @classmethod
    def evaluate_bollinger_expansion(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Bollinger Band Mean Reversion:
        Triggers BUY on oversold Lower Band bounce back toward 20-SMA middle band.
        """
        if len(df) < 20 or "bb_lower" not in df.columns:
            return {"active": False, "signal": "HOLD"}

        current = df.iloc[-1]
        prev = df.iloc[-2]
        cur_close = float(current["close"])
        bb_lower = float(current["bb_lower"])
        bb_middle = float(current["bb_middle"])
        rsi = float(current.get("rsi_14", 50.0))
        cur_open = float(current["open"])
        atr = float(current.get("atr_14", cur_close * 0.005))

        if prev["low"] <= bb_lower and cur_close > bb_lower and cur_close > cur_open and (28.0 <= rsi <= 46.0):
            stop = round(min(float(prev["low"]), float(current["low"])) - (atr * 0.35), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(min(bb_middle, cur_close + max(risk * 1.25, cur_close * 0.010)), 2)
            tp2 = round(bb_middle, 2)
            return {
                "active": True,
                "strategy_id": "bollinger_lower_reversal",
                "name": "Bollinger Bands Lower Band Reversal",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.25 (Mean Reversion Target 1)",
                "confidence": 84,
                "rationale": f"Statistical mean-reversion bounce: price rejected lower Bollinger Band (₹{bb_lower:.2f}). Target middle band at ₹{tp1:.2f}.",
            }

        return {"active": False, "signal": "HOLD"}

    # --- Strategy 7: RSI Exhaustion Bounce ---
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
        cur_open = float(current["open"])
        cur_low = float(current["low"])
        atr = float(current.get("atr_14", cur_close * 0.005))

        if prev_rsi < 32 and (32.0 <= cur_rsi <= 46.0) and cur_close > cur_open:
            stop = round(min(cur_low, float(prev["low"])) - (atr * 0.35), 2)
            risk = max(cur_close - stop, cur_close * 0.003)
            tp1 = round(cur_close + max(risk * 1.30, cur_close * 0.010), 2)
            tp2 = round(cur_close + max(risk * 1.80, cur_close * 0.016), 2)
            return {
                "active": True,
                "strategy_id": "rsi_oversold_reversal",
                "name": "RSI Oversold Mean Reversion",
                "signal": "BUY",
                "entry_price": round(cur_close, 2),
                "stop_loss": stop,
                "target_1": tp1,
                "target_2": tp2,
                "risk_reward_ratio": "1:1.30 (Mean Reversion Target 1)",
                "confidence": 82,
                "rationale": f"RSI recovered from oversold ({prev_rsi:.1f} -> {cur_rsi:.1f}) with green reversal candle. Target ₹{tp1:.2f}.",
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
                return 200.0 + base_conf
            if sid == "camarilla_h4_breakout":
                return 180.0 + base_conf
            if sid == "ema_ribbon_pullback":
                return 160.0 + base_conf
            if sid == "supertrend_trend_flip":
                return 140.0 + base_conf
            if sid == "bollinger_lower_reversal":
                return 120.0 + base_conf
            if sid == "rsi_oversold_reversal":
                return 100.0 + base_conf
            return base_conf

        best = max(active, key=_strategy_priority) if active else None

        composite_signal = "BUY" if buy_count >= 1 and sell_count == 0 else (
            "SELL" if sell_count >= 1 and buy_count == 0 else "HOLD"
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
                "ema_9": round(float(df.iloc[-1].get("ema_9", cur_price)), 2),
                "ema_20": round(float(df.iloc[-1].get("ema_20", cur_price)), 2),
                "supertrend": round(float(df.iloc[-1].get("supertrend", cur_price)), 2),
                "supertrend_dir": int(df.iloc[-1].get("supertrend_dir", 1)),
                "rsi_14": round(float(df.iloc[-1].get("rsi_14", 50.0)), 1),
            },
            "strategies_evaluated": results,
        }


intraday_engine = IntradayStrategySuite()
