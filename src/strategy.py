import pandas as pd

class Strategy:
    def __init__(self, name="BaseStrategy"):
        self.name = name

    def get_signal(self, row):
        """
        Input: Single row of features (pandas Series or dict)
        Output: Dict {'side': 'BUY'/'SELL'/'SKIP', 'confidence': float, 'comment': str}
        """
        raise NotImplementedError

import json
import os

class WeightedScoringStrategy(Strategy):
    def __init__(self, symbol="default"):
        super().__init__(name=f"WeightedScoringStrategy_{symbol}")
        self.symbol = symbol
        self.weights = self.load_weights(symbol)
        
    def load_weights(self, symbol):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        if not os.path.exists(config_path):
            print("Config not found, using defaults")
            return self.get_default_weights()
            
        with open(config_path, 'r') as f:
            data = json.load(f)
            
        # Try specific symbol, then default
        if symbol in data:
            self.config_data = data[symbol]
        elif 'default' in data:
             self.config_data = data['default']
        else:
             self.config_data = {}
             
        return self.config_data.get('weights', self.get_default_weights())

    def get_sizing_tier(self, score):
        # Default fallback
        res = { "leverage": 1, "risk_pct": 0.01 }
        
        if not hasattr(self, 'config_data') or 'tiers' not in self.config_data:
            # Fallback hardcoded if config missing
            if score >= 7.0: return { "leverage": 5, "cost_usdt": 8.0 }
            else: return { "leverage": 3, "cost_usdt": 3.0 }
            
        tiers = self.config_data['tiers']
        # Check High first
        if score >= tiers['high']['min_score']:
            return tiers['high']
        elif score >= tiers['low']['min_score']:
             return tiers['low']
             
        return res

    def get_default_weights(self):
        return {
            "EMA9_EMA21_cross_up": 1.5,
            "EMA50_EMA200_golden_cross": 2.0,
            "MACD_line_cross_signal_up": 1.5,
            "MACD_histogram_positive": 1.0,
            "RSI_oversold": 1.0,
            "RSI_above_50": 0.8,
            "Ichimoku_price_above_cloud": 1.2,
            "Ichimoku_tenkan_kijun_cross_up": 1.3,
            "Volume_spike_up": 1.0,
            "Price_above_VWAP": 0.9,
            
            "EMA9_EMA21_cross_down": 1.5,
            "EMA50_EMA200_death_cross": 2.0,
            "MACD_line_cross_signal_down": 1.5,
            "RSI_overbought": 1.0,
            "Ichimoku_price_below_cloud": 1.2,
            "Ichimoku_tenkan_kijun_cross_down": 1.3,
            "Volume_spike_down": 1.0,
            "Price_below_VWAP": 0.9
        }

    def get_signal(self, row):
        """
        Calculates LONG/SHORT score based on DYNAMIC signals from config.
        Checks if 'signal_{key}' exists in row and is True.
        """
        score_long = 0.0
        score_short = 0.0
        reasons_long = []
        reasons_short = []
        w = self.weights

        # Iterate through all weights in config
        for signal_name, weight in w.items():
            if weight == 0: continue
            
            # Construct column name internally used in FeatureEngineer
            # e.g., config 'RSI_14_oversold' matches 'signal_RSI_14_oversold' column
            col_name = f"signal_{signal_name}"
            
            # Check if this signal is active in the current row
            if col_name in row and row[col_name]:
                # Determine polarity based on name
                # (Simple heuristic: oversold/cross_up/gt/golden -> Long)
                is_long = any(x in signal_name for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'Price_lt_BB'])
                is_short = any(x in signal_name for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'Price_gt_BB'])
                
                # Special cases or manual mapping overrides
                # For safety, let's explicitly separate LONG vs SHORT keys in config or use robust naming.
                # Current config holds keys like "EMA9_EMA21_cross_up".
                
                if is_long:
                    score_long += weight
                    reasons_long.append(signal_name)
                elif is_short:
                    score_short += weight
                    reasons_short.append(signal_name)
                    
        # --- DECISION ---
        thresholds = self.config_data.get('thresholds', {'entry_score': 5.0, 'exit_score': 2.5})
        entry_thresh = thresholds['entry_score']
        
        confidence = 0.0
        signal = {'side': 'SKIP', 'confidence': 0.0, 'comment': 'Wait'}
        
        if score_long >= entry_thresh:
            confidence = min(score_long / 10.0, 1.0)
            signal = {
                'side': 'BUY', 
                'confidence': confidence, 
                'comment': f"Long Score {score_long:.1f} ({','.join(reasons_long)})"
            }
        elif score_short >= entry_thresh:
            confidence = min(score_short / 10.0, 1.0)
            signal = {
                'side': 'SELL', 
                'confidence': confidence, 
                'comment': f"Short Score {score_short:.1f} ({','.join(reasons_short)})"
            }
            
        return signal
