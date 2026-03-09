import pytest
import asyncio
import os
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from src.infrastructure.repository.database import DataManager
from src.data_manager import MarketDataManager

class TestDataManager:
    """
    Test suite for DataManager (Persistence) and MarketDataManager (Data Lifecycle).
    """

    @pytest.fixture
    async def db(self, tmp_path):
        """Create a fresh test database for each test."""
        db_path = str(tmp_path / "test_trading.db")
        await DataManager.clear_instances()
        db = DataManager(db_path)
        await db.initialize()
        await db.add_profile(name="TEST_P1", env="LIVE", exchange="BINANCE")
        
        # Reset MarketDataManager singleton for clean test
        MarketDataManager._instance = None
        
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, db):
        conn = await db.get_db()
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            assert row[0].lower() == 'wal'

    @pytest.mark.asyncio
    async def test_persistence_risk_metrics(self, db):
        await db.set_risk_metric(profile_id=1, metric_name="test_peak", value=50000.5, env="LIVE")
        val = await db.get_risk_metric(profile_id=1, metric_name="test_peak", env="LIVE")
        assert val == 50000.5

    @pytest.mark.asyncio
    async def test_market_data_manager_caching(self, db):
        """Verify MarketDataManager uses DB for persistence."""
        mock_adapter = MagicMock()
        mock_adapter.fetch_ohlcv = AsyncMock(return_value=[
            [1700000000000, 40000, 41000, 39000, 40500, 100]
        ])
        
        adapters = {"BINANCE": mock_adapter}
        mdm = MarketDataManager(db=db, adapters=adapters)
        
        # PATCH src.config DIRECTLY
        with patch("src.config.BINANCE_SYMBOLS", ["BTC/USDT"]), \
             patch("src.config.OHLCV_REFRESH_INTERVAL", 0):
             # Ensure the singleton is using the mocked symbols by re-syncing if needed
             # or just calling the update with explicit symbols
             mdm.data_store.clear()
             await mdm.update_data(symbols=["BTC/USDT"], timeframes=["1h"], force=True)
        
        assert "BINANCE_BTC/USDT_1h" in mdm.data_store
        candles = await db.get_candles("BTC/USDT", "1h")
        assert len(candles) == 1

    @pytest.mark.asyncio
    async def test_concurrent_writes_protection(self, db):
        with patch.object(db, '_write_lock', wraps=db._write_lock) as mock_lock:
            await db.set_risk_metric(1, "metric1", 100, "LIVE")
            assert mock_lock.__aenter__.called

    @pytest.mark.asyncio
    async def test_ohlcv_validation(self, db):
        mdm = MarketDataManager(db=db)
        is_valid, reason = mdm.validate_data(pd.DataFrame(), "BTC", "1h")
        assert is_valid is False
        
        df = pd.DataFrame([{
            'timestamp': pd.Timestamp.now(), 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 10
        }])
        is_valid, reason = mdm.validate_data(df, "BTC", "1h")
        assert is_valid is True
