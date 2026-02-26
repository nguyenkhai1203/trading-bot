import pytest
import asyncio
import os
import sys
import json
import time
import sqlite3
import uuid
import pytest_asyncio

# Shared ID for the duration of the test session to avoid collisions
TEST_DB_ID = uuid.uuid4().hex[:8]

def get_test_db_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"trading_test_{TEST_DB_ID}.db")

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.database import DataManager

@pytest_asyncio.fixture(autouse=True)
async def cleanup_db():
    """Cleanup the database before and after tests."""
    await DataManager.clear_instances()
    
    db_path = get_test_db_path()
    
    def remove_files():
        for ext in ["", "-wal", "-shm"]:
            f = db_path + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass
    
    remove_files()
    yield
    await DataManager.clear_instances()
    remove_files()

@pytest.mark.asyncio
async def test_database_basic_crud():
    db_path = get_test_db_path()
    db = DataManager(db_path)
    await db.initialize()
    
    # 1. Add Profile
    profile_id = await db.add_profile("TestUser", "TEST", "BINANCE")
    assert profile_id > 0
    
    # 2. Save Position
    trade_id = await db.save_position({
        'profile_id': profile_id,
        'exchange': 'BINANCE',
        'exchange_order_id': '12345',
        'symbol': 'BTCUSDT',
        'side': 'BUY',
        'qty': 0.1,
        'entry_price': 50000,
        'status': 'OPENED'
    })
    assert trade_id > 0
    
    # 3. Get Active Positions
    active = await db.get_active_positions(profile_id)
    assert len(active) == 1
    assert active[0]['symbol'] == 'BTCUSDT'
    
    # 4. Update Position Status
    await db.update_position_status(trade_id, 'CLOSED', exit_price=51000, pnl=100)
    
    # Verify it's no longer active
    active_after = await db.get_active_positions(profile_id)
    assert len(active_after) == 0
    await db.close()

@pytest.mark.asyncio
async def test_concurrent_writes():
    """Test WAL mode by running 10 concurrent writes."""
    db_path = get_test_db_path()
    db = DataManager(db_path)
    await db.initialize()
    profile_id = await db.add_profile("StressUser", "TEST", "BINANCE")
    
    async def insert_trade(i):
        return await db.save_position({
            'profile_id': profile_id,
            'exchange': 'BINANCE',
            'exchange_order_id': f'order_{i}',
            'symbol': 'ETHUSDT',
            'side': 'BUY',
            'status': 'ACTIVE'
        })
        
    tasks = [insert_trade(i) for i in range(50)]
    results = await asyncio.gather(*tasks)
    
    # Ensure 50 distinct trade IDs were returned
    assert len(set(results)) == 50
    
    active = await db.get_active_positions(profile_id)
    assert len(active) == 50
    await db.close()

@pytest.mark.asyncio
async def test_foreign_key_violation():
    db_path = get_test_db_path()
    db = DataManager(db_path)
    await db.initialize()
    
    # Attempt to insert trade for non-existent profile (e.g. 999)
    with pytest.raises(sqlite3.IntegrityError):
        await db.save_position({
            'profile_id': 999,
            'exchange': 'BINANCE',
            'exchange_order_id': 'error_1',
            'symbol': 'SOLUSDT',
            'side': 'BUY'
        })
    await db.close()

@pytest.mark.asyncio
async def test_ohlcv_ttl_purge():
    db_path = get_test_db_path()
    db = DataManager(db_path)
    await db.initialize()
    
    # Insert dummy candles
    await db.upsert_candles('BTCUSDT', '15m', [
        [1000000, 10, 20, 5, 15, 100]
    ])
    
    # Manually hack last_used_at to be very old (60 days ago) using internal _execute_write
    old_time = int(time.time() - 60 * 86400)
    await db._execute_write("UPDATE ohlcv_cache SET last_used_at = ?", (old_time,))
    
    # Run purge with 30 days cutoff
    await db.purge_old_candles(days=30)
    
    # Verify it was deleted
    candles = await db.get_candles('BTCUSDT', '15m')
    assert len(candles) == 0
    await db.close()

@pytest.mark.asyncio
async def test_idempotent_upsert():
    db_path = get_test_db_path()
    db = DataManager(db_path)
    await db.initialize()
    
    candles = [
        [2000000, 1, 2, 0.5, 1.5, 10]
    ]
    # Insert twice
    await db.upsert_candles('ETHUSDT', '1h', candles)
    await db.upsert_candles('ETHUSDT', '1h', candles)
    
    res = await db.get_candles('ETHUSDT', '1h')
    assert len(res) == 1  # Should only be one record due to PRIMARY KEY
    await db.close()
