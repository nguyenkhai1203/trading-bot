import pandas as pd
import numpy as np
import os
import sys

# Add src to path if running directly or from root
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.append(src_dir)
# import pandas_ta as ta # Optional, but we stick to manual pandas for now to keep deps simple if preferred, or use pandas_ta if installed.
# User verified requirements.txt has partial deps. Let's stick to standard pandas for robustness unless requested.

class FeatureEngineer:
    def __init__(self):
        pass

    def calculate_features(self, df, portfolio_state=None):
        """
        Input: 
            df: DataFrame with 'close', 'high', 'low', 'volume'
            portfolio_state: Dict with 'balance', 'unrealized_pnl', 'leverage', 'equity' (Optional)
        Output: DataFrame with added feature columns (RSI, EMA, etc.)
        """
        if df is None or df.empty:
            return df
            
        # Create a list to collect new feature columns (avoids fragmentation)
        new_features = {}

        # Ensure numeric types (fix for NoneType errors)
        cols_to_fix = ['open', 'high', 'low', 'close', 'volume']
        for col in cols_to_fix:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # --- Basic Indicators & Signals ---
        
        # 1. EMAs and Crosses
        for span in [9, 21, 50, 200]:
            new_features[f'EMA_{span}'] = df['close'].ewm(span=span, adjust=False).mean()
            
        em9 = new_features['EMA_9']
        em21 = new_features['EMA_21']
        em50 = new_features['EMA_50']
        em200 = new_features['EMA_200']
            
        # Crosses
        new_features['signal_EMA_9_cross_21_up'] = (em9 > em21) & (em9.shift(1) <= em21.shift(1))
        new_features['signal_EMA_9_gt_21'] = (em9 > em21) # Trend state
        new_features['signal_EMA_50_gt_200'] = (em50 > em200) # Golden Cross state
        new_features['signal_EMA_9_cross_21_down'] = (em9 < em21) & (em9.shift(1) >= em21.shift(1))
        new_features['signal_EMA_9_lt_21'] = (em9 < em21)
        new_features['signal_EMA_50_lt_200'] = (em50 < em200) # Death Cross state

        # 2. RSI (Multiple Lookbacks)
        for length in [7, 14, 21]:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            new_features[f'RSI_{length}'] = rsi
            
            # Signals
            new_features[f'signal_RSI_{length}_oversold'] = rsi < 30
            new_features[f'signal_RSI_{length}_overbought'] = rsi > 70
            new_features[f'signal_RSI_{length}_gt_50'] = rsi > 50
            new_features[f'signal_RSI_{length}_lt_50'] = rsi < 50
            
            # Normalization for RL
            new_features[f'norm_RSI_{length}'] = rsi / 100.0

        # 3. MACD
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        macd = exp12 - exp26
        signal_line = macd.ewm(span=9, adjust=False).mean()
        
        new_features['MACD'] = macd
        new_features['Signal_Line'] = signal_line
        new_features['MACD_Hist'] = macd - signal_line
        
        new_features['signal_MACD_cross_up'] = (macd > signal_line) & (macd.shift(1) <= signal_line.shift(1))
        new_features['signal_MACD_gt_signal'] = macd > signal_line
        new_features['signal_MACD_cross_down'] = (macd < signal_line) & (macd.shift(1) >= signal_line.shift(1))
        new_features['signal_MACD_lt_signal'] = macd < signal_line

        # MACD Normalization
        lookback_norm = 100
        macd_min = macd.rolling(lookback_norm).min()
        macd_max = macd.rolling(lookback_norm).max()
        norm_macd = (macd - macd_min) / (macd_max - macd_min).replace(0, 1)
        # Clip to 0-1 to handle outliers outside lookback
        new_features['norm_MACD'] = norm_macd.clip(0, 1)

        # 4. Bollinger Bands (20, 2)
        bb_window = 20
        bb_std = 2.0
        bb_mid = df['close'].rolling(window=bb_window).mean()
        bb_up = bb_mid + (bb_std * df['close'].rolling(window=bb_window).std())
        bb_low = bb_mid - (bb_std * df['close'].rolling(window=bb_window).std())
        
        new_features['BB_Mid'] = bb_mid
        new_features['BB_Up'] = bb_up
        new_features['BB_Low'] = bb_low
        
        new_features['signal_Price_lt_BB_Low'] = df['close'] < bb_low # Mean Reversion Buy
        new_features['signal_Price_gt_BB_Up'] = df['close'] > bb_up   # Mean Reversion Sell
        
        # BB Normalization
        new_features['norm_BB_Width'] = (bb_up - bb_low) / bb_mid
        new_features['norm_Price_in_BB'] = (df['close'] - bb_low) / (bb_up - bb_low).replace(0, 1)

        # 5. Ichimoku (simplified)
        high_9 = df['high'].rolling(window=9).max()
        low_9 = df['low'].rolling(window=9).min()
        tenkan = (high_9 + low_9) / 2
        
        high_26 = df['high'].rolling(window=26).max()
        low_26 = df['low'].rolling(window=26).min()
        kijun = (high_26 + low_26) / 2
        
        new_features['Tenkan'] = tenkan
        new_features['Kijun'] = kijun
        new_features['signal_Ichimoku_TK_Cross_Up'] = (tenkan.values > kijun.values)
        new_features['signal_Ichimoku_TK_Cross_Down'] = (tenkan.values < kijun.values)

        # 6. Volume
        vol_ma = df['volume'].rolling(20).mean()
        vol_spike = df['volume'] / vol_ma
        
        new_features['Vol_MA'] = vol_ma
        new_features['Vol_Spike'] = vol_spike
        new_features['signal_Vol_Spike'] = vol_spike > 2.0
        
        # Volume Normalization
        # Cast to numeric to avoid "loop of ufunc does not support argument 0 of type float which has no callable log1p method"
        vol_numeric = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
        log_volume = np.log1p(vol_numeric)
        new_features['log_volume'] = log_volume
        vol_min = log_volume.rolling(lookback_norm).min()
        vol_max = log_volume.rolling(lookback_norm).max()
        new_features['norm_Volume'] = (log_volume - vol_min) / (vol_max - vol_min).replace(0, 1)

        # --- ADVANCED INDICATORS ---
        
        # 7. ADX (Average Directional Index) - Trend Strength
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift(1))
        low_close = abs(df['low'] - df['close'].shift(1))
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        
        up_move = df['high'].diff()
        down_move = -df['low'].diff()
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        
        tr_14 = tr.rolling(14).sum()
        plus_dm_14 = pd.Series(plus_dm, index=df.index).rolling(14).sum()
        minus_dm_14 = pd.Series(minus_dm, index=df.index).rolling(14).sum()
        plus_di = 100 * (plus_dm_14 / tr_14.replace(0, np.nan))
        minus_di = 100 * (minus_dm_14 / tr_14.replace(0, np.nan))
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum.replace(0, np.nan)
        
        adx = dx.rolling(14).mean()
        
        new_features['ADX'] = adx
        new_features['signal_ADX_Strong'] = adx > 25  # Strong trend
        new_features['signal_ADX_Weak'] = adx < 20    # Weak/sideways
        new_features['signal_DI_Plus_Above'] = plus_di.values > minus_di.values  # Uptrend
        new_features['signal_DI_Minus_Above'] = minus_di.values > plus_di.values # Downtrend
        
        new_features['norm_ADX'] = adx / 100.0

        # 8. Stochastic Oscillator
        high_14 = df['high'].rolling(14).max()
        low_14 = df['low'].rolling(14).min()
        stoch_range = (high_14 - low_14).replace(0, np.nan)  # Avoid division by zero
        fastk = 100 * (df['close'] - low_14) / stoch_range
        fastd = fastk.rolling(3).mean()
        fastk_smooth = fastk.rolling(3).mean()
        
        new_features['Stoch_K'] = fastk_smooth
        new_features['Stoch_D'] = fastd
        
        new_features['signal_Stoch_Oversold'] = fastk_smooth < 20
        new_features['signal_Stoch_Overbought'] = fastk_smooth > 80
        new_features['signal_Stoch_K_Cross_Up'] = (fastk_smooth > fastd) & (fastk_smooth.shift(1) <= fastd.shift(1))
        new_features['signal_Stoch_K_Cross_Down'] = (fastk_smooth < fastd) & (fastk_smooth.shift(1) >= fastd.shift(1))

        # 9. ATR (Average True Range) - Volatility
        atr_14 = tr.rolling(14).mean()
        atr_ma20 = atr_14.rolling(20).mean()
        
        new_features['ATR_14'] = atr_14
        new_features['ATR_MA20'] = atr_ma20
        new_features['signal_High_Volatility'] = atr_14 > atr_ma20
        new_features['signal_Low_Volatility'] = atr_14 < atr_ma20
        
        new_features['norm_ATR'] = atr_14 / df['close']

        # 10. VWAP (Volume Weighted Average Price)
        vwap = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
        new_features['vwap'] = vwap
        new_features['signal_Price_Above_VWAP'] = df['close'] > vwap
        new_features['signal_Price_Below_VWAP'] = df['close'] < vwap

        # 11-12. Divergence Detection (RSI & MACD)
        for rsi_period in [14]:
            rsi_col = f'RSI_{rsi_period}'
            if rsi_col in new_features:
                rsi_series = new_features[rsi_col]
                # Simple divergence: compare peaks/troughs
                rsi_high = rsi_series.rolling(5).max()
                rsi_low = rsi_series.rolling(5).min()
                price_high = df['close'].rolling(5).max()
                price_low = df['close'].rolling(5).min()
                
                # Bearish: Price higher, RSI lower
                new_features[f'signal_RSI_Bearish_Div'] = (price_high > price_high.shift(5)) & (rsi_high < rsi_high.shift(5))
                # Bullish: Price lower, RSI higher
                new_features[f'signal_RSI_Bullish_Div'] = (price_low < price_low.shift(5)) & (rsi_low > rsi_low.shift(5))

        if 'MACD' in new_features:
            macd_series = new_features['MACD']
            macd_high = macd_series.rolling(5).max()
            macd_low = macd_series.rolling(5).min()
            price_high = df['close'].rolling(5).max()
            price_low = df['close'].rolling(5).min()
            
            # Bearish: Price higher, MACD lower
            new_features['signal_MACD_Bearish_Div'] = (price_high > price_high.shift(5)) & (macd_high < macd_high.shift(5))
            # Bullish: Price lower, MACD higher
            new_features['signal_MACD_Bullish_Div'] = (price_low < price_low.shift(5)) & (macd_low > macd_low.shift(5))

        # 13. Fibonacci Retracement Levels
        lookback_fibo = 50
        swing_high = df['high'].rolling(lookback_fibo).max()
        swing_low = df['low'].rolling(lookback_fibo).min()
        
        new_features['swing_high'] = swing_high
        new_features['swing_low'] = swing_low
        
        fibo_range = swing_high - swing_low
        fibo_236 = swing_high - (fibo_range * 0.236)
        fibo_382 = swing_high - (fibo_range * 0.382)
        fibo_50 = swing_high - (fibo_range * 0.5)
        fibo_618 = swing_high - (fibo_range * 0.618)
        
        new_features['fibo_0'] = swing_high
        new_features['fibo_236'] = fibo_236
        new_features['fibo_382'] = fibo_382
        new_features['fibo_50'] = fibo_50
        new_features['fibo_618'] = fibo_618
        new_features['fibo_100'] = swing_low
        
        fibo_threshold = 0.005
        sig_fibo_236 = (df['close'] >= fibo_236 * (1 - fibo_threshold)) & (df['close'] <= fibo_236 * (1 + fibo_threshold))
        sig_fibo_382 = (df['close'] >= fibo_382 * (1 - fibo_threshold)) & (df['close'] <= fibo_382 * (1 + fibo_threshold))
        sig_fibo_50 = (df['close'] >= fibo_50 * (1 - fibo_threshold)) & (df['close'] <= fibo_50 * (1 + fibo_threshold))
        sig_fibo_618 = (df['close'] >= fibo_618 * (1 - fibo_threshold)) & (df['close'] <= fibo_618 * (1 + fibo_threshold))
        
        new_features['signal_price_at_fibo_236'] = sig_fibo_236
        new_features['signal_price_at_fibo_382'] = sig_fibo_382
        new_features['signal_price_at_fibo_50'] = sig_fibo_50
        new_features['signal_price_at_fibo_618'] = sig_fibo_618
        new_features['signal_at_fibo_key_level'] = sig_fibo_236 | sig_fibo_382 | sig_fibo_50 | sig_fibo_618

        # 14. Support/Resistance Detection
        lookback_sr = 20
        is_swing_high = (
            (df['high'] == df['high'].rolling(window=lookback_sr, center=True).max()) &
            (df['high'] > df['high'].shift(1)) &
            (df['high'] > df['high'].shift(-1))
        )
        is_swing_low = (
            (df['low'] == df['low'].rolling(window=lookback_sr, center=True).min()) &
            (df['low'] < df['low'].shift(1)) &
            (df['low'] < df['low'].shift(-1))
        )
        
        new_features['is_swing_high'] = is_swing_high
        new_features['is_swing_low'] = is_swing_low
        
        resistance_level = np.where(is_swing_high, df['high'], np.nan)
        # Ensure correct index to avoid "Can only compare identically-labeled Series objects"
        resistance_level = pd.Series(resistance_level, index=df.index).ffill() 
        
        support_level = np.where(is_swing_low, df['low'], np.nan)
        support_level = pd.Series(support_level, index=df.index).ffill()
        
        new_features['resistance_level'] = resistance_level
        new_features['support_level'] = support_level
        
        sr_threshold = 0.01
        sig_price_res = (df['close'] >= resistance_level * (1 - sr_threshold)) & (df['close'] <= resistance_level * (1 + sr_threshold))
        sig_price_sup = (df['close'] >= support_level * (1 - sr_threshold)) & (df['close'] <= support_level * (1 + sr_threshold))
        
        new_features['signal_price_at_resistance'] = sig_price_res
        new_features['signal_price_at_support'] = sig_price_sup
        
        new_features['signal_bounce_from_support'] = sig_price_sup & (df['close'] > df['open']) & (df['low'] <= support_level * (1 + sr_threshold))
        new_features['signal_bounce_from_resistance'] = sig_price_res & (df['close'] < df['open']) & (df['high'] >= resistance_level * (1 - sr_threshold))
        
        # Fill NaN with 0 for comparison safety or keep as NaN (comparisons with NaN are False)
        # But we must ensure they are not None/Object type
        
        res_vals = resistance_level.fillna(0).values
        sup_vals = support_level.fillna(0).values
        
        # Safe comparison avoiding None types
        # Note: comparison with 0 might trigger signal if price is 0 (unlikely) but checking > 0 helps
        
        new_features['signal_breakout_above_resistance'] = (df['close'].values > res_vals) & (df['close'].shift(1).values <= pd.Series(res_vals).shift(1).fillna(0).values) & (res_vals > 0)
        new_features['signal_breakout_below_support'] = (df['close'].values < sup_vals) & (df['close'].shift(1).values >= pd.Series(sup_vals).shift(1).fillna(0).values) & (sup_vals > 0)

        # 16. PORTFOLIO STATE (Context Awareness)
        if portfolio_state:
            balance = portfolio_state.get('balance', 1000)
            equity = portfolio_state.get('equity', 1000)
            unrealized_pnl = portfolio_state.get('unrealized_pnl', 0)
            leverage = portfolio_state.get('leverage', 1)
            
            pnl_val = np.clip(unrealized_pnl / balance if balance > 0 else 0, -1.0, 1.0)
            lev_val = np.clip(leverage / 20.0, 0, 1.0)
            eq_val = equity / balance if balance > 0 else 1.0
            
            new_features['state_pnl_pct'] = pnl_val
            new_features['state_leverage'] = lev_val
            new_features['state_equity_ratio'] = eq_val
        else:
            new_features['state_pnl_pct'] = 0.0
            new_features['state_leverage'] = 0.0
            new_features['state_equity_ratio'] = 1.0

        # CONSOLIDATE: Use pd.concat once at the end
        # Convert dict to DataFrame
        features_df = pd.DataFrame(new_features, index=df.index)
        
        # Safe-guard: Fill NaNs in normalized features to avoid NaN in JSON
        norm_cols = [c for c in features_df.columns if c.startswith('norm_')]
        features_df[norm_cols] = features_df[norm_cols].fillna(0.5)
        
        # Merge with original df
        result_df = pd.concat([df, features_df], axis=1)

        return result_df
