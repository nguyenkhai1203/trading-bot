import pytest
import sys
import os
import pandas as pd
import numpy as np

# Add src to path for all tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

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
