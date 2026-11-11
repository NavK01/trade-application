"""Honest intraday backtester for NSE equities.

Design choices that prevent inflated results:
  * Signals are evaluated on a completed bar and filled at the NEXT bar open.
  * Paper slippage and realistic Indian intraday fees are charged.
  * Gap-through stops are filled at the gap open, not the stop price.
  * Maximum three entries per day, one open position at a time.
  * The 1% daily loss lock and 2% daily profit target are enforced.
  * Optional DeepSeek calls are charged against the result as AI cost.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf

from app.config import settings, create_genai_client
from app.storage.db import db
from app.system.tokens import token_tracker
from app.trading.costs import calculate_buy_fee, calculate_sell_fee
from app.trading.intraday_strategies import intraday_engine

logger = logging.getLogger("app.trading.backtester")


def _ist_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.to_datetime(df.index, errors="coerce", utc=True)
    idx = idx.tz_convert("Asia/Kolkata")
    out = df.copy()
    out.index = idx
    return out


def hard_invalidation(
    setup: Dict[str, Any],
    indicators: Dict[str, Any],
    recent_bars: List[Dict[str, Any]],
) -> Optional[str]:
    """Deterministic check for the three HARD invalidation conditions.

    The AI is only allowed to veto a validated momentum-continuation setup when
    one of these is literally true. This guard prevents the model from vetoing
    on weak grounds (low volume, "stalling", high RSI) which we measured to be
    a coin flip that can flip a profitable month negative.

    Returns a short reason string when a hard invalidation is present, else None.
    """
    entry = float(setup.get("entry_price") or 0.0)
    stop = float(setup.get("stop_loss") or 0.0)
    or_high = float(setup.get("or_high") or 0.0)

    # H3: stop more than 2.5% below entry -> unacceptable risk/reward.
    if entry > 0 and stop > 0 and (entry - stop) / entry > 0.025:
        return "stop wider than 2.5% of entry"

    if not recent_bars:
        return None
    last = recent_bars[-1]
    last_close = float(last.get("close") or last.get("c") or 0.0)
    vwap = float(indicators.get("vwap") or last.get("vwap") or 0.0)

    # H1: price below BOTH VWAP and the opening-range high -> breakout failed.
    if last_close > 0 and vwap > 0 and or_high > 0:
        if last_close < vwap and last_close < or_high:
            return "price below VWAP and opening-range high"

    # H2: violent reversal bar against the position.
    if len(recent_bars) >= 2:
        bar_open = float(last.get("open") or last.get("o") or 0.0)
        bar_high = float(last.get("high") or last.get("h") or 0.0)
        bar_low = float(last.get("low") or last.get("l") or 0.0)
        rng = bar_high - bar_low
        if rng > 0 and bar_open > 0 and last_close > 0:
            body_red = last_close < bar_open
            closes_near_low = (last_close - bar_low) / rng <= 0.25
            big_bar = (bar_open - last_close) / bar_open >= 0.006
            if body_red and closes_near_low and big_bar:
                return "violent reversal bar against the position"

    return None


def edge_to_fee_ratio(
    entry: float,
    target: float,
    quantity: int,
    direction: str = "BUY",
) -> float:
    """Expected gross profit to target divided by round-trip transaction cost.

    A ratio below 1.0 means the trade cannot pay for its own fees even if the
    target is reached, so it has negative expectancy by construction. The
    backtester uses this to reject fee-sink candidates before they open.

    ``direction`` selects which fee schedule is charged on each leg: a long
    buys then sells, a short sells then buys. The gross edge is the absolute
    distance from entry to target either way.
    """
    if entry <= 0 or quantity <= 0:
        return 0.0
    gross_edge = abs(target - entry) * quantity
    if gross_edge <= 0:
        return 0.0
    if direction.upper() == "SELL":
        entry_fee = calculate_sell_fee(entry * quantity)
        exit_fee = calculate_buy_fee(target * quantity)
    else:
        entry_fee = calculate_buy_fee(entry * quantity)
        exit_fee = calculate_sell_fee(target * quantity)
    round_trip = entry_fee + exit_fee
    if round_trip <= 0:
        return float("inf")
    return gross_edge / round_trip


def fee_to_risk_pct(
    entry: float,
    stop: float,
    quantity: int,
    direction: str = "BUY",
) -> float:
    """Round-trip transaction cost as a percentage of the rupee risk budget.

    The risk budget is ``abs(entry - stop) * quantity``, i.e. the money the
    stop can lose. When fees are a large fraction of that budget the trade is a
    fee sink: the position is too small (or the stop too tight) for the fixed
    cost to be amortised. Returns 100.0 when there is no risk budget (invalid
    setup).
    """
    if entry <= 0 or quantity <= 0:
        return 100.0
    risk_inr = abs(entry - stop) * quantity
    if risk_inr <= 0:
        return 100.0
    if direction.upper() == "SELL":
        entry_fee = calculate_sell_fee(entry * quantity)
        exit_fee = calculate_buy_fee(entry * quantity)
    else:
        entry_fee = calculate_buy_fee(entry * quantity)
        exit_fee = calculate_sell_fee(entry * quantity)
    return (entry_fee + exit_fee) / risk_inr * 100.0


class HistoricalBacktestEngine:
    def __init__(self, initial_capital: Optional[float] = None):
        self.initial_capital = float(initial_capital or settings.INITIAL_CAPITAL_INR)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def fetch_historical_bars(
        self, symbols: List[str], period: str = "5d", interval: str = "5m"
    ) -> Dict[str, pd.DataFrame]:
        data: Dict[str, pd.DataFrame] = {}
        for sym in symbols:
            clean = sym.strip().upper()
            yf_sym = clean if ("." in clean or "^" in clean) else f"{clean}.NS"
            try:
                df = yf.Ticker(yf_sym).history(period=period, interval=interval)
                if df.empty and yf_sym.endswith(".NS"):
                    df = yf.Ticker(yf_sym.replace(".NS", ".BO")).history(
                        period=period, interval=interval
                    )
                if df.empty:
                    continue
                df = _ist_index(df).reset_index()
                df = df.rename(columns={
                    "Datetime": "timestamp", "Date": "timestamp",
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                })
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
                df["time"] = df["timestamp"].dt.strftime("%H:%M")
                df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
                df = df[(df["time"] >= "09:15") & (df["time"] <= "15:30")]
                df = df.reset_index(drop=True)
                if not df.empty:
                    data[clean] = df
                    logger.info("Loaded %d %s bars for %s", len(df), interval, clean)
            except Exception as exc:
                logger.error("Error fetching data for %s: %s", sym, exc)
        return data

    # ------------------------------------------------------------------
    # Optional AI filter
    # ------------------------------------------------------------------
    async def _query_deepseek_decision(
        self,
        symbol: str,
        setup: Dict[str, Any],
        indicators: Dict[str, Any],
        recent_bars: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Veto-only AI risk filter.

        The deterministic momentum engine owns the trade geometry (entry, stop,
        target). The AI may only answer EXECUTE or PASS; it can never move the
        stop or target, because doing so destroys the validated edge. The prompt
        is written for a momentum-continuation setup, so the model is not asked
        to apply mean-reversion logic ("RSI overbought") to a trend trade.
        """
        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_"):
            return {
                "decision": "EXECUTE" if setup.get("confidence", 0) >= 80 else "PASS",
                "reasoning": "DeepSeek not configured; deterministic confidence gate.",
                "cost_inr": 0.0,
            }

        prompt = f"""You are a veto-only risk filter for an NSE intraday MOMENTUM CONTINUATION trade.
The trade geometry is already fixed by a validated quantitative model. You may NOT change the entry, stop, or target. You may only answer EXECUTE or PASS.

Symbol: {symbol}
Direction: {setup.get('signal')} (long)
Entry: ₹{setup.get('entry_price')}
Stop: ₹{setup.get('stop_loss')}
Target: ₹{setup.get('target_1')}
Strategy: {setup.get('name')}
Indicators: {json.dumps(indicators, default=str)}
Recent bars: {json.dumps(recent_bars, default=str)}

DEFAULT ANSWER IS EXECUTE. This is a TREND trade held to the close. A high RSI or price near the upper Bollinger band is NORMAL and EXPECTED in a momentum continuation and is NOT a reason to reject.

Answer PASS ONLY if at least one HARD invalidation is literally visible in the data above:
  H1. Price is currently BELOW both VWAP and the opening-range high (the breakout has fully failed).
  H2. The most recent bar is a violent reversal against the position: a large red bar that closes near its low AND gives back most of the breakout move.
  H3. The stop is more than 2.5% below entry (risk/reward unacceptable).

Do NOT answer PASS for any of these weak reasons: low volume ratio, "stalling", "consolidating", "waiting for confirmation", RSI being high, price near the upper band, or general caution. If none of H1-H3 is literally true, you MUST answer EXECUTE.

Return raw JSON only:
{{"decision":"EXECUTE" or "PASS","confidence_pct":50-95,"reasoning":"one sentence"}}
Do not invent data. Do not return stop or target fields."""

        try:
            client = create_genai_client(api_key, location="global")
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=settings.DEEPSEEK_MODEL,
                    contents=prompt,
                ),
                timeout=25.0,
            )
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            decision = json.loads(match.group(0) if match else text)
            usage = getattr(resp, "usage_metadata", None)
            p_toks = getattr(usage, "prompt_token_count", 180) if usage else 180
            c_toks = getattr(usage, "candidates_token_count", 80) if usage else 80
            cost = token_tracker.calculate_cost_inr(settings.DEEPSEEK_MODEL, p_toks, c_toks)
            decision["cost_inr"] = cost
            return decision
        except Exception as exc:
            logger.warning("DeepSeek filter failed: %s", exc)
            return {
                "decision": "EXECUTE" if setup.get("confidence", 0) >= 80 else "PASS",
                "reasoning": "DeepSeek unavailable; deterministic confidence gate.",
                "cost_inr": 0.0,
            }

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    async def run_backtest(
        self,
        symbols: Optional[List[str]] = None,
        period: str = "5d",
        interval: str = "5m",
        use_deepseek: bool = False,
        max_deepseek_calls: int = 15,
        time_filter: bool = True,
        primary_only: bool = True,
        strategy_override: Optional[Any] = None,
        ai_cost_hook: Optional[Any] = None,
        intraday_leverage_mult: float = 1.0,
        preloaded_data: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Dict[str, Any]:
        """
        primary_only: when True (default), entries are restricted to the
        validated primary edge (momentum_continuation). The secondary
        strategies are still evaluated for context but never open a position,
        because they have no positive expectancy after costs.

        interval: bar size for the simulation. Defaults to "5m" (live
        behaviour). "1h" is supported for longer-horizon tests; the strategy
        windows and the intraday time gates scale accordingly.

        strategy_override: optional callable ``(history, prev_day_ohlc,
        interval) -> setup_dict`` used INSTEAD of the built-in suite. It lets a
        research runner test an alternative strategy through the exact same
        execution, sizing, fee, and AI-veto machinery. When None (the default)
        the shipped suite is used and live behaviour is unchanged. The callable
        may be a coroutine function (e.g. an AI-decides strategy that awaits a
        model call); it is awaited transparently.

        ai_cost_hook: optional zero-argument callable returning the cumulative
        AI cost (in INR) incurred by a ``strategy_override``. The override path
        does not go through the veto path's cost accounting, so a research
        runner that makes its own model calls passes this hook to have that
        spend charged against the result. When None (the default) nothing is
        charged and live behaviour is unchanged.

        intraday_leverage_mult: effective notional exposure as a multiple of
        opening equity (NSE MIS margin). 1.0 (default) keeps the legacy
        cash-funded cap of ``MAX_POSITION_PCT``. Values above 1.0 allow
        risk-based sizing ``qty = min(max_risk / risk_per_share,
        day_start_equity * intraday_leverage_mult / entry)`` so rupee profit
        scales without increasing percentage friction, while the hard rupee
        loss remains capped at ``RISK_PER_TRADE_PCT``.

        preloaded_data: optional mapping of symbol -> OHLCV DataFrame. When
        supplied the network fetch is skipped and this data is simulated
        directly, which lets a caller (e.g. the continuous optimizer) build a
        fee-aware selector over the exact frames it fetched rather than
        monkey-patching ``fetch_historical_bars``. Must carry the same columns
        the fetch produces (``timestamp``/``open``/``high``/``low``/``close``/
        ``volume``/``time``/``date``). When None (the default) data is fetched
        as before.
        """
        hourly = interval in ("1h", "60m", "90m")
        # Minimum bars required for a day to be tradable. 5m sessions have 75
        # bars; hourly sessions have 7. Scale the floor so a partial day is
        # still skipped but a full hourly day is not.
        min_day_bars = 3 if hourly else 5
        # Last entry time: 5m bars can enter up to 14:45; hourly bars only have
        # a 14:15 bar before the 15:15 close, so the cutoff moves earlier.
        last_entry_time = "13:15" if hourly else "14:45"
        # Midday lull filter only makes sense on intraday bars.
        lull_start, lull_end = "11:30", "13:15"

        test_symbols = symbols or ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
        logger.info(
            "Starting next-bar backtest across %d symbols over %s (%s bars)",
            len(test_symbols), period, interval,
        )
        stock_data = (
            dict(preloaded_data)
            if preloaded_data is not None
            else self.fetch_historical_bars(test_symbols, period=period, interval=interval)
        )
        if not stock_data:
            return {"error": "Failed to fetch historical market data."}

        # Prior-day OHLC as seen on each session. The value stored under a
        # date is the PREVIOUS trading day's OHLC, so pivot and sweep strategies
        # never see the current session's own range (no look-ahead).
        prior_day: Dict[str, Dict[str, Dict[str, float]]] = {}
        for sym, df in stock_data.items():
            prior_day[sym] = {}
            day_ohlc = {
                date_str: {
                    "high": float(day["high"].max()),
                    "low": float(day["low"].min()),
                    "close": float(day["close"].iloc[-1]),
                }
                for date_str, day in df.groupby("date")
            }
            ordered = sorted(day_ohlc)
            for i in range(1, len(ordered)):
                prior_day[sym][ordered[i]] = day_ohlc[ordered[i - 1]]

        all_dates = sorted({d for df in stock_data.values() for d in df["date"].unique()})
        cash = self.initial_capital
        trades: List[Dict[str, Any]] = []
        daily_results: List[Dict[str, Any]] = []
        ai_cost_total = 0.0
        deepseek_calls = 0
        max_position_pct = settings.MAX_POSITION_PCT
        risk_per_trade = settings.RISK_PER_TRADE_PCT / 100.0
        slippage = settings.PAPER_SLIPPAGE_BPS / 10_000.0
        leverage = float(intraday_leverage_mult) if intraday_leverage_mult and intraday_leverage_mult > 0 else 1.0
        # Effective notional cap. With leverage 1.0 this is the legacy cash-funded
        # cap (MAX_POSITION_PCT of equity); above 1.0 it becomes NSE MIS exposure.
        exposure_pct = max(max_position_pct, leverage)

        def _settle(
            trade: Dict[str, Any],
            exit_price: float,
            exit_reason: str,
            exit_time: str,
        ) -> float:
            """Close ``trade`` at ``exit_price``, update cash, return net PnL.

            Long: exit leg is a SELL (sell fee). Short: exit leg is a BUY
            (buy fee, i.e. buy-to-cover). Cash is credited/debited for the
            actual leg so leverage can drive it negative intraday (financed by
            MIS margin) and the session restores it when the book is flat.
            """
            nonlocal cash
            exit_price = round(float(exit_price), 2)
            qty = trade["quantity"]
            if trade["action"] == "SELL":
                exit_fee = calculate_buy_fee(exit_price * qty)
                gross = round((trade["entry_price"] - exit_price) * qty, 2)
                cash = round(cash - exit_price * qty - exit_fee, 2)
            else:
                exit_fee = calculate_sell_fee(exit_price * qty)
                gross = round((exit_price - trade["entry_price"]) * qty, 2)
                cash = round(cash + exit_price * qty - exit_fee, 2)
            net = round(gross - trade["entry_fee"] - exit_fee, 2)
            ret_sign = -1.0 if trade["action"] == "SELL" else 1.0
            trade.update({
                "exit_time": exit_time,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "gross_pnl": gross,
                "fees_inr": round(trade["entry_fee"] + exit_fee, 2),
                "net_pnl": net,
                "return_pct": round(
                    ret_sign * (exit_price / trade["entry_price"] - 1) * 100, 2
                ),
                "won": net > 0,
            })
            return net

        for date_str in all_dates:
            day_frames = {
                sym: df[df["date"] == date_str].reset_index(drop=True)
                for sym, df in stock_data.items()
            }
            day_frames = {s: d for s, d in day_frames.items() if len(d) >= min_day_bars}
            if not day_frames:
                continue

            day_start_equity = cash
            day_trades = 0
            day_locked = False
            day_lock_reason = ""
            day_ai_cost = 0.0
            day_realized_pnl = 0.0
            open_trade: Optional[Dict[str, Any]] = None
            times = sorted({t for d in day_frames.values() for t in d["time"].unique()})

            for time_str in times:
                # 1. Manage an open position at this timestamp.
                if open_trade:
                    sym = open_trade["symbol"]
                    frame = day_frames.get(sym)
                    bar_rows = frame[frame["time"] == time_str] if frame is not None else pd.DataFrame()
                    if not bar_rows.empty:
                        bar = bar_rows.iloc[0]
                        exit_price = None
                        exit_reason = None
                        is_short = open_trade["action"] == "SELL"
                        bar_open = float(bar["open"])
                        bar_high = float(bar["high"])
                        bar_low = float(bar["low"])
                        bar_close = float(bar["close"])

                        if is_short:
                            # Short: stop sits ABOVE entry, target BELOW entry.
                            if bar_open >= open_trade["stop_loss"]:
                                exit_price, exit_reason = bar_open, "STOP_GAP"
                            elif bar_high >= open_trade["stop_loss"]:
                                exit_price, exit_reason = open_trade["stop_loss"], "STOP_LOSS"
                            elif not open_trade.get("hold_to_close"):
                                if bar_open <= open_trade["target_1"]:
                                    exit_price, exit_reason = bar_open, "TARGET_GAP"
                                elif bar_low <= open_trade["target_1"]:
                                    exit_price, exit_reason = open_trade["target_1"], "TARGET_HIT"
                        else:
                            if bar_open <= open_trade["stop_loss"]:
                                exit_price, exit_reason = bar_open, "STOP_GAP"
                            elif bar_low <= open_trade["stop_loss"]:
                                exit_price, exit_reason = open_trade["stop_loss"], "STOP_LOSS"
                            elif not open_trade.get("hold_to_close"):
                                if bar_open >= open_trade["target_1"]:
                                    exit_price, exit_reason = bar_open, "TARGET_GAP"
                                elif bar_high >= open_trade["target_1"]:
                                    exit_price, exit_reason = open_trade["target_1"], "TARGET_HIT"
                        if exit_price is None and time_str >= "15:20":
                            exit_price, exit_reason = bar_close, "EOD_SQUARE_OFF"

                        # Profit-lock ratchet. Effective from the NEXT bar
                        # (this bar's extreme is consumed here, after the stop
                        # was already evaluated), so an intrabar lock-out cannot
                        # be assumed. Long: only ever raises the stop. Short:
                        # only ever lowers it, using the bar LOW as the
                        # favourable extreme.
                        ratchet = open_trade.get("ratchet_levels") or []
                        if exit_price is None and ratchet:
                            entry_ref = float(open_trade["entry_price"])
                            if is_short:
                                gain_pct = (1.0 - bar_low / entry_ref) * 100.0
                            else:
                                gain_pct = (bar_high / entry_ref - 1.0) * 100.0
                            ridx = int(open_trade.get("ratchet_idx", 0))
                            while ridx < len(ratchet) and gain_pct >= float(ratchet[ridx]["trigger_pct"]):
                                lock_frac = float(ratchet[ridx]["lock_pct"]) / 100.0
                                if is_short:
                                    lock_price = round(entry_ref * (1.0 - lock_frac), 2)
                                    if lock_price < open_trade["stop_loss"]:
                                        open_trade["stop_loss"] = lock_price
                                else:
                                    lock_price = round(entry_ref * (1.0 + lock_frac), 2)
                                    if lock_price > open_trade["stop_loss"]:
                                        open_trade["stop_loss"] = lock_price
                                ridx += 1
                            open_trade["ratchet_idx"] = ridx

                        if exit_price is not None:
                            net = _settle(open_trade, exit_price, exit_reason, time_str)
                            trades.append(open_trade)
                            day_realized_pnl += net
                            open_trade = None

                # 2. Daily risk guard on REALIZED P&L (closed trades) plus AI cost.
                #    Using realized P&L avoids locking the day on an unrealized
                #    drawdown of a hold-to-close position that later recovers.
                #    The position's own stop is the risk control for open trades.
                net_today = day_realized_pnl - day_ai_cost
                if not day_locked:
                    if net_today <= -day_start_equity * settings.MAX_DAILY_LOSS_PCT / 100.0:
                        day_locked = True
                        day_lock_reason = "DAILY_LOSS_LIMIT"
                    elif net_today >= day_start_equity * settings.TARGET_NET_PROFIT_PCT / 100.0:
                        day_locked = True
                        day_lock_reason = "DAILY_PROFIT_TARGET"

                # A lock blocks NEW entries only. An open hold-to-close position
                # is left to its own stop / EOD square-off so a winner is never
                # cut short by the daily guard.
                if day_locked and open_trade and not open_trade.get("hold_to_close"):
                    frame = day_frames.get(open_trade["symbol"])
                    rows = frame[frame["time"] == time_str] if frame is not None else pd.DataFrame()
                    if not rows.empty:
                        exit_price = round(float(rows.iloc[0]["close"]), 2)
                        net = _settle(open_trade, exit_price, day_lock_reason, time_str)
                        trades.append(open_trade)
                        day_realized_pnl += net
                        open_trade = None

                # 3. Look for a new entry, filled on the NEXT bar's open.
                if open_trade or day_locked or day_trades >= settings.MAX_TRADES_PER_DAY:
                    continue
                if time_str >= last_entry_time:
                    continue
                if time_filter and (lull_start <= time_str <= lull_end):
                    continue

                candidates = []
                for sym, frame in day_frames.items():
                    rows = frame[frame["time"] == time_str]
                    if rows.empty:
                        continue
                    idx = int(rows.index[0])
                    if idx + 1 >= len(frame):
                        continue
                    next_bar = frame.iloc[idx + 1]
                    if str(next_bar["time"]) > "15:20":
                        continue
                    history = frame.iloc[: idx + 1].to_dict(orient="records")
                    if strategy_override is not None:
                        # Research path: the override owns signal generation and
                        # returns a setup dict (or None). Execution, sizing, fees
                        # and the AI veto below are identical to the shipped path.
                        # The override signature has no symbol argument, so tag
                        # each record with it; an override that needs the symbol
                        # (e.g. an AI-decides strategy) can read it from the
                        # history it is handed.
                        for _rec in history:
                            _rec["symbol"] = sym
                        best = strategy_override(
                            history,
                            prior_day.get(sym, {}).get(date_str),
                            interval,
                        )
                        if asyncio.iscoroutine(best):
                            best = await best
                        if not best or not best.get("active"):
                            continue
                        direction = str(best.get("signal", "BUY")).upper()
                        eval_res = {
                            "indicators": {
                                "price": float(best.get("entry_price", 0.0)),
                                "vwap": float(best.get("vwap", 0.0) or 0.0),
                                "or_high": float(best.get("or_high", 0.0) or 0.0),
                            }
                        }
                    else:
                        eval_res = intraday_engine.evaluate_all_strategies(
                            history,
                            prev_day_ohlc=prior_day.get(sym, {}).get(date_str),
                            interval=interval,
                        )
                        direction = str(eval_res.get("composite_signal", "")).upper()
                        if direction not in ("BUY", "SELL"):
                            continue
                        best = eval_res.get("best_strategy")
                        if not best:
                            continue
                        if primary_only and best.get("strategy_id") != "momentum_continuation":
                            continue
                    if direction not in ("BUY", "SELL"):
                        continue
                    is_short = direction == "SELL"
                    if is_short:
                        entry_px = round(float(next_bar["open"]) * (1.0 - slippage), 2)
                    else:
                        entry_px = round(float(next_bar["open"]) * (1.0 + slippage), 2)
                    # Fee-aware strategies publish percentage geometry because
                    # the fill is on this next bar's open; anchor it there.
                    if best.get("stop_pct") and best.get("target_pct"):
                        if is_short:
                            stop = round(entry_px * (1.0 + float(best["stop_pct"]) / 100.0), 2)
                            target = round(entry_px * (1.0 - float(best["target_pct"]) / 100.0), 2)
                        else:
                            stop = round(entry_px * (1.0 - float(best["stop_pct"]) / 100.0), 2)
                            target = round(entry_px * (1.0 + float(best["target_pct"]) / 100.0), 2)
                    else:
                        stop = round(float(best.get("stop_loss", entry_px * 0.992)), 2)
                        target = round(float(best.get("target_1", entry_px * 1.014)), 2)
                    # Direction-aware geometry validation: long needs
                    # stop < entry < target; short needs target < entry < stop.
                    if is_short:
                        if not (target < entry_px < stop):
                            continue
                    else:
                        if stop >= entry_px or target <= entry_px:
                            continue
                    # Fee-aware gate: estimate the position size the risk model
                    # would take and reject the candidate when its gross edge to
                    # target cannot cover round-trip fees by the required
                    # multiple. This is what stops the strategy from churning
                    # fee-sink trades (e.g. GOLDBEES) that lose money after costs.
                    est_risk_per_share = abs(entry_px - stop)
                    if est_risk_per_share > 0:
                        est_max_risk = day_start_equity * risk_per_trade
                        est_max_position = day_start_equity * exposure_pct
                        est_qty = int(min(
                            est_max_risk / est_risk_per_share,
                            est_max_position / entry_px,
                        ))
                        if est_qty > 0:
                            ratio = edge_to_fee_ratio(entry_px, target, est_qty, direction=direction)
                            if ratio < settings.MIN_EDGE_TO_FEE_RATIO:
                                continue
                            fee_risk = fee_to_risk_pct(entry_px, stop, est_qty, direction=direction)
                            if fee_risk > settings.MAX_FEE_TO_RISK_PCT:
                                continue
                    candidates.append({
                        "symbol": sym,
                        "setup": best,
                        "indicators": eval_res.get("indicators", {}),
                        "recent_bars": history[-3:],
                        "direction": direction,
                        "entry_price": entry_px,
                        "stop_loss": stop,
                        "target_1": target,
                        "ratchet_levels": best.get("ratchet_levels") or [],
                        "entry_time": str(next_bar["time"]),
                    })

                if not candidates:
                    continue
                candidates.sort(
                    key=lambda c: float(c["setup"].get("confidence", 0)), reverse=True
                )
                chosen = candidates[0]

                if use_deepseek and deepseek_calls < max_deepseek_calls:
                    deepseek_calls += 1
                    decision = await self._query_deepseek_decision(
                        symbol=chosen["symbol"],
                        setup=chosen["setup"],
                        indicators=chosen["indicators"],
                        recent_bars=chosen["recent_bars"],
                    )
                    call_cost = float(decision.get("cost_inr", 0.0))
                    ai_cost_total += call_cost
                    day_ai_cost += call_cost
                    if decision.get("decision") != "EXECUTE":
                        # The AI may only veto on a HARD invalidation. If the
                        # deterministic guard finds none, the veto is a weak
                        # signal (low volume, "stalling", high RSI) and is
                        # downgraded so it cannot destroy the validated edge.
                        hard = hard_invalidation(
                            chosen["setup"], chosen["indicators"], chosen["recent_bars"]
                        )
                        if hard is None:
                            logger.info(
                                "AI veto on %s downgraded (no hard invalidation): %s",
                                chosen["symbol"], decision.get("reasoning", ""),
                            )
                        else:
                            continue
                    # The AI is veto-only: it may never move the validated stop or
                    # target, because that destroys the momentum edge.

                direction = chosen["direction"]
                is_short = direction == "SELL"
                entry = chosen["entry_price"]
                stop = chosen["stop_loss"]
                target = chosen["target_1"]
                if is_short:
                    if not (target < entry < stop):
                        continue
                elif stop >= entry or target <= entry:
                    continue
                risk_per_share = abs(entry - stop)
                max_risk = day_start_equity * risk_per_trade
                max_position = day_start_equity * exposure_pct
                qty = int(min(max_risk / risk_per_share, max_position / entry))
                # Ensure the entry leg is funded out of cash when running
                # unleveraged (legacy behaviour). With MIS leverage the notional
                # may exceed cash; the exposure cap above is the binding limit.
                if exposure_pct <= max_position_pct + 1e-9:
                    while qty > 0:
                        cost = entry * qty
                        entry_fee = (
                            calculate_sell_fee(cost) if is_short else calculate_buy_fee(cost)
                        )
                        if cost + entry_fee <= cash:
                            break
                        qty -= 1
                if qty <= 0:
                    continue

                cost = round(entry * qty, 2)
                if is_short:
                    entry_fee = calculate_sell_fee(cost)
                    cash = round(cash + cost - entry_fee, 2)
                else:
                    entry_fee = calculate_buy_fee(cost)
                    cash = round(cash - cost - entry_fee, 2)
                open_trade = {
                    "symbol": chosen["symbol"],
                    "date": date_str,
                    "strategy": chosen["setup"].get("name", "confluence"),
                    "strategy_id": chosen["setup"].get("strategy_id", "confluence"),
                    "action": direction,
                    "entry_time": chosen["entry_time"],
                    "entry_price": entry,
                    "stop_loss": stop,
                    "target_1": target,
                    "quantity": qty,
                    "entry_fee": entry_fee,
                    "hold_to_close": bool(chosen["setup"].get("hold_to_close")),
                    "ratchet_levels": chosen.get("ratchet_levels") or [],
                    "ratchet_idx": 0,
                    "validated_by_deepseek": bool(use_deepseek and deepseek_calls),
                }
                day_trades += 1

            # 4. Force flat at the end of the session.
            if open_trade:
                frame = day_frames[open_trade["symbol"]]
                last = frame.iloc[-1]
                exit_price = round(float(last["close"]), 2)
                net = _settle(open_trade, exit_price, "EOD_SQUARE_OFF", str(last["time"]))
                trades.append(open_trade)
                day_realized_pnl += net
                open_trade = None

            day_end_equity = round(cash, 2)
            daily_results.append({
                "date": date_str,
                "start_equity": round(day_start_equity, 2),
                "end_equity": day_end_equity,
                "trading_pnl": round(day_end_equity - day_start_equity, 2),
                "realized_pnl": round(day_realized_pnl, 2),
                "ai_cost_inr": round(day_ai_cost, 2),
                "net_after_ai_inr": round(day_end_equity - day_start_equity - day_ai_cost, 2),
                "trades": day_trades,
                "locked": day_locked,
                "lock_reason": day_lock_reason,
            })

        total_trades = len(trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        gross_pnl = round(sum(t["gross_pnl"] for t in trades), 2)
        fees = round(sum(t["fees_inr"] for t in trades), 2)
        # A strategy_override that makes its own model calls reports its
        # cumulative spend through ai_cost_hook; charge it like the veto path.
        if ai_cost_hook is not None:
            try:
                override_cost = float(ai_cost_hook() or 0.0)
            except Exception as exc:
                logger.warning("ai_cost_hook failed: %s", exc)
                override_cost = 0.0
            if override_cost > 0:
                ai_cost_total += override_cost
                if daily_results:
                    daily_results[-1]["ai_cost_inr"] = round(
                        daily_results[-1]["ai_cost_inr"] + override_cost, 2
                    )
                    daily_results[-1]["net_after_ai_inr"] = round(
                        daily_results[-1]["net_after_ai_inr"] - override_cost, 2
                    )
        net_before_ai = round(gross_pnl - fees, 2)
        net_after_ai = round(net_before_ai - ai_cost_total, 2)
        win_rate = round(len(wins) / total_trades * 100, 1) if total_trades else 0.0
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else (99.0 if gross_profit else 0.0)

        equity_curve = [self.initial_capital]
        running = self.initial_capital
        peak = running
        max_dd = 0.0
        for t in trades:
            running += t["net_pnl"]
            equity_curve.append(running)
            peak = max(peak, running)
            max_dd = max(max_dd, (peak - running) / peak * 100.0 if peak else 0.0)

        strategy_stats: Dict[str, Dict[str, Any]] = {}
        for trade in trades:
            name = trade["strategy"]
            bucket = strategy_stats.setdefault(name, {"trades": 0, "wins": 0, "pnl": 0.0})
            bucket["trades"] += 1
            bucket["wins"] += 1 if trade["won"] else 0
            bucket["pnl"] = round(bucket["pnl"] + trade["net_pnl"], 2)
        for bucket in strategy_stats.values():
            bucket["win_rate_pct"] = round(bucket["wins"] / bucket["trades"] * 100, 1)

        summary = {
            "initial_capital_inr": self.initial_capital,
            "ending_capital_inr": round(self.initial_capital + net_after_ai, 2),
            "gross_pnl_inr": gross_pnl,
            "fees_inr": fees,
            "ai_cost_inr": round(ai_cost_total, 2),
            "net_pnl_before_ai_inr": net_before_ai,
            "net_pnl_after_ai_inr": net_after_ai,
            "net_return_before_ai_pct": round(net_before_ai / self.initial_capital * 100, 2),
            "net_return_after_ai_pct": round(net_after_ai / self.initial_capital * 100, 2),
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 2),
            "deepseek_queries_count": deepseek_calls,
            "interval": interval,
            "time_filter_applied": time_filter,
            "primary_only": primary_only,
            "intraday_leverage_mult": leverage,
            "daily_results": daily_results,
            "strategy_breakdown": strategy_stats,
            "trades": trades,
        }
        logger.info(
            "Backtest complete: %d trades, %.1f%% win rate, gross ₹%.2f, fees ₹%.2f, "
            "AI ₹%.2f, net after AI ₹%.2f (%.2f%%)",
            total_trades, win_rate, gross_pnl, fees, ai_cost_total,
            net_after_ai, summary["net_return_after_ai_pct"],
        )
        return summary


backtest_engine = HistoricalBacktestEngine()
