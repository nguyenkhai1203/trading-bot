import pytest
import pandas as pd
import numpy as np
import time
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from src.data_manager import MarketDataManager
from src.infrastructure.repository.database import DataManager

class TestDataManager:
    """
    Test suite for DataManager persistence and WAL mode.
    """
    @pytest.fixture
    async def db(self, tmp_path):
        db_path = str(tmp_path / "test_trading.db")
        await DataManager.clear_instances()
        db = DataManager(db_path)
        await db.initialize()
        await db.add_profile(name="TEST_P1", env="LIVE", exchange="BINANCE")
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

    @pytest.fixture
    def mdm(self, db, mock_adapter):
        """Mocked MarketDataManager for validation tests."""
        MarketDataManager._instance = None
        return MarketDataManager(db=db, adapters={"BINANCE": mock_adapter})

    @pytest.mark.asyncio
    async def test_ohlcv_validation(self, mdm):
        is_valid, reason = mdm.validate_data(pd.DataFrame(), "BTC", "1h")
        assert is_valid is False
        df = pd.DataFrame([{
            'timestamp': pd.Timestamp.now(), 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 10
        }])
        is_valid, reason = mdm.validate_data(df, "BTC", "1h")
        assert is_valid is True

    @pytest.mark.asyncio
    async def test_market_data_manager_caching(self, mdm, db, mock_adapter):
        """Verify MarketDataManager uses DB for persistence."""
        symbol = "BTC/USDT"
        tf = "1h"
        
        mdm.data_store.clear()
        mdm._ohlcv_sync_state.clear()
        mdm.set_active_symbols_provider(lambda: [symbol])
        
        with patch("src.config.BINANCE_SYMBOLS", [symbol]), \
             patch("src.config.OHLCV_REFRESH_INTERVAL", 0):
             await mdm.update_data(symbols=[symbol], timeframes=[tf], force=True)
        
        assert f"BINANCE_{symbol}_{tf}" in mdm.data_store
        candles = await db.get_candles(symbol, tf)
        assert len(candles) >= 1

    @pytest.mark.asyncio
    async def test_concurrent_writes_protection(self, db):
        with patch.object(db, '_write_lock', wraps=db._write_lock) as mock_lock:
            await db.set_risk_metric(1, "metric1", 100, "LIVE")
            assert mock_lock.__aenter__.called

