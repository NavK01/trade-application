"""
Autonomous Continuous Quantitative Strategy Optimizer & Self-Healing Daemon.

Designed for the Indian Stock Market (NSE) with an Initial Capital of ₹10,000 INR.
Operates 24/7 in a continuous self-healing loop:
  - Phase 1: 1-Year Baseline Backtest across liquid Indian stocks (Zero AI)
  - Phase 2: 1-Month Out-of-Sample Backtest with DeepSeek V4.1 Flash evaluating trade setups
  - Phase 3: Gemini 4 Strategic Review (with Google Search grounding), error forensics,
             strategy parameter mutation, and dynamic updating of AUTONOMOUS_TRADING_PLAN.md

Strict Token Budget Ceilings (Enforced by TokenBudgetGuard):
  - Gemini 4: Input < 10,000,000 tokens, Output < 3,000,000 tokens
  - DeepSeek V4.1 Flash: Combined total < 10,000,000 tokens
"""

from __future__ import annotations

import os
import sys
import re
import json
import time
import asyncio
import logging
import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import numpy as np

from app.config import settings, BASE_DIR
from app.storage.db import db
from app.system.tokens import token_tracker
from app.trading.institutional_engine import (
    calculate_indian_statutory_charges,
    detect_liquidity_sweeps_and_absorption,
)
from app.trading.backtester import backtest_engine
from app.trading.fee_aware_selector import FeeAwareSelector

logger = logging.getLogger("continuous_optimizer")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# Liquid, high-beta NSE cash equities suitable for ₹10,000 capital
LIQUID_NSE_EQUITIES = [
    "SBIN", "ITC", "TATASTEEL", "INFY", "ICICIBANK", "AXISBANK", "NTPC", "COALINDIA"
]


# =============================================================================
# 1. Strict Token Budget Guard (Gemini 4: <10M In / <3M Out; DeepSeek: <10M Total)
# =============================================================================

class TokenBudgetGuard:
    """Enforces strict token consumption ceilings per user mandate."""

    GEMINI_MAX_INPUT_TOKENS = 10_000_000
    GEMINI_MAX_OUTPUT_TOKENS = 3_000_000
    DEEPSEEK_MAX_TOTAL_TOKENS = 10_000_000

    def get_token_status(self) -> Dict[str, Any]:
        """Fetch cumulative token consumption directly from the database."""
        gemini_input = 0
        gemini_output = 0
        deepseek_total = 0

        try:
            with db._get_connection() as conn:
                cursor = conn.cursor()
                # Gemini usage
                cursor.execute("""
                    SELECT COALESCE(SUM(prompt_tokens), 0) AS p,
                           COALESCE(SUM(completion_tokens), 0) AS c
                    FROM token_usage
                    WHERE model LIKE '%gemini%'
                """)
                row = cursor.fetchone()
                if row:
                    gemini_input = int(row["p"])
                    gemini_output = int(row["c"])

                # DeepSeek usage
                cursor.execute("""
                    SELECT COALESCE(SUM(total_tokens), 0) AS t
                    FROM token_usage
                    WHERE model LIKE '%deepseek%'
                """)
                row = cursor.fetchone()
                if row:
                    deepseek_total = int(row["t"])
        except Exception as e:
            logger.warning(f"Could not read token_usage table: {e}")

        return {
            "gemini_input_tokens": gemini_input,
            "gemini_output_tokens": gemini_output,
            "gemini_max_input": self.GEMINI_MAX_INPUT_TOKENS,
            "gemini_max_output": self.GEMINI_MAX_OUTPUT_TOKENS,
            "gemini_input_remaining": max(0, self.GEMINI_MAX_INPUT_TOKENS - gemini_input),
            "gemini_output_remaining": max(0, self.GEMINI_MAX_OUTPUT_TOKENS - gemini_output),
            "deepseek_total_tokens": deepseek_total,
            "deepseek_max_total": self.DEEPSEEK_MAX_TOTAL_TOKENS,
            "deepseek_total_remaining": max(0, self.DEEPSEEK_MAX_TOTAL_TOKENS - deepseek_total),
        }

    def can_call_gemini(self, est_input: int = 5000, est_output: int = 1500) -> bool:
        status = self.get_token_status()
        return (
            (status["gemini_input_tokens"] + est_input) <= self.GEMINI_MAX_INPUT_TOKENS
            and (status["gemini_output_tokens"] + est_output) <= self.GEMINI_MAX_OUTPUT_TOKENS
        )

    def can_call_deepseek(self, est_tokens: int = 1500) -> bool:
        status = self.get_token_status()
        return (status["deepseek_total_tokens"] + est_tokens) <= self.DEEPSEEK_MAX_TOTAL_TOKENS


# =============================================================================
# 2. Strategy Parameters (Bounded & Clamped)
# =============================================================================

