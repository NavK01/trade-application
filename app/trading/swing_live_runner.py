"""Live paper-trading runner for the walk-forward validated multi-day swing edge.

Why this exists
---------------
Every intraday arm this project tested was net-negative after Indian statutory
fees (iteration 115/116). The only durable positive-expectancy edge is the
multi-day RSI-2 exhaustion swing (``RSI2OversoldSwing``). It existed only in the
backtester; nothing in the live loop traded it. This runner closes that gap:
it evaluates the frozen ``rsi2_swing_v1`` specification once per NSE session and
opens real paper *delivery* positions on the live market, so the Truth tab can
measure realized fills against the frozen expectation over months.

Hard rules
----------
* The strategy parameters come from ``FROZEN_BASELINE.json`` -- never from the
  class defaults -- so the live track record stays pinned to the frozen spec.
* Positions are marked ``delivery_hold`` so the 15:20 EOD square-off (which
  exists to flatten intraday MIS trades) never touches them.
* Entries happen only inside the NSE entry window and only when the account's
  auto-execute flag is on. Timing out (max-hold) is the only time-based exit;
  stop/target are enforced by the shared protective-exit loop and the broker.
* This runner never places an intraday trade and never touches the legacy
  index/intraday engines.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import pandas as pd

from app.config import ACCOUNT_IDS, DEFAULT_ACCOUNT_ID, settings
from app.storage.cache import cache
from app.storage.db import db
from app.trading import market_hours
from app.trading.backtester import fee_to_risk_pct
from app.trading.execution_engine import execution_engine
from app.trading.paper_truth import FROZEN_BASELINE_PATH, load_frozen_baseline
from app.trading.swing_strategies import RSI2OversoldSwing

logger = logging.getLogger("app.trading.swing_live_runner")

# Liquid NSE large-caps used for the research/walk-forward universe. Kept
# deliberately short and high-quality: the 2-period RSI edge only survives
# after delivery fees when the position is large enough to amortise the flat
# DP sell charge, and concentration (max 2) is part of the frozen spec.
SWING_UNIVERSE: List[str] = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "ITC", "LT", "AXISBANK", "KOTAKBANK",
    "BHARTIARTL", "HINDUNILVR", "MARUTI", "ASIANPAINT", "BAJFINANCE",
    "SUNPHARMA", "TITAN", "WIPRO", "TATAMOTORS", "TATASTEEL",
    "NTPC", "POWERGRID", "COALINDIA", "ONGC", "JSWSTEEL",
    "ULTRACEMCO", "NESTLEIND", "TECHM", "HCLTECH", "ADANIENT",
]


def _ist_date(value: Any) -> Optional[datetime.date]:
    """Parse a stored UTC ISO timestamp and return its IST calendar date."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(market_hours.IST).date()


