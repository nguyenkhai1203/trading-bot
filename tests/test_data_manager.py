import pytest
import asyncio
import os
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from data_manager import MarketDataManager

class TestDataManager:
    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset the Singleton instance before each test to ensure clean state."""
        MarketDataManager._instance = None
        yield
        MarketDataManager._instance = None

    @pytest.fixture
    def mock_adapters(self):
        mock_binance = MagicMock()
        mock_binance.name = 'BINANCE'
        mock_binance.fetch_ohlcv = AsyncMock()
        mock_binance.fetch_tickers = AsyncMock()
        
        mock_bybit = MagicMock()
        mock_bybit.name = 'BYBIT'
        mock_bybit.fetch_ohlcv = AsyncMock()
        mock_bybit.fetch_tickers = AsyncMock()
        
        return {'BINANCE': mock_binance, 'BYBIT': mock_bybit}

    @pytest.fixture
    def dm(self, mock_adapters):
        # Inject mock adapters
        manager = MarketDataManager(adapters=mock_adapters)
        # Prevent actually saving to disk during tests unless explicitly tested
        manager._save_to_disk_mock = True
        return manager

    @pytest.mark.asyncio
    async def test_singleton_pattern(self, mock_adapters):
        """Test that MarketDataManager correctly implements the Singleton pattern."""
        dm1 = MarketDataManager(adapters=mock_adapters)
        dm2 = MarketDataManager(adapters=mock_adapters)
        
        assert dm1 is dm2
        assert dm1.initialized is True

    @pytest.mark.asyncio
    @patch('config.BINANCE_SYMBOLS', ['BTC/USDT'])
    @patch('config.BYBIT_SYMBOLS', ['BTC/USDT'])
    async def test_update_data_fetch_and_store(self, dm):
        """Test fetching OHLCV data and storing it in data_store."""
        symbol = "BTC/USDT"
        tf = "1h"
        
        # Mock API Response for Binance
        mock_ohlcv = [
            [1600000000000, 40000, 41000, 39000, 40500, 100],
            [1600003600000, 40500, 42000, 40000, 41500, 150]
        ]
        dm.adapters['BINANCE'].fetch_ohlcv.return_value = mock_ohlcv
        
        # Call update_data (force=True to bypass rate limiter)
        with patch('os.path.exists', return_value=False), patch('os.makedirs'), patch('pandas.DataFrame.to_csv'): 
            # prevent actual disk write and read
            result = await dm.update_data([symbol], [tf], force=True)
        
        assert result is True
        
        # 1. API should be called
        dm.adapters['BINANCE'].fetch_ohlcv.assert_called_once_with(symbol, tf, limit=50)
        
        # 2. Data should be in store
        key = f"BINANCE_{symbol}_{tf}"
        assert key in dm.data_store
        
        df = dm.data_store[key]
        assert len(df) == 2
        assert df.iloc[-1]['close'] == 41500
        assert 'timestamp' in df.columns

    @pytest.mark.asyncio
    @patch('config.BINANCE_SYMBOLS', ['BTC/USDT'])
    async def test_update_tickers_live_price(self, dm):
        """Test that update_tickers correctly updates the last candle's close price."""
        symbol = "BTC/USDT"
        tf = "1h"
        key = f"BINANCE_{symbol}_{tf}"
        
        # Pre-seed data_store with a dummy dataframe
        df = pd.DataFrame({
            'timestamp': [pd.to_datetime(1600000000000, unit='ms')],
            'open': [40000], 'high': [41000], 'low': [39000], 'close': [40500], 'volume': [100]
        })
        dm.data_store[key] = df
        
        # Mock fetch_tickers response
        dm.adapters['BINANCE'].fetch_tickers.return_value = {
            symbol: {'last': 42999.0}
        }
        
        # Bypass rate limiter
        dm._last_ticker_update = 0 
        
        updated_count = await dm.update_tickers([symbol])
        
        assert updated_count > 0
        # The last candle's 'close' should now be the new live ticker price
        assert dm.data_store[key].iloc[-1]['close'] == 42999.0

    def test_validate_data_integrity(self, dm):
        """Test the validate_data function rejects bad dataframes."""
        
        # 1. None DF
        is_valid, _ = dm.validate_data(None, "BTC/USDT", "1h")
        assert is_valid is False
        
        # 2. Missing columns
        bad_df = pd.DataFrame({'close': [1,2,3]})
        is_valid, reason = dm.validate_data(bad_df, "BTC/USDT", "1h")
        assert is_valid is False
        assert "Missing" in reason
        
        # 3. NaN in close
        nan_df = pd.DataFrame({
            'timestamp': [1,2,3,4,5],
            'open': [1,2,3,4,5], 'high': [1,2,3,4,5], 'low': [1,2,3,4,5],
            'close': [10, 20, None, 40, 50],
            'volume': [1,2,3,4,5]
        })
        is_valid, reason = dm.validate_data(nan_df, "BTC/USDT", "1h")
        assert is_valid is False
        assert "NaN" in reason
