import pytest
import pandas as pd
import numpy as np
from feature_engineering import FeatureEngineer

class TestFeatureEngineer:
    @pytest.fixture
    def fe(self):
        return FeatureEngineer()

    def test_calculate_features_basic_flow(self, fe, sample_ohlcv_data):
        """Verify that calculate_features adds all expected technical indicators and stays in numeric format."""
        result_df = fe.calculate_features(sample_ohlcv_data.copy())
        
        # Check basic EMAs
        assert 'EMA_9' in result_df.columns
        assert 'EMA_21' in result_df.columns
        assert 'EMA_50' in result_df.columns
        assert 'EMA_200' in result_df.columns
        
        # Check RSI
        assert 'RSI_14' in result_df.columns
        assert 'norm_RSI_14' in result_df.columns
        assert result_df['norm_RSI_14'].min() >= 0
        assert result_df['norm_RSI_14'].max() <= 1
        
        # Check MACD
        assert 'MACD' in result_df.columns
        assert 'norm_MACD' in result_df.columns
        
        # Check BB
        assert 'BB_Up' in result_df.columns
        assert 'norm_BB_Width' in result_df.columns
        
        # Check Ichimoku
        assert 'Tenkan' in result_df.columns
        assert 'Kijun' in result_df.columns
        
        # Check Volume
        assert 'Vol_MA' in result_df.columns
        assert 'Vol_Spike' in result_df.columns
        
        # Ensure no fragmentation/duplicates
        assert result_df.columns.duplicated().any() == False

        # RULE: Ensure output tails don't contain NaNs (lookback is expected at start)
        # Check last 5 rows for NaNs across all columns
        tail_nans = result_df.tail(5).isna().sum().sum()
        assert tail_nans == 0, f"Found {tail_nans} NaNs in the tail of the features dataframe"

    def test_calculate_features_with_portfolio_state(self, fe, sample_ohlcv_data):
        """Verify that portfolio state features are correctly calculated and normalized."""
        portfolio_state = {
            'balance': 2000,
            'equity': 1800,
            'unrealized_pnl': -200,
            'leverage': 10
        }
        
        result_df = fe.calculate_features(sample_ohlcv_data.copy(), portfolio_state=portfolio_state)
        
        # Check state features
        assert 'state_pnl_pct' in result_df.columns
        assert 'state_leverage' in result_df.columns
        assert 'state_equity_ratio' in result_df.columns
        
        # Verify values (snapshot check)
        # pnl_pct = -200 / 2000 = -0.1
        assert result_df['state_pnl_pct'].iloc[-1] == -0.1
        # leverage_val = 10 / 20 = 0.5
        assert result_df['state_leverage'].iloc[-1] == 0.5
        # equity_ratio = 1800 / 2000 = 0.9
        assert result_df['state_equity_ratio'].iloc[-1] == 0.9

    def test_advanced_indicators(self, fe, sample_ohlcv_data):
        """Verify advanced indicators like ADX, Stochastic, ATR, and S/R levels."""
        result_df = fe.calculate_features(sample_ohlcv_data.copy())
        
        # ADX
        assert 'ADX' in result_df.columns
        assert 'norm_ADX' in result_df.columns
        
        # Stochastic
        assert 'Stoch_K' in result_df.columns
        assert 'Stoch_D' in result_df.columns
        
        # ATR / Volatility
        assert 'ATR_14' in result_df.columns
        assert 'norm_ATR' in result_df.columns
        
        # VWAP
        assert 'vwap' in result_df.columns
        
        # Support / Resistance
        assert 'resistance_level' in result_df.columns
        assert 'support_level' in result_df.columns
        
        # Fibonacci
        assert 'fibo_618' in result_df.columns
        assert 'signal_price_at_fibo_618' in result_df.columns

    def test_divergence_logic(self, fe, sample_ohlcv_data):
        """Ensure divergence signals are generated (even if False in sample data)."""
        result_df = fe.calculate_features(sample_ohlcv_data.copy())
        
        assert 'signal_RSI_Bearish_Div' in result_df.columns
        assert 'signal_MACD_Bullish_Div' in result_df.columns

    def test_breakout_signals(self, fe):
        """Verify custom support/resistance breakout logic."""
        # Create a df with a clear peak that becomes resistance
        # Index 0-19: Up
        # Index 20: Peak (Resistance)
        # Index 21-40: Down
        # Index 41: Breakout
        rows = 45
        data = {
            'close': [10 + i if i < 20 else 30 - (i-20) if i < 40 else 30 + (i-40) for i in range(rows)],
            'high':  [11 + i if i < 20 else 31 - (i-20) if i < 40 else 31 + (i-40) for i in range(rows)],
            'low':   [9 + i if i < 20 else 29 - (i-20) if i < 40 else 29 + (i-40) for i in range(rows)],
            'open':  [10 + i if i < 20 else 30 - (i-20) if i < 40 else 30 + (i-40) for i in range(rows)],
            'volume':[100] * rows
        }
        df = pd.DataFrame(data)
        
        result_df = fe.calculate_features(df)
        
        # Check if resistance was found (around index 20)
        # The lookback_sr is 20, so center=True needs 10 rows after.
        # Breakout signal should appear at index 41+
        breakout_col = 'signal_breakout_above_resistance'
        assert breakout_col in result_df.columns
        # Verify there's at least one breakout signal
        assert result_df[breakout_col].any() == True
        """Ensure it handles empty DataFrames gracefully."""
        df = pd.DataFrame()
        result_df = fe.calculate_features(df)
        assert result_df.empty
        
        result_df_none = fe.calculate_features(None)
        assert result_df_none is None

    def test_hardcore_noise_trap(self, fe):
        """Hardcore Test 1: The Noise Trap (False Breakout Prevention)
        Simulates sideways movement with massive wicks (high/low) that pierce S/R,
        but closes remain tight. Ensures breakout signals don't trigger on wicks.
        """
        rows = 40
        # Sideways close/open between 100-105
        close_prices = np.random.uniform(100, 105, rows)
        open_prices = close_prices - np.random.uniform(-1, 1, rows)
        
        # Normal highs/lows
        highs = np.maximum(close_prices, open_prices) + np.random.uniform(0, 2, rows)
        lows = np.minimum(close_prices, open_prices) - np.random.uniform(0, 2, rows)
        
        # Inject massive wicks (The Noise Trap) at specific indices
        trap_indices = [15, 25, 35]
        for i in trap_indices:
            highs[i] = 150.0  # Massive upper wick
            lows[i] = 50.0    # Massive lower wick
            # Close/Open remain tight
            close_prices[i] = 103.0 
            open_prices[i] = 101.0
            
        data = {
            'close': close_prices,
            'open': open_prices,
            'high': highs,
            'low': lows,
            'volume': np.random.uniform(100, 500, rows)
        }
        df = pd.DataFrame(data)
        
        result_df = fe.calculate_features(df)
        
        # Verify that despite the massive wicks, breakout signals are NOT triggered
        # because the Close price never actually broke the sustained Resistance/Support 
        # (which should be calculated ignoring single outlier wicks if lookback is robust, 
        # or at least close < res).
        if 'signal_breakout_above_resistance' in result_df.columns:
            # We don't want false positives on the trap candles
            for i in trap_indices:
                assert not result_df['signal_breakout_above_resistance'].iloc[i], f"False Positive breakout at index {i}"
                assert not result_df['signal_breakout_below_support'].iloc[i], f"False Positive breakdown at index {i}"

    def test_hardcore_volume_divergence(self, fe):
        """Hardcore Test 2: Volume Divergence (Weak Breakouts)
        Simulates a price breakout (Close > Resistance) but with plummeting Volume.
        Verifies that indicators like Vol_Spike indicate weakness.
        """
        rows = 40
        # Create a resistance level around 120
        close_prices = np.linspace(100, 118, 30).tolist() + [122, 125, 128] + np.linspace(128, 130, 7).tolist()
        
        # Volume drops drastically during the breakout
        volumes = np.random.uniform(1000, 1500, 30).tolist() + [200, 150, 100] + np.random.uniform(50, 100, 7).tolist()
        
        data = {
            'close': close_prices,
            'open': [c - 1 for c in close_prices],
            'high': [c + 2 for c in close_prices],
            'low': [c - 2 for c in close_prices],
            'volume': volumes
        }
        df = pd.DataFrame(data)
        
        result_df = fe.calculate_features(df)
        
        # Check breakout indices (30, 31, 32)
        assert 'Vol_Spike' in result_df.columns
        assert 'signal_Vol_Spike' in result_df.columns
        
        # Breakout occurred but volume should technically be very low compared to moving average
        assert result_df['Vol_Spike'].iloc[30] < 1.0, "Volume spike detected on a weak breakout!"
        assert not result_df['signal_Vol_Spike'].iloc[30], "Signal_Vol_Spike should be False on volume divergence"

    def test_hardcore_zero_nan_integrity(self, fe):
        """Hardcore Test 3: Zero/NaN Integrity
        Injects purely corrupt data (0s, NaNs, missing columns) in the middle of a valid series.
        Ensures the engine doesn't crash and handles coercions safely.
        """
        rows = 50
        # Valid start
        close_prices = np.random.uniform(100, 110, 20).tolist()
        # Corrupt middle (Zeros and NaNs)
        close_prices += [0.0, 0.0, np.nan, np.nan, 0.0]
        # Valid end
        close_prices += np.random.uniform(105, 115, 25).tolist()
        
        volumes = np.random.uniform(100, 500, 20).tolist()
        volumes += [0.0, 0.0, np.nan, np.nan, 0.0]
        volumes += np.random.uniform(200, 600, 25).tolist()
        
        data = {
            'close': close_prices,
            'open': close_prices, # Simplify
            'high': [c + 5 if pd.notna(c) else np.nan for c in close_prices],
            'low': [c - 5 if pd.notna(c) else np.nan for c in close_prices],
            'volume': volumes
        }
        df = pd.DataFrame(data)
        
        # Should NOT crash
        try:
            result_df = fe.calculate_features(df)
        except Exception as e:
            pytest.fail(f"Feature calculation crashed on NaN/Zero data: {e}")
            
        # Verify valid output exists
        assert result_df is not None
        assert not result_df.empty
        assert 'SMA_20' in result_df.columns or 'EMA_21' in result_df.columns # Basic presence check
        
        # Ensure division by zero errors are handled (e.g. in normative calculations or RSI)
        assert result_df['norm_RSI_14'].isna().sum() < 50 # Some might be NaN due to lookback, but not all
        
        # Verify Volume log handling doesn't explode on Zero
        assert not np.isinf(result_df['log_volume']).any(), "Infinity found in log_volume (Zero division/log(0) issue)"
