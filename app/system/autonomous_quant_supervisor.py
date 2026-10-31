"""Read-only operations supervisor.

Auto-editing, auto-committing, and auto-deploying are intentionally disabled.
The supervisor may inspect health/logs and run a backtest for the learning log,
but it never changes source code and never restarts a production service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict

from app.config import BASE_DIR
from app.storage.db import db
from app.trading.strategy_manager import strategy_manager

logger = logging.getLogger("quant_supervisor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

AUTO_DEPLOY_ENABLED = os.getenv("SUPERVISOR_AUTO_DEPLOY", "false").lower() in ("true", "1", "yes")


class AutonomousQuantSupervisor:
    def __init__(self):
        self.is_running = True
        self.loop_cycle = 0
        self.last_optimization_time = 0.0
        self.last_log_check_time = 0.0

    def get_server_health(self) -> Dict[str, Any]:
        try:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            return {
                "ram_total_gb": round(mem.total / (1024 ** 3), 2),
                "ram_used_gb": round(mem.used / (1024 ** 3), 2),
                "ram_available_gb": round(mem.available / (1024 ** 3), 2),
                "ram_percent": mem.percent,
                "cpu_percent": psutil.cpu_percent(interval=0.5),
                "disk_free_gb": round(disk.free / (1024 ** 3), 2),
                "disk_percent": disk.percent,
                "security_alert": mem.percent > 85.0 or disk.percent > 90.0,
            }
        except Exception:
            return {"ram_percent": 0.0, "cpu_percent": 0.0, "security_alert": False}

    async def monitor_live_errors(self):
        """Alert on tracebacks without modifying or restarting anything."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", "gemini-agent.service", "-n", "30", "--no-pager",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            logs = stdout.decode("utf-8", errors="replace")
            if "Traceback (most recent call last)" in logs:
                logger.error("Critical traceback detected in service logs. Manual review required.")
        except Exception as exc:
            logger.debug("Log monitor unavailable: %s", exc)

    async def execute_optimization_cycle(self):
        """Run a research backtest and record it. Never edit code or deploy."""
        self.loop_cycle += 1
        from app.trading.backtester import backtest_engine
        basket = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]
        try:
            result = await backtest_engine.run_backtest(
                symbols=basket,
                period="5d",
                use_deepseek=False,
                time_filter=True,
            )
        except Exception as exc:
            logger.error("Research backtest failed: %s", exc)
            return
        pnl = result.get("net_pnl_after_ai_inr", 0.0)
        db.record_learning(
            category=f"RESEARCH_CYCLE_{self.loop_cycle}",
            insight=(
                f"Read-only research cycle: {result.get('total_trades', 0)} trades, "
                f"{result.get('win_rate_pct', 0.0)}% win rate, "
                f"₹{pnl:+,.2f} net after AI, "
                f"{result.get('max_drawdown_pct', 0.0)}% max drawdown."
            ),
            strategy_ref="confluence_quant_engine",
            symbol_ref="MULTI_EQUITY_NSE",
            impact_pnl=pnl,
            account_id="deepseek_flash",
        )
        logger.info(
            "Research cycle #%s complete: net ₹%+.2f. No code changes, commits, or deploys.",
            self.loop_cycle, pnl,
        )

    async def start(self):
        logger.info("Read-only quant supervisor started.")
        logger.info("Auto-deploy: %s", "ENABLED (manual override)" if AUTO_DEPLOY_ENABLED else "DISABLED")
        while self.is_running:
            try:
                now = time.time()
                health = self.get_server_health()
                if health.get("security_alert"):
                    logger.warning("High resource use: %s", health)

                if strategy_manager.is_market_open():
                    if now - self.last_log_check_time >= 60:
                        self.last_log_check_time = now
                        await self.monitor_live_errors()
                    await asyncio.sleep(15)
                else:
                    if now - self.last_optimization_time >= 1800:
                        self.last_optimization_time = now
                        await self.execute_optimization_cycle()
                    await asyncio.sleep(30)
            except Exception as exc:
                logger.error("Supervisor loop error: %s", exc)
                await asyncio.sleep(60)

    def stop(self):
        self.is_running = False


supervisor = AutonomousQuantSupervisor()

if __name__ == "__main__":
    try:
        asyncio.run(supervisor.start())
    except KeyboardInterrupt:
        supervisor.stop()
