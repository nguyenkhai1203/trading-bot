import pytest
import pandas as pd
from analyzer import StrategyAnalyzer

class TestAnalyzer:
    def test_analyzer_backtest_with_signals(self):
        """Test the ultra-fast mini-backtester logic used during optimization grid search."""
        analyzer = StrategyAnalyzer(data_dir="/tmp/fake")
        
        # We need a DataFrame of at least 25 rows since the analyzer skips the first 20 rows
        # reserved for Indicator warmup padding. 
        data = []
        for i in range(20):
            data.append({'close': 100.0, 'high': 100.0, 'low': 100.0})
            
        # Add meaningful price action for rows 20 to 24
        # Row 20: Trigger LONG
        data.append({'close': 100.0, 'high': 100.0, 'low': 100.0})
        # Row 21: High hits TP for long (104.0)
        data.append({'close': 105.0, 'high': 105.0, 'low': 99.0})
        # Row 22: Trigger SHORT
        data.append({'close': 100.0, 'high': 100.0, 'low': 100.0})
        # Row 23: Low hits TP for short (96.0). Cap high at 100 to avoid SL (105.0)
        data.append({'close': 95.0, 'high': 100.0, 'low': 95.0})
        # Row 24: End Padding
        data.append({'close': 100.0, 'high': 100.0, 'low': 100.0})
        
        df = pd.DataFrame(data)
        
        # Provide signals corresponding to idx 20-24
        signals = ['BUY', None, 'SELL', None, None]
        
        # Config: TP = 4%, SL = 5%, Fee = 0.1% (per trade simulation)
        sl_pct = 0.05
        tp_pct = 0.04
        fee = 0.001
        
        res = analyzer._backtest_with_signals(df, signals, sl_pct, tp_pct, fee)
        
        # Both trades should mathematically hit TP on the consecutive candle
        assert res['trades'] == 2
        assert res['win_rate'] == 1.0 
        
        # PnL math (using analyzer's internal calculation logic):
        # Entry size = 1000 fixed
        
        # Trade 1 (Long at 100): 
        # Hit TP = 104. PnL % = (104 - 100)/100 - 0.001 = 0.04 - 0.001 = 0.039. PnL = $39
        
        # Trade 2 (Short at 100):
        # Hit TP = 96. PnL % = (100 - 96)/100 - 0.001 = 0.04 - 0.001 = 0.039. PnL = $39
        
        # Total PnL = $78.0
        assert round(res['pnl'], 2) == 78.0
