"""
SQLite Database & Persistence Engine for Gemini Quant Platform.
Persists trades, strategies, performance, predictions, learning logs, and token usage
for multiple independent AI accounts (Gemini 3.8 Flash vs DeepSeek Flash).
"""

import sqlite3
import datetime
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.config import settings

logger = logging.getLogger("app.storage.db")


class DatabaseManager:
    """Thread-safe SQLite manager for dual-agent trading state, strategies, and learning data."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.absolute_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Initialize database schema with multi-account support and safe migrations."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Account state table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY,
                    account_id TEXT UNIQUE DEFAULT 'gemini_flash',
                    account_name TEXT DEFAULT 'Gemini 3.8 Flash',
                    initial_capital REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    total_realized_pnl REAL DEFAULT 0.0,
                    auto_execute INTEGER DEFAULT 0,
                    updated_at TEXT
                )
            """)

            # 2. Trades table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT DEFAULT 'gemini_flash',
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
                    exit_price REAL,
                    exit_timestamp TEXT,
                    timestamp TEXT NOT NULL
                )
            """)

            # 3. Strategies table
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

            # 4. Predictions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT DEFAULT 'gemini_flash',
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

            # 5. Continuous Learning Log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT DEFAULT 'gemini_flash',
                    category TEXT NOT NULL,
                    insight TEXT NOT NULL,
                    strategy_ref TEXT,
                    symbol_ref TEXT,
                    impact_pnl REAL DEFAULT 0.0,
                    timestamp TEXT NOT NULL
                )
            """)

            # 6. Daily Performance Snapshots
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_performance (
                    date TEXT NOT NULL,
                    account_id TEXT DEFAULT 'gemini_flash',
                    starting_balance REAL NOT NULL,
                    ending_balance REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    trades_count INTEGER DEFAULT 0,
                    win_rate_pct REAL DEFAULT 0.0,
                    total_tokens_used INTEGER DEFAULT 0,
                    estimated_ai_cost_inr REAL DEFAULT 0.0,
                    net_profit_after_ai_cost REAL DEFAULT 0.0,
                    PRIMARY KEY (date, account_id)
                )
            """)

            # 7. Token Usage & Cost tracking
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost_inr REAL DEFAULT 0.0,
                    timestamp TEXT NOT NULL
                )
            """)

            # 8. Open Positions Table (Composite PK: account_id + symbol)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, symbol)
                )
            """)

            # Run safe migrations for legacy tables
            self._run_schema_migrations(cursor)
            conn.commit()

        # Seed initial accounts and default strategies
        self._seed_default_state()
        self._seed_default_strategies()

    def _run_schema_migrations(self, cursor: sqlite3.Cursor):
        """Safely ensure all existing tables have necessary multi-account columns."""
        # Check account_state
        cursor.execute("PRAGMA table_info(account_state)")
        cols = [r["name"] for r in cursor.fetchall()]
        if "account_id" not in cols:
            cursor.execute("ALTER TABLE account_state ADD COLUMN account_id TEXT DEFAULT 'gemini_flash'")
        if "account_name" not in cols:
            cursor.execute("ALTER TABLE account_state ADD COLUMN account_name TEXT DEFAULT 'Gemini 3.8 Flash'")

        # Check trades
        cursor.execute("PRAGMA table_info(trades)")
        t_cols = [r["name"] for r in cursor.fetchall()]
        if "account_id" not in t_cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN account_id TEXT DEFAULT 'gemini_flash'")

        # Check learning_log
        cursor.execute("PRAGMA table_info(learning_log)")
        l_cols = [r["name"] for r in cursor.fetchall()]
        if "account_id" not in l_cols:
            cursor.execute("ALTER TABLE learning_log ADD COLUMN account_id TEXT DEFAULT 'gemini_flash'")

        # Check open_positions
        cursor.execute("PRAGMA table_info(open_positions)")
        p_cols = [r["name"] for r in cursor.fetchall()]
        if "account_id" not in p_cols:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS open_positions_v2 (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    stop_loss REAL,
                    target_price REAL,
                    PRIMARY KEY (account_id, symbol)
                )
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO open_positions_v2 (account_id, symbol, quantity, avg_price, created_at)
                SELECT 'gemini_flash', symbol, quantity, avg_price, created_at FROM open_positions
            """)
            cursor.execute("DROP TABLE open_positions")
            cursor.execute("ALTER TABLE open_positions_v2 RENAME TO open_positions")
            cursor.execute("PRAGMA table_info(open_positions)")
            p_cols = [r["name"] for r in cursor.fetchall()]

        if "stop_loss" not in p_cols:
            cursor.execute("ALTER TABLE open_positions ADD COLUMN stop_loss REAL")
        if "target_price" not in p_cols:
            cursor.execute("ALTER TABLE open_positions ADD COLUMN target_price REAL")

        # Check predictions
        cursor.execute("PRAGMA table_info(predictions)")
        pred_cols = [r["name"] for r in cursor.fetchall()]
        if "account_id" not in pred_cols:
            cursor.execute("ALTER TABLE predictions ADD COLUMN account_id TEXT DEFAULT 'gemini_flash'")

    def _seed_default_state(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            init_cap = settings.INITIAL_CAPITAL_INR
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            # Seed Account 1: Gemini 3.8 Flash
            cursor.execute("SELECT COUNT(*) as count FROM account_state WHERE account_id = 'gemini_flash' OR id = 1")
            if cursor.fetchone()["count"] == 0:
                cursor.execute("""
                    INSERT INTO account_state (id, account_id, account_name, initial_capital, available_cash, total_realized_pnl, auto_execute, updated_at)
                    VALUES (1, 'gemini_flash', 'Gemini 3.8 Flash', ?, ?, 0.0, ?, ?)
                """, (init_cap, init_cap, 1 if settings.AUTO_EXECUTE_DEFAULT else 0, now))
            else:
                cursor.execute("""
                    UPDATE account_state SET account_id = 'gemini_flash', account_name = 'Gemini 3.8 Flash' WHERE id = 1 AND account_id IS NULL
                """)

            # Seed Account 2: DeepSeek Flash MaaS
            cursor.execute("SELECT COUNT(*) as count FROM account_state WHERE account_id = 'deepseek_flash' OR id = 2")
            if cursor.fetchone()["count"] == 0:
                cursor.execute("""
                    INSERT INTO account_state (id, account_id, account_name, initial_capital, available_cash, total_realized_pnl, auto_execute, updated_at)
                    VALUES (2, 'deepseek_flash', 'DeepSeek Flash MaaS', ?, ?, 0.0, ?, ?)
                """, (init_cap, init_cap, 1 if settings.AUTO_EXECUTE_DEFAULT else 0, now))

            conn.commit()

    def _seed_default_strategies(self):
        default_strategies = [
            {
                "strategy_id": "argon_ai_momentum",
                "name": "Gemini 3.8 Flash Momentum",
                "rules": "Intraday momentum, VWAP support, breaking news sentiment, 1.5% target with 1% stop-loss.",
                "description": "Autonomous quantitative model analyzing live order flow & real-time market breadth.",
                "is_active": 1,
            },
            {
                "strategy_id": "deepseek_flash_scalp",
                "name": "DeepSeek Flash Scalper",
                "rules": "Ultra-fast mean reversion, 20-EMA pullback, tight 0.8% stop with 1.6% profit targets.",
                "description": "Autonomous DeepSeek Flash model focusing on high-beta equity scalps.",
                "is_active": 1,
            },
            {
                "strategy_id": "orb_breakout_day_trader",
                "name": "Opening Range Breakout (ORB)",
                "rules": "15-minute high breakout with volume surge > 1.3x. ORB midpoint stop loss.",
                "description": "Proven intraday breakout strategy adapted from day-trader-skill.",
                "is_active": 1,
            },
            {
                "strategy_id": "bollinger_volume_breakout",
                "name": "Bollinger Bands & Volume Surge",
                "rules": "Buy when Close breaks above Upper Band with Volume > 1.2x SMA(20). Exit below Middle Band.",
                "description": "Volatility expansion scalper optimized for high-beta Indian equities.",
                "is_active": 1,
            },
        ]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for s in default_strategies:
                cursor.execute("""
                    INSERT OR IGNORE INTO strategies (strategy_id, name, rules, description, total_trades, winning_trades, losing_trades, total_pnl_inr, win_rate_pct, is_active, created_at, last_run)
                    VALUES (?, ?, ?, ?, 0, 0, 0, 0.0, 0.0, ?, ?, ?)
                """, (s["strategy_id"], s["name"], s["rules"], s["description"], s["is_active"], now, now))
            conn.commit()

    # --- Multi-Account Methods ---
    def get_account_state(self, account_id: str = "gemini_flash") -> Dict[str, Any]:
        """Fetch account state by account_id ('gemini_flash' or 'deepseek_flash')."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM account_state WHERE account_id = ? OR (id = 1 AND ? = 'gemini_flash')", (account_id, account_id))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {
                "account_id": account_id,
                "account_name": "Gemini 3.8 Flash" if account_id == "gemini_flash" else "DeepSeek Flash MaaS",
                "initial_capital": settings.INITIAL_CAPITAL_INR,
                "available_cash": settings.INITIAL_CAPITAL_INR,
                "total_realized_pnl": 0.0,
                "auto_execute": 1,
            }

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        """Return states for all registered agent accounts."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM account_state ORDER BY id ASC")
            return [dict(r) for r in cursor.fetchall()]

    def save_account_state(self, *args, **kwargs):
        """
        Flexible polymorphic account state saving:
        Supports:
          save_account_state(available_cash, total_realized_pnl, auto_execute=None, account_id="gemini_flash")
          save_account_state(account_id, available_cash, total_realized_pnl, auto_execute=None)
        """
        if len(args) >= 1 and isinstance(args[0], str):
            account_id = args[0]
            available_cash = args[1]
            total_realized_pnl = args[2] if len(args) > 2 else 0.0
            auto_execute = args[3] if len(args) > 3 else kwargs.get("auto_execute")
        else:
            available_cash = args[0] if len(args) > 0 else kwargs.get("available_cash", 10000.0)
            total_realized_pnl = args[1] if len(args) > 1 else kwargs.get("total_realized_pnl", 0.0)
            auto_execute = args[2] if len(args) > 2 else kwargs.get("auto_execute")
            account_id = kwargs.get("account_id", "gemini_flash")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if auto_execute is not None:
                cursor.execute("""
                    UPDATE account_state
                    SET available_cash = ?, total_realized_pnl = ?, auto_execute = ?, updated_at = ?
                    WHERE account_id = ? OR (id = 1 AND ? = 'gemini_flash')
                """, (round(available_cash, 2), round(total_realized_pnl, 2), 1 if auto_execute else 0, now, account_id, account_id))
            else:
                cursor.execute("""
                    UPDATE account_state
                    SET available_cash = ?, total_realized_pnl = ?, updated_at = ?
                    WHERE account_id = ? OR (id = 1 AND ? = 'gemini_flash')
                """, (round(available_cash, 2), round(total_realized_pnl, 2), now, account_id, account_id))
            conn.commit()

    # --- Position Persistence ---
    def save_position(self, *args, **kwargs):
        stop_loss = kwargs.get("stop_loss", None)
        target_price = kwargs.get("target_price", None)
        if len(args) >= 5:
            account_id, symbol, quantity, avg_price, created_at = args[:5]
            if len(args) >= 6:
                stop_loss = args[5]
            if len(args) >= 7:
                target_price = args[6]
        elif len(args) == 4:
            symbol, quantity, avg_price, created_at = args
            account_id = kwargs.get("account_id", "gemini_flash")
        else:
            account_id = kwargs.get("account_id", "gemini_flash")
            symbol = kwargs["symbol"]
            quantity = kwargs["quantity"]
            avg_price = kwargs["avg_price"]
            created_at = kwargs["created_at"]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO open_positions (account_id, symbol, quantity, avg_price, created_at, stop_loss, target_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (account_id, symbol.upper(), quantity, round(avg_price, 2), created_at, stop_loss, target_price))
            conn.commit()

    def delete_position(self, *args, **kwargs):
        if len(args) == 2:
            account_id, symbol = args
        elif len(args) == 1:
            symbol = args[0]
            account_id = kwargs.get("account_id", "gemini_flash")
        else:
            account_id = kwargs.get("account_id", "gemini_flash")
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
        """Reset specified or all testing accounts back to initial capital (₹10,000 each)."""
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
            else:
                cursor.execute("""
                    UPDATE account_state
                    SET initial_capital = ?, available_cash = ?, total_realized_pnl = 0.0, updated_at = ?
                """, (cap, cap, now))
                cursor.execute("DELETE FROM trades")
                cursor.execute("DELETE FROM open_positions")
                cursor.execute("DELETE FROM predictions")
                cursor.execute("DELETE FROM daily_performance")
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
                now
            ))
            conn.commit()
        return self.get_account_state(account_id or "gemini_flash")

    # --- Trades Methods ---
    def record_trade(self, trade_data: Dict[str, Any]):
        acc_id = trade_data.get("account_id", "gemini_flash")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trades (
                    order_id, account_id, symbol, action, quantity, order_type,
                    fill_price, total_value_inr, fee_inr, latency_ms,
                    status, strategy_name, pnl_inr, exit_price,
                    exit_timestamp, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                trade_data.get("strategy_name", "Argon AI 5-Min Predictor"),
                round(trade_data.get("pnl_inr", 0.0), 2),
                round(trade_data["exit_price"], 2) if trade_data.get("exit_price") else None,
                trade_data.get("exit_timestamp"),
                trade_data.get("timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat())
            ))
            conn.commit()

    def get_trades(self, account_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute("""
                    SELECT * FROM trades WHERE account_id = ? ORDER BY timestamp DESC LIMIT ?
                """, (account_id, limit))
            else:
                cursor.execute("""
                    SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
                """, (limit,))
            return [dict(r) for r in cursor.fetchall()]

    # --- Strategies Methods ---
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

    # --- Predictions Methods ---
    def record_prediction(self, pred: Dict[str, Any]):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO predictions (
                    account_id, symbol, signal, confidence_pct, current_price, target_1, stop_loss,
                    catalyst, action_summary, timeframe, model, tokens_used, auto_executed, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pred.get("account_id", "gemini_flash"),
                pred.get("symbol", "").upper(),
                pred.get("signal", "HOLD").upper(),
                pred.get("confidence_pct", 50),
                pred.get("current_price_inr") or pred.get("current_price", 0.0),
                pred.get("target_1_inr") or pred.get("target_1"),
                pred.get("stop_loss_inr") or pred.get("stop_loss"),
                pred.get("catalyst", ""),
                pred.get("action_summary", ""),
                pred.get("timeframe", "5-Min Fast Scalp"),
                pred.get("model", "gemini-3.8-flash"),
                pred.get("tokens_used", 0),
                1 if pred.get("auto_executed") else 0,
                pred.get("timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat())
            ))
            conn.commit()

    def get_predictions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    # --- Learning Log Methods ---
    def record_learning(self, category: str, insight: str, strategy_ref: Optional[str] = None, symbol_ref: Optional[str] = None, impact_pnl: float = 0.0, account_id: str = "gemini_flash"):
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
                datetime.datetime.now(datetime.timezone.utc).isoformat()
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

    # --- Daily Performance Snapshots ---
    def record_daily_snapshot(self, date_str: str, starting_balance: float, ending_balance: float, realized_pnl: float, trades_count: int, win_rate_pct: float, total_tokens: int, ai_cost_inr: float, account_id: str = "gemini_flash"):
        net_profit = round(realized_pnl - ai_cost_inr, 2)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO daily_performance (
                    date, account_id, starting_balance, ending_balance, realized_pnl,
                    unrealized_pnl, trades_count, win_rate_pct, total_tokens_used,
                    estimated_ai_cost_inr, net_profit_after_ai_cost
                ) VALUES (?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)
            """, (
                date_str, account_id, round(starting_balance, 2), round(ending_balance, 2),
                round(realized_pnl, 2), trades_count, round(win_rate_pct, 1),
                total_tokens, round(ai_cost_inr, 2), net_profit
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

    # --- Token Accounting Methods ---
    def record_token_usage(self, module: str, model: str, prompt_tokens: int, completion_tokens: int, cost_inr: float):
        total = prompt_tokens + completion_tokens
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO token_usage (module, model, prompt_tokens, completion_tokens, total_tokens, cost_inr, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (module, model, prompt_tokens, completion_tokens, total, round(cost_inr, 4), datetime.datetime.now(datetime.timezone.utc).isoformat()))
            conn.commit()

    def get_today_tokens(self) -> int:
        today_prefix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(SUM(total_tokens), 0) as total FROM token_usage WHERE timestamp LIKE ?", (f"{today_prefix}%",))
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def get_token_summary(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as calls, COALESCE(SUM(total_tokens), 0) as tokens, COALESCE(SUM(cost_inr), 0.0) as cost FROM token_usage")
            row = cursor.fetchone()
            cursor.execute("SELECT model, COUNT(*) as calls, SUM(total_tokens) as tokens, SUM(cost_inr) as cost FROM token_usage GROUP BY model")
            breakdown = [dict(r) for r in cursor.fetchall()]
            return {
                "total_calls": row["calls"] if row else 0,
                "total_tokens": row["tokens"] if row else 0,
                "total_cost_inr": round(row["cost"], 2) if row else 0.0,
                "breakdown_by_model": breakdown,
            }

    # --- AI Dual Arena Comparison ---
    def get_arena_comparison(self) -> Dict[str, Any]:
        """
        Computes real-time comparative metrics for Gemini 3.8 Flash vs DeepSeek Flash Arena:
        Capital, Available Cash, Realized PnL, Win Rate, Trades Count, and Leader Status.
        """
        gemini_state = self.get_account_state("gemini_flash")
        deepseek_state = self.get_account_state("deepseek_flash")

        gemini_trades = self.get_trades("gemini_flash", limit=100)
        deepseek_trades = self.get_trades("deepseek_flash", limit=100)

        def _calc_stats(trades):
            closed = [t for t in trades if t.get("action") == "SELL"]
            wins = len([t for t in closed if t.get("pnl_inr", 0) > 0])
            total = len(closed)
            win_rate = round((wins / total * 100), 1) if total > 0 else 0.0
            return total, wins, win_rate

        g_total, g_wins, g_win_rate = _calc_stats(gemini_trades)
        d_total, d_wins, d_win_rate = _calc_stats(deepseek_trades)

        g_pnl = gemini_state.get("total_realized_pnl", 0.0)
        d_pnl = deepseek_state.get("total_realized_pnl", 0.0)

        g_roc = round((g_pnl / gemini_state.get("initial_capital", 10000.0)) * 100, 2)
        d_roc = round((d_pnl / deepseek_state.get("initial_capital", 10000.0)) * 100, 2)

        if g_pnl > d_pnl:
            diff = round(g_pnl - d_pnl, 2)
            leader = "gemini_flash"
            leader_statement = f"🏆 Gemini 3.8 Flash is leading by +₹{diff:.2f} ({g_roc:+0.2f}% vs {d_roc:+0.2f}% Return)"
        elif d_pnl > g_pnl:
            diff = round(d_pnl - g_pnl, 2)
            leader = "deepseek_flash"
            leader_statement = f"🏆 DeepSeek Flash is leading by +₹{diff:.2f} ({d_roc:+0.2f}% vs {g_roc:+0.2f}% Return)"
        else:
            leader = "tied"
            leader_statement = f"⚖️ Both agents are tied with identical P&L of ₹{g_pnl:,.2f}"

        return {
            "leader": leader,
            "leader_statement": leader_statement,
            "gemini": {
                "account_id": "gemini_flash",
                "name": "Gemini 3.8 Flash",
                "model": "gemini-3.8-flash",
                "initial_capital": gemini_state.get("initial_capital", 10000.0),
                "available_cash": gemini_state.get("available_cash", 10000.0),
                "realized_pnl": g_pnl,
                "roc_percent": g_roc,
                "total_trades": len(gemini_trades),
                "closed_trades": g_total,
                "winning_trades": g_wins,
                "win_rate_pct": g_win_rate,
                "open_positions": self.get_open_positions("gemini_flash"),
                "auto_execute": bool(gemini_state.get("auto_execute", 1)),
            },
            "deepseek": {
                "account_id": "deepseek_flash",
                "name": "DeepSeek Flash MaaS",
                "model": "deepseek-ai/deepseek-v4.1-flash-maas",
                "initial_capital": deepseek_state.get("initial_capital", 10000.0),
                "available_cash": deepseek_state.get("available_cash", 10000.0),
                "realized_pnl": d_pnl,
                "roc_percent": d_roc,
                "total_trades": len(deepseek_trades),
                "closed_trades": d_total,
                "winning_trades": d_wins,
                "win_rate_pct": d_win_rate,
                "open_positions": self.get_open_positions("deepseek_flash"),
                "auto_execute": bool(deepseek_state.get("auto_execute", 1)),
            }
        }

    def prune_historical_records(self, keep_predictions: int = 3000, keep_learning_logs: int = 1000) -> Dict[str, int]:
        """
        Prunes old records from predictions and learning_log tables to strictly bound SQLite file size.
        Guarantees that database storage never bloats over time.
        """
        deleted_preds = 0
        deleted_logs = 0
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM predictions
                    WHERE id NOT IN (
                        SELECT id FROM predictions
                        ORDER BY id DESC
                        LIMIT ?
                    )
                """, (keep_predictions,))
                deleted_preds = cursor.rowcount

                cursor.execute("""
                    DELETE FROM learning_log
                    WHERE id NOT IN (
                        SELECT id FROM learning_log
                        ORDER BY id DESC
                        LIMIT ?
                    )
                """, (keep_learning_logs,))
                deleted_logs = cursor.rowcount

                conn.commit()
                if deleted_preds > 0 or deleted_logs > 0:
                    logger.info(f"Database pruned: removed {deleted_preds} old predictions and {deleted_logs} old learning logs.")
        except Exception as e:
            logger.error(f"Error during database pruning: {e}")

        return {"deleted_predictions": max(0, deleted_preds), "deleted_learning_logs": max(0, deleted_logs)}


db = DatabaseManager()
