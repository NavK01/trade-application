"""
Historical Intraday Backtesting Engine for Indian Equities (NSE/BSE).
Simulates real bar-by-bar market replay over past 5-minute candles,
evaluates all 7 intraday strategies, and uses DeepSeek Flash MaaS
to filter high-probability trade setups and measure profitability.
"""

import asyncio
import datetime
import json
import logging
import math
import re
from typing import Dict, Any, List, Optional
import yfinance as yf
import pandas as pd

from app.config import settings, create_genai_client
from app.trading.intraday_strategies import intraday_engine
from app.trading.skills.day_trader_adapter import day_trader
from app.storage.db import db
from app.system.tokens import token_tracker

logger = logging.getLogger("app.trading.backtester")


class HistoricalBacktestEngine:
    """
    Simulates high-fidelity intraday trading over past 5-minute bar sessions.
    Executes trades with bracket orders (Entry, Stop Loss, Target 1, Target 2),
    enforces 15:25 IST end-of-day square-off, and computes full quant metrics.
    """

    def __init__(self, initial_capital: float = 10000.0):
        self.initial_capital = initial_capital
        self.max_loss_pct = 0.02  # 2% max risk per trade (₹200 on ₹10k)
        self.max_position_pct = 0.70  # Max ₹7,000 position cap for intraday alpha

    def fetch_historical_bars(self, symbols: List[str], period: str = "5d") -> Dict[str, pd.DataFrame]:
        """Fetches verified 5-minute historical candle data for the requested Indian tickers."""
        data = {}
        for sym in symbols:
            clean = sym.strip().upper()
            yf_sym = clean if ("." in clean or "^" in clean) else f"{clean}.NS"
            try:
                t = yf.Ticker(yf_sym)
                df = t.history(period=period, interval="5m")
                if df.empty and yf_sym.endswith(".NS"):
                    df = yf.Ticker(yf_sym.replace(".NS", ".BO")).history(period=period, interval="5m")
                if not df.empty:
                    df = df.reset_index()
                    # Standardize column names
                    col_map = {
                        "Datetime": "timestamp", "Date": "timestamp",
                        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
                    }
                    df = df.rename(columns=col_map)
                    df["symbol"] = clean
                    data[clean] = df
                    logger.info(f"Loaded {len(df)} 5m bars for {clean}")
            except Exception as e:
                logger.error(f"Error fetching data for {sym}: {e}")
        return data

    async def _query_deepseek_decision(
        self,
        symbol: str,
        setup: Dict[str, Any],
        indicators: Dict[str, Any],
        recent_bars: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Queries DeepSeek Flash MaaS to evaluate the technical intraday setup
        and determine whether to EXECUTE or PASS.
        """
        api_key = settings.GEMINI_API_KEY
        client = create_genai_client(api_key, location="global")

        prompt = f"""You are an elite quantitative day trading analyst evaluating an active intraday setup on the National Stock Exchange of India (NSE).
Symbol: {symbol}
Current Price: ₹{setup.get('entry_price', indicators.get('price'))}
Strategy: {setup.get('name', 'Confluence Setup')}
Signal: {setup.get('signal')}
Stop-Loss: ₹{setup.get('stop_loss')}
Target 1: ₹{setup.get('target_1')}
Target 2: ₹{setup.get('target_2')}
Risk:Reward: {setup.get('risk_reward_ratio')}
Technical Indicators: {json.dumps(indicators)}
Strategy Rationale: {setup.get('rationale', 'Technical trigger')}

Evaluate if this trade has genuine institutional momentum or is a choppy false breakout.
Respond strictly in JSON:
{{
  "decision": "EXECUTE" | "PASS",
  "confidence_pct": <integer between 50 and 95>,
  "adjusted_stop_loss": <float>,
  "adjusted_target": <float>,
  "reasoning": "<concise 1-2 sentence explanation>"
}}
"""
        try:
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model="deepseek-ai/deepseek-v4.1-flash-maas",
                    contents=prompt,
                ),
                timeout=30.0
            )
            text = resp.text.strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            clean_json = m.group(0) if m else text
            decision_data = json.loads(clean_json)
            token_tracker.log_usage("backtester_deepseek", "deepseek-ai/deepseek-v4.1-flash-maas", 180, 80)
            return decision_data
        except Exception as e:
            logger.warning(f"DeepSeek query failed/timeout: {e}. Defaulting to algorithmic execution.")
            return {
                "decision": "EXECUTE" if setup.get("confidence", 75) >= 78 else "PASS",
                "confidence_pct": setup.get("confidence", 75),
                "adjusted_stop_loss": setup.get("stop_loss"),
                "adjusted_target": setup.get("target_1"),
                "reasoning": "Algorithmic rule execution (DeepSeek fallback)",
            }

    async def run_backtest(
        self,
        symbols: Optional[List[str]] = None,
        period: str = "5d",
        use_deepseek: bool = True,
        max_deepseek_calls: int = 15,
        time_filter: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes a historical simulation over multi-day 5-minute bars.
        Optionally uses DeepSeek Flash MaaS for trade validation and AI filtering.
        """
        test_symbols = symbols or ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
        logger.info(f"Starting intraday backtest across {len(test_symbols)} symbols over {period}...")

        stock_data = self.fetch_historical_bars(test_symbols, period=period)
        if not stock_data:
            return {"error": "Failed to fetch historical market data."}

        capital = self.initial_capital
        cash = capital
        trades: List[Dict[str, Any]] = []
        deepseek_call_count = 0

        for sym, df in stock_data.items():
            if len(df) < 20:
                continue

            # Group candles by trading day
            df["date_str"] = df["timestamp"].astype(str).str[:10]
            grouped_days = df.groupby("date_str")
            prev_day_summary: Optional[Dict[str, float]] = None

            for date_str, day_df in grouped_days:
                day_df = day_df.reset_index(drop=True)
                if len(day_df) < 15:
                    continue

                open_trade: Optional[Dict[str, Any]] = None

                # Bar-by-bar progression through the session (09:15 to 15:30 IST)
                for i in range(10, len(day_df)):
                    current_bar = day_df.iloc[i]
                    cur_time = str(current_bar["timestamp"])
                    time_part = cur_time[11:16] if len(cur_time) >= 16 else ""

                    # 1. Manage existing open position
                    if open_trade:
                        high_p = float(current_bar["high"])
                        low_p = float(current_bar["low"])
                        close_p = float(current_bar["close"])

                        # Check Stop Loss first (conservative check)
                        if low_p <= open_trade["stop_loss"]:
                            exit_price = open_trade["stop_loss"]
                            pnl = (exit_price - open_trade["entry_price"]) * open_trade["quantity"]
                            fees = (open_trade["entry_price"] + exit_price) * open_trade["quantity"] * 0.0003
                            net_pnl = round(pnl - fees, 2)
                            cash += (open_trade["quantity"] * exit_price) - fees
                            trades.append({
                                "symbol": sym,
                                "date": date_str,
                                "strategy": open_trade["strategy"],
                                "action": "BUY",
                                "entry_time": open_trade["entry_time"],
                                "entry_price": open_trade["entry_price"],
                                "exit_time": time_part,
                                "exit_price": exit_price,
                                "quantity": open_trade["quantity"],
                                "gross_pnl": round(pnl, 2),
                                "net_pnl": net_pnl,
                                "return_pct": round(((exit_price - open_trade["entry_price"]) / open_trade["entry_price"]) * 100, 2),
                                "exit_reason": "STOP_LOSS",
                                "won": net_pnl > 0,
                                "validated_by_deepseek": open_trade.get("validated_by_deepseek", False),
                            })
                            open_trade = None
                            continue

                        # Check Target 1 Hit
                        elif high_p >= open_trade["target_1"]:
                            exit_price = open_trade["target_1"]
                            pnl = (exit_price - open_trade["entry_price"]) * open_trade["quantity"]
                            fees = (open_trade["entry_price"] + exit_price) * open_trade["quantity"] * 0.0003
                            net_pnl = round(pnl - fees, 2)
                            cash += (open_trade["quantity"] * exit_price) - fees
                            trades.append({
                                "symbol": sym,
                                "date": date_str,
                                "strategy": open_trade["strategy"],
                                "action": "BUY",
                                "entry_time": open_trade["entry_time"],
                                "entry_price": open_trade["entry_price"],
                                "exit_time": time_part,
                                "exit_price": exit_price,
                                "quantity": open_trade["quantity"],
                                "gross_pnl": round(pnl, 2),
                                "net_pnl": net_pnl,
                                "return_pct": round(((exit_price - open_trade["entry_price"]) / open_trade["entry_price"]) * 100, 2),
                                "exit_reason": "TARGET_HIT",
                                "won": True,
                                "validated_by_deepseek": open_trade.get("validated_by_deepseek", False),
                            })
                            open_trade = None
                            continue

                        # Check End of Day Auto Square-off (at or after 15:20 IST)
                        elif time_part >= "15:20":
                            exit_price = close_p
                            pnl = (exit_price - open_trade["entry_price"]) * open_trade["quantity"]
                            fees = (open_trade["entry_price"] + exit_price) * open_trade["quantity"] * 0.0003
                            net_pnl = round(pnl - fees, 2)
                            cash += (open_trade["quantity"] * exit_price) - fees
                            trades.append({
                                "symbol": sym,
                                "date": date_str,
                                "strategy": open_trade["strategy"],
                                "action": "BUY",
                                "entry_time": open_trade["entry_time"],
                                "entry_price": open_trade["entry_price"],
                                "exit_time": time_part,
                                "exit_price": exit_price,
                                "quantity": open_trade["quantity"],
                                "gross_pnl": round(pnl, 2),
                                "net_pnl": net_pnl,
                                "return_pct": round(((exit_price - open_trade["entry_price"]) / open_trade["entry_price"]) * 100, 2),
                                "exit_reason": "EOD_SQUARE_OFF",
                                "won": net_pnl > 0,
                                "validated_by_deepseek": open_trade.get("validated_by_deepseek", False),
                            })
                            open_trade = None
                            continue

                    # 2. Look for new trade entry if no position is open
                    trades_in_day = sum(1 for t in trades if t["symbol"] == sym and t["date"] == date_str)
                    if not open_trade and time_part < "14:45" and trades_in_day < 1:
                        # Optional time filter: avoid choppy lunch session (11:30 to 13:15 IST)
                        if time_filter and ("11:30" <= time_part <= "13:15"):
                            continue

                        history_slice = day_df.iloc[:i + 1].to_dict(orient="records")
                        eval_res = intraday_engine.evaluate_all_strategies(history_slice, prev_day_ohlc=prev_day_summary)

                        comp_signal = eval_res.get("composite_signal")
                        best_setup = eval_res.get("best_strategy")

                        # Confluence Requirement: Only execute if multi-strategy composite signal is BUY
                        if comp_signal == "BUY" and best_setup:
                            entry_p = best_setup["entry_price"]
                            sl_p = best_setup["stop_loss"]
                            tp_p = best_setup["target_1"]

                            # Sizing: 2% risk rule
                            risk_per_share = max(entry_p - sl_p, entry_p * 0.003)
                            max_risk_inr = self.initial_capital * self.max_loss_pct  # 2% of ₹10,000 (₹200)
                            max_alloc_inr = self.initial_capital * self.max_position_pct  # 70% position cap (₹7,000)

                            shares = int(min(max_risk_inr // risk_per_share, max_alloc_inr // entry_p, cash // entry_p))
                            if shares <= 0 and cash >= entry_p and entry_p <= max_alloc_inr:
                                shares = 1

                            if shares > 0 and cash >= (shares * entry_p):
                                validated_by_ds = False

                                # Query DeepSeek Flash MaaS if enabled and quota permits
                                if use_deepseek and deepseek_call_count < max_deepseek_calls:
                                    deepseek_call_count += 1
                                    ds_dec = await self._query_deepseek_decision(
                                        symbol=sym,
                                        setup=best_setup,
                                        indicators=eval_res.get("indicators", {}),
                                        recent_bars=history_slice[-3:],
                                    )
                                    if ds_dec.get("decision") == "PASS":
                                        continue
                                    if ds_dec.get("adjusted_stop_loss"):
                                        sl_p = float(ds_dec["adjusted_stop_loss"])
                                    if ds_dec.get("adjusted_target"):
                                        tp_p = float(ds_dec["adjusted_target"])
                                    validated_by_ds = True

                                # Execute simulated position
                                cost = shares * entry_p
                                fee = cost * 0.0003
                                cash -= (cost + fee)

                                open_trade = {
                                    "symbol": sym,
                                    "strategy": best_setup["name"],
                                    "strategy_id": best_setup.get("strategy_id", "confluence"),
                                    "entry_time": time_part,
                                    "entry_price": entry_p,
                                    "stop_loss": sl_p,
                                    "initial_stop_loss": sl_p,
                                    "target_1": tp_p,
                                    "quantity": shares,
                                    "validated_by_deepseek": validated_by_ds,
                                }

                # Update previous day summary for next session's Camarilla & Pivot calculations
                prev_day_summary = {
                    "high": float(day_df["high"].max()),
                    "low": float(day_df["low"].min()),
                    "close": float(day_df["close"].iloc[-1]),
                }

        # Aggregate backtest performance metrics
        total_trades = len(trades)
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]

        win_rate = round((len(wins) / total_trades * 100), 1) if total_trades > 0 else 0.0
        total_net_pnl = round(sum(t["net_pnl"] for t in trades), 2)
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)

        # Drawdown calculation
        equity_curve = [self.initial_capital]
        running = self.initial_capital
        peak = running
        max_dd = 0.0
        for t in trades:
            running += t["net_pnl"]
            equity_curve.append(running)
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

        # Strategy breakdown
        strategy_stats = {}
        for t in trades:
            s_name = t["strategy"]
            if s_name not in strategy_stats:
                strategy_stats[s_name] = {"trades": 0, "wins": 0, "pnl": 0.0}
            strategy_stats[s_name]["trades"] += 1
            if t["won"]:
                strategy_stats[s_name]["wins"] += 1
            strategy_stats[s_name]["pnl"] = round(strategy_stats[s_name]["pnl"] + t["net_pnl"], 2)

        for s_name, data in strategy_stats.items():
            data["win_rate_pct"] = round((data["wins"] / data["trades"]) * 100, 1) if data["trades"] > 0 else 0.0

        ending_capital = round(self.initial_capital + total_net_pnl, 2)
        net_return_pct = round((total_net_pnl / self.initial_capital) * 100, 2)

        summary = {
            "initial_capital_inr": self.initial_capital,
            "ending_capital_inr": ending_capital,
            "net_pnl_inr": total_net_pnl,
            "net_return_pct": net_return_pct,
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 2),
            "deepseek_queries_count": deepseek_call_count,
            "time_filter_applied": time_filter,
            "strategy_breakdown": strategy_stats,
            "trades": trades,
        }

        logger.info(f"Backtest completed: {total_trades} trades, {win_rate}% win rate, net P&L ₹{total_net_pnl:+} ({net_return_pct}%)")
        return summary


backtest_engine = HistoricalBacktestEngine()

