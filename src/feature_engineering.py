import pandas as pd
import numpy as np
# import pandas_ta as ta # Optional, but we stick to manual pandas for now to keep deps simple if preferred, or use pandas_ta if installed.
# User verified requirements.txt has partial deps. Let's stick to standard pandas for robustness unless requested.

class FeatureEngineer:
    def __init__(self):
        pass

    def calculate_features(self, df):
        """
        Input: DataFrame with 'close', 'high', 'low', 'volume'
        Output: DataFrame with added feature columns (RSI, EMA, etc.)
        """
        if df is None or df.empty:
            return df

        # --- Basic Indicators & Signals ---
        
        # 1. EMAs and Crosses
        for span in [9, 21, 50, 200]:
            df[f'EMA_{span}'] = df['close'].ewm(span=span, adjust=False).mean()
            
        # Crosses
        df['signal_EMA_9_cross_21_up'] = (df['EMA_9'] > df['EMA_21']) & (df['EMA_9'].shift(1) <= df['EMA_21'].shift(1))
        df['signal_EMA_9_gt_21'] = (df['EMA_9'] > df['EMA_21']) # Trend state
        df['signal_EMA_50_gt_200'] = (df['EMA_50'] > df['EMA_200']) # Golden Cross state
        df['signal_EMA_9_cross_21_down'] = (df['EMA_9'] < df['EMA_21']) & (df['EMA_9'].shift(1) >= df['EMA_21'].shift(1))
        df['signal_EMA_9_lt_21'] = (df['EMA_9'] < df['EMA_21'])
        df['signal_EMA_50_lt_200'] = (df['EMA_50'] < df['EMA_200']) # Death Cross state

        # 2. RSI (Multiple Lookbacks)
        for length in [7, 14, 21]:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
            rs = gain / loss
            df[f'RSI_{length}'] = 100 - (100 / (1 + rs))
            
            # Signals
            df[f'signal_RSI_{length}_oversold'] = df[f'RSI_{length}'] < 30
            df[f'signal_RSI_{length}_overbought'] = df[f'RSI_{length}'] > 70
            df[f'signal_RSI_{length}_gt_50'] = df[f'RSI_{length}'] > 50
            df[f'signal_RSI_{length}_lt_50'] = df[f'RSI_{length}'] < 50

        # 3. MACD
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['Signal_Line']
        
        df['signal_MACD_cross_up'] = (df['MACD'] > df['Signal_Line']) & (df['MACD'].shift(1) <= df['Signal_Line'].shift(1))
        df['signal_MACD_gt_signal'] = df['MACD'] > df['Signal_Line']
        df['signal_MACD_cross_down'] = (df['MACD'] < df['Signal_Line']) & (df['MACD'].shift(1) >= df['Signal_Line'].shift(1))
        df['signal_MACD_lt_signal'] = df['MACD'] < df['Signal_Line']

        # 4. Bollinger Bands (20, 2)
        bb_window = 20
        bb_std = 2.0
        df['BB_Mid'] = df['close'].rolling(window=bb_window).mean()
        df['BB_Up'] = df['BB_Mid'] + (bb_std * df['close'].rolling(window=bb_window).std())
        df['BB_Low'] = df['BB_Mid'] - (bb_std * df['close'].rolling(window=bb_window).std())
        
        df['signal_Price_lt_BB_Low'] = df['close'] < df['BB_Low'] # Mean Reversion Buy
        df['signal_Price_gt_BB_Up'] = df['close'] > df['BB_Up']   # Mean Reversion Sell

        # 5. Ichimoku (simplified)
        high_9 = df['high'].rolling(window=9).max()
        low_9 = df['low'].rolling(window=9).min()
        df['Tenkan'] = (high_9 + low_9) / 2
        
        high_26 = df['high'].rolling(window=26).max()
        low_26 = df['low'].rolling(window=26).min()
        df['Kijun'] = (high_26 + low_26) / 2
        
        df['signal_Ichimoku_TK_Cross_Up'] = (df['Tenkan'] > df['Kijun'])
        df['signal_Ichimoku_TK_Cross_Down'] = (df['Tenkan'] < df['Kijun'])

        # 6. Volume
        df['Vol_MA'] = df['volume'].rolling(20).mean()
        df['Vol_Spike'] = df['volume'] / df['Vol_MA']
        df['signal_Vol_Spike'] = df['Vol_Spike'] > 2.0

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
        plus_dm_14 = pd.Series(plus_dm).rolling(14).sum()
        minus_dm_14 = pd.Series(minus_dm).rolling(14).sum()
        
        plus_di = 100 * (plus_dm_14 / tr_14)
        minus_di = 100 * (minus_dm_14 / tr_14)
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        
        df['ADX'] = dx.rolling(14).mean()
        df['signal_ADX_Strong'] = df['ADX'] > 25  # Strong trend
        df['signal_ADX_Weak'] = df['ADX'] < 20    # Weak/sideways
        df['signal_DI_Plus_Above'] = plus_di > minus_di  # Uptrend
        df['signal_DI_Minus_Above'] = minus_di > plus_di # Downtrend

        # 8. Stochastic Oscillator
        high_14 = df['high'].rolling(14).max()
        low_14 = df['low'].rolling(14).min()
        fastk = 100 * (df['close'] - low_14) / (high_14 - low_14)
        fastd = fastk.rolling(3).mean()
        fastk_smooth = fastk.rolling(3).mean()
        
        df['Stoch_K'] = fastk_smooth
        df['Stoch_D'] = fastd
        
        df['signal_Stoch_Oversold'] = df['Stoch_K'] < 20
        df['signal_Stoch_Overbought'] = df['Stoch_K'] > 80
        df['signal_Stoch_K_Cross_Up'] = (df['Stoch_K'] > df['Stoch_D']) & (df['Stoch_K'].shift(1) <= df['Stoch_D'].shift(1))
        df['signal_Stoch_K_Cross_Down'] = (df['Stoch_K'] < df['Stoch_D']) & (df['Stoch_K'].shift(1) >= df['Stoch_D'].shift(1))

        # 9. ATR (Average True Range) - Volatility
        df['ATR_14'] = tr.rolling(14).mean()
        df['ATR_MA20'] = df['ATR_14'].rolling(20).mean()
        df['signal_High_Volatility'] = df['ATR_14'] > df['ATR_MA20']
        df['signal_Low_Volatility'] = df['ATR_14'] < df['ATR_MA20']

        # 10. VWAP (Volume Weighted Average Price)
        df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
        df['signal_Price_Above_VWAP'] = df['close'] > df['vwap']
        df['signal_Price_Below_VWAP'] = df['close'] < df['vwap']

        # 11. Divergence Detection (RSI)
        # Look for RSI making lower highs while price makes higher highs (bearish)
        # or RSI making higher lows while price makes lower lows (bullish)
        for rsi_period in [14]:
            rsi_col = f'RSI_{rsi_period}'
            if rsi_col in df.columns:
                # Simple divergence: compare peaks/troughs
                rsi_high = df[rsi_col].rolling(5).max()
                rsi_low = df[rsi_col].rolling(5).min()
                price_high = df['close'].rolling(5).max()
                price_low = df['close'].rolling(5).min()
                
                # Bearish: Price higher, RSI lower
                df[f'signal_RSI_Bearish_Div'] = (price_high > price_high.shift(5)) & (rsi_high < rsi_high.shift(5))
                # Bullish: Price lower, RSI higher
                df[f'signal_RSI_Bullish_Div'] = (price_low < price_low.shift(5)) & (rsi_low > rsi_low.shift(5))

        # 12. Divergence Detection (MACD)
        if 'MACD' in df.columns:
            macd_high = df['MACD'].rolling(5).max()
            macd_low = df['MACD'].rolling(5).min()
            
            # Bearish: Price higher, MACD lower
            df['signal_MACD_Bearish_Div'] = (price_high > price_high.shift(5)) & (macd_high < macd_high.shift(5))
            # Bullish: Price lower, MACD higher
            df['signal_MACD_Bullish_Div'] = (price_low < price_low.shift(5)) & (macd_low > macd_low.shift(5))

        return df
