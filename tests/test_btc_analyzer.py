import pytest
import pandas as pd
import numpy as np
import asyncio
from unittest.mock import MagicMock, AsyncMock
from src.btc_analyzer import BTCAnalyzer

@pytest.fixture
def mock_btc_data():
    data = {
        'timestamp': pd.to_datetime([1, 2, 3, 4, 5], unit='h'),
        'close': [50000, 51000, 52000, 51500, 53000],
        'EMA_21': [49000] * 5,
        'EMA_50': [48000] * 5,
        'EMA_200': [45000] * 5,
        'RSI_14': [60, 65, 70, 55, 75],
        'MACD': [100, 110, 120, 100, 150],
        'MACD_signal': [80, 90, 100, 90, 110],
        'ATR': [500] * 5,
        'volume': [1000] * 5
    }
    return pd.DataFrame(data)

@pytest.mark.asyncio
async def test_btc_analyzer_update_sentiment(mock_btc_data):
    # Mock DataManager (SQLite)
    mock_db = MagicMock()
    mock_db.upsert_market_sentiment = AsyncMock()
    
    # Mock MarketDataManager
    mock_mdm = MagicMock()
    mock_mdm.get_data_with_features.return_value = mock_btc_data
    mock_mdm.get_data.return_value = mock_btc_data
    
    analyzer = BTCAnalyzer(mock_mdm, mock_db)
    
    # Run update
    result = await analyzer.update_sentiment('BTC/USDT:USDT')
    
    assert result is not None
    assert 'bms' in result
    assert 0.0 <= result['bms'] <= 1.0
    assert result['zone'] in ['GREEN', 'YELLOW', 'RED']
    
    # Verify DB call
    mock_db.upsert_market_sentiment.assert_called_once()
    _, kwargs = mock_db.upsert_market_sentiment.call_args
    assert kwargs['symbol'] == 'BTC/USDT:USDT'
    assert abs(kwargs['bms'] - result['bms']) < 0.001

def test_btc_analyzer_bulk_calculation(mock_btc_data):
    analyzer = BTCAnalyzer(None, None)
    
    bms_df = analyzer.calculate_bulk_sentiment(mock_btc_data)
    
    assert bms_df is not None
    assert 'bms' in bms_df.columns
    assert 'zone' in bms_df.columns
    assert len(bms_df) == len(mock_btc_data)
    
    # Check bullish row
    last_row = bms_df.iloc[-1]
    assert last_row['bms'] > 0.5
    assert last_row['zone'] == 'GREEN'

def test_btc_analyzer_optimize_weights(mock_btc_data):
    # Add a Target column for optimization (24h future returns)
    # Since we only have 5 samples, we'll mock a small return
    df = mock_btc_data.copy()
    
    mock_mdm = MagicMock()
    mock_mdm.get_data_with_features.return_value = df
    
    analyzer = BTCAnalyzer(mock_mdm, None)
    
    initial_weights = analyzer.weights.copy()
    
    # Run optimization (Loop A)
    best_weights = analyzer.optimize_weights()
    
    assert isinstance(best_weights, dict)
    assert 'trend' in best_weights
    # Ensure it updated the internal state
    assert analyzer.weights == best_weights
