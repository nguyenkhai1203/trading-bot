import os
import logging
import asyncio
import aiosqlite
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

class DataManager:
    """
    Singleton Async SQLite Database Manager.
    Handles profiles, trades, AI training logs, OHLCV caching and risk metrics.
    """
    _instances = {}
    _lock = asyncio.Lock()

    def __init__(self, db_path: str):
        """Private init - use get_instance() instead."""
        self.db_path = db_path
        self.logger = logging.getLogger("DataManager")
        self._db = None
        self._write_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, env: str = 'LIVE') -> 'DataManager':
        """Get or create singleton instance for the given environment."""
        env_upper = str(env).upper()
        
        async with cls._lock:
            if env_upper not in cls._instances:
                db_name = "trading_live.db" if env_upper == 'LIVE' else "trading_test_v2.db"
                db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
                os.makedirs(db_dir, exist_ok=True)
                db_path = os.path.join(db_dir, db_name)
                
                instance = cls(db_path)
                await instance.initialize()
                cls._instances[env_upper] = instance
                
            return cls._instances[env_upper]

    @classmethod
    async def clear_instances(cls):
        """Close all instances and clear cache. Useful for tests."""
        async with cls._lock:
            for inst in cls._instances.values():
                await inst.close()
            cls._instances.clear()

    async def initialize(self):
        """Initialize DB connection, WAL mode and create tables from schema."""
        self.logger.info(f"Initializing Database at {self.db_path}")
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        
        self._db = await aiosqlite.connect(self.db_path, timeout=30)
        self._db.row_factory = aiosqlite.Row
        # Enable Write-Ahead Logging for better concurrency
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # Enable Foreign Keys
        await self._db.execute("PRAGMA foreign_keys=ON")
        
        if os.path.exists(schema_path):
            with open(schema_path, 'r', encoding='utf-8') as f:
                schema_sql = f.read()

            # [PRE-MIGRATION] Add missing columns to existing tables BEFORE running schema.
            # This prevents "no such column" errors when schema.sql creates indexes that
            # reference columns not yet present in an older DB (e.g. pos_key, leverage).
            try:
                async with self._db.execute("PRAGMA table_info(trades)") as cursor:
                    columns = [row[1] for row in await cursor.fetchall()]
                # Only migrate if the table already exists (columns list is non-empty)
                if columns:
                    if 'pos_key' not in columns:
                        self.logger.info("Pre-migration: Adding pos_key to trades table")
                        await self._db.execute("ALTER TABLE trades ADD COLUMN pos_key TEXT")
                    if 'leverage' not in columns:
                        self.logger.info("Pre-migration: Adding leverage to trades table")
                        await self._db.execute("ALTER TABLE trades ADD COLUMN leverage REAL")
                    await self._db.commit()
            except Exception as e:
                self.logger.error(f"Pre-migration error: {e}")

            # Run schema script (CREATE TABLE IF NOT EXISTS + indexes)
            # Now safe to run because all required columns already exist.
            await self._db.executescript(schema_sql)
            await self._db.commit()
        else:
            self.logger.error(f"Schema file not found at {schema_path}")

    async def _execute_write(self, sql: str, params: tuple = ()):
        """Internal helper to execute and commit with a lock."""
        db = await self.get_db()
        async with self._write_lock:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor

    async def _execute_write_many(self, sql: str, data: list):
        """Internal helper for executemany with a lock."""
        db = await self.get_db()
        async with self._write_lock:
            cursor = await db.executemany(sql, data)
            await db.commit()
            return cursor

    async def get_db(self):
        """Returns the active aiosqlite connection."""
        if not self._db:
            await self.initialize()
        return self._db

    async def close(self):
        """Close DB connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------------
    # PROFILES CRUD
    # ------------------------------------------------------------------------
    async def get_profiles(self) -> List[dict]:
        """Get all active profiles."""
        db = await self.get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM profiles WHERE is_active = 1") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_profile(self, name: str, env: str, exchange: str, label: str="", api_key: str="", api_secret: str="", color: str="white") -> int:
        """Add a new profile or return existing ID."""
        db = await self.get_db()
        # First check if exists
        async with db.execute("SELECT id FROM profiles WHERE name = ?", (name,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
                
        await db.execute("""
            INSERT INTO profiles (name, label, environment, exchange, api_key, api_secret, color)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, label, env, exchange, api_key, api_secret, color))
        await db.commit()
        
        async with db.execute("SELECT id FROM profiles WHERE name = ?", (name,)) as cursor:
            row = await cursor.fetchone()
            return row[0]

    # ------------------------------------------------------------------------
    # TRADES CRUD
    # ------------------------------------------------------------------------
    async def save_position(self, pos_data: dict) -> int:
        """
        Insert or Update an ACTIVE/OPENED position.
        Returns trade_id.
        Expected keys in pos_data: profile_id, exchange, symbol, side, qty, etc.
        """
        db = await self.get_db()
        
        # Check if updating existing by ID or pos_key
        trade_id = pos_data.get('id')
        pos_key = pos_data.get('pos_key')
        
        # Primary lookup by pos_key + profile_id (most reliable for active slots)
        if not trade_id and pos_key:
            profile_id = pos_data.get('profile_id')
            async with db.execute("SELECT id FROM trades WHERE pos_key = ? AND profile_id = ? AND status IN ('ACTIVE', 'OPENED')", 
                                 (pos_key, profile_id)) as cursor:
                row = await cursor.fetchone()
                if row:
                    trade_id = row[0]
                    
        # Fallback lookup by exchange_order_id + exchange
        exchange_order_id = pos_data.get('exchange_order_id')
        exchange = pos_data.get('exchange')
        
        if not trade_id and exchange_order_id and exchange:
            async with db.execute("SELECT id FROM trades WHERE exchange_order_id = ? AND exchange = ?", 
                                 (exchange_order_id, exchange)) as cursor:
                row = await cursor.fetchone()
                if row:
                    trade_id = row[0]

        meta_json = json.dumps(pos_data.get('meta', {})) if 'meta' in pos_data else None
        
        if trade_id:
            # Update existing
            await self._execute_write("""
                UPDATE trades SET 
                    exchange_order_id=?, symbol=?, side=?, qty=?, entry_price=?, 
                    sl_price=?, tp_price=?, sl_order_id=?, tp_order_id=?, 
                    pos_key=?, status=?, timeframe=?, pnl=?, meta_json=?, leverage=?,
                    exit_price=?, exit_reason=?, exit_time=?
                WHERE id=?
            """, (
                pos_data.get('exchange_order_id'), pos_data.get('symbol'), pos_data.get('side'),
                pos_data.get('qty', 0), pos_data.get('entry_price'), pos_data.get('sl_price'), pos_data.get('tp_price'),
                pos_data.get('sl_order_id'), pos_data.get('tp_order_id'), pos_key,
                pos_data.get('status', 'OPENED'), pos_data.get('timeframe'), pos_data.get('pnl', 0), 
                meta_json, pos_data.get('leverage'),
                pos_data.get('exit_price'), pos_data.get('exit_reason'), pos_data.get('exit_time'),
                trade_id
            ))
            return trade_id
        else:
            # Insert new
            entry_time = pos_data.get('entry_time', int(datetime.now().timestamp() * 1000))
            cursor = await self._execute_write("""
                INSERT INTO trades (
                    profile_id, exchange_order_id, exchange, symbol, side, qty, 
                    entry_price, sl_price, tp_price, sl_order_id, tp_order_id, 
                    pos_key, status, timeframe, entry_time, pnl, meta_json, leverage,
                    exit_price, exit_reason, exit_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos_data.get('profile_id'), pos_data.get('exchange_order_id'), pos_data.get('exchange'), 
                pos_data.get('symbol'), pos_data.get('side'), pos_data.get('qty', 0),
                pos_data.get('entry_price'), pos_data.get('sl_price'), pos_data.get('tp_price'),
                pos_data.get('sl_order_id'), pos_data.get('tp_order_id'), pos_key,
                pos_data.get('status', 'OPENED'), pos_data.get('timeframe'), entry_time, 
                pos_data.get('pnl', 0), meta_json, pos_data.get('leverage'),
                pos_data.get('exit_price'), pos_data.get('exit_reason'), pos_data.get('exit_time')
            ))
            return cursor.lastrowid

    async def update_position_status(self, trade_id: int, status: str, exit_price: Optional[float] = None, pnl: Optional[float] = None, exit_reason: Optional[str] = None):
        """Atomically update a position to CLOSED/CANCELLED/ERROR state."""
        exit_time = int(datetime.now().timestamp() * 1000) if status == 'CLOSED' else None
        
        await self._execute_write("""
            UPDATE trades SET 
                status=?, exit_price=?, pnl=?, exit_reason=?, exit_time=?
            WHERE id=?
        """, (status, exit_price, pnl, exit_reason, exit_time, trade_id))

    async def get_active_positions(self, profile_id: int) -> List[dict]:
        """Fetch all ACTIVE or OPENED positions for a profile."""
        db = await self.get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM trades 
            WHERE profile_id = ? AND status IN ('ACTIVE', 'OPENED')
        """, (profile_id,)) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get('meta_json'):
                    d['meta'] = json.loads(d['meta_json'])
                result.append(d)
            return result

    async def insert_trade_history(self, trade_data: dict) -> int:
        """Alias for save_position when inserting past trades."""
        return await self.save_position(trade_data)

    async def get_trade_history(self, profile_id: int, limit: int = 100) -> List[dict]:
        """Fetch closed/cancelled trade history for a profile."""
        db = await self.get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM trades 
            WHERE profile_id = ? AND status IN ('CLOSED', 'CANCELLED')
            ORDER BY exit_time DESC LIMIT ?
        """, (profile_id, limit)) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get('meta_json'):
                    d['meta'] = json.loads(d['meta_json'])
                result.append(d)
            return result

    # ------------------------------------------------------------------------
    # AI TRAINING LOGS
    # ------------------------------------------------------------------------
    async def log_ai_snapshot(self, trade_id: int, snapshot_json: str, entry_confidence: float):
        """Save AI features snapshot for a trade (used in training later)."""
        await self._execute_write("""
            INSERT OR REPLACE INTO ai_training_logs (trade_id, snapshot_json, entry_confidence)
            VALUES (?, ?, ?)
        """, (trade_id, snapshot_json, entry_confidence))

    # ------------------------------------------------------------------------
    # OHLCV CACHE
    # ------------------------------------------------------------------------
    async def upsert_candles(self, symbol: str, timeframe: str, candles: List[List]):
        """
        Insert/Update OHLCV candles to cache.
        Candle format: [Timestamp, Open, High, Low, Close, Volume]
        """
        if not candles: return
        
        db = await self.get_db()
        now = int(datetime.now().timestamp())
        
        # Prepare bulk insert
        data = [
            (symbol, timeframe, c[0], c[1], c[2], c[3], c[4], c[5], now)
            for c in candles
        ]
        
        await self._execute_write_many("""
            INSERT OR REPLACE INTO ohlcv_cache 
            (symbol, timeframe, timestamp, open, high, low, close, volume, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> List[List]:
        """Fetch candles from cache and update last_used_at."""
        db = await self.get_db()
        await db.execute("""
            UPDATE ohlcv_cache SET last_used_at = ? 
            WHERE symbol = ? AND timeframe = ?
        """, (int(datetime.now().timestamp()), symbol, timeframe))
        # Commit not strictly required immediately but good practice
        
        async with db.execute("""
            SELECT timestamp, open, high, low, close, volume 
            FROM ohlcv_cache 
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp ASC LIMIT ?
        """, (symbol, timeframe, limit)) as cursor:
            rows = await cursor.fetchall()
            return [list(r) for r in rows]

    async def purge_old_candles(self, days: int = 30):
        """Cleanup old unused candles."""
        cutoff = int((datetime.now() - timedelta(days=days)).timestamp())
        await self._execute_write("DELETE FROM ohlcv_cache WHERE last_used_at < ?", (cutoff,))

    # ------------------------------------------------------------------------
    # RISK METRICS
    # ------------------------------------------------------------------------
    async def get_risk_metric(self, profile_id: int, metric_name: str, env: str) -> Optional[float]:
        """Retrieve a specific risk metric (e.g. 'peak_balance')."""
        db = await self.get_db()
        async with db.execute("""
            SELECT value FROM risk_metrics 
            WHERE profile_id = ? AND environment = ? AND metric_name = ?
        """, (profile_id, env, metric_name)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_risk_metric(self, profile_id: int, metric_name: str, value: float, env: str):
        """Set a risk metric asynchronously."""
        now = int(datetime.now().timestamp())
        await self._execute_write("""
            INSERT OR REPLACE INTO risk_metrics (profile_id, environment, metric_name, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (profile_id, env, metric_name, value, now))
