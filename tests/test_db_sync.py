import pytest
import asyncio
import os
import sys
import json
import time
import uuid
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest_asyncio

from src.database import DataManager
from src.execution import Trader

# Mock send_telegram_message to avoid emoji/unicode issues in tests
import src.execution
src.execution.send_telegram_message = lambda *args, **kwargs: asyncio.sleep(0)
src.execution.format_position_filled = lambda *args, **kwargs: "filled"
src.execution.format_position_closed = lambda *args, **kwargs: "closed"
src.execution.format_pending_order = lambda *args, **kwargs: "pending"
src.execution.format_order_cancelled = lambda *args, **kwargs: "cancelled"

@pytest_asyncio.fixture
async def db():
    """Use a fresh non-colliding test database for each run."""
    db_id = uuid.uuid4().hex[:8]
    db_path = f"data/test_sync_{db_id}.db"
    
    # Ensure cleanup of any previous run with same name
    for ext in ["", "-wal", "-shm"]:
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)

    # Instantiate directly to avoid singleton side-effects
    db_manager = DataManager(db_path)
    await db_manager.initialize()
    
    yield db_manager
    
    await db_manager.close()
    for ext in ["", "-wal", "-shm"]:
        if os.path.exists(db_path + ext):
            try:
                os.remove(db_path + ext)
            except:
                pass

@pytest.mark.asyncio
async def test_trader_recovery_fidelity(db):
    """
    Test that a Trader can restore its active_positions state 
    from the database with 100% fidelity.
    """
    # 0. Create Profile first (Foreign Key requirement)
    profile_id = await db.add_profile("RecoveryTest", "TEST", "BINANCE")
    
    # Mock exchange object
    class MockExchange:
        def __init__(self):
            self.name = 'BINANCE'
            self.id = 'binance'
    
    trader = Trader(
        exchange=MockExchange(),
        db=db,
        profile_id=profile_id,
        profile_name='RecoveryTest',
        dry_run=True
    )
    
    # 1. Create a complex position state in memory
    pos_key = "BINANCE_BTCUSDT_1h"
    original_pos = {
        "symbol": "BTC/USDT",
        "side": "BUY",
        "qty": 0.1,
        "entry_price": 50000.0,
        "sl": 49000.0,
        "tp": 55000.0,
        "timeframe": "1h",
        "status": "filled",
        "leverage": 10.0,
        "order_id": "order_123",
        "sl_order_id": "sl_456",
        "tp_order_id": "tp_789",
        "timestamp": int(time.time() * 1000),
        "signals_used": ["RSI", "MACD"],
        "entry_confidence": 0.85,
        "snapshot": {"rsi": 30, "macd_hist": -5}
    }
    
    trader.active_positions[pos_key] = original_pos
    
    # 2. Persist to DB
    await trader._update_db_position(pos_key)
    
    # 3. Create a second pending position
    pending_key = "BINANCE_ETHUSDT_15m"
    pending_pos = {
        "symbol": "ETH/USDT",
        "side": "SELL",
        "qty": 1.0,
        "entry_price": 3000.0,
        "sl": 3100.0,
        "tp": 2800.0,
        "timeframe": "15m",
        "status": "pending",
        "leverage": 5.0,
        "order_id": "pending_001",
        "timestamp": int(time.time() * 1000),
        "signals_used": ["ATR"],
        "entry_confidence": 0.6
    }
    trader.active_positions[pending_key] = pending_pos
    await trader._update_db_position(pending_key)
    
    # 4. Clear memory and re-sync
    trader.active_positions = {}
    trader.pending_orders = {}
    await trader.sync_from_db()
    
    # 5. Assertions for Active Position
    assert pos_key in trader.active_positions
    restored = trader.active_positions[pos_key]
    
    assert restored['symbol'] == original_pos['symbol']
    assert restored['side'] == original_pos['side']
    assert restored['qty'] == original_pos['qty']
    assert restored['entry_price'] == original_pos['entry_price']
    assert restored['leverage'] == original_pos['leverage']
    assert restored['status'] == original_pos['status']
    assert restored['order_id'] == original_pos['order_id']
    assert restored['sl_order_id'] == original_pos['sl_order_id']
    assert restored['tp_order_id'] == original_pos['tp_order_id']
    assert restored['signals_used'] == original_pos['signals_used']
    assert restored['entry_confidence'] == original_pos['entry_confidence']
    assert restored['snapshot'] == original_pos['snapshot']
    
    # 6. Assertions for Pending Order
    assert pending_key in trader.pending_orders
    assert trader.pending_orders[pending_key]['status'] == "pending"
    assert trader.pending_orders[pending_key]['leverage'] == 5.0

@pytest.mark.asyncio
async def test_pos_key_collision_prevention(db):
    """Ensure that pos_key protects against duplication collisions."""
    profile_name = f"CollisionTest_{uuid.uuid4().hex[:4]}"
    profile_id = await db.add_profile(profile_name, "TEST", "BINANCE")
    
    class MockExchange:
        def __init__(self):
            self.name = 'BINANCE'
            self.id = 'binance'

    trader = Trader(exchange=MockExchange(), db=db, profile_id=profile_id, profile_name=profile_name)
    
    pos_key = trader._get_pos_key("BTC/USDT", "1h")
    pos_data = {
        "symbol": "BTC/USDT", "side": "BUY", "qty": 0.1, "entry_price": 50000.0,
        "status": "filled", "leverage": 10.0, "timeframe": "1h"
    }
    
    trader.active_positions[pos_key] = pos_data
    # 1. First save (Insert)
    await trader._update_db_position(pos_key)
    first_id = trader.active_positions[pos_key].get('id')
    assert first_id is not None
    
    # 2. Second save (Update) - should NOT create new row
    await trader._update_db_position(pos_key)
    second_id = trader.active_positions[pos_key].get('id')
    assert first_id == second_id
    
    db_conn = await db.get_db()
    async with db_conn.execute("SELECT COUNT(*) FROM trades WHERE profile_id = ? AND pos_key = ?", (profile_id, pos_key)) as cursor:
        row = await cursor.fetchone()
        count = row[0]
        assert count == 1, f"Expected 1 trade for {pos_key}, found {count}"