def _business_days_between(start: datetime.date, end: datetime.date) -> int:
    """Count completed weekdays from ``start`` (exclusive of start day) to end."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += datetime.timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a yfinance daily frame to lowercase OHLCV with a date column."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    out = out.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    required = {"open", "high", "low", "close"}
    if not required.issubset(out.columns):
        return pd.DataFrame()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    if "date" not in out.columns:
        try:
            out["date"] = pd.to_datetime(out.index).strftime("%Y-%m-%d")
        except Exception:
            out["date"] = [str(i) for i in range(len(out))]
    out = out.reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def fetch_daily_bars(symbol: str, period: str = "1y") -> pd.DataFrame:
    """Fetch daily OHLCV bars for an NSE symbol (BSE fallback on empty)."""
    import yfinance as yf

    clean = symbol.strip().upper()
    yf_sym = clean if ("." in clean or "^" in clean) else f"{clean}.NS"
    try:
        raw = yf.Ticker(yf_sym).history(period=period, interval="1d")
        if raw is None or raw.empty:
            if yf_sym.endswith(".NS"):
                raw = yf.Ticker(yf_sym.replace(".NS", ".BO")).history(
                    period=period, interval="1d"
                )
    except Exception as exc:
        logger.warning("Daily bar fetch failed for %s: %s", symbol, exc)
        return pd.DataFrame()
    return _normalize_daily(raw)


class SwingLiveRunner:
    """Daily-entry paper runner for the frozen RSI-2 multi-day swing edge."""

    def __init__(
        self,
        baseline_path: Optional[Path] = None,
        universe: Optional[List[str]] = None,
        data_provider: Optional[Callable[[str], pd.DataFrame]] = None,
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
        price_provider: Optional[Callable[[str], float]] = None,
        broker_engine: Any = None,
        database: Any = None,
    ):
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.interval_seconds = 60
        self.baseline_path = baseline_path or FROZEN_BASELINE_PATH
        self.universe = list(universe or SWING_UNIVERSE)
        self._data_provider = data_provider or fetch_daily_bars
        self._now_provider = now_provider or market_hours.now_ist
        self._price_provider = price_provider or self._default_live_price
        self.engine = broker_engine or execution_engine
        self.db = database or db
        self._last_scan_date: Optional[str] = None

        state = self.db.get_account_state(DEFAULT_ACCOUNT_ID)
        self.auto_execute = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))

    # ------------------------------------------------------------------
    # Status / control
    # ------------------------------------------------------------------
    def get_auto_execute(self, account_id: str) -> bool:
        state = self.db.get_account_state(account_id)
        return bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))

    def _now(self) -> datetime.datetime:
        return market_hours.as_ist(self._now_provider())

    def load_params(self) -> Dict[str, Any]:
        """Return the frozen baseline's strategy/sizing parameters."""
        baseline = load_frozen_baseline(self.baseline_path)
        primary = baseline["primary_strategy"]["params"]
        gates = baseline.get("sizing_and_gates", {})
        return {
            "rsi2_max": float(primary["rsi2_max"]),
            "stop_atr_mult": float(primary["stop_atr_mult"]),
            "target_atr_mult": float(primary["target_atr_mult"]),
            "max_hold_days": int(primary["max_hold_days"]),
            "max_concurrent": int(gates.get("max_concurrent_swing_positions", 2)),
            "min_edge_to_fee": float(gates.get("min_edge_to_fee_ratio", 2.2)),
            "max_fee_to_risk_pct": float(gates.get("max_fee_to_risk_pct", 18.0)),
            "risk_per_trade_pct": float(gates.get("risk_per_trade_pct", 0.75)),
            "capital_inr": float(gates.get("capital_inr", settings.INITIAL_CAPITAL_INR)),
        }

    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._loop())
            logger.info("Started swing live runner (edge: rsi2_oversold_swing).")

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("Stopped swing live runner.")

    async def _loop(self):
        while self.is_running:
            try:
                now = self._now()
                if market_hours.is_market_open(now):
                    await self.manage_positions()
                    if self.auto_execute and market_hours.is_entry_window(now):
                        await self.run_daily_scan(now)
            except Exception as exc:
                logger.error("Swing live runner loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    def _evaluate_symbol(self, symbol: str, params: Mapping[str, Any], allocation_inr: float) -> Optional[Dict[str, Any]]:
        """Return the active swing signal for ``symbol`` using completed daily bars.

        Today's still-forming daily bar is dropped so the signal is computed on
        the last *completed* session, matching the backtester's
        signal-on-completed-bar / fill-next-open convention.
        """
        raw = self._data_provider(symbol)
        df = _normalize_daily(raw)
        if df.empty or len(df) < 60:
            return None
        today_str = market_hours.today_ist_str(self._now_provider())
        if str(df["date"].iloc[-1]) == today_str:
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < 60:
            return None
        sig = RSI2OversoldSwing.evaluate(df, params={
            "rsi2_max": params["rsi2_max"],
            "stop_atr_mult": params["stop_atr_mult"],
            "target_atr_mult": params["target_atr_mult"],
            "max_hold_days": params["max_hold_days"],
            "min_edge_to_fee": params["min_edge_to_fee"],
            "allocation_inr": float(allocation_inr),
        })
        return sig if sig.get("active") else None

    def _today_buy_symbols(self, account_id: str) -> set:
        """Symbols already entered today (IST), to keep the scan idempotent."""
        today = market_hours.today_ist_str(self._now_provider())
        entered = set()
        for trade in self.db.get_trades(account_id=account_id, limit=100):
            if str(trade.get("action", "")).upper() != "BUY":
                continue
            if _ist_date(trade.get("timestamp")) == today:
                entered.add(str(trade.get("symbol", "")).upper())
        return entered

    def _size_position(self, account: Any, entry: float, stop: float, params: Mapping[str, Any]) -> int:
        """Risk-based whole-share size inside per-position and cash budgets."""
        if entry <= 0:
            return 0
        per_pos = float(account.initial_capital) / max(1, int(params["max_concurrent"]))
        risk_budget = float(account.initial_capital) * float(params["risk_per_trade_pct"]) / 100.0
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return 0
        qty = int(min(per_pos / entry, risk_budget / risk_per_share))
        # Never exceed the real cash on hand (delivery buys settle in full).
        from app.trading.costs import calculate_buy_fee

        while qty > 0:
            cost = entry * qty
            if cost + calculate_buy_fee(cost) <= float(account.available_cash) + 0.001:
                break
            qty -= 1
        return max(qty, 0)

    async def run_daily_scan(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """Evaluate the universe once and open up to the concurrency cap."""
        now = market_hours.as_ist(now or self._now_provider())
        result: Dict[str, Any] = {"status": "skipped", "entered": [], "accounts": []}
        if not market_hours.is_trading_day(now):
            result["reason"] = "not a trading day"
            return result
        if not market_hours.is_entry_window(now):
            result["reason"] = "outside the 09:15-15:20 IST entry window"
            return result

        today = now.date().isoformat()
        if self._last_scan_date == today:
            result["status"] = "already_scanned"
            result["reason"] = f"daily scan already completed for {today}"
            return result
        self._last_scan_date = today

        params = self.load_params()
        allocation = float(params["capital_inr"]) / max(1, int(params["max_concurrent"]))

        # Evaluate the universe once; reuse the signals for every active account.
        candidates: List[Dict[str, Any]] = []
        for symbol in self.universe:
            try:
                sig = self._evaluate_symbol(symbol, params, allocation)
            except Exception as exc:
                logger.warning("Swing evaluation failed for %s: %s", symbol, exc)
                continue
            if sig:
                candidates.append({"symbol": symbol, "signal": sig})
        candidates.sort(key=lambda c: float(c["signal"].get("edge_to_fee_ratio", 0.0)), reverse=True)
        result["candidates"] = [c["symbol"] for c in candidates]
        if not candidates:
            result["status"] = "no_signal"
            return result

        for acc_id in ACCOUNT_IDS:
            if not self.get_auto_execute(acc_id):
                continue
            acc = self.engine.get_account(acc_id)
            if acc is None:
                continue
            held = [p for p in acc.positions.values() if getattr(p, "delivery_hold", False)]
            slots = int(params["max_concurrent"]) - len(held)
            if slots <= 0:
                result["accounts"].append({"account_id": acc_id, "entered": [], "reason": "concurrency cap"})
                continue
            entered: List[str] = []
            entered_today = self._today_buy_symbols(acc_id)
            for cand in candidates:
                if slots <= 0:
                    break
                symbol = cand["symbol"]
                sig = cand["signal"]
                if symbol in acc.positions or symbol in entered_today:
                    continue
                entry_price = self._live_price(symbol, sig.get("entry_price"))
                if entry_price <= 0:
                    continue
                atr = float(sig.get("atr_14", 0.0) or 0.0)
                if atr <= 0:
                    continue
                stop = round(max(0.05, entry_price - float(params["stop_atr_mult"]) * atr), 2)
                target = round(entry_price + float(params["target_atr_mult"]) * atr, 2)
                qty = self._size_position(acc, entry_price, stop, params)
                if qty <= 0:
                    continue
                fee_risk = fee_to_risk_pct(entry_price, stop, qty)
                if fee_risk > float(params["max_fee_to_risk_pct"]):
                    logger.info(
                        "Swing skip %s: fee-to-risk %.1f%% > %.1f%%",
                        symbol, fee_risk, params["max_fee_to_risk_pct"],
                    )
                    continue
                order = await self.engine.fast_execute_order(
                    symbol=symbol,
                    action="BUY",
                    quantity=qty,
                    order_type="MARKET",
                    strategy_name=RSI2OversoldSwing.STRATEGY_ID,
                    account_id=acc_id,
                    stop_loss=stop,
                    target_price=target,
                    delivery_hold=True,
                )
                if order.get("status") == "FILLED":
                    entered.append(symbol)
                    entered_today.add(symbol)
                    slots -= 1
                    self.db.record_learning(
                        category="Swing Entry",
                        insight=(
                            f"[{acc_id}] Delivery swing BUY {qty} {symbol} @ ₹{entry_price:.2f} "
                            f"(stop ₹{stop:.2f}, target ₹{target:.2f}, edge/fee "
                            f"{sig.get('edge_to_fee_ratio')}x). Held overnight; excluded from EOD square-off."
                        ),
                        strategy_ref=RSI2OversoldSwing.STRATEGY_ID,
                        symbol_ref=symbol,
                        account_id=acc_id,
                    )
                else:
                    logger.info("Swing order for %s not filled: %s", symbol, order.get("reason"))
            result["entered"].extend(entered)
            result["accounts"].append({"account_id": acc_id, "entered": entered})
        result["status"] = "ok"
        return result

    def _live_price(self, symbol: str, fallback: Optional[float]) -> float:
        try:
            value = float(self._price_provider(symbol) or 0.0)
            if value > 0:
                return value
        except Exception as exc:
            logger.debug("Price provider failed for %s: %s", symbol, exc)
        return float(fallback or 0.0)

    def _default_live_price(self, symbol: str) -> float:
        """Production price source: live cache first, then a verified quote."""
        price = cache.get_price(symbol)
        if price and price > 0:
            return float(price)
        try:
            from app.financial_advisor.india_market import get_indian_stock_quote

            quote = get_indian_stock_quote(symbol)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                value = float(quote["current_price"])
                cache.set_price(symbol, value)
                return value
        except Exception as exc:
            logger.debug("Live price lookup failed for %s: %s", symbol, exc)
        return 0.0

    # ------------------------------------------------------------------
    # Exits (time cap only; stop/target handled by the broker + shared loop)
    # ------------------------------------------------------------------
    async def manage_positions(self) -> List[Dict[str, Any]]:
        params = self.load_params()
        max_hold = int(params["max_hold_days"])
        today = self._now().date()
        closed: List[Dict[str, Any]] = []
        for acc_id in ACCOUNT_IDS:
            acc = self.engine.get_account(acc_id)
            if acc is None:
                continue
            for sym in list(acc.positions.keys()):
                pos = acc.positions.get(sym)
                if pos is None or not getattr(pos, "delivery_hold", False):
                    continue
                entry_date = _ist_date(getattr(pos, "created_at", None))
                if entry_date is None:
                    continue
                if _business_days_between(entry_date, today) >= max_hold:
                    logger.info("[%s] Swing max-hold reached for %s; closing.", acc_id, sym)
                    order = await self.engine.close_position(
                        sym, account_id=acc_id, reason="time_exit"
                    )
                    if order:
                        closed.append(order)
        return closed


swing_live_runner = SwingLiveRunner()
