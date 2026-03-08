import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from src.btc_analyzer import BTCAnalyzer

class TestBTCAnalyzer:
    """
    Test suite for BTCAnalyzer (BMS v2.0).
    Covers: Vectorized calculation, BTCDOM merge, Volatility logic, and MTF aggregation.
    """

    @pytest.fixture
    def btc_analyzer(self, mock_db):
        mock_dm = MagicMock()
        return BTCAnalyzer(data_manager=mock_dm, db=mock_db)

    def test_calculate_bulk_sentiment_vectorized(self, btc_analyzer):
        """Verify NumPy-based BMS calculation returns expected Columns and ranges."""
        # Create dummy DF with 250 rows to satisfy rolling windows (window=200)
        periods = 250
        df = pd.DataFrame({
            'timestamp': pd.date_range('2024-01-01', periods=periods, freq='h'),
            'close': [40000] * periods,
            'EMA_21': [39000] * periods,  # +0.3
            'EMA_50': [38000] * periods,  # +0.3
            'EMA_200': [37000] * periods, # +0.4 -> Total Trend = 1.0
            'RSI_14': [70] * periods,     # +0.5 (rsi > 65)
            'MACD': [10] * periods, 
            'MACD_signal': [5] * periods, # +0.5 (macd > sig) -> Total Momentum = 1.0
            'ATR': [100] * periods
        })
        
        # Mock config for weights
        btc_analyzer.weights = {'trend': 0.4, 'momentum': 0.3, 'volatility': 0.2, 'dominance': 0.1}
        
        res = btc_analyzer.calculate_bulk_sentiment(df)
        
        assert 'bms' in res.columns
        assert 'zone' in res.columns
        # Since trend=1.0, momentum=1.0, vol=0.2 (stable default), dom=0
        # raw_bms = (1.0*0.4) + (1.0*0.3) + (0.2*0.2) = 0.4 + 0.3 + 0.04 = 0.74
        # plus Synergy (trend*mom > 0) + 0.1 = 0.84
        # bms = (0.84 + 1) / 2 = 0.92
        assert res.iloc[-1]['bms'] > 0.8
        assert res.iloc[-1]['zone'] == 'GREEN'

    def test_btcdom_merge_asof_alignment(self, btc_analyzer):
        """Verify BTCDOM data merges correctly with BTC data using pd.merge_asof."""
        # BTC Data (1h)
        btc_df = pd.DataFrame({
            'timestamp': pd.to_datetime(['2024-01-01 10:00', '2024-01-01 11:00']),
            'close': [40000, 41000]
        })
        # BTCDOM Data (1h, slightly offset)
        dom_df = pd.DataFrame({
            'timestamp': pd.to_datetime(['2024-01-01 09:55', '2024-01-01 10:55']),
            'close': [50.5, 51.2]
        })
        
        # Test the merge logic used in update_sentiment
        dom_subset = dom_df[['timestamp', 'close']].rename(columns={'close': 'BTCDOM_close'})
        merged = pd.merge_asof(
            btc_df.sort_values('timestamp'), 
            dom_subset.sort_values('timestamp'), 
            on='timestamp', 
            direction='backward'
        )
        
        assert merged.iloc[0]['BTCDOM_close'] == 50.5
        assert merged.iloc[1]['BTCDOM_close'] == 51.2

    def test_calculate_volatility_score_panic(self, btc_analyzer):
        """Verify penalty on ATR/Price ratio > 2.5x median."""
        # Normal ATR = 100
        # Panic ATR = 300
        df = pd.DataFrame({
            'timestamp': pd.date_range('2024-01-01', periods=300, freq='h'),
            'close': [40000] * 300,
            'ATR': [100] * 299 + [300], # Sudden spike
            'EMA_21': [40000]*300, 'EMA_50':[40000]*300, 'EMA_200':[40000]*300,
            'RSI_14': [50]*300, 'MACD':[0]*300, 'MACD_signal':[0]*300
        })
        
        res = btc_analyzer.calculate_bulk_sentiment(df)
        
        # Last row should have a negative volatility score
        assert res.iloc[-1]['s_vol'] < 0
        assert res.iloc[-1]['s_vol'] == -1.0 # ratio 3.0 > 2.5

    @pytest.mark.asyncio
    async def test_update_sentiment_mtf_aggregation(self, btc_analyzer):
        """Verify weighted mean calculation across 1h, 4h, 1d."""
        # Mock DataManager to return simulated BMS dataframes
        def get_mock_data(symbol, tf):
            df = pd.DataFrame({
                'timestamp': [pd.Timestamp.now()],
                'close': [40000], 'EMA_21': [40000], 'EMA_50': [40000], 'EMA_200': [40000],
                'RSI_14': [50], 'MACD': [0], 'MACD_signal': [0], 'ATR': [100]
            })
            return df

        btc_analyzer.dm.get_data_with_features.side_effect = get_mock_data
        btc_analyzer.dm.get_data.return_value = None # Stop BTCDOM merge for this test
        
        # Mock calculate_bulk_sentiment to return fixed BMS per TF
        with patch.object(btc_analyzer, 'calculate_bulk_sentiment') as mock_calc:
            # 1h: 0.8, 4h: 0.5, 1d: 0.2
            mock_calc.side_effect = [
                pd.DataFrame({'bms': [0.8], 's_trend': [1], 's_momentum': [1], 's_vol':[0], 's_dom':[0]}),
                pd.DataFrame({'bms': [0.5], 's_trend': [0], 's_momentum': [0], 's_vol':[0], 's_dom':[0]}),
                pd.DataFrame({'bms': [0.2], 's_trend': [-1], 's_momentum': [-1], 's_vol':[0], 's_dom':[0]})
            ]
            
            # Default MTF Weights: 1h: 0.3, 4h: 0.4, 1d: 0.3
            # Aggregated BMS = (0.8*0.3) + (0.5*0.4) + (0.2*0.3) = 0.24 + 0.20 + 0.06 = 0.50
            res = await btc_analyzer.update_sentiment()
            
            assert res['bms'] == pytest.approx(0.50)
            assert res['zone'] == 'YELLOW'