class TestMarketDataManagerSync:
    """
    Comprehensive test suite for MarketDataManager's Smart Sync and Bridging logic.
    Follows AAA pattern and CONTRIBUTING_TESTS.md guidelines.
    """

    @pytest.fixture
    async def db(self, tmp_path):
        """Provides a fresh in-memory SQLite database."""
        db_path = str(tmp_path / "test_sync.db")
        await DataManager.clear_instances()
        db = DataManager(db_path)
        await db.initialize()
        yield db
        await db.close()
        await DataManager.clear_instances()

    @pytest.fixture
    def mdm(self, db, mock_adapter):
        """Provides a MarketDataManager instance with mocked adapter."""
        MarketDataManager._instance = None
        adapters = {"BINANCE": mock_adapter}
        return MarketDataManager(db=db, adapters=adapters)

    @pytest.fixture
    def sample_df(self):
        """Provides a minimal OHLCV DataFrame."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        data = {
            'timestamp': [now - timedelta(hours=1), now],
            'open': [50000.0, 50100.0],
            'high': [50500.0, 50200.0],
            'low': [49900.0, 50050.0],
            'close': [50100.0, 50150.0],
            'volume': [10.0, 15.0]
        }
        return pd.DataFrame(data)

    # --- 1. _patch_current_candle Tests ---

    @pytest.mark.parametrize("price,expected_close,expected_high,expected_low", [
        (50300.0, 50300.0, 50300.0, 50050.0), # New High
        (49800.0, 49800.0, 50200.0, 49800.0), # New Low
        (50120.0, 50120.0, 50200.0, 50050.0), # Inside Range
    ])
    def test_patch_current_candle_updates_correctly(self, mdm, sample_df, price, expected_close, expected_high, expected_low):
        """
        [HAPPY PATH] Verifies that _patch_current_candle correctly updates Close/High/Low extremes.
        """
        # Arrange & Act
        patched_df = mdm._patch_current_candle(sample_df.copy(), price)
        last_row = patched_df.iloc[-1]
        
        # Assert
        assert last_row['close'] == expected_close
        assert last_row['high'] == expected_high
        assert last_row['low'] == expected_low
        assert len(patched_df) == len(sample_df) # No new rows added

    def test_patch_current_candle_handles_empty_df(self, mdm):
        """[EDGE CASE] Handles empty DataFrame without crashing."""
        df = pd.DataFrame()
        result = mdm._patch_current_candle(df, 50000.0)
        assert result.empty

    # --- 2. _should_fetch_new_candle Tests ---

    def test_should_fetch_new_candle_on_boundary(self, mdm, mock_adapter):
        """
        [HAPPY PATH] Returns True when exchange time crosses into a new candle period.
        """
        symbol = "BTC/USDT"
        tf = "1h"
        # Set sync state to 1 hour ago
        one_hour_ago = (1704067200000 / 1000) - 3600
        mdm._ohlcv_sync_state[f"BINANCE_{symbol}_{tf}"] = one_hour_ago
        
        # mock_adapter has milliseconds() returning 1704067200000 (boundary)
        assert mdm._should_fetch_new_candle("BINANCE", symbol, tf, mock_adapter) is True

    def test_should_fetch_new_candle_skips_within_period(self, mdm, mock_adapter, sample_df):
        """
        [HAPPY PATH] Returns False when still within the same candle period and data is fresh.
        """
        symbol = "BTC/USDT"
        tf = "1h"
        key = f"BINANCE_{symbol}_{tf}"
        
        # Sync with mock adapter time (Jan 1, 2024 00:00:00) = 1704067200
        mock_ts = 1704067200
        mdm._ohlcv_sync_state[key] = mock_ts
        mdm.data_store[key] = sample_df 
        
        # Patch last candle timestamp to be within mocking period
        sample_df.iloc[-1, sample_df.columns.get_loc('timestamp')] = datetime.fromtimestamp(mock_ts, tz=timezone.utc)
        
        # Fake fresh ticker (same as mock clock)
        mdm._ticker_cache[f"BINANCE_{symbol}"] = {'last': 50000, 'timestamp': mock_ts}

        assert mdm._should_fetch_new_candle("BINANCE", symbol, tf, mock_adapter) is False

    @pytest.mark.parametrize("delay_seconds,expected", [
        (40, True),  # Ticker stale (>30s) -> Should fetch
        (5, False),  # Ticker fresh -> Should skip (if boundary not crossed)
    ])
    def test_should_fetch_new_candle_freshness_check(self, mdm, mock_adapter, sample_df, delay_seconds, expected):
        """
        [RESILIENCE] Forces fetch if batch ticker data is excessively delayed.
        """
        symbol = "BTC/USDT"
        tf = "1h"
        key = f"BINANCE_{symbol}_{tf}"
        mock_ts = 1704067200
        
        mdm._ohlcv_sync_state[key] = mock_ts
        mdm.data_store[key] = sample_df
        
        # Set last candle ts
        sample_df.iloc[-1, sample_df.columns.get_loc('timestamp')] = datetime.fromtimestamp(mock_ts, tz=timezone.utc)
        
        # Set ticker timestamp relative to mock clock
        mdm._ticker_cache[f"BINANCE_{symbol}"] = {'last': 50000, 'timestamp': mock_ts - delay_seconds}
        
        # Align mock adapter clock
        mock_adapter.exchange.milliseconds.return_value = int(mock_ts * 1000)

        assert mdm._should_fetch_new_candle("BINANCE", symbol, tf, mock_adapter) is expected

    # --- 3. update_tickers & Signal Refresh ---

    @pytest.mark.asyncio
    async def test_update_tickers_refreshes_features(self, mdm, mock_adapter, sample_df):
        """
        [STATE MUTATION] Verifies that patching price triggers indicator recalculation.
        """
        # Arrange
        symbol = "BTC/USDT"
        key = f"BINANCE_{symbol}_1h"
        mdm.data_store[key] = sample_df
        mdm._last_ticker_update = 0 # Bypass throttle
        
        # Mock FeatureEngineer
        mock_fe = MagicMock()
        mock_fe.calculate_features = MagicMock(side_effect=lambda df: df.assign(refreshed=True))
        mdm._feature_engineer = mock_fe
        
        # Act
        with patch("src.config.BINANCE_SYMBOLS", [symbol]):
            await mdm.update_tickers([symbol])
        
        # Assert
        assert mdm.data_store[key].iloc[-1]['close'] == 50050.0 
        
        # Verify that features_cache is cleared (so next call triggers mock_fe)
        assert key not in mdm.features_cache
        
        # Request data via getter to trigger recalculation
        fresh_df = mdm.get_data_with_features(symbol, "1h", "BINANCE")
        assert 'refreshed' in fresh_df.columns
        assert mock_fe.calculate_features.called

    @pytest.mark.asyncio
    async def test_update_tickers_invalidates_features_cache(self, mdm, mock_adapter, sample_df):
        """
        [REGRESSION] Verifies that update_tickers clears features_cache to prevent stale signals.
        """
        # Arrange
        symbol = "BTC/USDT"
        key = f"BINANCE_{symbol}_1h"
        mdm.data_store[key] = sample_df
        mdm._last_ticker_update = 0
        
        # Populate cache with "old" features
        old_df = sample_df.copy()
        old_df['indicator'] = 100
        mdm.features_cache[key] = old_df
        
        # Mock FeatureEngineer to return "new" features
        mock_fe = MagicMock()
        mock_fe.calculate_features = MagicMock(side_effect=lambda df: df.assign(indicator=200))
        mdm._feature_engineer = mock_fe
        
        # Act
        with patch("src.config.BINANCE_SYMBOLS", [symbol]):
            await mdm.update_tickers([symbol])
            
        # Verify cache was cleared
        assert key not in mdm.features_cache
        
        # Verify next getter call gets fresh data
        fresh_df = mdm.get_data_with_features(symbol, "1h", "BINANCE")
        assert fresh_df.iloc[-1]['indicator'] == 200
        assert mock_fe.calculate_features.called

    # --- 4. update_data Integration & Logic ---

    @pytest.mark.asyncio
    async def test_update_data_skips_fetch_when_within_boundary(self, mdm, mock_adapter, sample_df):
        """
        [PERFORMANCE] Verifies no fetch_ohlcv calls are made when within a period.
        """
        # Arrange
        symbol = "BTC/USDT"
        tf = "1h"
        key = f"BINANCE_{symbol}_{tf}"
        mdm.data_store[key] = sample_df
        
        # Simulate that we just synced this period (aligned with mock clock)
        mock_ts = 1704067200
        mdm._ohlcv_sync_state[key] = mock_ts
        mdm._ticker_cache[f"BINANCE_{symbol}"] = {'last': 50000, 'timestamp': mock_ts}
        sample_df.iloc[-1, sample_df.columns.get_loc('timestamp')] = datetime.fromtimestamp(mock_ts, tz=timezone.utc)
        
        # Mock adapter to track calls
        mock_adapter.fetch_ohlcv = AsyncMock()
        
        # Act 
        with patch("src.config.BINANCE_SYMBOLS", [symbol]):
            await mdm.update_data([symbol], [tf], force=True)
        
        # Assert
        mock_adapter.fetch_ohlcv.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_data_performs_fetch_at_boundary(self, mdm, mock_adapter):
        """
        [HAPPY PATH] Verifies fetch_ohlcv IS called when a boundary is crossed.
        """
        # Arrange
        symbol = "BTC/USDT"
        tf = "1h"
        mdm._ohlcv_sync_state[f"BINANCE_{symbol}_{tf}"] = 0 # Forces boundary detection
        
        # Act
        with patch("src.config.BINANCE_SYMBOLS", [symbol]), \
             patch("src.config.OHLCV_REFRESH_INTERVAL", 0):
            await mdm.update_data([symbol], [tf], force=True)
        
        # Assert
        mock_adapter.fetch_ohlcv.assert_called_with(symbol, tf, limit=50)

    # --- 5. Multi-Timeframe Isolation ---

    @pytest.mark.asyncio
    async def test_multi_timeframe_isolation(self, mdm, mock_adapter, sample_df):
        """
        [ISOLATION] Patching 1h candle should not accidentally corrupt 1d if not specified.
        """
        # Arrange
        symbol = "BTC/USDT"
        key_1h = f"BINANCE_{symbol}_1h"
        key_1d = f"BINANCE_{symbol}_1d"
        
        df_1h = sample_df.copy()
        df_1d = sample_df.copy()
        df_1d.iloc[-1, df_1d.columns.get_loc('close')] = 40000.0 # Distinct old price
        
        mdm.data_store[key_1h] = df_1h
        mdm.data_store[key_1d] = df_1d
        
        # Update ticker: 50050.0
        mdm._last_ticker_update = 0
        with patch("src.config.BINANCE_SYMBOLS", [symbol]), \
             patch("src.config.BYBIT_SYMBOLS", []):
            await mdm.update_tickers([symbol])
        
        # Assert
        assert mdm.data_store[key_1h].iloc[-1]['close'] == 50050.0
        assert mdm.data_store[key_1d].iloc[-1]['close'] == 50050.0 # Shared symbol should update both
        
        # But wait, isolation check: if I have a symbol on EXCHANGE1 and EXCHANGE2
        # Patching BINANCE should not touch BYBIT
        key_bybit = f"BYBIT_{symbol}_1h"
        mdm.data_store[key_bybit] = sample_df.set_index('timestamp').copy().reset_index() # Fresh copy
        old_bybit_close = mdm.data_store[key_bybit].iloc[-1]['close']
        
        mdm._last_ticker_update = 0
        with patch("src.config.BINANCE_SYMBOLS", [symbol]), \
             patch("src.config.BYBIT_SYMBOLS", []):
            await mdm.update_tickers([symbol])
        
        # Bybit adapter wasn't in tickers response from BINANCE adapter
        assert mdm.data_store[key_bybit].iloc[-1]['close'] == old_bybit_close
        # Actually our mock_adapter is global. In a real scenario, the prefix check handles this.

    # --- 6. Exception Resilience ---

    @pytest.mark.asyncio
    async def test_exception_recovery_on_boundary_fetch(self, mdm, mock_adapter, sample_df):
        """
        [RESILIENCE] Gracefully handles network errors during boundary fetch.
        """
        # Arrange
        symbol = "BTC/USDT"
        tf = "1h"
        mdm._ohlcv_sync_state[f"BINANCE_{symbol}_{tf}"] = 0 # Force fetch
        
        # Mock network failure
        mock_adapter.fetch_ohlcv = AsyncMock(side_effect=Exception("Network Timeout"))
        
        # Act
        await mdm.update_data([symbol], [tf], force=True)
        
        # Assert: No crash, just logged error (check log with caplog if needed)
        # We verify that fetch_and_store task completed without raising to top level
        assert True 

    # --- 7. Ticker Cache TTL ---

    @pytest.mark.asyncio
    async def test_fetch_ticker_uses_cache(self, mdm, mock_adapter):
        """[PERFORMANCE] Verifies ticker calls are throttled by 2s TTL cache."""
        symbol = "BTC/USDT"
        mock_adapter.fetch_ticker = AsyncMock(return_value={'last': 50000})
        
        # 1st call
        await mdm.fetch_ticker(symbol)
        assert mock_adapter.fetch_ticker.call_count == 1
        
        # 2nd call (instant) -> Should use cache
        await mdm.fetch_ticker(symbol)
        assert mock_adapter.fetch_ticker.call_count == 1
        
        # 3rd call (after 2s)
        with patch("src.data_manager.time.time", return_value=time.time() + 3):
            await mdm.fetch_ticker(symbol)
            assert mock_adapter.fetch_ticker.call_count == 2
