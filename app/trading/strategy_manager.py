"""Autonomous strategy orchestration with protective exits and daily risk locks."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

from app.config import ACCOUNT_IDS, DEFAULT_ACCOUNT_ID, settings, create_genai_client
from app.financial_advisor.india_market import get_indian_stock_quote, search_indian_market_news
from app.storage.cache import cache
from app.storage.db import db
from app.system.tokens import token_tracker
from app.trading import market_hours
from app.trading.execution_engine import execution_engine
from app.trading.risk_manager import RiskManager
from app.trading.skills.day_trader_adapter import day_trader

logger = logging.getLogger("app.trading.strategy_manager")

BUDGET_WATCHLIST = [
    {"symbol": "ITC", "typical_price": 470.0, "category": "FMCG / High Dividend"},
    {"symbol": "SBIN", "typical_price": 810.0, "category": "Public Sector Banking"},
    {"symbol": "TATAMOTORS", "typical_price": 384.0, "category": "Automotive / EV Growth"},
    {"symbol": "WIPRO", "typical_price": 540.0, "category": "IT / Tech Services"},
    {"symbol": "ICICIBANK", "typical_price": 1260.0, "category": "Private Banking"},
    {"symbol": "INFY", "typical_price": 1850.0, "category": "IT Giant"},
    {"symbol": "RELIANCE", "typical_price": 1280.0, "category": "Energy & Retail Conglomerate"},
]


class StrategyManager:
    def __init__(self):
        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.last_pre_market_report: Optional[Dict[str, Any]] = None
        self._last_pre_market_date: Optional[str] = None
        self._market_open_notified: bool = False
        self._candidate_index: int = 0
        self.risk = RiskManager(db)

    # ------------------------------------------------------------------
    # Compatibility / status
    # ------------------------------------------------------------------
    @property
    def auto_execute(self) -> bool:
        state = db.get_account_state(DEFAULT_ACCOUNT_ID)
        return bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))

    @auto_execute.setter
    def auto_execute(self, enabled: bool):
        self.set_auto_execute(bool(enabled))

    def get_auto_execute(self, account_id: str) -> bool:
        state = db.get_account_state(account_id)
        return bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))

    def is_market_open(self) -> bool:
        return market_hours.is_market_open()

    def is_near_market_close(self) -> bool:
        return market_hours.is_near_market_close()

    def get_market_status(self) -> Dict[str, Any]:
        status = market_hours.get_market_status()
        status["auto_execute_configured"] = {
            acc_id: self.get_auto_execute(acc_id) for acc_id in ACCOUNT_IDS
        }
        status["auto_execute_active"] = bool(status["is_open"] and any(
            self.get_auto_execute(acc_id) for acc_id in ACCOUNT_IDS
        ))
        status["trading_mode"] = getattr(settings, "TRADING_MODE", "paper")
        return status

    def set_auto_execute(self, enabled: bool, account_id: Optional[str] = None) -> bool:
        targets = [account_id] if account_id else list(ACCOUNT_IDS)
        for acc_id in targets:
            state = db.get_account_state(acc_id)
            db.save_account_state(
                acc_id,
                float(state.get("available_cash", settings.INITIAL_CAPITAL_INR)),
                float(state.get("total_realized_pnl", 0.0)),
                auto_execute=enabled,
            )
        try:
            from app.trading.predictor import argon_predictor
            argon_predictor.auto_execute = enabled
        except Exception:
            pass
        logger.info("Auto-execute %s for %s", "enabled" if enabled else "disabled", ", ".join(targets))
        return enabled

    # ------------------------------------------------------------------
    # Pre-market intelligence
    # ------------------------------------------------------------------
    async def run_pre_market_analysis(self) -> Dict[str, Any]:
        query = "Indian stock market pre market Gift Nifty NSE Nifty 50 key stocks to watch today"
        news_res = search_indian_market_news(query, max_results=4)
        articles = news_res.get("articles", [])
        sources = [{"title": a.get("title", ""), "url": a.get("url", "")} for a in articles]
        headlines = "\n".join(
            f"- {a.get('title')}: {a.get('snippet')}" for a in articles[:3]
        )
        grounded_text = ""
        tokens_used = 0

        if settings.DEEPSEEK_API_KEY and not settings.DEEPSEEK_API_KEY.startswith("your_"):
            try:
                client = create_genai_client(settings.DEEPSEEK_API_KEY, location="global")
                prompt = f"""You are a senior quantitative strategist for Indian equities.
Live headlines:
{headlines}

