import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import pandas as pd
import numpy as np
import os
import sys
import asyncio
from concurrent.futures import as_completed

# Add src to path
src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
if src_dir not in sys.path:
    sys.path.append(src_dir)

from src.analyzer import StrategyAnalyzer, _compute_and_cache_bms
from src.strategy import WeightedScoringStrategy
from src.feature_engineering import FeatureEngineer
from src.infrastructure.repository.database import DataManager

class TestBMSIntegratedAnalyzer:
    @pytest.fixture(autouse=True)
    async def setup(self):
        self.analyzer = StrategyAnalyzer()
        self.fe = FeatureEngineer()
        # Mock data WITHOUT bms columns to test fallback
        self.df = pd.DataFrame({
            'timestamp': pd.date_range(start='2023-01-01', periods=100, freq='h'),
            'open': np.random.randn(100) + 100,
            'high': np.random.randn(100) + 101,
            'low': np.random.randn(100) + 99,
            'close': np.random.randn(100) + 100,
            'volume': np.random.randn(100) * 1000 + 5000,
        })
        # Add some signal columns
        self.df['signal_EMA_9_cross_21_up'] = [True] * 100
        self.df['signal_EMA_9_cross_21_down'] = [False] * 100
        
        yield
        await DataManager.clear_instances()

    @pytest.mark.asyncio
    async def test_compute_signals_with_bms_veto(self):
        """Verify that BMS zone vetos work in _compute_signals."""
        strat = WeightedScoringStrategy()
        strat.weights = {'EMA_9_cross_21_up': 5.0, 'EMA_9_cross_21_down': 5.0}
        strat._precalculate_signal_categories()
        
        # 1. GREEN zone -> Should only allow BUY
        sigs_green = self.analyzer._compute_signals(self.df, strat, bms_score=0.8, bms_zone='GREEN')
        assert all(s == 'BUY' or s is None for s in sigs_green)
        assert 'BUY' in sigs_green
        assert 'SELL' not in sigs_green
        
        # 2. RED zone -> Should only allow SELL
        df_red = self.df.copy()
        df_red['signal_EMA_9_cross_21_down'] = [True] * 100
        sigs_red = self.analyzer._compute_signals(df_red, strat, bms_score=0.2, bms_zone='RED')
        assert all(s == 'SELL' or s is None for s in sigs_red)
        assert 'SELL' in sigs_red
        assert 'BUY' not in sigs_red

    @pytest.mark.asyncio
    async def test_validate_weights_passes_bms(self):
        """Verify that validate_weights passes bms params to _compute_signals."""
        weights = {'EMA_9_cross_21_up': 5.0}
        
        with patch.object(StrategyAnalyzer, '_compute_signals') as mock_compute:
            mock_compute.return_value = []
            self.analyzer.validate_weights(self.df, weights, 'BTC/USDT', '1h', bms_zone='RED', bms_score=0.1)
            
            # Check if bms_zone was passed
            args, kwargs = mock_compute.call_args
            assert kwargs.get('bms_zone') == 'RED'
            assert kwargs.get('bms_score') == 0.1

    @pytest.mark.asyncio
    @patch('src.analyzer._compute_and_cache_bms')
    @patch('src.analyzer.StrategyAnalyzer.analyze')
    @patch('src.analyzer.StrategyAnalyzer.get_features')
    @patch('src.analyzer.StrategyAnalyzer.validate_weights')
    @patch('src.analyzer.StrategyAnalyzer.update_config', new_callable=AsyncMock)
    @patch('src.infrastructure.repository.database.DataManager.get_instance')
    @patch('src.infrastructure.notifications.notification.send_telegram_chunked')
    @patch('src.analyzer.run_nn_training', new_callable=AsyncMock)
    @patch('src.analyzer.ThreadPoolExecutor')
    async def test_run_global_optimization_commit_filter(self, mock_executor, mock_brain, mock_notify, mock_db, mock_update, mock_validate, mock_get_feat, mock_analyze, mock_bms):
        """Verify that commit logic filters by BMS zone bias."""
        from src.analyzer import run_global_optimization
        
        # 1. Mock Sequential Executor
        mock_pool = MagicMock()
        mock_executor.return_value.__enter__.return_value = mock_pool
        
        # Helper to mock as_completed results
        def mock_submit(fn, *args, **kwargs):
            mock_future = MagicMock()
            mock_future.result.return_value = fn(*args, **kwargs)
            return mock_future
        mock_pool.submit.side_effect = mock_submit
        
        # 2. Mock BMS to RED
        mock_bms.return_value = {'bms': 0.1, 'sentiment_zone': 'RED'}
        
        # 3. Mock analysis result with LONG bias
        mock_analyze.return_value = {'EMA_9_cross_21_up': 5.0} 
        
        # 4. Mock get_features to return something not None
        mock_get_feat.return_value = self.df
        
        # 5. Mock validation result (profitable on its own)
        mock_validate.return_value = {
            'win_rate': 0.6, 'test_wr': 0.6, 'consistency': 0.1, 
            'pnl': 100, 'trades': 10, 'sl_pct': 0.02, 'tp_pct': 0.04, 'entry_score': 5.0
        }
        
        mock_db_inst = AsyncMock()
        mock_db.return_value = mock_db_inst
        
        # 6. Mock Brain
        mock_brain.return_value = {"status": "success", "accuracy": 0.0, "mse": 0.0, "samples": 0}

        # 7. Execute with mocks
        with patch('src.analyzer.as_completed', side_effect=lambda fs: fs):
            with patch('src.config.ACTIVE_EXCHANGES', ['BYBIT']):
                with patch('src.config.TRADING_SYMBOLS', ['BTC/USDT']):
                    with patch('src.config.TRADING_TIMEFRAMES', ['1h']):
                        with patch('src.config.BYBIT_SYMBOLS', ['BTC/USDT']):
                            with patch('src.analyzer.TRADING_SYMBOLS', ['BTC/USDT']):
                                with patch('src.analyzer.TRADING_TIMEFRAMES', ['1h']):
                                    with patch('sys.argv', ['analyzer.py']):
                                        await run_global_optimization(download=False)
        
        # Verify it was disabled because of RED + LONG bias
        found_disabled = False
        for call in mock_update.call_args_list:
            if call.kwargs.get('enabled') == False:
                found_disabled = True
        
        assert found_disabled is True, "Long-biased config should be disabled in RED zone"
