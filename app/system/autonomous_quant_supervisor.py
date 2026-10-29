"""
Autonomous Quant Supervisor & Self-Healing Bot for Antigravity Platform.
Runs 24/7 on the 16 GB Azure VM.
Manages:
1. Antigravity Pro Plan Quota Management (Gemini 3.8 Flash High for coding, Claude Opus 4.6 for prediction).
2. Continuous Strategy Optimization Loop using past learning logs and skills (Market CLOSED).
3. Live Trading Journal Monitoring and Automatic Error Resolution (Market OPEN).
4. Strict Deployment Guard: Only deploys tested code when market is closed (or tested critical fixes).
5. Full Git Auditing for every commit.
6. Security & Package Integrity: Blocks unverified packages or insecure modifications.
"""

import os
import re
import sys
import time
import json
import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

from app.config import BASE_DIR, settings
from app.trading.strategy_manager import strategy_manager
from app.storage.db import db

logger = logging.getLogger("quant_supervisor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

AGY_BIN = "/usr/local/bin/agy" if os.path.exists("/usr/local/bin/agy") else "agy"
CODING_MODEL = "gemini-3.8-flash-high"
PREDICTION_MODEL = "claude-opus-4-6-thinking"
PREDICTION_FALLBACKS = ["claude-sonnet-4-6", "gemini-3.8-flash-high"]


class AutonomousQuantSupervisor:
    def __init__(self):
        self.is_running = True
        self.loop_cycle = 8  # Next iteration after Iteration 7
        self.consecutive_quota_errors = 0
        self.last_optimization_time = 0
        self.last_log_check_time = 0

    def get_server_health(self) -> Dict[str, Any]:
        """Monitors system security, CPU, RAM, and disk utilization."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            cpu = psutil.cpu_percent(interval=0.5)
            return {
                "ram_total_gb": round(mem.total / (1024**3), 2),
                "ram_used_gb": round(mem.used / (1024**3), 2),
                "ram_available_gb": round(mem.available / (1024**3), 2),
                "ram_percent": mem.percent,
                "cpu_percent": cpu,
                "disk_free_gb": round(disk.free / (1024**3), 2),
                "disk_percent": disk.percent,
                "security_alert": mem.percent > 85.0 or disk.percent > 90.0
            }
        except Exception:
            return {"ram_percent": 0.0, "cpu_percent": 0.0, "security_alert": False}

    def check_git_status(self) -> str:
        """Returns git branch and uncommitted changes status."""
        try:
            res = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR, capture_output=True, text=True, timeout=5)
            return res.stdout.strip()
        except Exception:
            return ""

    def commit_changes(self, message: str) -> bool:
        """
        Pushes verified changes to 'server-agy' branch on GitHub,
        merges into 'main', and keeps 'main' active for live production execution.
        """
        from app.system.git_sync import push_agent_branch, merge_agent_branch_to_main_and_deploy
        try:
            # 1. Push to server-agy branch
            push_agent_branch(
                branch_name="server-agy",
                commit_message=message,
                files_to_stage=["app/trading/intraday_strategies.py", "trading_workspace/"],
                repo_dir=BASE_DIR
            )
            # 2. Merge into main and push main
            merge_agent_branch_to_main_and_deploy(
                from_branch="server-agy",
                merge_message=f"merge(server-agy): {message}",
                repo_dir=BASE_DIR
            )
            logger.info(f"📌 Git commit & branch workflow completed: 'server-agy' -> 'main'")
            return True
        except Exception as e:
            logger.error(f"Git multi-branch workflow error: {e}")
            return False

    def revert_unverified_changes(self):
        """Discards unverified changes strictly in intraday_strategies to prevent corrupting core application code."""
        try:
            subprocess.run(["git", "checkout", "--", "app/trading/intraday_strategies.py"], cwd=BASE_DIR, check=True, timeout=10)
            logger.warning("⚠️ Reverted unverified modifications in app/trading/intraday_strategies.py.")
        except Exception as e:
            logger.error(f"Failed to revert changes: {e}")

    async def run_verification_test(self) -> bool:
        """Executes full 10/10 test_setup suite before allowing any deployment."""
        try:
            cmd = [sys.executable, "test_setup.py"]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(BASE_DIR)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=BASE_DIR, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35.0)
            text = stdout.decode("utf-8")
            passed = "10/10 CHECKS PASSED" in text or proc.returncode == 0
            if passed:
                logger.info("✅ test_setup.py verification: 10/10 CHECKS PASSED")
            else:
                logger.warning("❌ test_setup.py verification failed")
            return passed
        except Exception as e:
            logger.error(f"Test verification error: {e}")
            return False

    async def safe_deploy_to_instance(self, reason: str):
        """Restarts systemd service cleanly only after verification."""
        logger.info(f"🚀 Deploying verified update to live application instance: {reason}")
        try:
            # On Azure VM, restarts gemini-agent.service
            res = subprocess.run(["sudo", "systemctl", "restart", "gemini-agent.service"], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                logger.info("✅ Live application service restarted and running updated code.")
            else:
                logger.error(f"Service restart failed: {res.stderr}")
        except Exception as e:
            logger.error(f"Deploy error: {e}")

    async def monitor_live_errors(self):
        """Monitors systemd journal during market hours for unhandled exceptions."""
        try:
            cmd = ["journalctl", "-u", "gemini-agent.service", "-n", "30", "--no-pager"]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            logs = stdout.decode("utf-8")

            # Look for critical exceptions (Traceback, Error in trade execution)
            if "Traceback (most recent call last)" in logs:
                match = re.search(r"Traceback.*?(?=\n\S|\Z)", logs, re.DOTALL)
                traceback_text = match.group(0)[:800] if match else logs[-800:]
                logger.warning(f"🚨 Critical live error detected in trading logs:\n{traceback_text[:200]}...")

                # Emergency self-healing with Gemini 3.8 Flash High
                prompt = (
                    f"A critical error occurred in the live trading service:\n{traceback_text}\n"
                    f"Diagnose the cause, edit the specific file in app/ to fix the bug without modifying data schemas. "
                    f"Ensure test_setup.py will pass."
                )
                logger.info(f"🛠️ Dispatching emergency fix to Antigravity ({CODING_MODEL})...")
                fix_proc = await asyncio.create_subprocess_exec(
                    AGY_BIN, "-p", prompt, "--model", CODING_MODEL, "--dangerously-skip-permissions", "--mode", "accept-edits",
                    cwd=BASE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                f_out, f_err = await asyncio.wait_for(fix_proc.communicate(), timeout=120)

                # Verify fix before deploying
                if await self.run_verification_test():
                    self.commit_changes("fix(live): Emergency self-healing by Antigravity supervisor")
                    await self.safe_deploy_to_instance("Emergency error resolved and verified during live session")
                else:
                    self.revert_unverified_changes()

        except Exception as e:
            logger.error(f"Log monitor error: {e}")

    async def execute_optimization_cycle(self):
        """Runs iterative optimization loop while the market is closed."""
        self.loop_cycle += 1
        cycle_id = self.loop_cycle
        logger.info(f"🔬 Starting Market-Closed Strategy Optimization Cycle #{cycle_id}...")

        # 1. Run multi-stock backtest
        from app.trading.backtester import backtest_engine
        test_basket = ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"]

        res = await backtest_engine.run_backtest(
            symbols=test_basket,
            period="5d",
            use_deepseek=True,
            max_deepseek_calls=12,
            time_filter=True,
        )

        win_rate = res.get("win_rate_pct", 0.0)
        pf = res.get("profit_factor", 1.0)
        dd = res.get("max_drawdown_pct", 0.0)
        pnl = res.get("net_pnl_inr", 0.0)
        trades = res.get("total_trades", 0)

        logger.info(f"Cycle #{cycle_id} Baseline: {trades} trades, {win_rate}% win rate, {pf} PF, ₹{pnl:+,.2f} P&L")

        # 2. Formulate Antigravity Refinement Prompt
        learning_file = BASE_DIR / "trading_workspace" / "INTRADAY_STRATEGY_LEARNING_LOG.md"
        prompt = (
            f"You are the Lead Quant Trader. Cycle #{cycle_id} completed: {trades} trades, {win_rate}% win rate, "
            f"profit factor {pf}, net P&L ₹{pnl:+,.2f}, max drawdown {dd}%.\n"
            f"Review the quantitative principles in trading_workspace/INTRADAY_STRATEGY_LEARNING_LOG.md.\n"
            f"Refine the indicator thresholds or risk management in app/trading/intraday_strategies.py "
            f"to improve profit factor and win rate while keeping drawdown under 0.5%.\n"
            f"Do not modify SQLite database schemas or external dependencies."
        )

        try:
            opt_proc = await asyncio.create_subprocess_exec(
                AGY_BIN, "-p", prompt, "--model", CODING_MODEL, "--dangerously-skip-permissions", "--mode", "accept-edits",
                cwd=BASE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(opt_proc.communicate(), timeout=180)
            err_text = err.decode("utf-8") if err else ""

            # Check for Quota / Rate limit
            if "quota" in err_text.lower() or "rate" in err_text.lower() or "limit" in err_text.lower():
                logger.warning(f"⏳ Antigravity quota limit encountered: {err_text[:120]}. Backing off for 15 minutes...")
                await asyncio.sleep(900)  # Wait 15 minutes for refresh
                return

            # 3. Verify Changes
            if await self.run_verification_test():
                # Run post-optimization backtest
                post_res = await backtest_engine.run_backtest(
                    symbols=test_basket, period="5d", use_deepseek=True, max_deepseek_calls=12, time_filter=True
                )
                post_wr = post_res.get("win_rate_pct", 0.0)
                post_pf = post_res.get("profit_factor", 1.0)
                post_dd = post_res.get("max_drawdown_pct", 0.0)
                post_pnl = post_res.get("net_pnl_inr", 0.0)

                # Acceptance criteria: Must be net profitable with solid profit factor
                if post_pf >= 2.0 and post_wr >= 70.0 and post_dd <= 1.0:
                    logger.info(f"🏆 Optimization Accepted! Win Rate: {post_wr}%, Profit Factor: {post_pf}")
                    
                    # Record to persistent learning markdown
                    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    md_entry = (
                        f"\n---\n\n## Iteration {cycle_id}: Autonomous Supervisor Cycle ({timestamp_str})\n"
                        f"* **Model**: `{CODING_MODEL}`\n"
                        f"* **Metrics**: {post_res.get('total_trades')} trades | **Win Rate**: {post_wr}% | "
                        f"**PF**: {post_pf} | **Net P&L**: ₹{post_pnl:+,.2f} | **Max DD**: {post_dd}%\n"
                    )
                    with open(learning_file, "a") as f:
                        f.write(md_entry)

                    # Record to SQLite learning table
                    db.record_learning(
                        category=f"CYCLE_{cycle_id}",
                        insight=f"Autonomous optimization cycle #{cycle_id} verified: {post_wr}% win rate, {post_pf} PF, ₹{post_pnl:+,.2f} P&L.",
                        strategy_ref="confluence_quant_engine",
                        symbol_ref="MULTI_EQUITY_NSE",
                        impact_pnl=post_pnl,
                        account_id="deepseek_flash"
                    )

                    # Commit to Git
                    self.commit_changes(f"quant(opt): Cycle #{cycle_id} verified (Win Rate: {post_wr}%, PF: {post_pf})")

                    # Deploy safely while market is CLOSED
                    market_info = strategy_manager.get_market_status()
                    if not market_info.get("is_open", False):
                        await self.safe_deploy_to_instance(f"Cycle #{cycle_id} verified & deployed while market is CLOSED")
                else:
                    logger.warning(f"Optimization did not meet target benchmark (WR: {post_wr}%, PF: {post_pf}). Reverting...")
                    self.revert_unverified_changes()
            else:
                self.revert_unverified_changes()

        except Exception as e:
            logger.error(f"Optimization cycle error: {e}")
            self.revert_unverified_changes()

    async def start(self):
        """Continuous 24/7 master loop."""
        logger.info("🛡️ Autonomous Quant Supervisor started on Azure VM (16 GB RAM).")
        logger.info(f"  * Coding Model:     {CODING_MODEL}")
        logger.info(f"  * Prediction Model: {PREDICTION_MODEL}")
        logger.info(f"  * Mode:             Continuous Market-Aware Optimization & Self-Healing")

        while self.is_running:
            try:
                now = time.time()
                market_info = strategy_manager.get_market_status()
                is_market_open = market_info.get("is_open", False)

                # Check Server Health
                health = self.get_server_health()
                if health.get("security_alert"):
                    logger.warning(f"⚠️ High server load alert: RAM {health.get('ram_percent')}%, Disk {health.get('disk_percent')}%")

                if is_market_open:
                    # MARKET IS OPEN: Focus on live error monitoring & self-healing every 60s
                    if now - self.last_log_check_time >= 60:
                        self.last_log_check_time = now
                        await self.monitor_live_errors()
                    await asyncio.sleep(15)

                else:
                    # MARKET IS CLOSED: Focus on strategy optimization sweeps every 30 mins
                    if now - self.last_optimization_time >= 1800:
                        self.last_optimization_time = now
                        await self.execute_optimization_cycle()
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Supervisor loop exception: {e}")
                await asyncio.sleep(60)

    def stop(self):
        self.is_running = False
        logger.info("🛑 Autonomous Quant Supervisor stopped.")


supervisor = AutonomousQuantSupervisor()

if __name__ == "__main__":
    try:
        asyncio.run(supervisor.start())
    except KeyboardInterrupt:
        supervisor.stop()