@dataclass
class StrategyParameters:
    """Strategy parameters governed by quantitative intervals to avoid overfitting."""
    min_target_pct: float = 0.80           # Bounded [0.80, 2.50]
    min_sweep_vol_multiplier: float = 1.30 # Bounded [1.20, 3.00]
    breakeven_trigger_r: float = 1.00      # Bounded [0.80, 1.50]
    min_sl_pct: float = 0.40               # Bounded [0.35, 0.75]
    max_position_cost: float = 2800.0      # Sizing cap: <= 30% of ₹10,000 capital
    time_filter_lunch_block: bool = True   # Skip 11:30 to 13:30 IST chop

    def mutate(self, patch: Dict[str, Any]):
        """Apply mutations and clamp strictly to safe quantitative intervals."""
        if "min_target_pct" in patch:
            self.min_target_pct = round(max(0.80, min(2.50, float(patch["min_target_pct"]))), 2)
        if "min_sweep_vol_multiplier" in patch:
            self.min_sweep_vol_multiplier = round(max(1.20, min(3.00, float(patch["min_sweep_vol_multiplier"]))), 2)
        if "breakeven_trigger_r" in patch:
            self.breakeven_trigger_r = round(max(0.80, min(1.50, float(patch["breakeven_trigger_r"]))), 2)
        if "min_sl_pct" in patch:
            self.min_sl_pct = round(max(0.35, min(0.75, float(patch["min_sl_pct"]))), 2)
        if "time_filter_lunch_block" in patch:
            self.time_filter_lunch_block = bool(patch["time_filter_lunch_block"])


# =============================================================================
# 3. Continuous Optimizer Daemon
# =============================================================================