Produce a concise pre-market brief:
1. Macro/Gift Nifty tone.
2. Two or three liquid stocks with a clear BUY or SELL bias.
3. For a ₹10,000 intraday account, state entries, protective stops, and invalidation.
Do not promise returns. Markdown only."""
                response = await client.aio.models.generate_content(
                    model=settings.DEEPSEEK_MODEL,
                    contents=prompt,
                )
                grounded_text = response.text or ""
                usage = getattr(response, "usage_metadata", None)
                prompt_toks = getattr(usage, "prompt_token_count", 350) if usage else 350
                comp_toks = getattr(usage, "candidates_token_count", 400) if usage else 400
                tokens_used = prompt_toks + comp_toks
                token_tracker.log_usage(
                    "pre_market_analysis",
                    settings.DEEPSEEK_MODEL,
                    prompt_toks,
                    comp_toks,
                    account_id="deepseek_flash",
                )
            except Exception as exc:
                logger.warning("DeepSeek pre-market call failed: %s", exc)

        if not grounded_text:
            grounded_text = (
                "### Pre-Market Intelligence Brief\n\n"
                f"**Recent Headlines:**\n{headlines}\n\n"
                "**Execution Plan:** Wait for the 09:15-09:30 opening range. "
                "Only trade a confirmed ORB with volume, a technical stop, and a "
                "maximum 70% position. No trade is preferable to a forced trade."
            )

        report = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "analysis_markdown": grounded_text,
            "sources": sources,
            "tokens_used": tokens_used,
            "recommended_budget_stocks": BUDGET_WATCHLIST[:4],
        }
        self.last_pre_market_report = report
        db.record_learning(
            category="Pre-Market Screening",
            insight="Pre-market scan completed using DeepSeek; risk limits unchanged.",
            strategy_ref="pre_market_gap_scalp",
            account_id="deepseek_flash",
        )
        return report

    # ------------------------------------------------------------------
    # Exits and daily accounting
    # ------------------------------------------------------------------
    async def auto_square_off_all_positions(self, reason: str = "eod_square_off") -> List[Dict[str, Any]]:
        closed_orders: List[Dict[str, Any]] = []
        for acc_id in ACCOUNT_IDS:
            acc = execution_engine.get_account(acc_id)
            if not acc:
                continue
            for sym in list(acc.positions.keys()):
                closed = await execution_engine.close_position(sym, account_id=acc_id, reason=reason)
                if closed:
                    closed_orders.append(closed)
                    logger.info("[%s] Square-off %s: %s", acc_id, sym, closed.get("status"))
        await self.record_daily_snapshots()
        return closed_orders

    async def record_daily_snapshots(self) -> List[Dict[str, Any]]:
        snapshots = []
        date_str = market_hours.today_ist_str()
        for acc_id in ACCOUNT_IDS:
            summary = execution_engine.get_portfolio_summary(acc_id)
            risk = self.risk.get_state(acc_id, execution_engine.get_account(acc_id))
            stats = db.get_trade_stats(acc_id)
            ai_cost = db.get_ai_cost_for_account(acc_id, date_str)
            db.record_daily_snapshot(
                date_str=date_str,
                starting_balance=float(risk["start_equity"]),
                ending_balance=float(summary["total_portfolio_value_inr"]),
                realized_pnl=float(summary["realized_pnl_inr"]),
                trades_count=int(risk["trades_today"]),
                win_rate_pct=float(stats.get("win_rate_pct", 0.0)),
                total_tokens=db.get_today_tokens(acc_id),
                ai_cost_inr=ai_cost,
                account_id=acc_id,
                fees_inr=float(summary["total_fees_paid_inr"]),
            )
            snapshots.append(db.get_daily_performance(acc_id, limit=1)[0])
        return snapshots

    async def evaluate_budget_trade(
        self,
        symbol: str,
        signal: str,
        confidence: int,
        account_id: str = "deepseek_flash",
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.get_auto_execute(account_id):
            return None
        if not market_hours.is_entry_window():
            return {
                "order_id": None,
                "symbol": symbol,
                "action": signal,
                "status": "STANDBY_MARKET_CLOSED",
                "reason": "Entries run only during 09:15-15:20 IST, Monday-Friday.",
                "market_open": False,
            }
        if signal not in ("BUY", "SELL") or confidence < settings.MIN_CONFIDENCE:
            return None

        price = cache.get_price(symbol)
        if not price or price <= 0:
            quote = get_indian_stock_quote(symbol)
            if quote.get("success") and quote.get("current_price", 0) > 0:
                price = float(quote["current_price"])
                cache.set_price(symbol, price)
            else:
                return None

        acc = execution_engine.get_account(account_id)
        summary = execution_engine.get_portfolio_summary(account_id)
        if signal == "SELL":
            pos = acc.positions.get(symbol.upper())
            if not pos:
                return None
            return await execution_engine.fast_execute_order(
                symbol=symbol,
                action="SELL",
                quantity=pos.quantity,
                strategy_name="deepseek_alpha_strategy",
                account_id=account_id,
                exit_reason="signal_exit",
            )

        stop = float(stop_loss or price * 0.992)
        target = float(target_price or price * 1.014)
        budget = day_trader.calculate_budget_size(
            initial_capital=float(acc.initial_capital),
            available_cash=float(acc.available_cash),
            entry_price=price,
            stop_loss=stop,
            max_loss_pct=settings.RISK_PER_TRADE_PCT / 100.0,
            max_position_pct=settings.MAX_POSITION_PCT,
        )
        qty = int(budget.get("quantity", 0))
        if qty <= 0:
            return None
        return await execution_engine.fast_execute_order(
            symbol=symbol,
            action="BUY",
            quantity=qty,
            strategy_name="deepseek_alpha_strategy",
            account_id=account_id,
            stop_loss=stop,
            target_price=target,
        )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------
    def start_background_loop(self):
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._strategy_loop())
            logger.info("Started autonomous strategy manager.")

    def stop_background_loop(self):
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
            logger.info("Stopped autonomous strategy manager.")

    def _select_next_budget_candidate(self) -> str:
        sym = BUDGET_WATCHLIST[self._candidate_index % len(BUDGET_WATCHLIST)]["symbol"]
        self._candidate_index += 1
        return sym

    async def _strategy_loop(self):
        while self.is_running:
            try:
                now = market_hours.now_ist()

                if (
                    now.weekday() < 5
                    and now.hour == 9
                    and 0 <= now.minute < 14
                    and self._last_pre_market_date != now.date().isoformat()
                ):
                    self._last_pre_market_date = now.date().isoformat()
                    await self.run_pre_market_analysis()

                if market_hours.is_market_open():
                    if not self._market_open_notified:
                        self._market_open_notified = True
                        db.record_learning(
                            category="Market Schedule",
                            insight="NSE session open. Protective exits and risk locks are active.",
                            strategy_ref="market_schedule",
                            account_id="deepseek_flash",
                        )

                    # Protective exits run even when auto-entry is disabled.
                    for acc_id in ACCOUNT_IDS:
                        acc = execution_engine.get_account(acc_id)
                        if not acc:
                            continue

                        risk_state = self.risk.get_state(acc_id, acc)
                        if risk_state["locked"] and risk_state["net_pnl_today_inr"] <= risk_state["max_daily_loss_inr"]:
                            if acc.positions:
                                logger.warning("[%s] Daily loss limit hit; flattening all positions.", acc_id)
                                for sym in list(acc.positions.keys()):
                                    await execution_engine.close_position(
                                        sym, account_id=acc_id, reason="daily_loss_limit"
                                    )
                            continue

                        for sym in list(acc.positions.keys()):
                            pos = acc.positions.get(sym)
                            if not pos:
                                continue
                            live_p = cache.get_price(sym)
                            if not live_p or live_p <= 0:
                                quote = get_indian_stock_quote(sym)
                                if quote.get("success") and quote.get("current_price", 0) > 0:
                                    live_p = float(quote["current_price"])
                                    cache.set_price(sym, live_p)
                            if not live_p or live_p <= 0:
                                continue
                            pos.update_price(live_p)

                            exit_reason = None
                            if pos.hold_to_close:
                                # Trend trade: exit only on the protective stop.
                                # The end-of-day square-off handles the rest.
                                if pos.stop_loss and pos.current_price <= pos.stop_loss:
                                    exit_reason = "stop_loss"
                            else:
                                if pos.target_price and pos.current_price >= pos.target_price:
                                    exit_reason = "target"
                                elif pos.stop_loss and pos.current_price <= pos.stop_loss:
                                    exit_reason = "stop_loss"
                                elif pos.unrealized_pnl_pct >= 1.5:
                                    exit_reason = "profit_trail"
                                elif pos.unrealized_pnl_pct <= -1.2:
                                    exit_reason = "hard_stop"
                            if exit_reason:
                                await execution_engine.close_position(
                                    sym, account_id=acc_id, reason=exit_reason
                                )
                else:
                    self._market_open_notified = False

                if market_hours.is_near_market_close():
                    has_positions = any(
                        len(acc.positions) > 0
                        for acc_id in ACCOUNT_IDS
                        if (acc := execution_engine.get_account(acc_id)) is not None
                    )
                    if has_positions:
                        logger.info("15:20 IST square-off window; flattening accounts.")
                        await self.auto_square_off_all_positions("eod_square_off")

            except Exception as exc:
                logger.error("Strategy manager loop error: %s", exc, exc_info=True)

            await asyncio.sleep(10.0)


strategy_manager = StrategyManager()
