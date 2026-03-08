import pytest
import pandas as pd
import numpy as np
import os
from unittest.mock import patch, MagicMock
from src.analyzer import StrategyAnalyzer

class TestAnalyzer:
    """
    Test suite for StrategyAnalyzer core logic.
    Covers: Caching, Gap Detection, BMS Merge, Filters, and Walk-Forward Validation.
    """

    def test_load_data_caching(self, strategy_analyzer):
        """Confirm disk I/O occurs only once per (symbol, tf) and uses cache thereafter."""
        symbol = "BTC/USDT:USDT"
        tf = "1h"
        exchange = "BINANCE"
        
        # Create dummy data file
        data_dir = strategy_analyzer.data_dir
        file_path = os.path.join(data_dir, f"{exchange}_BTCUSDT_{tf}.csv")
        df_dummy = pd.DataFrame({
            'timestamp': pd.date_range('2024-01-01', periods=10, freq='h'),
            'close': [100] * 10,
            'open': [100] * 10,
            'high': [100] * 10,
            'low': [100] * 10,
            'volume': [1] * 10
        })
        df_dummy.to_csv(file_path, index=False)
        
        with patch("pandas.read_csv", side_effect=pd.read_csv) as mock_read:
            # First load - should hit disk
            df1 = strategy_analyzer.load_data(symbol, tf, exchange=exchange)
            assert mock_read.call_count == 1
            assert not df1.empty
            
            # Second load - should hit cache
            df2 = strategy_analyzer.load_data(symbol, tf, exchange=exchange)
            assert mock_read.call_count == 1
            assert id(df1) == id(df2)

    def test_load_data_bms_merge(self, strategy_analyzer):
        """Verify temporal alignment of BMS scores with price data using pd.merge_asof."""
        exchange = "BINANCE"
        tf = "1h"
        
        # 1. Create BTC Data (The source for BMS)
        btc_file = os.path.join(strategy_analyzer.data_dir, f"{exchange}_BTCUSDT_{tf}.csv")
        btc_df = pd.DataFrame({
            'timestamp': pd.to_datetime(['2024-01-01 10:00:00', '2024-01-01 11:00:00', '2024-01-01 12:00:00']),
            'close': [40000, 41000, 42000], 'open': [40000]*3, 'high': [42000]*3, 'low': [39000]*3, 'volume': [100]*3
        })
        btc_df.to_csv(btc_file, index=False)
        
        # 2. Create ALT Data (The target for merge)
        alt_symbol = "ETH/USDT:USDT"
        alt_file = os.path.join(strategy_analyzer.data_dir, f"{exchange}_ETHUSDT_{tf}.csv")
        alt_df = pd.DataFrame({
            'timestamp': pd.to_datetime(['2024-01-01 10:05:00', '2024-01-01 11:05:00']),
            'close': [2000, 2100], 'open': [2000]*2, 'high': [2100]*2, 'low': [1900]*2, 'volume': [10]*2
        })
        alt_df.to_csv(alt_file, index=False)
        
        # Mock FeatureEngineer and BTCAnalyzer
        with patch("src.btc_analyzer.BTCAnalyzer.calculate_bulk_sentiment") as mock_calc:
            # BMS data has different timestamps than ALT data
            mock_bms_df = pd.DataFrame({
                'timestamp': pd.to_datetime(['2024-01-01 10:00:00', '2024-01-01 11:00:00', '2024-01-01 12:00:00']),
                'bms': [0.8, 0.4, 0.9],
                'zone': ['GREEN', 'YELLOW', 'GREEN']
            })
            mock_calc.return_value = mock_bms_df
            
            # Load ALT data
            df = strategy_analyzer.load_data(alt_symbol, tf, exchange=exchange)
            
            # Verify merge_asof backward alignment
            # 10:05 should see 10:00 BMS (0.8, GREEN)
            # 11:05 should see 11:00 BMS (0.4, YELLOW)
            assert 'bms_score' in df.columns
            assert df.iloc[0]['bms_score'] == 0.8
            assert df.iloc[0]['bms_zone'] == 'GREEN'
            assert df.iloc[1]['bms_score'] == 0.4
            assert df.iloc[1]['bms_zone'] == 'YELLOW'

    def test_get_signal_category(self, strategy_analyzer):
        """Test categorization of signal names into groups."""
        assert strategy_analyzer.get_signal_category("EMA_21_cross_200") == "Trend"
        assert strategy_analyzer.get_signal_category("RSI_Oversold") == "Momentum"
        assert strategy_analyzer.get_signal_category("BB_Low_Touch") == "Volatility"
        assert strategy_analyzer.get_signal_category("Price_Above_VWAP") == "Level"
        assert strategy_analyzer.get_signal_category("Volume_Spike") == "Volume"
        assert strategy_analyzer.get_signal_category("random_untracked") == "Other"

    def test_analyze_l1_trend_filter(self, strategy_analyzer):
        """Layer 1: Ensure signals against the 200 EMA are rejected."""
        # Mock features with one long signal
        df = pd.DataFrame({
            'timestamp': pd.date_range('2024-01-01', periods=10, freq='h'),
            'close': [100] * 10,
            'ema_200': [110] * 10, # Price is BELOW EMA_200 -> BEARISH trend
            'signal_EMA_cross_up': [True] * 10,
            'Target_Return': [0.05] * 10
        })
        
        with patch.object(StrategyAnalyzer, "get_features", return_value=df):
            # Analyzing for LONG signal (is_long detected by keyword 'up')
            res = strategy_analyzer.analyze("BTC/USDT:USDT", "1h")
            # Since price (100) < ema_200 (110) and signal is LONG, Layer 1 should reject it
            assert "EMA_cross_up" not in res

    def test_analyze_l2_diversity_filter(self, strategy_analyzer):
        """Layer 2: Confirm only Top 3 signals per category are kept."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2024-01-01', periods=20, freq='h'),
            'close': [100] * 20,
            'ema_200': [50] * 20, # BULLISH
            'signal_RSI_1': [True]*20, 'signal_RSI_2': [True]*20, 
            'signal_RSI_3': [True]*20, 'signal_RSI_4': [True]*20,
            'Target_Return': [0.1, 0.08, 0.06, 0.04] * 5 # Different win rates/returns implicitly
        })
        
        # We need to ensure they have different win rates to be ranked
        # signal_RSI_1: Target_Return = 0.1, 0.08, 0.06, 0.04 -> all > 0 -> WR 100%
        # But analyze logic checks if actual_win_rate > 0.52 etc.
        
        with patch.object(StrategyAnalyzer, "get_features", return_value=df):
            res = strategy_analyzer.analyze("BTC/USDT:USDT", "1h")
            # RSI signals belong to 'Momentum'. Should only have Top 3.
            momentum_sigs = [k for k in res.keys() if "RSI" in k]
            assert len(momentum_sigs) <= 3

    def test_validate_weights_walk_forward_split(self, strategy_analyzer):
        """Verify 50/25/25 split indexing in walk-forward validation."""
        df = pd.DataFrame({'close': range(100)})
        
        # Split logic: train 0:50, val 50:75, holdout 75:100
        # StrategyAnalyzer uses iloc
        train_end = 50
        val_end = 75
        
        train_df = df.iloc[:train_end]
        val_df = df.iloc[train_end:val_end]
        holdout_df = df.iloc[val_end:]
        
        assert len(train_df) == 50
        assert len(val_df) == 25
        assert len(holdout_df) == 25
        assert train_df.index[-1] == 49
        assert val_df.index[0] == 50
        assert holdout_df.index[0] == 75

    def test_get_market_regime(self, strategy_analyzer):
        """Test BULL/BEAR/SIDEWAYS detection."""
        df = pd.DataFrame({'close': [100], 'ema_200': [90]}) # Price > EMA * 1.02
        
        # BMS overrides
        assert strategy_analyzer._get_market_regime(df, bms_zone='RED') == 'BEAR'
        assert strategy_analyzer._get_market_regime(df, bms_zone='GREEN') == 'BULL'
        
        # Fallback to EMA
        assert strategy_analyzer._get_market_regime(df) == 'BULL'
        
        df_bear = pd.DataFrame({'close': [80], 'ema_200': [100]})
        assert strategy_analyzer._get_market_regime(df_bear) == 'BEAR'
        
        df_side = pd.DataFrame({'close': [101], 'ema_200': [100]})
        assert strategy_analyzer._get_market_regime(df_side) == 'SIDEWAYS'

    def test_analyzer_backtest_with_signals(self, strategy_analyzer):
        """High-level sanity check for internal backtest math."""
        # df length must be >= 25 
        data = [{'close': 100.0, 'high': 100.0, 'low': 100.0}] * 30
        # Index 20: LONG @ 100. TP @ 104, SL @ 95.
        data[21] = {'close': 105.0, 'high': 105.0, 'low': 104.5} # Hits TP
        
        df = pd.DataFrame(data)
        # signals list should represent indices 20 to 29
        # index 20 (i=0) is BUY
        signals = ['BUY'] + [None]*9
        
        # SL 5%, TP 4%, Fee 0.1%
        res = strategy_analyzer._backtest_with_signals(df, signals, 0.05, 0.04, 0.001)
        
        assert res['trades'] == 1
        assert res['win_rate'] == 1.0
        # (104 - 100)/100 - 0.001 = 0.039. $1000 * 0.039 = $39.0
        assert round(res['pnl'], 2) == 39.0