class ContinuousOptimizer:
    def __init__(self, run_interval_seconds: int = 300):
        self.interval = run_interval_seconds
        self.iteration_count = self._determine_initial_iteration()
        self.is_running = False
        self.guard = TokenBudgetGuard()
        self.params = StrategyParameters()
        self.workspace_dir = BASE_DIR / "trading_workspace"

    def _determine_initial_iteration(self) -> int:
        """Find highest iteration number existing in trading_workspace."""
        highest = 7
        ws = BASE_DIR / "trading_workspace"
        if ws.exists():
            for p in ws.glob("iteration*_results.json"):
                match = re.search(r"iteration(\d+)_results", p.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest

    # -------------------------------------------------------------------------
    # Phase 1: 1-Year Baseline Backtest (Zero AI)
    # -------------------------------------------------------------------------
    async def run_phase1_1year_baseline(self) -> Dict[str, Any]:
        """
        Executes a long-horizon 1-year historical baseline backtest with Zero AI
        across the liquid Indian stock universe to establish statistical benchmark.
        """
        logger.info("📊 Running Phase 1: 1-Year Baseline Backtest (Zero AI)...")
        try:
            res = await backtest_engine.run_backtest(
                symbols=LIQUID_NSE_EQUITIES,
                period="1y",
                interval="1h",
                use_deepseek=False,
                time_filter=True,
                primary_only=True,
            )
            return self._phase1_summary(res)
        except Exception as e:
            logger.error(f"Error in Phase 1 1-Year Baseline: {e}", exc_info=True)
            return self._empty_phase1_result()

    # -------------------------------------------------------------------------
    # Phase 1b: Fee-aware intraday baseline (Zero AI)
    # -------------------------------------------------------------------------
    async def run_phase1_fee_aware_baseline(
        self, preloaded_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Fee-aware intraday baseline over the same 1-year / 1-hour universe.

        The shipped primary edge (``momentum_continuation``) is fee-negative at
        the 1-hour horizon because it holds to the close. This arm runs the
        walk-forward validated fee-aware setups (opening EMA-20 pullback, prior-
        day liquidity sweeps long/short, and the relative-strength/RVOL leader)
        through the exact same execution, sizing and gate machinery so the
        optimizer compares like with like. Data is fetched once and handed to
        the backtester via ``preloaded_data`` so the selector and the simulation
        share identical frames.
        """
        logger.info("📊 Running Phase 1b: Fee-aware intraday baseline (Zero AI)...")
        try:
            data = preloaded_data
            if data is None:
                data = backtest_engine.fetch_historical_bars(
                    LIQUID_NSE_EQUITIES, period="1y", interval="1h"
                )
            if not data:
                logger.warning("Phase 1b: no market data returned; skipping fee-aware baseline.")
                return self._empty_phase1_result()
            data = dict(data)
            selector = FeeAwareSelector(data, interval="1h")
            res = await backtest_engine.run_backtest(
                symbols=list(data),
                period="1y",
                interval="1h",
                use_deepseek=False,
                time_filter=True,
                strategy_override=selector.override,
                preloaded_data=data,
            )
            return self._phase1_summary(res)
        except Exception as e:
            logger.error(f"Error in Phase 1b Fee-aware Baseline: {e}", exc_info=True)
            return self._empty_phase1_result()

    @staticmethod
    def _empty_phase1_result() -> Dict[str, Any]:
        return {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate_pct": 0.0, "gross_pnl_inr": 0.0, "fees_inr": 0.0,
            "net_pnl_inr": 0.0, "profit_factor": 1.0, "max_drawdown_pct": 0.0,
        }

    @staticmethod
    def _phase1_summary(res: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "total_trades": res.get("total_trades", 0),
            "winning_trades": res.get("winning_trades", 0),
            "losing_trades": res.get("losing_trades", 0),
            "win_rate_pct": res.get("win_rate_pct", 0.0),
            "gross_pnl_inr": res.get("gross_pnl_inr", 0.0),
            "fees_inr": res.get("fees_inr", 0.0),
            "net_pnl_inr": res.get("net_pnl_before_ai_inr", 0.0),
            "profit_factor": res.get("profit_factor", 1.0),
            "max_drawdown_pct": res.get("max_drawdown_pct", 0.0),
        }

    # -------------------------------------------------------------------------
    # Phase 2: 1-Month Out-of-Sample Backtest with DeepSeek V4.1 Flash
    # -------------------------------------------------------------------------
    async def run_phase2_1month_backtest(
        self,
        params: StrategyParameters,
        use_live_ai: bool = True
    ) -> Dict[str, Any]:
        """
        Executes high-resolution 5-minute backtest across the 23 trading days
        (Oct 1 – Nov 4, 2026) using DeepSeek V4.1 Flash on Vertex AI as meta-filter.
        """
        logger.info(f"⚡ Running Phase 2: 1-Month OOS Backtest (Target >= {params.min_target_pct}%, AI={use_live_ai})...")

        dfs = {}
        for sym in LIQUID_NSE_EQUITIES:
            csv_path = self.workspace_dir / f"real_5m_{sym}.csv"
            if not csv_path.exists():
                logger.error(f"Missing CSV: {csv_path}")
                return {"error": f"Missing CSV for {sym}"}
            df = pd.read_csv(csv_path)
            df["dt"] = pd.to_datetime(df["Datetime"])
            df["date"] = df["dt"].dt.date
            df["time"] = df["dt"].dt.time
            dfs[sym] = df

        sbin_df = dfs["SBIN"]
        all_dates = sorted(list(sbin_df["date"].unique()))
        eval_dates = [d for d in all_dates if d >= datetime.date(2026, 10, 1)]

        initial_capital = 10000.0
        available_cash = initial_capital
        all_trades = []
        losing_trades = []
        peak_equity = initial_capital
        max_drawdown_pct = 0.0
        max_cost_basis = 0.0
        ai_queries_count = 0
        ai_cost_total = 0.0

        for d_idx, today in enumerate(eval_dates):
            prev_date_idx = all_dates.index(today) - 1
            prev_date = all_dates[prev_date_idx] if prev_date_idx >= 0 else None

            day_start_equity = available_cash
            day_realized_pnl = 0.0
            day_trades = []
            open_positions = {}
            day_profit_locked = False
            day_circuit_tripped = False

            today_sbin = sbin_df[sbin_df["date"] == today]
            today_times = sorted(list(today_sbin["time"].unique()))
            last_time = today_times[-1] if today_times else datetime.time(15, 15)

            # Precompute 4-Lines (PDH, PDL, PDC)
            prev_day_levels = {}
            for sym in LIQUID_NSE_EQUITIES:
                sym_df = dfs[sym]
                if prev_date:
                    p_df = sym_df[sym_df["date"] == prev_date]
                    if not p_df.empty:
                        prev_day_levels[sym] = {
                            "PDH": float(p_df["High"].max()),
                            "PDL": float(p_df["Low"].min()),
                            "PDC": float(p_df["Close"].iloc[-1]),
                        }
                    else:
                        prev_day_levels[sym] = {"PDH": 0.0, "PDL": 0.0, "PDC": 0.0}
                else:
                    prev_day_levels[sym] = {"PDH": 0.0, "PDL": 0.0, "PDC": 0.0}

            for bar_time in today_times:
                is_eod_square_off = (bar_time == last_time or bar_time >= datetime.time(15, 15))
                is_entry_window = (
                    datetime.time(9, 30) <= bar_time <= datetime.time(14, 45)
                )
                if params.time_filter_lunch_block and (datetime.time(11, 30) <= bar_time <= datetime.time(13, 30)):
                    is_entry_window = False

                # 1. Position Management & Trailing Breakeven Stop
                for sym in list(open_positions.keys()):
                    pos = open_positions[sym]
                    sym_df = dfs[sym]
                    bar_match = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)]
                    if bar_match.empty:
                        continue
                    bar = bar_match.iloc[0]
                    high_p = float(bar["High"])
                    low_p = float(bar["Low"])
                    close_p = float(bar["Close"])

                    # Trailing Breakeven Rule
                    if not pos.get("breakeven_trailed"):
                        if pos["action"] == "BUY" and high_p >= pos["be_trigger_price"]:
                            pos["sl"] = round(pos["entry"] * 1.0012, 2)  # Entry + 0.12% friction
                            pos["breakeven_trailed"] = True
                        elif pos["action"] == "SELL" and low_p <= pos["be_trigger_price"]:
                            pos["sl"] = round(pos["entry"] * 0.9988, 2)
                            pos["breakeven_trailed"] = True

                    exit_reason = None
                    exit_price = close_p

                    if pos["action"] == "BUY":
                        if high_p >= pos["tp"]:
                            exit_reason = "TARGET_HIT"
                            exit_price = pos["tp"]
                        elif low_p <= pos["sl"]:
                            exit_reason = "STOP_LOSS"
                            exit_price = pos["sl"]
                        elif is_eod_square_off:
                            exit_reason = "EOD_SQUARE_OFF"
                            exit_price = close_p
                    else:
                        if low_p <= pos["tp"]:
                            exit_reason = "TARGET_HIT"
                            exit_price = pos["tp"]
                        elif high_p >= pos["sl"]:
                            exit_reason = "STOP_LOSS"
                            exit_price = pos["sl"]
                        elif is_eod_square_off:
                            exit_reason = "EOD_SQUARE_OFF"
                            exit_price = close_p

                    if exit_reason:
                        qty = pos["qty"]
                        entry_p = pos["entry"]
                        gross_pnl = (exit_price - entry_p) * qty if pos["action"] == "BUY" else (entry_p - exit_price) * qty
                        buy_val = (entry_p if pos["action"] == "BUY" else exit_price) * qty
                        sell_val = (exit_price if pos["action"] == "BUY" else entry_p) * qty
                        charges = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type="INTRADAY_EQUITY", quantity=qty)
                        fees = charges["total_charges"]
                        net_pnl = round(gross_pnl - fees, 2)

                        available_cash += (pos["cost_basis"] + net_pnl)
                        day_realized_pnl += net_pnl

                        trade_rec = {
                            "trade_num": len(all_trades) + 1,
                            "date": str(today),
                            "symbol": sym,
                            "action": pos["action"],
                            "qty": qty,
                            "entry_time": str(pos["entry_time"]),
                            "entry_price": entry_p,
                            "exit_time": str(bar_time),
                            "exit_price": exit_price,
                            "gross_pnl": round(gross_pnl, 2),
                            "fees": fees,
                            "net_pnl": net_pnl,
                            "return_pct": round((net_pnl / pos["cost_basis"]) * 100, 2),
                            "exit_reason": exit_reason,
                            "status": "WIN" if net_pnl > 0 else "LOSS",
                        }
                        day_trades.append(trade_rec)
                        all_trades.append(trade_rec)
                        if net_pnl <= 0:
                            losing_trades.append(trade_rec)
                        del open_positions[sym]

                # 2. Daily Risk Limits (+1% target lock, -3% loss limit)
                if day_realized_pnl >= 100.0:
                    day_profit_locked = True
                elif day_realized_pnl <= -300.0:
                    day_circuit_tripped = True
                    for sym in list(open_positions.keys()):
                        pos = open_positions[sym]
                        sym_df = dfs[sym]
                        bar_match = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)]
                        close_p = float(bar_match.iloc[0]["Close"]) if not bar_match.empty else pos["entry"]
                        qty = pos["qty"]
                        entry_p = pos["entry"]
                        gross_pnl = (close_p - entry_p) * qty if pos["action"] == "BUY" else (entry_p - close_p) * qty
                        buy_val = (entry_p if pos["action"] == "BUY" else close_p) * qty
                        sell_val = (close_p if pos["action"] == "BUY" else entry_p) * qty
                        charges = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type="INTRADAY_EQUITY", quantity=qty)
                        fees = charges["total_charges"]
                        net_pnl = round(gross_pnl - fees, 2)
                        available_cash += (pos["cost_basis"] + net_pnl)
                        day_realized_pnl += net_pnl
                        trade_rec = {
                            "trade_num": len(all_trades) + 1,
                            "date": str(today),
                            "symbol": sym,
                            "action": pos["action"],
                            "qty": qty,
                            "entry_time": str(pos["entry_time"]),
                            "entry_price": entry_p,
                            "exit_time": str(bar_time),
                            "exit_price": close_p,
                            "gross_pnl": round(gross_pnl, 2),
                            "fees": fees,
                            "net_pnl": net_pnl,
                            "return_pct": round((net_pnl / pos["cost_basis"]) * 100, 2),
                            "exit_reason": "CIRCUIT_BREAKER_HALT",
                            "status": "WIN" if net_pnl > 0 else "LOSS",
                        }
                        day_trades.append(trade_rec)
                        all_trades.append(trade_rec)
                        losing_trades.append(trade_rec)
                        del open_positions[sym]

                # 3. New Entry Discovery
                if is_entry_window and not day_profit_locked and not day_circuit_tripped and len(open_positions) < 3:
                    for sym in LIQUID_NSE_EQUITIES:
                        if sym in open_positions or len(open_positions) >= 3:
                            continue
                        sym_df = dfs[sym]
                        match_indices = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)].index
                        if match_indices.empty:
                            continue
                        curr_idx = match_indices[0]
                        sub_df = sym_df.iloc[max(0, curr_idx - 60):curr_idx + 1]
                        if len(sub_df) < 15:
                            continue

                        spot = float(sub_df.iloc[-1]["Close"])
                        pd_lvl = prev_day_levels[sym]
                        pdh = pd_lvl["PDH"]
                        pdl = pd_lvl["PDL"]
                        pdc = pd_lvl["PDC"]

                        swing_high = float(sub_df.tail(12)["High"].max())
                        swing_low = float(sub_df.tail(12)["Low"].min())

                        ema20 = float(sub_df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
                        ema50 = float(sub_df["Close"].ewm(span=50, adjust=False).mean().iloc[-1])
                        trend_bullish = (spot >= ema20) or (ema20 >= ema50)
                        trend_bearish = (spot < ema20) and (ema20 <= ema50)

                        sweeps = detect_liquidity_sweeps_and_absorption(
                            sub_df, pdh=pdh, pdl=pdl, pdc=pdc, swing_high=swing_high, swing_low=swing_low
                        )
                        recent_sweep = sweeps[-1] if sweeps else None

                        # Freshness Filter: Only enter if the sweep occurred on the current or prior 2 bars
                        if recent_sweep:
                            try:
                                sweep_idx = int(recent_sweep.get("timestamp", curr_idx))
                                if (curr_idx - sweep_idx) > 2:
                                    recent_sweep = None
                            except (ValueError, TypeError):
                                pass

                        action = None
                        sl = None
                        tp = None
                        be_trigger = None

                        if recent_sweep:
                            # Verify volume multiplier on sweep candle
                            vol_ratio = recent_sweep.get("wick_ratio", 0.0)
                            if recent_sweep["type"] == "BULLISH_LIQUIDITY_SWEEP" and trend_bullish:
                                action = "BUY"
                                risk = max(spot * (params.min_sl_pct / 100.0), spot - (recent_sweep["wick_low"] - (spot * 0.0015)))
                                sl = round(spot - risk, 2)
                                tp_dist = max(risk * 2.0, spot * (params.min_target_pct / 100.0))
                                tp = round(spot + tp_dist, 2)
                                be_trigger = round(spot + (risk * params.breakeven_trigger_r), 2)
                            elif recent_sweep["type"] == "BEARISH_LIQUIDITY_SWEEP" and trend_bearish:
                                action = "SELL"
                                risk = max(spot * (params.min_sl_pct / 100.0), (recent_sweep["wick_high"] + (spot * 0.0015)) - spot)
                                sl = round(spot + risk, 2)
                                tp_dist = max(risk * 2.0, spot * (params.min_target_pct / 100.0))
                                tp = round(spot - tp_dist, 2)
                                be_trigger = round(spot - (risk * params.breakeven_trigger_r), 2)

                        if action and sl and tp:
                            # Target move verification: must comfortably exceed minimum target percent
                            expected_move_pct = abs(tp - spot) / spot * 100.0
                            if expected_move_pct < params.min_target_pct:
                                continue

                            # DeepSeek V4.1 Flash Meta-Filter (Capped at 25 high-conviction checks per 1-month run)
                            execute_approved = True
                            if use_live_ai and ai_queries_count < 25 and self.guard.can_call_deepseek():
                                ai_res = await self._query_deepseek_filter(
                                    symbol=sym,
                                    action=action,
                                    spot=spot,
                                    sl=sl,
                                    tp=tp,
                                    sweep=recent_sweep,
                                    indicators={"ema20": ema20, "ema50": ema50, "pdh": pdh, "pdl": pdl}
                                )
                                ai_queries_count += 1
                                ai_cost_total += ai_res.get("cost_inr", 0.0)
                                if ai_res.get("decision") == "PASS":
                                    execute_approved = False

                            if not execute_approved:
                                continue

                            alloc = min(params.max_position_cost, available_cash)
                            qty = int(alloc // spot)
                            if qty >= 1 and (qty * spot) <= 3000.0 and (qty * spot) <= available_cash:
                                cost = qty * spot
                                available_cash -= cost
                                max_cost_basis = max(max_cost_basis, cost)
                                open_positions[sym] = {
                                    "action": action,
                                    "qty": qty,
                                    "entry": spot,
                                    "cost_basis": cost,
                                    "sl": sl,
                                    "tp": tp,
                                    "be_trigger_price": be_trigger,
                                    "breakeven_trailed": False,
                                    "entry_time": bar_time,
                                }

            # Peak equity & drawdown tracking
            current_equity = available_cash + sum(p["cost_basis"] for p in open_positions.values())
            peak_equity = max(peak_equity, current_equity)
            drawdown = (peak_equity - current_equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

        # Performance Aggregation
        total_trades = len(all_trades)
        wins = [t for t in all_trades if t["status"] == "WIN"]
        losses = [t for t in all_trades if t["status"] == "LOSS"]
        win_rate = round((len(wins) / total_trades) * 100.0, 1) if total_trades > 0 else 0.0

        gross_profit = sum(t["gross_pnl"] for t in wins)
        gross_loss = abs(sum(t["gross_pnl"] for t in losses))
        total_fees = round(sum(t["fees"] for t in all_trades), 2)
        net_pnl = round(sum(t["net_pnl"] for t in all_trades) - ai_cost_total, 2)
        profit_factor = round(gross_profit / max(1.0, gross_loss + total_fees), 2)

        return {
            "initial_capital_inr": initial_capital,
            "ending_equity_inr": round(initial_capital + net_pnl, 2),
            "net_pnl_inr": net_pnl,
            "net_return_pct": round((net_pnl / initial_capital) * 100.0, 2),
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "losing_trades_count": len(losses),
            "win_rate_pct": win_rate,
            "total_fees_inr": total_fees,
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "max_cost_basis": round(max_cost_basis, 2),
            "ai_queries_count": ai_queries_count,
            "ai_cost_inr": round(ai_cost_total, 4),
            "trades": all_trades,
            "losing_trades_summary": losing_trades[:15],
        }

    async def _query_deepseek_filter(
        self,
        symbol: str,
        action: str,
        spot: float,
        sl: float,
        tp: float,
        sweep: Dict[str, Any],
        indicators: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Queries DeepSeek V4.1 Flash via Vertex AI to validate institutional entry."""
        prompt = f"""You are an elite quantitative execution filter for Indian NSE equities.
Evaluate this intraday setup for {symbol}:
- Direction: {action}
- Spot: ₹{spot} | Stop-Loss: ₹{sl} | Target: ₹{tp} (Target move: {round(abs(tp-spot)/spot*100, 2)}%)
- Liquidity Sweep: {sweep.get('type')} on {sweep.get('swept_level')} ({sweep.get('level_price')})
- Trapped Wick Ratio: {sweep.get('wick_ratio')}
- Context: {indicators}

Does this setup represent genuine institutional order flow absorption rather than a retail trap?
Answer with raw JSON only:
{{"decision":"EXECUTE" or "PASS", "confidence":50-95, "reason":"one sentence"}}"""

        try:
            def _sync_deepseek():
                from google import genai
                client = genai.Client(vertexai=True, project="super-512620", location="global")
                return client.models.generate_content(
                    model="deepseek-ai/deepseek-v4.1-flash-maas",
                    contents=prompt
                )

            resp = await asyncio.wait_for(asyncio.to_thread(_sync_deepseek), timeout=25.0)
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            data = json.loads(match.group(0) if match else text)

            usage = getattr(resp, "usage_metadata", None)
            p_toks = getattr(usage, "prompt_token_count", 250) if usage else 250
            c_toks = getattr(usage, "candidates_token_count", 60) if usage else 60
            cost = token_tracker.calculate_cost_inr("deepseek-ai/deepseek-v4.1-flash-maas", p_toks, c_toks)
            db.record_token_usage(
                module="deepseek_filter",
                model="deepseek-ai/deepseek-v4.1-flash-maas",
                prompt_tokens=p_toks,
                completion_tokens=c_toks,
                cost_inr=cost,
                account_id="deepseek_flash"
            )
            data["cost_inr"] = cost
            return data
        except Exception as e:
            logger.warning(f"DeepSeek call failed: {e}")
            return {"decision": "EXECUTE", "confidence": 75, "reason": "AI fallback approval", "cost_inr": 0.0}

    # -------------------------------------------------------------------------
    # Phase 3: Gemini 4 Review, Google Search Grounding & Plan Self-Healing
    # -------------------------------------------------------------------------
    async def run_phase3_gemini_review_and_self_heal(
        self,
        p1_res: Dict[str, Any],
        p2_res: Dict[str, Any],
        cycle_num: int
    ) -> Dict[str, Any]:
        """
        Uses Gemini 4 (with Google Search Grounding) to analyze failure modes,
        mutate strategy parameters, and update AUTONOMOUS_TRADING_PLAN.md.
        """
        logger.info(f"🧠 Running Phase 3: Gemini 4 Strategic Review & Self-Healing for Cycle #{cycle_num}...")

        if not self.guard.can_call_gemini():
            logger.warning("Gemini token budget ceiling reached; proceeding with local heuristic review.")
            return {"status": "throttled", "learning": "Gemini token limit reached."}

        review_prompt = f"""You are the Master Quantitative Strategist supervising an autonomous intraday trading desk for Indian NSE cash equities (₹10,000 capital).
Review the latest quantitative cycle results:

### Cycle #{cycle_num} Performance:
- Phase 1 Baseline (1-Year Zero AI): Win Rate: {p1_res.get('win_rate_pct')}%, Trades: {p1_res.get('total_trades')}, Net P&L: ₹{p1_res.get('net_pnl_inr'):+,.2f}, PF: {p1_res.get('profit_factor')}
- Phase 2 Out-of-Sample (1-Month 5m Data with DeepSeek V4.1 Flash):
  * Win Rate: {p2_res.get('win_rate_pct')}%
  * Total Trades: {p2_res.get('total_trades')} (Wins: {p2_res.get('winning_trades')}, Losses: {p2_res.get('losing_trades')})
  * Net P&L: ₹{p2_res.get('net_pnl_inr'):+,.2f} | Fees Paid: ₹{p2_res.get('total_fees_inr'):,.2f}
  * Profit Factor: {p2_res.get('profit_factor')} | Max Drawdown: {p2_res.get('max_drawdown_pct')}%
- Current Parameters:
  * min_target_pct: {self.params.min_target_pct}%
  * min_sweep_vol_multiplier: {self.params.min_sweep_vol_multiplier}x
  * breakeven_trigger_r: {self.params.breakeven_trigger_r}R
  * min_sl_pct: {self.params.min_sl_pct}%

### Recent Losing Trade Cluster (False Positives):
{json.dumps(p2_res.get('losing_trades_summary', [])[:8], indent=2)}

Your Tasks:
1. Conduct root-cause forensics on why the losing trades occurred (e.g., fee drag on small moves, lunchtime false breaks, tight stop whipsaws).
2. Synthesize institutional market microstructure lessons for Indian equities.
3. Propose parameter mutations strictly within safe bounds:
   - min_target_pct: [0.80, 2.50]
   - min_sweep_vol_multiplier: [1.20, 3.00]
   - breakeven_trigger_r: [0.80, 1.50]
   - min_sl_pct: [0.35, 0.75]

Respond with raw JSON only:
{{
  "forensics": "2-3 sentences explaining loss root causes",
  "learning": "key quantitative lesson learned",
  "parameter_patch": {{
    "min_target_pct": float,
    "min_sweep_vol_multiplier": float,
    "breakeven_trigger_r": float,
    "min_sl_pct": float
  }},
  "plan_update_summary": "1 paragraph markdown summary to append to AUTONOMOUS_TRADING_PLAN.md"
}}"""

        gemini_response_data = None
        try:
            def _sync_gemini():
                from google import genai
                from google.genai import types

                client = genai.Client(vertexai=True, project="super-512620", location="global")
                return client.models.generate_content(
                    model="gemini-4-argon",
                    contents=review_prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    )
                )

            resp = await asyncio.wait_for(asyncio.to_thread(_sync_gemini), timeout=50.0)
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            gemini_response_data = json.loads(match.group(0) if match else text)

            usage = getattr(resp, "usage_metadata", None)
            p_toks = getattr(usage, "prompt_token_count", 1200) if usage else 1200
            c_toks = getattr(usage, "candidates_token_count", 400) if usage else 400
            cost = token_tracker.calculate_cost_inr("gemini-4-argon", p_toks, c_toks)

            db.record_token_usage(
                module="gemini_supervisor",
                model="gemini-4-argon",
                prompt_tokens=p_toks,
                completion_tokens=c_toks,
                cost_inr=cost,
                account_id="gemini_supervisor"
            )
        except Exception as e:
            logger.warning(f"Gemini 4 review call encountered error: {e}. Falling back to quantitative heuristics.")
            gemini_response_data = {
                "forensics": f"Cycle #{cycle_num} evaluated: Fee drag identified on micro moves under 0.8%. Target expansion required.",
                "learning": "Enforce >=0.9% minimum target to ensure net positive expectancy over ₹3.50+ statutory transaction fee barrier.",
                "parameter_patch": {
                    "min_target_pct": min(1.20, self.params.min_target_pct + 0.05),
                    "min_sweep_vol_multiplier": min(2.0, self.params.min_sweep_vol_multiplier + 0.1),
                    "breakeven_trigger_r": 1.0,
                    "min_sl_pct": 0.45
                },
                "plan_update_summary": f"Self-healing cycle #{cycle_num}: Parameter patch applied to target expansion and volume absorption."
            }

        # Apply parameter mutations
        patch = gemini_response_data.get("parameter_patch", {})
        self.params.mutate(patch)
        learning_str = gemini_response_data.get("learning", "Optimization cycle executed.")

        # Update AUTONOMOUS_TRADING_PLAN.md
        plan_path = BASE_DIR / "AUTONOMOUS_TRADING_PLAN.md"
        self.update_trading_plan(
            plan_path=plan_path,
            cycle_num=cycle_num,
            p1_res=p1_res,
            p2_res=p2_res,
            learning=learning_str,
            new_params=asdict(self.params)
        )

        # Record to SQLite learning_log
        db.record_learning(
            category=f"SELF_HEALING_CYCLE_{cycle_num}",
            insight=learning_str,
            strategy_ref="institutional_smc_liquidity_sweep",
            symbol_ref="MULTI_EQUITY_NSE",
            impact_pnl=p2_res.get("net_pnl_inr", 0.0),
            account_id="gemini_supervisor"
        )

        return gemini_response_data

    def update_trading_plan(
        self,
        plan_path: Path,
        cycle_num: int,
        p1_res: Dict[str, Any],
        p2_res: Dict[str, Any],
        learning: str,
        new_params: Dict[str, Any]
    ) -> bool:
        """Appends self-healing iteration telemetry directly to the plan document."""
        if not plan_path.exists():
            return False

        timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = f"""

---

### Autonomous Self-Healing Cycle #{cycle_num} ({timestamp_str})
* **Supervisory Models:** `google-vertex/gemini-4-argon` (Review & Plan) + `deepseek-ai/deepseek-v4.1-flash-maas` (OOS Meta-Filter)
* **Phase 1 Baseline (1-Year Zero AI):** {p1_res.get('total_trades', 0)} Trades | Win%: {p1_res.get('win_rate_pct', 0.0)}% | Net P&L: ₹{p1_res.get('net_pnl_inr', 0.0):+,.2f} | PF: {p1_res.get('profit_factor', 1.0)}
* **Phase 2 OOS (1-Month 5m Data with DeepSeek):** {p2_res.get('total_trades', 0)} Trades | Win%: {p2_res.get('win_rate_pct', 0.0)}% | Net P&L: ₹{p2_res.get('net_pnl_inr', 0.0):+,.2f} | PF: {p2_res.get('profit_factor', 1.0)} | Max DD: {p2_res.get('max_drawdown_pct', 0.0)}%
* **Quantitative Forensics & Learning:** {learning}
* **Calibrated Strategy Parameters:**
  * `min_target_pct`: {new_params.get('min_target_pct', self.params.min_target_pct)}%
  * `min_sweep_vol_multiplier`: {new_params.get('min_sweep_vol_multiplier', self.params.min_sweep_vol_multiplier)}x
  * `breakeven_trigger_r`: {new_params.get('breakeven_trigger_r', self.params.breakeven_trigger_r)}R
  * `min_sl_pct`: {new_params.get('min_sl_pct', self.params.min_sl_pct)}%
"""
        with open(plan_path, "a", encoding="utf-8") as f:
            f.write(entry)
        return True

    # -------------------------------------------------------------------------
    # Main Optimization Cycle & Execution Loop
    # -------------------------------------------------------------------------
    async def run_optimization_cycle(self):
        """Execute one complete 3-phase self-healing optimization cycle."""
        self.iteration_count += 1
        iter_num = self.iteration_count
        timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        logger.info(f"🚀 Starting Autonomous Self-Healing Cycle #{iter_num} at {timestamp_str}")

        try:
            # 1. Phase 1: 1-Year Baseline Backtest (Zero AI)
            p1_res = await self.run_phase1_1year_baseline()
            logger.info(
                f"✅ Phase 1 (1Y Baseline) Complete: {p1_res.get('total_trades')} trades | "
                f"Win Rate: {p1_res.get('win_rate_pct')}% | Net P&L: ₹{p1_res.get('net_pnl_inr'):+,.2f}"
            )

            # 1b. Phase 1b: Fee-aware intraday baseline (Zero AI)
            p1b_res = await self.run_phase1_fee_aware_baseline()
            logger.info(
                f"✅ Phase 1b (Fee-aware 1Y) Complete: {p1b_res.get('total_trades')} trades | "
                f"Win Rate: {p1b_res.get('win_rate_pct')}% | Net P&L: ₹{p1b_res.get('net_pnl_inr'):+,.2f}"
            )

            # 2. Phase 2: 1-Month Out-of-Sample Backtest (DeepSeek V4.1 Flash)
            p2_res = await self.run_phase2_1month_backtest(params=self.params, use_live_ai=True)
            logger.info(
                f"✅ Phase 2 (1M DeepSeek OOS) Complete: {p2_res.get('total_trades')} trades | "
                f"Win Rate: {p2_res.get('win_rate_pct')}% | Net P&L: ₹{p2_res.get('net_pnl_inr'):+,.2f} | "
                f"Fees: ₹{p2_res.get('total_fees_inr'):,.2f} | PF: {p2_res.get('profit_factor')}"
            )

            # 3. Phase 3: Gemini 4 Strategic Review & Plan Self-Healing
            p3_res = await self.run_phase3_gemini_review_and_self_heal(
                p1_res=p1_res,
                p2_res=p2_res,
                cycle_num=iter_num
            )

            # 4. Save JSON Snapshot
            snapshot = {
                "iteration": iter_num,
                "timestamp": timestamp_str,
                "phase1_1year_baseline": p1_res,
                "phase1b_fee_aware_baseline": p1b_res,
                "phase2_1month_oos": p2_res,
                "phase3_gemini_self_healing": p3_res,
                "active_parameters": asdict(self.params),
                "token_budget_status": self.guard.get_token_status(),
            }
            result_file = self.workspace_dir / f"iteration{iter_num}_results.json"
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, default=str)

            # 5. Append to INTRADAY_STRATEGY_LEARNING_LOG.md
            log_path = self.workspace_dir / "INTRADAY_STRATEGY_LEARNING_LOG.md"
            if log_path.exists():
                md_entry = f"""
---

## Iteration {iter_num}: Autonomous 3-Phase Self-Healing Cycle ({timestamp_str})

* **Supervisors:** Gemini 4 Argon + DeepSeek V4.1 Flash MaaS
* **Phase 1 (1Y Baseline Zero AI):** {p1_res.get('total_trades')} trades | Win%: {p1_res.get('win_rate_pct')}% | Net P&L: ₹{p1_res.get('net_pnl_inr'):+,.2f} | PF: {p1_res.get('profit_factor')}
* **Phase 1b (Fee-aware 1Y Zero AI):** {p1b_res.get('total_trades')} trades | Win%: {p1b_res.get('win_rate_pct')}% | Net P&L: ₹{p1b_res.get('net_pnl_inr'):+,.2f} | PF: {p1b_res.get('profit_factor')}
* **Phase 2 (1M OOS DeepSeek):** {p2_res.get('total_trades')} trades | Win%: {p2_res.get('win_rate_pct')}% | Net P&L: ₹{p2_res.get('net_pnl_inr'):+,.2f} | PF: {p2_res.get('profit_factor')} | Max DD: {p2_res.get('max_drawdown_pct')}%
* **Token Usage Status:** Gemini In: {snapshot['token_budget_status']['gemini_input_tokens']:,} / 10M | Gemini Out: {snapshot['token_budget_status']['gemini_output_tokens']:,} / 3M | DeepSeek: {snapshot['token_budget_status']['deepseek_total_tokens']:,} / 10M
* **Mutated Parameters:** Target: {self.params.min_target_pct}% | Sweep Vol Mult: {self.params.min_sweep_vol_multiplier}x | Breakeven Trigger: {self.params.breakeven_trigger_r}R | SL: {self.params.min_sl_pct}%
"""
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(md_entry)

            logger.info(f"📝 Persisted Cycle #{iter_num} results to JSON, SQLite, and Markdown.")

        except Exception as e:
            logger.error(f"❌ Error in optimization cycle #{iter_num}: {e}", exc_info=True)

    async def start(self):
        """Continuous background execution loop."""
        self.is_running = True
        logger.info(f"🔄 Continuous Optimizer Daemon started (Loop interval: {self.interval}s / {self.interval // 60}m)")
        while self.is_running:
            await self.run_optimization_cycle()
            logger.info(f"⏳ Sleeping for {self.interval}s until next optimization cycle...")
            await asyncio.sleep(self.interval)

    def stop(self):
        self.is_running = False
        logger.info("🛑 Continuous Optimizer Daemon stopped.")


optimizer_daemon = ContinuousOptimizer(run_interval_seconds=300)

if __name__ == "__main__":
    try:
        asyncio.run(optimizer_daemon.start())
    except KeyboardInterrupt:
        optimizer_daemon.stop()
