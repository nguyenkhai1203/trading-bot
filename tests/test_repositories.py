import pytest
import os
import time
import uuid
from src.infrastructure.repository.database import DataManager
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.infrastructure.repository.sqlite_profile_repository import SQLiteProfileRepository
from src.infrastructure.repository.sqlite_sentiment_repository import SQLiteSentimentRepository
from src.domain.models import Trade

# Shared ID for the duration of the test session to avoid collisions
TEST_DB_ID = uuid.uuid4().hex[:8]

def get_test_db_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"repo_test_{TEST_DB_ID}.db")

@pytest.fixture(scope="function")
async def db():
    path = get_test_db_path()
    manager = DataManager(path)
    await manager.initialize()
    yield manager
    await manager.close()
    if os.path.exists(path):
        os.remove(path)

@pytest.mark.asyncio
async def test_profile_repository(db):
    repo = SQLiteProfileRepository(db)
    
    # 1. Add profile
    pid = await db.add_profile("RepoUser", "TEST", "BYBIT")
    
    # 2. Get active
    profiles = await repo.get_active_profiles()
    assert any(p['name'] == "RepoUser" for p in profiles)

@pytest.mark.asyncio
async def test_trade_repository(db):
    repo = SQLiteTradeRepository(db)
    
    # 1. Setup profile
    pid = await db.add_profile("TradeRepoUser", "TEST", "BINANCE")
    
    # 2. Save trade
    trade = Trade(
        profile_id=pid,
        exchange='BINANCE',
        symbol='BTCUSDT',
        side='BUY',
        qty=1.0,
        entry_price=40000.0,
        entry_time=int(time.time() * 1000),
        leverage=10.0,
        timeframe="1h",
        pos_key="BINANCE_BTCUSDT_1h"
    )
    tid = await repo.save_trade(trade)
    assert tid > 0
    
    # 3. Get active
    active = await repo.get_active_positions(pid)
    assert len(active) == 1
    assert active[0].symbol == 'BTCUSDT'
    
    # 4. Update status
    await repo.update_status(tid, 'CLOSED', exit_price=41000.0, pnl=1000.0)
    
    active_after = await repo.get_active_positions(pid)
    assert len(active_after) == 0
