import pandas as pd
import numpy as np
import os
import json
from config import TRADING_SYMBOLS
from feature_engineering import FeatureEngineer

class StrategyAnalyzer:
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        self.feature_engineer = FeatureEngineer()

    def load_data(self, symbol, timeframe='1h'):
        safe_symbol = symbol.replace('/', '').replace(':', '')
        file_path = os.path.join(self.data_dir, f"{safe_symbol}_{timeframe}.csv")
        
        if not os.path.exists(file_path):
            print(f"Data not found for {symbol} at {file_path}")
            return None
            
        df = pd.read_csv(file_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df

    def analyze(self, symbol, timeframe='1h', horizon=4):
        """
        Analyzes ALL 'signal_' columns to find best predictors.
        """
        print(f"Analyzing {symbol} {timeframe}...")
        df = self.load_data(symbol, timeframe)
        if df is None: return

        # 1. Calc Features
        df = self.feature_engineer.calculate_features(df)
        
        # 2. Label: Future Return
        df['Target_Return'] = df['close'].shift(-horizon) / df['close'] - 1.0
        
        # 3. Identify Signals
        signal_cols = [c for c in df.columns if c.startswith('signal_')]
        
        results = {}
        
        print(f"  Scanning {len(signal_cols)} potential signals...")
        
        for name in signal_cols:
            # Strip 'signal_' prefix for config key
            config_key = name.replace('signal_', '')
            
            subset = df[df[name] == True]
            
            if len(subset) < 10:
                continue
                
            avg_return = subset['Target_Return'].mean()
            win_rate = (subset['Target_Return'] > 0).mean()
            
            # Adjusted scoring: 
            weight = 0.0
            
            # Case 1: High Win Rate
            if win_rate > 0.55: weight = 2.0
            elif win_rate > 0.52: weight = 1.0
            
            # Case 2: High Reward (Trend)
            # If AvgRet > 0.5% and WinRate > 35%
            if weight == 0.0 and avg_return > 0.005 and win_rate > 0.35:
                weight = 1.5
            
            if weight > 0:
                print(f"    FOUND: {config_key} | WR={win_rate*100:.1f}% | Ret={avg_return*100:.2f}% -> W={weight}")
                results[config_key] = weight
            
        return results

    def update_config(self, symbol, new_weights):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        
        # Load existing
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
        else:
            data = {"default": {}}

        # Structure
        if symbol not in data:
            data[symbol] = {
                "weights": {}, 
                "thresholds": {"entry_score": 5.0, "exit_score": 2.5},
                "tiers": {
                    "low": { "min_score": 5.0, "leverage": 3, "cost_usdt": 3.0 },
                    "high": { "min_score": 7.0, "leverage": 5, "cost_usdt": 8.0 }
                }
            }
            
        # Update weights
        for k, v in new_weights.items():
            data[symbol]['weights'][k] = v
            
        with open(config_path, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"Updated config for {symbol}")

if __name__ == "__main__":
    analyzer = StrategyAnalyzer()
    
    print("Running Global Strategy Optimization...")
    for symbol in TRADING_SYMBOLS:
        weights = analyzer.analyze(symbol)
        if weights:
            # Check if any weight > 0 (tradeable)
            vals = [v for k,v in weights.items() if v > 0]
            if not vals:
                print(f"  [WARNING] {symbol} seems untradeable (All weights 0).")
            
            analyzer.update_config(symbol, weights)
        else:
            print(f"  [SKIP] No data for {symbol}")
