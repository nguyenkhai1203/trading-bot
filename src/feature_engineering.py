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

        return df
