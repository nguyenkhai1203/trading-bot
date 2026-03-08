import pytest
import sys
import os
import pandas as pd
import numpy as np
from unittest.mock import MagicMock

# Mock telegram module before it's imported anywhere
mock_telegram = MagicMock()
sys.modules['telegram'] = mock_telegram
sys.modules['telegram.ext'] = MagicMock()
sys.modules['telegram.error'] = MagicMock()

# Add project root to path for all tests
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Global flag for isolation
os.environ['TRADING_BOT_TEST_MODE'] = 'True'

@pytest.fixture
def sample_ohlcv_data():
    """Provides a sample DataFrame with OHLCV data for testing indicators."""
    np.random.seed(42)
    rows = 300
    dates = pd.date_range('2024-01-01', periods=rows, freq='h')
    
    # Generate some semi-realistic price action
    close = 50000 + np.cumsum(np.random.randn(rows) * 100)
    high = close + np.random.rand(rows) * 50
    low = close - np.random.rand(rows) * 50
    open_price = close - np.random.randn(rows) * 20
    volume = np.random.rand(rows) * 10
    
    df = pd.DataFrame({
        'timestamp': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df

@pytest.fixture
def mock_exchange():
    """Mock CCXT async exchange with basic order/position state."""
    exchange = MagicMock()
    exchange.fetch_balance = MagicMock(return_value={'free': {'USDT': 10000}, 'total': {'USDT': 10000}})
    exchange.fetch_positions = MagicMock(return_value=[])
    exchange.create_order = MagicMock(return_value={'id': 'test_order_123', 'status': 'open'})
    exchange.cancel_order = MagicMock(return_value={'id': 'test_order_123', 'status': 'canceled'})
    return exchange

@pytest.fixture
async def mock_db():
    """Provides an in-memory DataManager instance for testing."""
    from src.infrastructure.repository.database import DataManager
    # Clear existing instances to ensure isolation
    await DataManager.clear_instances()
    db = await DataManager.get_instance(env='TEST')
    yield db
    await DataManager.clear_instances()

@pytest.fixture
def strategy_analyzer(tmp_path):
    """Initialized StrategyAnalyzer with a temporary data directory."""
    from src.analyzer import StrategyAnalyzer
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return StrategyAnalyzer(data_dir=str(data_dir))
