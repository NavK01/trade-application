"""
Candlestick Chart Data Service for Professional Trading Panel.
Provides historical OHLCV candle feeds (1m, 5m, 15m, 1h, 1D),
calculates 20-EMA, 25-EMA, 200-EMA and VWAP overlays, automated 4-line mapping,
Volume Profile (POC, VAH, VAL), Fibonacci zones, and formats live execution
and liquidity sweep markers for TradingView Lightweight Charts.
"""

import datetime
import logging
from typing import Dict, Any, List, Optional
import yfinance as yf
import pandas as pd

from app.config import ACCOUNT_NAMES
from app.financial_advisor.india_market import normalize_indian_symbol
from app.storage.db import db
from app.trading.institutional_engine import (
    compute_premarket_4lines,
    compute_volume_profile,
    compute_fibonacci_zones,
    detect_liquidity_sweeps_and_absorption,
)

logger = logging.getLogger("app.trading.candles")


def get_symbol_candles(
    symbol: str = "NIFTY",
    interval: str = "5m",
    period: str = "5d"
) -> Dict[str, Any]:
    """
    Fetches OHLCV candles, computes 20-EMA, 25-EMA, 200-EMA, and VWAP overlays,
    computes 4-line pre-market levels, Volume Profile, Fibonacci zones,
    and attaches order execution + liquidity sweep markers.
    """
    clean_sym = symbol.strip().upper()
    yf_sym = normalize_indian_symbol(clean_sym)

    # Sanitize interval & period combination for Yahoo Finance constraints
    intraday_sub_5m = ["1m", "2m"]
    intraday = ["5m", "15m", "30m", "60m", "90m", "1h"]
    safe_period = period
    if interval in intraday_sub_5m and period not in ["1d", "5d", "7d"]:
        safe_period = "5d"
    elif interval in intraday and period in ["3mo", "6mo", "1y", "2y", "5y", "max", "ytd"]:
        safe_period = "1mo"

    df = pd.DataFrame()
    try:
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period=safe_period, interval=interval)
        if df.empty and yf_sym.endswith(".NS"):
            # Try BSE fallback
            bse_sym = yf_sym.replace(".NS", ".BO")
            df = yf.Ticker(bse_sym).history(period=safe_period, interval=interval)
    except Exception as e:
        logger.error(f"Error fetching candles for {yf_sym}: {e}")
        df = pd.DataFrame()

    candles = []
    ema20_line = []
    ema25_line = []
    ema200_line = []
    vwap_line = []

    four_lines = {}
    volume_profile = {}
    fibonacci_zones = {}
    sweep_markers = []

    if not df.empty:
        # Calculate moving averages
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
        df["EMA25"] = df["Close"].ewm(span=25, adjust=False).mean()
        df["EMA200"] = df["Close"].ewm(span=min(200, max(10, len(df))), adjust=False).mean()

        # Calculate VWAP by day
        df["Date_Day"] = df.index.date
        df["Typical_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
        df["PV"] = df["Typical_Price"] * df["Volume"]

        vwap_values = []
        for _, day_df in df.groupby("Date_Day"):
            cum_pv = day_df["PV"].cumsum()
            cum_vol = day_df["Volume"].cumsum()
            day_vwap = cum_pv / cum_vol.replace(0, 1)
            vwap_values.extend(day_vwap.tolist())

        df["VWAP"] = vwap_values

        for idx, row in df.iterrows():
            ts = int(idx.timestamp())
            o = round(float(row["Open"]), 2)
            h = round(float(row["High"]), 2)
            l = round(float(row["Low"]), 2)
            c = round(float(row["Close"]), 2)
            v = int(row.get("Volume", 0))

            candles.append({
                "time": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            })

            if not pd.isna(row["EMA20"]):
                ema20_line.append({"time": ts, "value": round(float(row["EMA20"]), 2)})
            if not pd.isna(row["EMA25"]):
                ema25_line.append({"time": ts, "value": round(float(row["EMA25"]), 2)})
            if not pd.isna(row["EMA200"]):
                ema200_line.append({"time": ts, "value": round(float(row["EMA200"]), 2)})
            if not pd.isna(row["VWAP"]):
                vwap_line.append({"time": ts, "value": round(float(row["VWAP"]), 2)})

        # Compute Institutional Overlays
        four_lines = compute_premarket_4lines(df)
        volume_profile = compute_volume_profile(df)
        fibonacci_zones = compute_fibonacci_zones(
            df,
            vah=volume_profile.get("VAH", 0.0),
            val=volume_profile.get("VAL", 0.0)
        )

        # Detect Liquidity Sweeps
        sweeps = detect_liquidity_sweeps_and_absorption(
            df,
            pdh=four_lines.get("PDH", 0.0),
            pdl=four_lines.get("PDL", 0.0),
            pdc=four_lines.get("PDC", 0.0),
            swing_high=four_lines.get("SWING_1H_HIGH", 0.0),
            swing_low=four_lines.get("SWING_1H_LOW", 0.0)
        )

        for sw in sweeps:
            try:
                sw_ts = int(pd.to_datetime(sw["timestamp"]).timestamp())
            except Exception:
                continue

            if sw["type"] == "BEARISH_LIQUIDITY_SWEEP":
                sweep_markers.append({
                    "time": sw_ts,
                    "position": "aboveBar",
                    "color": "#f59e0b",
                    "shape": "circle",
                    "text": f"SWEEP: {sw['swept_level']} (Trap)",
                })
            else:
                sweep_markers.append({
                    "time": sw_ts,
                    "position": "belowBar",
                    "color": "#38bdf8",
                    "shape": "circle",
                    "text": f"SWEEP: {sw['swept_level']} (Absorb)",
                })

    # Fetch executed trades for this symbol to overlay as markers
    trades = db.get_trades(limit=100)
    symbol_trades = [t for t in trades if t.get("symbol", "").upper() == clean_sym]
    trade_markers = []

    for t in symbol_trades:
        action = t.get("action", "BUY").upper()
        acc = ACCOUNT_NAMES.get(t.get("account_id", ""), t.get("account_id", "Account"))
        price = t.get("fill_price", 0.0)
        qty = t.get("quantity", 1)
        ts_str = t.get("timestamp")

        try:
            trade_dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            trade_ts = int(trade_dt.timestamp())
        except Exception:
            continue

        if action == "BUY":
            trade_markers.append({
                "time": trade_ts,
                "position": "belowBar",
                "color": "#22c55e",
                "shape": "arrowUp",
                "text": f"BUY {qty} @ ₹{price} ({acc})",
            })
        else:
            trade_markers.append({
                "time": trade_ts,
                "position": "aboveBar",
                "color": "#ef4444",
                "shape": "arrowDown",
                "text": f"SELL {qty} @ ₹{price} ({acc})",
            })

    all_markers = sweep_markers + trade_markers
    all_markers.sort(key=lambda m: m["time"])

    # Latest price summary
    latest_close = candles[-1]["close"] if candles else 0.0
    first_open = candles[0]["open"] if candles else latest_close
    chg_pct = round(((latest_close - first_open) / first_open) * 100, 2) if first_open > 0 else 0.0

    return {
        "symbol": clean_sym,
        "yf_symbol": yf_sym,
        "interval": interval,
        "period": period,
        "count": len(candles),
        "latest_price": latest_close,
        "change_percent": chg_pct,
        "candles": candles,
        "ema20": ema20_line,
        "ema25": ema25_line,
        "ema200": ema200_line,
        "vwap": vwap_line,
        "four_lines": four_lines,
        "volume_profile": volume_profile,
        "fibonacci_zones": fibonacci_zones,
        "markers": all_markers,
    }

