"""
Gemini Chatbot Service with Built-in Google Search Grounding and Live Quant Awareness.
Controls server health, RAM monitoring, auto-execution toggling, pre-market screening,
and conversational order routing.
"""

import asyncio
import json
import logging
import re
from typing import Dict, Any, Optional, List
from google.genai import types

from app.config import settings, create_genai_client
from app.trading.execution_engine import execution_engine
from app.trading.predictor import argon_predictor
from app.trading.strategy_manager import strategy_manager
from app.trading.sentiment_analyzer import NSE_SECTOR_MAP
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)
from app.system.metrics import get_system_metrics
from app.system.tokens import token_tracker
from app.storage.db import db
from app.chatbot.prompts import CHATBOT_SYSTEM_PROMPT
from app.chatbot.models import ChatbotRequest, ChatbotResponse, GroundingInfo, GroundingSource, ChatMessage

logger = logging.getLogger("app.chatbot")


class ChatbotService:
    """Core Chatbot orchestrator supporting Gemini models with Google Search grounding and VM control."""

    async def detect_and_execute_trade(self, text: str) -> Optional[Dict[str, Any]]:
        """Detect and execute high-speed market orders from natural language input."""
        buy_match = re.search(r'\b(buy|long)\s+(\d+)\s+([A-Za-z0-9]+)', text, re.IGNORECASE)
        sell_match = re.search(r'\b(sell|short)\s+(\d+)\s+([A-Za-z0-9]+)', text, re.IGNORECASE)
        close_match = re.search(r'\b(close|exit)\s+([A-Za-z0-9]+)', text, re.IGNORECASE)

        if buy_match:
            qty = int(buy_match.group(2))
            sym = buy_match.group(3).upper()
            return await execution_engine.fast_execute_order(sym, "BUY", qty, "MARKET", strategy_name="chatbot_command")
        elif sell_match:
            qty = int(sell_match.group(2))
            sym = sell_match.group(3).upper()
            return await execution_engine.fast_execute_order(sym, "SELL", qty, "MARKET", strategy_name="chatbot_command")
        elif close_match:
            sym = close_match.group(2).upper()
            return await execution_engine.close_position(sym)

        return None

    def _get_live_context(self, trade_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Compile live terminal context for Gemini reasoning."""
        portfolio = execution_engine.get_portfolio_summary()
        latest_pred = argon_predictor.last_prediction
        metrics = get_system_metrics()
        return {
            "portfolio_summary": {
                "initial_capital_inr": portfolio.get("initial_capital_inr", 10000.0),
                "available_cash_inr": portfolio.get("available_cash_inr", 0.0),
                "total_portfolio_value_inr": portfolio.get("total_portfolio_value_inr", 0.0),
                "total_pnl_inr": portfolio.get("total_pnl_inr", 0.0),
                "open_positions": portfolio.get("positions", []),
            },
            "server_system_health": {
                "cpu_percent": metrics["cpu"]["percent"],
                "ram_used_gb": metrics["ram"]["used_gb"],
                "ram_total_gb": metrics["ram"]["total_gb"],
                "ram_percent": metrics["ram"]["percent"],
                "uptime": metrics["uptime"],
                "cache_mode": metrics["cache"]["mode"],
            },
            "auto_execute_active": strategy_manager.auto_execute,
            "latest_argon_prediction": latest_pred,
            "executed_trade": trade_result,
        }

    async def chat(
        self,
        message: str,
        model: Optional[str] = None,
        use_search: bool = True,
        history: Optional[List[ChatMessage]] = None,
    ) -> ChatbotResponse:
        """
        Process chat with selected Gemini model, server control commands, and Google Search grounding.
        """
        clean_text = message.strip()
        selected_model = model or getattr(settings, "GEMINI_CHAT_MODEL", None) or settings.GEMINI_AGENT_MODEL
        portfolio = execution_engine.get_portfolio_summary()

        # ------------------------------------------------------------------
        # 1. System Control: Server, CPU, RAM, & Uptime Status
        # ------------------------------------------------------------------
        if re.search(r'\b(status|server|ram|cpu|hardware|health|system)\b', clean_text, re.IGNORECASE):
            metrics = get_system_metrics()
            token_stats = token_tracker.get_summary(portfolio.get("realized_pnl_inr", 0.0))
            reply = (
                f"### 🖥️ Server & Application Diagnostics\n\n"
                f"- **RAM Usage:** {metrics['ram']['used_gb']} GB / {metrics['ram']['total_gb']} GB ({metrics['ram']['percent']}% used)\n"
                f"- **CPU Load:** {metrics['cpu']['percent']}% across {metrics['cpu']['cores']} cores\n"
                f"- **Disk Storage:** {metrics['disk']['used_gb']} GB / {metrics['disk']['total_gb']} GB ({metrics['disk']['percent']}%)\n"
                f"- **Uptime:** {metrics['uptime']}\n"
                f"- **Fast Cache:** {metrics['cache']['mode'].upper()} (Hit Ratio: {metrics['cache']['hit_ratio_pct']}%)\n"
                f"- **Database:** SQLite `{metrics['database']['size_kb']} KB` ({metrics['database']['path']})\n"
                f"- **Trading Mode:** `{portfolio.get('trading_mode', 'paper_testing')}` (Auto-Execute: `{'ON' if strategy_manager.auto_execute else 'OFF'}`)\n"
                f"- **Dummy Capital:** ₹{portfolio['initial_capital_inr']:,.2f} | Available: ₹{portfolio['available_cash_inr']:,.2f} | Net PnL: ₹{portfolio['total_pnl_inr']:+,.2f}\n"
                f"- **AI Tokens Used:** {token_stats['total_tokens_used']:,} (Cost: ₹{token_stats['estimated_ai_cost_inr']} | Net Profit: ₹{token_stats['net_profit_after_ai_cost_inr']:+,.2f})"
            )
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (System Diagnostic)",
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 2. Trading Control: Stop / Pause Auto-Execute
        # ------------------------------------------------------------------
        if re.search(r'\b(stop|pause|disable)\s+(trading|auto|execution)\b|\bstop\s+auto\b', clean_text, re.IGNORECASE):
            strategy_manager.set_auto_execute(False)
            argon_predictor.auto_execute = False
            reply = (
                f"🛑 **Auto-Trading Paused.**\n\n"
                f"Automated order placement has been disabled.\n"
                f"- Open Positions: {portfolio['open_positions_count']}\n"
                f"- Current Unrealized PnL: ₹{portfolio['unrealized_pnl_inr']:+,.2f}\n"
                f"- Available Cash: ₹{portfolio['available_cash_inr']:,.2f}\n"
                f"You can type `\"Close all\"` or `\"Close [symbol]\"` to square off positions, or `\"Start trading\"` to resume."
            )
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Trading Controller)",
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 3. Trading Control: Start / Enable Auto-Execute
        # ------------------------------------------------------------------
        if re.search(r'\b(start|enable|resume)\s+(trading|auto|execution)\b|\bstart\s+auto\b', clean_text, re.IGNORECASE):
            strategy_manager.set_auto_execute(True)
            argon_predictor.auto_execute = True
            reply = (
                f"🚀 **Auto-Trading Activated!**\n\n"
                f"The autonomous engine is active. It will:\n"
                f"1. Screen budget-friendly stocks (Tata Motors, SBI, ITC, Wipro, Infosys) that fit within your ₹{portfolio['initial_capital_inr']:,.2f} account.\n"
                f"2. Size trades safely (max 35% of funds per position) and place fast BUY/SELL orders on signals >= 70% confidence.\n"
                f"3. Automatically square off intraday trades before market close (15:20 IST) to lock in PnL."
            )
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Trading Controller)",
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 4. Pre-Market Google Search Screening
        # ------------------------------------------------------------------
        if re.search(r'\b(pre[\s-]?market|scan stocks|morning scan|market analysis)\b', clean_text, re.IGNORECASE):
            report = await strategy_manager.run_pre_market_analysis()
            grounding_sources = [GroundingSource(title=s["title"], url=s["url"]) for s in report.get("sources", [])]
            return ChatbotResponse(
                reply=report["analysis_markdown"],
                model_used=f"{selected_model} (Pre-Market Scanner)",
                portfolio=portfolio,
                grounding=GroundingInfo(
                    search_enabled=True,
                    search_queries=["Indian stock market pre market Gift Nifty key stocks"],
                    sources=grounding_sources
                ),
                success=True,
            )

        # ------------------------------------------------------------------
        # 5. Strategies, Success Rate & Learning Retrospective
        # ------------------------------------------------------------------
        if re.search(r'\b(strategies|strategy|win\s?rate|learning|lessons)\b', clean_text, re.IGNORECASE):
            strats = db.get_strategies()
            logs = db.get_learning_logs(limit=3)
            strat_table = "| Strategy | Win Rate | Total Trades | P&L (₹) | Status |\n|---|---|---|---|---|\n"
            for s in strats:
                status_icon = "🟢 Active" if s["is_active"] else "⚪ Inactive"
                strat_table += f"| **{s['name']}** | {s['win_rate_pct']}% | {s['total_trades']} | ₹{s['total_pnl_inr']:+,.2f} | {status_icon} |\n"

            learning_text = "\n".join([f"- **[{l['category']}]** {l['insight']}" for l in logs]) if logs else "No learning logs yet. Trades will generate learning insights as they close."

            reply = f"### 📊 Strategy Performance & Win Rates\n\n{strat_table}\n\n### 🧠 Recent Autonomous Learning Insights\n{learning_text}"
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Quant Registry)",
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 6. Token Usage & AI Operational Profit Calculation
        # ------------------------------------------------------------------
        if re.search(r'\b(tokens?|ai\s?costs?|costs?|net\s?profit)\b', clean_text, re.IGNORECASE):
            summary = token_tracker.get_summary(portfolio.get("realized_pnl_inr", 0.0))
            breakdown_str = "\n".join([f"- `{b['model']}`: {b['tokens']:,} tokens across {b['calls']} calls (₹{b['cost_inr']})" for b in summary["breakdown_by_model"]]) or "No usage recorded yet."

            reply = (
                f"### 🪙 Token Accounting & Profit Analysis\n\n"
                f"- **Total Tokens Consumed:** {summary['total_tokens_used']:,} tokens\n"
                f"- **Estimated AI Operational Cost:** ₹{summary['estimated_ai_cost_inr']:,.2f}\n"
                f"- **Realized Trading PnL:** ₹{summary['realized_trading_pnl_inr']:+,.2f}\n"
                f"- **Net Profit (Trading PnL - AI Costs):** **₹{summary['net_profit_after_ai_cost_inr']:+,.2f}**\n\n"
                f"**Model Breakdown:**\n{breakdown_str}"
            )
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Token Accounting)",
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 7. Reset Account Balance (e.g. re-seed with ₹10,000)
        # ------------------------------------------------------------------
        if re.search(r'\b(reset|refund)\s+(account|credits|balance|capital)\b', clean_text, re.IGNORECASE):
            new_port = execution_engine.reset_account(settings.INITIAL_CAPITAL_INR)
            reply = f"🔄 **Account Reset Complete.**\nDummy capital re-seeded to **₹{new_port['initial_capital_inr']:,.2f}**. All positions and PnL reset for fresh testing."
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Account Manager)",
                portfolio=new_port,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        # ------------------------------------------------------------------
        # 8. Fast Order Execution Command (Buy, Sell, Close)
        # ------------------------------------------------------------------
        trade_result = await self.detect_and_execute_trade(clean_text)
        portfolio = execution_engine.get_portfolio_summary()
        context = self._get_live_context(trade_result)

        api_key = settings.GEMINI_API_KEY
        if not api_key or api_key.startswith("your_"):
            if trade_result:
                reply = (
                    f"⚡ Fast Order Executed: {trade_result.get('action')} {trade_result.get('quantity')} "
                    f"{trade_result.get('symbol')} at ₹{trade_result.get('fill_price')} in {trade_result.get('latency_ms')}ms! "
                    f"(Status: {trade_result.get('status')})"
                )
            else:
                reply = (
                    f"Live terminal active. Available Cash: ₹{portfolio['available_cash_inr']:,.2f} with total PnL: ₹{portfolio['total_pnl_inr']:+,.2f}. "
                    f"(Note: Configure your GEMINI_API_KEY in .env for full Gemini reasoning & Google Search)."
                )
            return ChatbotResponse(
                reply=reply,
                model_used=f"{selected_model} (Local Simulation)",
                trade_result=trade_result,
                portfolio=portfolio,
                grounding=GroundingInfo(search_enabled=False),
                success=True,
            )

        client = create_genai_client(api_key, location="global")

        # Grounding with live Indian market data & news if requested or relevant
        grounded_context: Dict[str, Any] = {}
        grounding_sources: List[GroundingSource] = []

        if use_search:
            # 1. Detect if any known Indian stock symbol was mentioned
            extracted_words = re.findall(r'\b[A-Za-z]{2,12}\b', clean_text.upper())
            top_stocks = [
                "RELIANCE", "TCS", "INFY", "TATAMOTORS", "HDFCBANK", "SBIN", "ITC",
                "BHARTIARTL", "LT", "ICICIBANK", "KOTAKBANK", "MARUTI", "WIPRO",
                "TITAN", "BAJFINANCE", "ASIANPAINT", "BEL", "HAL", "NTPC", "POWERGRID"
            ]
            found_symbols = [s for s in extracted_words if s in top_stocks]
            if not found_symbols:
                # Check for common stock names
                name_map = {"RELIANCE": "RELIANCE", "TATA": "TATAMOTORS", "SBI": "SBIN", "HDFC": "HDFCBANK", "INFOSYS": "INFY"}
                for k, v in name_map.items():
                    if k in clean_text.upper():
                        found_symbols.append(v)

            # Fetch live verified quote for mentioned stocks
            for sym in found_symbols[:2]:
                try:
                    q = get_indian_stock_quote(sym)
                    if q.get("success") and q.get("current_price", 0) > 0:
                        grounded_context[f"live_quote_{sym}"] = {
                            "symbol": sym,
                            "current_price_inr": q["current_price"],
                            "change_percent": q.get("change_percent", 0.0),
                            "day_high": q.get("day_high"),
                            "day_low": q.get("day_low"),
                        }
                        grounding_sources.append(GroundingSource(
                            title=f"NSE Live Quote: {sym} (₹{q['current_price']})",
                            url=f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"
                        ))
                except Exception:
                    pass

            # Fetch real-time market indices
            try:
                loop = asyncio.get_event_loop()
                idx_data = await asyncio.wait_for(loop.run_in_executor(None, get_indian_market_indices), timeout=3.0)
                if idx_data.get("indices"):
                    grounded_context["benchmark_indices"] = idx_data["indices"]
            except Exception:
                pass

            # Fetch live market news if query is research or trend oriented
            if any(w in clean_text.lower() for w in ["news", "why", "trend", "sector", "today", "market", "fall", "rise", "boom", "earnings"]):
                try:
                    loop = asyncio.get_event_loop()
                    query = f"{found_symbols[0]} NSE stock news" if found_symbols else "Indian stock market NSE today trends"
                    news_res = await asyncio.wait_for(loop.run_in_executor(None, search_indian_market_news, query), timeout=4.0)
                    articles = news_res.get("articles", [])[:3]
                    if articles:
                        grounded_context["live_news_articles"] = [{"title": a.get("title"), "snippet": a.get("snippet")} for a in articles]
                        for a in articles:
                            grounding_sources.append(GroundingSource(title=a.get("title", "Market Intelligence"), url=a.get("link", "https://news.google.com")))
                except Exception:
                    pass

        # Assemble prompt with multi-turn history
        prompt_parts = [
            f"LIVE TERMINAL CONTEXT:\n{json.dumps(context, indent=2)}\n\n"
        ]
        if grounded_context:
            prompt_parts.append(f"VERIFIED LIVE MARKET DATA & NEWS:\n{json.dumps(grounded_context, indent=2)}\n\n")

        if history:
            prompt_parts.append("CONVERSATION HISTORY:\n")
            for h in history[-6:]:
                prompt_parts.append(f"{h.role.upper()}: {h.content}\n")
            prompt_parts.append("\n")

        prompt_parts.append(f"USER MESSAGE:\n\"{clean_text}\"\n\nProvide an actionable quant response in INR (₹):")
        final_prompt = "".join(prompt_parts)

        config = types.GenerateContentConfig(
            system_instruction=CHATBOT_SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=300,
        )

        ds_config = types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=300,
        )

        reply_text = None
        model_used = selected_model
        is_deepseek = "deepseek" in selected_model.lower()

        # 1. If DeepSeek is chosen, query DeepSeek Flash MaaS directly
        if is_deepseek:
            try:
                ds_client = create_genai_client(location="global")
                ds_prompt = f"{CHATBOT_SYSTEM_PROMPT}\n\n{final_prompt}"
                ds_resp = await asyncio.wait_for(
                    ds_client.aio.models.generate_content(
                        model="deepseek-ai/deepseek-v4.1-flash-maas",
                        contents=ds_prompt,
                        config=ds_config,
                    ),
                    timeout=25.0
                )
                if ds_resp.text:
                    reply_text = ds_resp.text.strip()
                    model_used = "deepseek-ai/deepseek-v4.1-flash-maas"
            except Exception as ds_err:
                logger.warning(f"DeepSeek chatbot error: {type(ds_err).__name__} {ds_err}")
        else:
            # 2. Parallel Dual-Model Race: Launch both Gemini 3.8 Flash & DeepSeek Flash MaaS concurrently
            # The fastest model to finish delivers the answer immediately to eliminate user wait time!
            ds_client = create_genai_client(location="global")
            ds_prompt = f"{CHATBOT_SYSTEM_PROMPT}\n\n{final_prompt}"

            async def _query_gemini():
                r = await client.aio.models.generate_content(
                    model=selected_model,
                    contents=final_prompt,
                    config=config,
                )
                if r.text:
                    return (selected_model, r.text.strip())
                elif r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
                    pts = [p.text for p in r.candidates[0].content.parts if getattr(p, "text", None)]
                    if pts:
                        return (selected_model, "".join(pts).strip())
                raise ValueError("Empty Gemini response text")

            async def _query_deepseek():
                r = await ds_client.aio.models.generate_content(
                    model="deepseek-ai/deepseek-v4.1-flash-maas",
                    contents=ds_prompt,
                    config=ds_config,
                )
                if r.text:
                    return ("deepseek-ai/deepseek-v4.1-flash-maas", r.text.strip())
                raise ValueError("Empty DeepSeek response text")

            task_g = asyncio.create_task(_query_gemini())
            task_d = asyncio.create_task(_query_deepseek())

            try:
                done, pending = await asyncio.wait(
                    [task_g, task_d],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=25.0
                )

                # Check completed tasks
                for t in done:
                    try:
                        m_name, m_text = t.result()
                        if m_text:
                            reply_text = m_text
                            model_used = m_name
                            break
                    except Exception as task_err:
                        logger.debug(f"Candidate model error: {task_err}")

                # If first completed task had an error but pending is still running, wait for remaining task
                if not reply_text and pending:
                    done_second, _ = await asyncio.wait(pending, timeout=8.0)
                    for t in done_second:
                        try:
                            m_name, m_text = t.result()
                            if m_text:
                                reply_text = m_text
                                model_used = m_name
                                break
                        except Exception:
                            pass

                # Cancel remaining pending tasks
                for p in pending:
                    if not p.done():
                        p.cancel()
            except Exception as race_err:
                logger.warning(f"Dual-model race timeout: {race_err}")

        # 3. Deterministic Local Quant Synthesizer if both external AI APIs fail
        if not reply_text:
            market_open = strategy_manager.is_market_open()
            reply_text = (
                f"### 📊 Live Terminal & Market Intelligence\n\n"
                f"- **Account Balance:** ₹{portfolio.get('available_cash_inr', 10000.0):,.2f} INR (Available)\n"
                f"- **Realized PnL:** ₹{portfolio.get('total_pnl_inr', 0.0):+,.2f}\n"
                f"- **Open Positions:** {portfolio.get('open_positions_count', 0)}\n"
                f"- **NSE Market Status:** `{'OPEN' if market_open else 'CLOSED (Next Open: 09:15 IST)'}`\n\n"
                f"Real-time market feeds and high-speed order execution remain fully active."
            )
            model_used = f"{selected_model} (Local Quant Synthesizer)"

        # Token Accounting
        token_tracker.log_usage("chatbot_query", model_used, 350, 200)

        grounding_info = GroundingInfo(
            search_enabled=bool(grounding_sources),
            sources=grounding_sources,
        )

        return ChatbotResponse(
            reply=reply_text,
            response=reply_text,
            model_used=model_used,
            trade_result=trade_result,
            portfolio=portfolio,
            grounding=grounding_info,
            success=True,
        )


chatbot_service = ChatbotService()


async def process_chat_message(
    user_message: str,
    model: Optional[str] = None,
    use_search: bool = True
) -> Dict[str, Any]:
    """Compatibility wrapper returning dict matching original process_chat_message API."""
    res = await chatbot_service.chat(user_message, model=model, use_search=use_search)
    return {
        "reply": res.reply,
        "trade_result": res.trade_result,
        "portfolio": res.portfolio,
        "model_used": res.model_used,
        "grounding": res.grounding.model_dump(),
        "success": res.success,
        "error": res.error,
    }
