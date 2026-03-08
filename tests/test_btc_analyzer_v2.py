import pytest
import pandas as pd
import numpy as np
import asyncio
from unittest.mock import MagicMock, AsyncMock
from src.btc_analyzer import BTCAnalyzer

@pytest.fixture
def mock_btc_data_v2():
    # Long sequence to support rolling windows (window=200 for vol, 50 for dom)
    n = 250
    data = {
        'timestamp': pd.date_range(start='2024-01-01', periods=n, freq='h'),
        'close': np.linspace(50000, 60000, n) + np.random.normal(0, 100, n),
        'high': np.linspace(50500, 60500, n),
        'low': np.linspace(49500, 59500, n),
        'volume': [1000] * n,
        'EMA_21': np.linspace(49000, 59000, n),
        'EMA_50': np.linspace(48000, 58000, n),
        'EMA_200': np.linspace(45000, 55000, n),
        'RSI_14': [60] * n,
        'MACD': [10] * n,
        'MACD_signal': [5] * n,
        'ATR': [500] * n
    }
    df = pd.DataFrame(data)
    # Add a spike at the end for volatility test
    df.loc[df.index[-1], 'ATR'] = 1500 # 3x baseline
    return df

def test_btc_analyzer_v2_bulk_calculation(mock_btc_data_v2):
    analyzer = BTCAnalyzer(None, None)
    
    # Test without BTCDOM (fallback logic)
    bms_df = analyzer.calculate_bulk_sentiment(mock_btc_data_v2)
    
    assert bms_df is not None
    assert 's_vol' in bms_df.columns
    assert 's_dom' in bms_df.columns
    assert 'bms' in bms_df.columns
    
    # Check volatility penalty for the spike at the end
    last_row = bms_df.iloc[-1]
    # Ratio 1500 / 500 = 3.0 > 2.5 -> s_vol should be -1.0
    assert last_row['s_vol'] == -1.0
    
    # Calculation:
    # t_score = 1.0, m_score = 0.2 (default RSI 60, MACD > Sig) -> m_score 0.2 + 0.5 = 0.7
    # synergy = 0.1
    # raw_bms = (1.0*0.4) + (0.7*0.3) + (-1.0*0.2) + (0.0*0.1) + 0.1 = 0.4 + 0.21 - 0.2 + 0.1 = 0.51
    # bms = (0.51 + 1) / 2 = 1.51 / 2 = 0.755
    assert last_row['bms'] > 0.7 # It's still GREEN because trend/momentum are strong
    assert last_row['zone'] == 'GREEN'

def test_btc_analyzer_v2_dominance_real(mock_btc_data_v2):
    analyzer = BTCAnalyzer(None, None)
    df = mock_btc_data_v2.copy()
    
    # Add fake BTCDOM data
    df['BTCDOM_close'] = np.linspace(50, 55, len(df))
    # Make a sharp drop in dominance at the end
    df.loc[df.index[-5:], 'BTCDOM_close'] = 45 
    
    bms_df = analyzer.calculate_bulk_sentiment(df)
    
    # Last rows should have negative dominance score due to drop
    last_rows_dom = bms_df['s_dom'].iloc[-3:]
    assert (last_rows_dom < 0).all()

def test_btc_analyzer_v2_synergy(mock_btc_data_v2):
    analyzer = BTCAnalyzer(None, None)
    df = mock_btc_data_v2.copy()
    
    # Set bullish trend and momentum
    df['close'] = 70000
    df['EMA_21'] = 60000
    df['RSI_14'] = 70
    df['MACD'] = 100
    df['MACD_signal'] = 50
    
    bms_df = analyzer.calculate_bulk_sentiment(df)
    
    # raw_bms should include synergy boost of 0.1
    # Check middle row to avoid boundary issues
    row = bms_df.iloc[100]
    
    # Trend score should be max (0.3+0.3+0.4 = 1.0)
    # Momentum score should be max (~1.0)
    # Synergy should be 0.1
    # raw_bms = 1.0*0.4 + 1.0*0.3 + ... + 0.1
    assert row['s_trend'] == 1.0
    assert row['s_momentum'] == 1.0
    
    # Verify raw_bms is high
    assert row['raw_bms'] > 0.5
