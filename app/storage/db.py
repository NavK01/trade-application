"""
SQLite persistence for the quant terminal.

The database stores two independent paper accounts:
  * opus_flash     - Claude Opus 4.6 Thinking benchmark account
  * deepseek_flash - DeepSeek 4.1 Flash execution account

All money is stored in INR. Trading P&L is kept separate from AI operating
cost so the dashboard can show gross P&L, fees, AI cost, and net P&L after AI.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Generator

from app.config import ACCOUNT_IDS, ACCOUNT_NAMES, DEFAULT_ACCOUNT_ID, settings
from app.trading import market_hours

logger = logging.getLogger("app.storage.db")


def _today_ist_prefix() -> str:
    return market_hours.today_ist_str()


class DatabaseManager:
    """Thread-safe SQLite manager for accounts, trades, predictions, and costs."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.absolute_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY,
                    account_id TEXT UNIQUE NOT NULL,
                    account_name TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    total_realized_pnl REAL DEFAULT 0.0,
                    auto_execute INTEGER DEFAULT 0,
                    updated_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    fill_price REAL NOT NULL,
                    total_value_inr REAL NOT NULL,
                    fee_inr REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    strategy_name TEXT,
                    pnl_inr REAL DEFAULT 0.0,
                    gross_pnl_inr REAL DEFAULT 0.0,
                    entry_fee_inr REAL DEFAULT 0.0,
                    exit_price REAL,
                    exit_timestamp TEXT,
                    exit_reason TEXT,
                    timestamp TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategies (
                    strategy_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rules TEXT,
                    description TEXT,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_pnl_inr REAL DEFAULT 0.0,
                    win_rate_pct REAL DEFAULT 0.0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    last_run TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    confidence_pct INTEGER,
                    current_price REAL,
                    target_1 REAL,
                    stop_loss REAL,
                    catalyst TEXT,
                    action_summary TEXT,
                    timeframe TEXT,
                    model TEXT,
                    tokens_used INTEGER DEFAULT 0,
                    auto_executed INTEGER DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    insight TEXT NOT NULL,
                    strategy_ref TEXT,
                    symbol_ref TEXT,
                    impact_pnl REAL DEFAULT 0.0,
                    timestamp TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_performance (
                    date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    starting_balance REAL NOT NULL,
                    ending_balance REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    gross_pnl REAL DEFAULT 0.0,
                    fees_inr REAL DEFAULT 0.0,
                    trades_count INTEGER DEFAULT 0,
                    win_rate_pct REAL DEFAULT 0.0,
                    total_tokens_used INTEGER DEFAULT 0,
                    estimated_ai_cost_inr REAL DEFAULT 0.0,
                    net_profit_before_ai REAL DEFAULT 0.0,
                    net_profit_after_ai_cost REAL DEFAULT 0.0,
                    net_return_pct REAL DEFAULT 0.0,
                    PRIMARY KEY (date, account_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT DEFAULT 'shared',
                    module TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost_inr REAL DEFAULT 0.0,
                    timestamp TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    entry_fee_inr REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    stop_loss REAL,
                    target_price REAL,
                    hold_to_close INTEGER DEFAULT 0,
                    PRIMARY KEY (account_id, symbol)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    start_equity REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0.0,
                    ai_cost_inr REAL DEFAULT 0.0,
                    trades_count INTEGER DEFAULT 0,
                    locked INTEGER DEFAULT 0,
                    lock_reason TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (date, account_id)
                )
            """)

            self._run_schema_migrations(cursor)
            self._migrate_legacy_account(cursor)
            conn.commit()

        self._seed_default_state()
        self._seed_default_strategies()

    def _run_schema_migrations(self, cursor: sqlite3.Cursor):
        def columns(table: str) -> List[str]:
            cursor.execute(f"PRAGMA table_info({table})")
            return [r["name"] for r in cursor.fetchall()]

        account_cols = columns("account_state")
        if "account_name" not in account_cols:
            cursor.execute("ALTER TABLE account_state ADD COLUMN account_name TEXT DEFAULT 'Claude Opus 4.6 Thinking'")

        trade_cols = columns("trades")
        for name, ddl in [
            ("gross_pnl_inr", "REAL DEFAULT 0.0"),
            ("entry_fee_inr", "REAL DEFAULT 0.0"),
            ("exit_reason", "TEXT"),
        ]:
            if name not in trade_cols:
                cursor.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")

        position_cols = columns("open_positions")
        for name, ddl in [
            ("entry_fee_inr", "REAL DEFAULT 0.0"),
            ("stop_loss", "REAL"),
            ("target_price", "REAL"),
            ("hold_to_close", "INTEGER DEFAULT 0"),
        ]:
            if name not in position_cols:
                cursor.execute(f"ALTER TABLE open_positions ADD COLUMN {name} {ddl}")

        token_cols = columns("token_usage")
        if "account_id" not in token_cols:
            cursor.execute("ALTER TABLE token_usage ADD COLUMN account_id TEXT DEFAULT 'shared'")

        daily_cols = columns("daily_performance")
        for name, ddl in [
            ("gross_pnl", "REAL DEFAULT 0.0"),
            ("fees_inr", "REAL DEFAULT 0.0"),
            ("net_profit_before_ai", "REAL DEFAULT 0.0"),
            ("net_return_pct", "REAL DEFAULT 0.0"),
        ]:
            if name not in daily_cols:
                cursor.execute(f"ALTER TABLE daily_performance ADD COLUMN {name} {ddl}")

        # Attribute historical DeepSeek/backtest costs rather than dumping them
        # onto a trading account.
        cursor.execute("UPDATE token_usage SET account_id = 'deepseek_flash' WHERE account_id = 'shared' AND module LIKE '%deepseek%'")
        cursor.execute("UPDATE token_usage SET account_id = 'backtest' WHERE account_id = 'shared' AND module LIKE '%backtest%'")
        cursor.execute("UPDATE token_usage SET account_id = 'opus_flash' WHERE account_id = 'shared' AND module LIKE '%opus%'")

    def _migrate_legacy_account(self, cursor: sqlite3.Cursor):
        """Rename the old Gemini account ID without losing paper-trading history."""
        cursor.execute("SELECT COUNT(*) AS n FROM account_state WHERE account_id = 'opus_flash'")
        opus_exists = cursor.fetchone()["n"] > 0
        cursor.execute("SELECT COUNT(*) AS n FROM account_state WHERE account_id = 'gemini_flash'")
        legacy_exists = cursor.fetchone()["n"] > 0

        if legacy_exists:
            if opus_exists:
                cursor.execute("DELETE FROM account_state WHERE account_id = 'gemini_flash'")
            else:
                cursor.execute(
                    "UPDATE account_state SET account_id = 'opus_flash', account_name = ? WHERE account_id = 'gemini_flash'",
                    (ACCOUNT_NAMES["opus_flash"],),
                )

        for table in ("trades", "predictions", "learning_log", "token_usage"):
            cursor.execute(f"UPDATE {table} SET account_id = 'opus_flash' WHERE account_id = 'gemini_flash'")

        cursor.execute("UPDATE OR IGNORE open_positions SET account_id = 'opus_flash' WHERE account_id = 'gemini_flash'")
        cursor.execute("DELETE FROM open_positions WHERE account_id = 'gemini_flash'")
        cursor.execute("UPDATE OR IGNORE daily_performance SET account_id = 'opus_flash' WHERE account_id = 'gemini_flash'")
        cursor.execute("DELETE FROM daily_performance WHERE account_id = 'gemini_flash'")
        cursor.execute("UPDATE OR IGNORE risk_state SET account_id = 'opus_flash' WHERE account_id = 'gemini_flash'")
        cursor.execute("DELETE FROM risk_state WHERE account_id = 'gemini_flash'")

    def _seed_default_state(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            init_cap = settings.INITIAL_CAPITAL_INR
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for idx, account_id in enumerate(ACCOUNT_IDS, start=1):
                name = ACCOUNT_NAMES[account_id]
                cursor.execute("SELECT COUNT(*) AS n FROM account_state WHERE account_id = ?", (account_id,))
                if cursor.fetchone()["n"] == 0:
                    cursor.execute("""
                        INSERT INTO account_state
                        (id, account_id, account_name, initial_capital, available_cash, total_realized_pnl, auto_execute, updated_at)
                        VALUES (?, ?, ?, ?, ?, 0.0, ?, ?)
                    """, (
                        idx, account_id, name, init_cap, init_cap,
                        1 if settings.AUTO_EXECUTE_DEFAULT else 0, now,
                    ))
                else:
                    # Preserve the user's auto-execute choice on restart.
                    cursor.execute(
                        "UPDATE account_state SET account_name = ?, updated_at = ? WHERE account_id = ?",
                        (name, now, account_id),
                    )
            conn.commit()

    def _seed_default_strategies(self):
        default_strategies = [
            {
                "strategy_id": "opus_momentum",
                "name": "Opus Momentum",
                "rules": "Intraday momentum with VWAP support, volume confirmation, and ATR-based risk.",
                "description": "Claude Opus 4.6 benchmark strategy for high-conviction Indian equity momentum.",
                "is_active": 1,
            },
            {
                "strategy_id": "deepseek_flash_scalp",
                "name": "DeepSeek Flash Scalper",
                "rules": "Mean reversion and 20-EMA pullback with 0.8% stop and 1.6% target.",
                "description": "DeepSeek 4.1 model focusing on liquid high-beta equity scalps.",
                "is_active": 1,
            },
            {
                "strategy_id": "orb_breakout_day_trader",
                "name": "Opening Range Breakout (ORB-15m)",
                "rules": "15-minute high/low breakout with volume > 1.3x. ORB midpoint stop.",
                "description": "Institutional intraday breakout using the first 15 minutes of the NSE session.",
                "is_active": 1,
            },
            {
                "strategy_id": "vwap_institutional_bounce",
                "name": "VWAP Institutional Bounce & Expansion",
                "rules": "Pullback to VWAP with a bullish reversal candle and trend confirmation.",
                "description": "Exploits institutional liquidity anchored at intraday VWAP.",
                "is_active": 1,
            },
            {
                "strategy_id": "ema_ribbon_pullback",
                "name": "EMA Ribbon Momentum Scalper",
                "rules": "9 EMA > 20 EMA > 50 EMA alignment with a 20-EMA pullback.",
                "description": "Momentum continuation scalper during active trend phases.",
                "is_active": 1,
            },
            {
                "strategy_id": "supertrend_trend_flip",
                "name": "SuperTrend Dynamic Trend Following",
                "rules": "ATR(10, 3) bullish flip with price above VWAP.",
                "description": "Volatility-adjusted trend following with dynamic stops.",
                "is_active": 1,
            },
            {
                "strategy_id": "confluence_quant_engine",
                "name": "Multi-Strategy Confluence Quant Engine",
                "rules": "Composite score with trend, volume, VWAP, and momentum confirmation.",
                "description": "High-conviction filter that rejects low-probability chop.",
                "is_active": 1,
            },
            {
                "strategy_id": "bollinger_volume_breakout",
                "name": "Bollinger Bands & Volume Surge",
                "rules": "Volatility expansion with volume confirmation and mean-reversion exits.",
                "description": "Volatility scalper for high-beta Indian equities.",
                "is_active": 1,
            },
        ]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for s in default_strategies:
                cursor.execute("""
                    INSERT OR IGNORE INTO strategies
                    (strategy_id, name, rules, description, total_trades, winning_trades,
                     losing_trades, total_pnl_inr, win_rate_pct, is_active, created_at, last_run)
                    VALUES (?, ?, ?, ?, 0, 0, 0, 0.0, 0.0, ?, ?, ?)
                """, (s["strategy_id"], s["name"], s["rules"], s["description"], s["is_active"], now, now))
            cursor.execute(
                "UPDATE strategies SET name = 'Opus Momentum' WHERE strategy_id = 'argon_ai_momentum'"
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def get_account_state(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM account_state WHERE account_id = ?", (account_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return {
            "account_id": account_id,
            "account_name": ACCOUNT_NAMES.get(account_id, account_id.replace("_", " ").title()),
            "initial_capital": settings.INITIAL_CAPITAL_INR,
            "available_cash": settings.INITIAL_CAPITAL_INR,
            "total_realized_pnl": 0.0,
            "auto_execute": 0,
        }

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM account_state ORDER BY id ASC")
            return [dict(r) for r in cursor.fetchall()]

    def save_account_state(self, *args, **kwargs):
        """
        Supports both legacy call styles:
          save_account_state(cash, pnl, auto_execute=None, account_id="...")
          save_account_state(account_id, cash, pnl, auto_execute=None)
        """
        if args and isinstance(args[0], str):
            account_id = args[0]
            available_cash = args[1]
            total_realized_pnl = args[2] if len(args) > 2 else 0.0
            auto_execute = args[3] if len(args) > 3 else kwargs.get("auto_execute")
        else:
            available_cash = args[0] if len(args) > 0 else kwargs.get("available_cash", settings.INITIAL_CAPITAL_INR)
            total_realized_pnl = args[1] if len(args) > 1 else kwargs.get("total_realized_pnl", 0.0)
            auto_execute = args[2] if len(args) > 2 else kwargs.get("auto_execute")
            account_id = kwargs.get("account_id", DEFAULT_ACCOUNT_ID)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if auto_execute is not None:
                cursor.execute("""
                    UPDATE account_state
                    SET available_cash = ?, total_realized_pnl = ?, auto_execute = ?, updated_at = ?
                    WHERE account_id = ?
                """, (round(available_cash, 2), round(total_realized_pnl, 2), 1 if auto_execute else 0, now, account_id))
            else:
                cursor.execute("""
                    UPDATE account_state
                    SET available_cash = ?, total_realized_pnl = ?, updated_at = ?
                    WHERE account_id = ?
                """, (round(available_cash, 2), round(total_realized_pnl, 2), now, account_id))
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT INTO account_state
                    (account_id, account_name, initial_capital, available_cash, total_realized_pnl, auto_execute, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    account_id,
                    ACCOUNT_NAMES.get(account_id, account_id.replace("_", " ").title()),
                    settings.INITIAL_CAPITAL_INR,
                    round(available_cash, 2),
                    round(total_realized_pnl, 2),
                    1 if auto_execute else 0,
                    now,
                ))
            conn.commit()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def save_position(self, *args, **kwargs):
        stop_loss = kwargs.get("stop_loss")
        target_price = kwargs.get("target_price")
        entry_fee_inr = kwargs.get("entry_fee_inr", 0.0)
        hold_to_close = kwargs.get("hold_to_close", 0)
        if len(args) >= 5:
            account_id, symbol, quantity, avg_price, created_at = args[:5]
            if len(args) >= 6:
                stop_loss = args[5]
            if len(args) >= 7:
                target_price = args[6]
            if len(args) >= 8:
                entry_fee_inr = args[7]
            if len(args) >= 9:
                hold_to_close = args[8]
        elif len(args) == 4:
            symbol, quantity, avg_price, created_at = args
            account_id = kwargs.get("account_id", DEFAULT_ACCOUNT_ID)
        else:
            account_id = kwargs.get("account_id", DEFAULT_ACCOUNT_ID)
            symbol = kwargs["symbol"]
            quantity = kwargs["quantity"]
            avg_price = kwargs["avg_price"]
            created_at = kwargs["created_at"]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO open_positions
                (account_id, symbol, quantity, avg_price, entry_fee_inr, created_at, stop_loss, target_price, hold_to_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id, symbol.upper(), quantity, round(avg_price, 2),
                round(entry_fee_inr, 2), created_at, stop_loss, target_price,
                1 if hold_to_close else 0,
            ))
            conn.commit()

    def delete_position(self, *args, **kwargs):
        if len(args) == 2:
            account_id, symbol = args
        elif len(args) == 1:
            symbol = args[0]
            account_id = kwargs.get("account_id", DEFAULT_ACCOUNT_ID)
        else:
            account_id = kwargs.get("account_id", DEFAULT_ACCOUNT_ID)
            symbol = kwargs.get("symbol", "")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM open_positions WHERE account_id = ? AND symbol = ?", (account_id, symbol.upper()))
            conn.commit()

    def get_open_positions(self, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("SELECT * FROM open_positions WHERE account_id = ?", (account_id,))
            else:
                cursor.execute("SELECT * FROM open_positions")
            return [dict(r) for r in cursor.fetchall()]

    def clear_open_positions(self, account_id: Optional[str] = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("DELETE FROM open_positions WHERE account_id = ?", (account_id,))
            else:
                cursor.execute("DELETE FROM open_positions")
            conn.commit()

    def reset_account(self, account_id: Optional[str] = None, capital: Optional[float] = None) -> Dict[str, Any]:
        cap = capital if capital is not None else settings.INITIAL_CAPITAL_INR
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if account_id:
                cursor.execute("""
                    UPDATE account_state
                    SET initial_capital = ?, available_cash = ?, total_realized_pnl = 0.0, updated_at = ?
                    WHERE account_id = ?
                """, (cap, cap, now, account_id))
                cursor.execute("DELETE FROM trades WHERE account_id = ?", (account_id,))
                cursor.execute("DELETE FROM open_positions WHERE account_id = ?", (account_id,))
                cursor.execute("DELETE FROM predictions WHERE account_id = ?", (account_id,))
                cursor.execute("DELETE FROM daily_performance WHERE account_id = ?", (account_id,))
                cursor.execute("DELETE FROM risk_state WHERE account_id = ?", (account_id,))
            else:
                cursor.execute("""
                    UPDATE account_state
                    SET initial_capital = ?, available_cash = ?, total_realized_pnl = 0.0, updated_at = ?
                """, (cap, cap, now))
                cursor.execute("DELETE FROM trades")
                cursor.execute("DELETE FROM open_positions")
                cursor.execute("DELETE FROM predictions")
                cursor.execute("DELETE FROM daily_performance")
                cursor.execute("DELETE FROM risk_state")
                cursor.execute("DELETE FROM token_usage")

            cursor.execute("""
                INSERT INTO learning_log (account_id, category, insight, symbol_ref, strategy_ref, impact_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id or "all_accounts",
                "Arena Initialization",
                f"Testing database initialized fresh with ₹{cap:,.2f} dummy capital for {account_id or 'both agents'}.",
                None,
                "system",
                0.0,
                now,
            ))
            conn.commit()
        return self.get_account_state(account_id or DEFAULT_ACCOUNT_ID)

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def record_trade(self, trade_data: Dict[str, Any]):
        acc_id = trade_data.get("account_id", DEFAULT_ACCOUNT_ID)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trades (
                    order_id, account_id, symbol, action, quantity, order_type,
                    fill_price, total_value_inr, fee_inr, latency_ms, status,
                    strategy_name, pnl_inr, gross_pnl_inr, entry_fee_inr,
                    exit_price, exit_timestamp, exit_reason, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data["order_id"],
                acc_id,
                trade_data["symbol"].upper(),
                trade_data["action"].upper(),
                trade_data["quantity"],
                trade_data.get("order_type", "MARKET").upper(),
                round(trade_data["fill_price"], 2),
                round(trade_data["total_value_inr"], 2),
                round(trade_data.get("fee_inr", 0.0), 2),
                round(trade_data.get("latency_ms", 0.0), 2),
                trade_data.get("status", "FILLED").upper(),
                trade_data.get("strategy_name", "quant_engine"),
                round(trade_data.get("pnl_inr", 0.0), 2),
                round(trade_data.get("gross_pnl_inr", 0.0), 2),
                round(trade_data.get("entry_fee_inr", 0.0), 2),
                round(trade_data["exit_price"], 2) if trade_data.get("exit_price") else None,
                trade_data.get("exit_timestamp"),
                trade_data.get("exit_reason"),
                trade_data.get("timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            ))
            conn.commit()

    def get_trades(self, account_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("SELECT * FROM trades WHERE account_id = ? ORDER BY timestamp DESC LIMIT ?", (account_id, limit))
            else:
                cursor.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Strategies / predictions / learning
    # ------------------------------------------------------------------
    def get_strategies(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM strategies ORDER BY total_pnl_inr DESC")
            return [dict(r) for r in cursor.fetchall()]

    def update_strategy_performance(self, strategy_id: str, pnl: float, won: bool):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM strategies WHERE strategy_id = ?", (strategy_id,))
            row = cursor.fetchone()
            if row:
                trades = row["total_trades"] + 1
                wins = row["winning_trades"] + (1 if won else 0)
                losses = row["losing_trades"] + (0 if won else 1)
                new_pnl = round(row["total_pnl_inr"] + pnl, 2)
                win_rate = round((wins / trades) * 100, 1) if trades > 0 else 0.0
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                cursor.execute("""
                    UPDATE strategies
                    SET total_trades = ?, winning_trades = ?, losing_trades = ?,
                        total_pnl_inr = ?, win_rate_pct = ?, last_run = ?
                    WHERE strategy_id = ?
                """, (trades, wins, losses, new_pnl, win_rate, now, strategy_id))
                conn.commit()

    def toggle_strategy(self, strategy_id: str, is_active: bool) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE strategies SET is_active = ? WHERE strategy_id = ?", (1 if is_active else 0, strategy_id))
            conn.commit()
            return cursor.rowcount > 0

    def record_prediction(self, pred: Dict[str, Any]):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO predictions (
                    account_id, symbol, signal, confidence_pct, current_price, target_1, stop_loss,
                    catalyst, action_summary, timeframe, model, tokens_used, auto_executed, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pred.get("account_id", DEFAULT_ACCOUNT_ID),
                pred.get("symbol", "").upper(),
                pred.get("signal", "HOLD").upper(),
                pred.get("confidence_pct", 50),
                pred.get("current_price_inr") or pred.get("current_price", 0.0),
                pred.get("target_1_inr") or pred.get("target_1"),
                pred.get("stop_loss_inr") or pred.get("stop_loss"),
                pred.get("catalyst", ""),
                pred.get("action_summary", ""),
                pred.get("timeframe", "5-15 Min Scalp"),
                pred.get("model", settings.DEEPSEEK_MODEL),
                pred.get("tokens_used", 0),
                1 if pred.get("auto_executed") else 0,
                pred.get("timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            ))
            conn.commit()

    def get_predictions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def record_learning(self, category: str, insight: str, strategy_ref: Optional[str] = None,
                        symbol_ref: Optional[str] = None, impact_pnl: float = 0.0,
                        account_id: str = DEFAULT_ACCOUNT_ID):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO learning_log (account_id, category, insight, strategy_ref, symbol_ref, impact_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id,
                category,
                insight,
                strategy_ref,
                symbol_ref.upper() if symbol_ref else None,
                round(impact_pnl, 2),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))
            conn.commit()

    def get_learning_logs(self, account_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("SELECT * FROM learning_log WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit))
            else:
                cursor.execute("SELECT * FROM learning_log ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Daily performance
    # ------------------------------------------------------------------
    def record_daily_snapshot(
        self,
        date_str: str,
        starting_balance: float,
        ending_balance: float,
        realized_pnl: float,
        trades_count: int,
        win_rate_pct: float,
        total_tokens: int,
        ai_cost_inr: float,
        account_id: str = DEFAULT_ACCOUNT_ID,
        fees_inr: float = 0.0,
    ):
        gross_pnl = round(ending_balance - starting_balance, 2)
        net_before_ai = gross_pnl
        net_after_ai = round(net_before_ai - ai_cost_inr, 2)
        net_return_pct = round((net_after_ai / starting_balance) * 100, 2) if starting_balance else 0.0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO daily_performance (
                    date, account_id, starting_balance, ending_balance, realized_pnl,
                    unrealized_pnl, gross_pnl, fees_inr, trades_count, win_rate_pct,
                    total_tokens_used, estimated_ai_cost_inr, net_profit_before_ai,
                    net_profit_after_ai_cost, net_return_pct
                ) VALUES (?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, account_id, round(starting_balance, 2), round(ending_balance, 2),
                round(realized_pnl, 2), gross_pnl, round(fees_inr, 2), trades_count,
                round(win_rate_pct, 1), total_tokens, round(ai_cost_inr, 2),
                net_before_ai, net_after_ai, net_return_pct,
            ))
            conn.commit()

    def get_daily_performance(self, account_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("SELECT * FROM daily_performance WHERE account_id = ? ORDER BY date DESC LIMIT ?", (account_id, limit))
            else:
                cursor.execute("SELECT * FROM daily_performance ORDER BY date DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def get_last_daily_balance_before(self, account_id: str, date_str: str) -> Optional[float]:
        """Return the most recent prior-day closing equity for risk baselines."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ending_balance FROM daily_performance "
                "WHERE account_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
                (account_id, date_str),
            )
            row = cursor.fetchone()
            return float(row["ending_balance"]) if row else None

    # ------------------------------------------------------------------
    # Token / AI cost accounting
    # ------------------------------------------------------------------
    def record_token_usage(self, module: str, model: str, prompt_tokens: int,
                           completion_tokens: int, cost_inr: float,
                           account_id: str = "shared"):
        total = prompt_tokens + completion_tokens
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO token_usage
                (account_id, module, model, prompt_tokens, completion_tokens, total_tokens, cost_inr, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id, module, model, prompt_tokens, completion_tokens,
                total, round(cost_inr, 4),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))
            conn.commit()

    def get_today_tokens(self, account_id: Optional[str] = None) -> int:
        today_prefix = _today_ist_prefix()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM token_usage WHERE timestamp LIKE ? AND account_id = ?",
                    (f"{today_prefix}%", account_id),
                )
            else:
                cursor.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM token_usage WHERE timestamp LIKE ?",
                    (f"{today_prefix}%",),
                )
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def get_today_token_breakdown(self, account_id: Optional[str] = None) -> Dict[str, int]:
        today_prefix = _today_ist_prefix()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("""
                    SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens
                    FROM token_usage WHERE timestamp LIKE ? AND account_id = ?
                """, (f"{today_prefix}%", account_id))
            else:
                cursor.execute("""
                    SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens
                    FROM token_usage WHERE timestamp LIKE ?
                """, (f"{today_prefix}%",))
            row = cursor.fetchone()
            if row:
                return {
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "total_tokens": int(row["total_tokens"]),
                }
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def get_token_summary(self, account_id: Optional[str] = None, date_prefix: Optional[str] = None) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            where = []
            params: List[Any] = []
            if account_id:
                where.append("account_id = ?")
                params.append(account_id)
            if date_prefix:
                where.append("timestamp LIKE ?")
                params.append(f"{date_prefix}%")
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""

            cursor.execute(
                f"SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens, COALESCE(SUM(cost_inr), 0.0) AS cost FROM token_usage {where_sql}",
                params,
            )
            row = cursor.fetchone()
            cursor.execute(
                f"SELECT model, COUNT(*) AS calls, SUM(total_tokens) AS tokens, SUM(cost_inr) AS cost FROM token_usage {where_sql} GROUP BY model",
                params,
            )
            breakdown = [dict(r) for r in cursor.fetchall()]
            return {
                "total_calls": row["calls"] if row else 0,
                "total_tokens": row["tokens"] if row else 0,
                "total_cost_inr": round(row["cost"], 2) if row else 0.0,
                "breakdown_by_model": breakdown,
            }

    def get_ai_cost_for_account(self, account_id: str, date_prefix: Optional[str] = None) -> float:
        return float(self.get_token_summary(account_id=account_id, date_prefix=date_prefix)["total_cost_inr"])

    # ------------------------------------------------------------------
    # Daily risk state
    # ------------------------------------------------------------------
    def get_daily_risk_state(self, account_id: str, date_str: Optional[str] = None,
                             default_equity: Optional[float] = None) -> Dict[str, Any]:
        date_str = date_str or _today_ist_prefix()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM risk_state WHERE date = ? AND account_id = ?", (date_str, account_id))
            row = cursor.fetchone()
            if row:
                return dict(row)
            state = self.get_account_state(account_id)
            start_equity = float(default_equity if default_equity is not None else state.get("initial_capital", settings.INITIAL_CAPITAL_INR))
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            cursor.execute("""
                INSERT INTO risk_state
                (date, account_id, start_equity, realized_pnl, ai_cost_inr, trades_count, locked, lock_reason, updated_at)
                VALUES (?, ?, ?, 0.0, 0.0, 0, 0, NULL, ?)
            """, (date_str, account_id, round(start_equity, 2), now))
            conn.commit()
            cursor.execute("SELECT * FROM risk_state WHERE date = ? AND account_id = ?", (date_str, account_id))
            return dict(cursor.fetchone())

    def update_daily_risk_state(self, account_id: str, date_str: Optional[str] = None, **fields):
        date_str = date_str or _today_ist_prefix()
        self.get_daily_risk_state(account_id, date_str)
        allowed = {"start_equity", "realized_pnl", "ai_cost_inr", "trades_count", "locked", "lock_reason"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sets = ", ".join(f"{key} = ?" for key in updates)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE risk_state SET {sets} WHERE date = ? AND account_id = ?",
                list(updates.values()) + [date_str, account_id],
            )
            conn.commit()

    def get_today_trade_count(self, account_id: str) -> int:
        today_prefix = _today_ist_prefix()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE account_id = ? AND action = 'BUY' AND timestamp LIKE ?",
                (account_id, f"{today_prefix}%"),
            )
            return int(cursor.fetchone()["n"])

    def get_today_realized_pnl(self, account_id: str) -> float:
        today_prefix = _today_ist_prefix()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(pnl_inr), 0.0) AS pnl FROM trades WHERE account_id = ? AND action = 'SELL' AND timestamp LIKE ?",
                (account_id, f"{today_prefix}%"),
            )
            return round(float(cursor.fetchone()["pnl"]), 2)

    def get_trade_stats(self, account_id: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE account_id = ? ORDER BY timestamp ASC", (account_id,))
            trades = [dict(r) for r in cursor.fetchall()]
        closed = [t for t in trades if t.get("action") == "SELL"]
        wins = [t for t in closed if float(t.get("pnl_inr", 0) or 0) > 0]
        losses = [t for t in closed if float(t.get("pnl_inr", 0) or 0) <= 0]
        fees = round(sum(float(t.get("fee_inr", 0) or 0) for t in trades), 2)
        return {
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round((len(wins) / len(closed)) * 100, 1) if closed else 0.0,
            "fees_inr": fees,
        }

    # ------------------------------------------------------------------
    # Arena comparison
    # ------------------------------------------------------------------
    def get_arena_comparison(self) -> Dict[str, Any]:
        def _account_payload(account_id: str) -> Dict[str, Any]:
            state = self.get_account_state(account_id)
            stats = self.get_trade_stats(account_id)
            ai_cost = self.get_ai_cost_for_account(account_id)
            try:
                from app.trading.execution_engine import execution_engine
                summary = execution_engine.get_portfolio_summary(account_id)
            except Exception:
                summary = {
                    "available_cash_inr": state.get("available_cash", 0.0),
                    "total_portfolio_value_inr": state.get("available_cash", 0.0),
                    "total_pnl_inr": state.get("total_realized_pnl", 0.0),
                    "realized_pnl_inr": state.get("total_realized_pnl", 0.0),
                    "unrealized_pnl_inr": 0.0,
                    "total_fees_paid_inr": stats["fees_inr"],
                    "positions": self.get_open_positions(account_id),
                }
            gross = float(summary.get("total_pnl_inr", 0.0))
            initial = float(state.get("initial_capital", settings.INITIAL_CAPITAL_INR))
            net = round(gross - ai_cost, 2)
            return {
                "account_id": account_id,
                "name": ACCOUNT_NAMES.get(account_id, state.get("account_name", account_id)),
                "model": settings.DEEPSEEK_MODEL if account_id == "deepseek_flash" else settings.OPUS_MODEL,
                "initial_capital": initial,
                "available_cash": round(float(summary.get("available_cash_inr", state.get("available_cash", 0.0))), 2),
                "total_portfolio_value": round(float(summary.get("total_portfolio_value_inr", 0.0)), 2),
                "gross_pnl": round(gross, 2),
                "realized_pnl": round(float(summary.get("realized_pnl_inr", 0.0)), 2),
                "unrealized_pnl": round(float(summary.get("unrealized_pnl_inr", 0.0)), 2),
                "fees_inr": round(float(summary.get("total_fees_paid_inr", stats["fees_inr"])), 2),
                "ai_cost_inr": round(ai_cost, 2),
                "net_pnl_after_ai_inr": net,
                "net_return_after_ai_pct": round((net / initial) * 100, 2) if initial else 0.0,
                "roc_percent": round((gross / initial) * 100, 2) if initial else 0.0,
                "total_trades": stats["total_trades"],
                "closed_trades": stats["closed_trades"],
                "winning_trades": stats["winning_trades"],
                "win_rate_pct": stats["win_rate_pct"],
                "open_positions": summary.get("positions", []),
                "auto_execute": bool(state.get("auto_execute", 0)),
            }

        opus = _account_payload("opus_flash")
        deepseek = _account_payload("deepseek_flash")

        if opus["net_pnl_after_ai_inr"] > deepseek["net_pnl_after_ai_inr"]:
            diff = round(opus["net_pnl_after_ai_inr"] - deepseek["net_pnl_after_ai_inr"], 2)
            leader = "opus_flash"
            leader_statement = f"🏆 Claude Opus leads by ₹{diff:,.2f} net after AI ({opus['net_return_after_ai_pct']:+.2f}% vs {deepseek['net_return_after_ai_pct']:+.2f}%)"
        elif deepseek["net_pnl_after_ai_inr"] > opus["net_pnl_after_ai_inr"]:
            diff = round(deepseek["net_pnl_after_ai_inr"] - opus["net_pnl_after_ai_inr"], 2)
            leader = "deepseek_flash"
            leader_statement = f"🏆 DeepSeek leads by ₹{diff:,.2f} net after AI ({deepseek['net_return_after_ai_pct']:+.2f}% vs {opus['net_return_after_ai_pct']:+.2f}%)"
        else:
            leader = "tied"
            leader_statement = f"⚖️ Both accounts are tied at ₹{opus['net_pnl_after_ai_inr']:,.2f} net after AI"

        return {
            "leader": leader,
            "leader_statement": leader_statement,
            "opus": opus,
            "deepseek": deepseek,
        }

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------
    def prune_historical_records(self, keep_predictions: int = 3000, keep_learning_logs: int = 1000) -> Dict[str, int]:
        deleted_preds = 0
        deleted_logs = 0
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM predictions WHERE id NOT IN (
                        SELECT id FROM predictions ORDER BY id DESC LIMIT ?
                    )
                """, (keep_predictions,))
                deleted_preds = cursor.rowcount
                cursor.execute("""
                    DELETE FROM learning_log WHERE id NOT IN (
                        SELECT id FROM learning_log ORDER BY id DESC LIMIT ?
                    )
                """, (keep_learning_logs,))
                deleted_logs = cursor.rowcount
                conn.commit()
                if deleted_preds > 0 or deleted_logs > 0:
                    logger.info(f"Database pruned: removed {deleted_preds} predictions and {deleted_logs} learning logs.")
        except Exception as e:
            logger.error(f"Error during database pruning: {e}")
        return {"deleted_predictions": max(0, deleted_preds), "deleted_learning_logs": max(0, deleted_logs)}


db = DatabaseManager()
