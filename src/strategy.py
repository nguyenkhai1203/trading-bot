import pandas as pd
from neural_brain import NeuralBrain

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
    def __init__(self, symbol="default", timeframe="1h", exchange=None):
        super().__init__(name=f"WeightedScoringStrategy_{symbol}_{timeframe}")
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange
        import logging
        self.logger = logging.getLogger(__name__)
        self.config_mtime = 0  # Track config file modification time
        self.config_version = 0  # Version number for positions to reference
        self.weights = self.load_weights(symbol, timeframe, exchange)
        
        # RL BRAIN (Input Size = 17 Normalized Features v4.0)
        try:
            self.brain = NeuralBrain(input_size=17)
            self.use_brain = True
        except Exception as e:
            print(f"⚠️ Failed to init NeuralBrain: {e}")
            self.use_brain = False
        
    def load_weights(self, symbol, timeframe, exchange=None):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        if not os.path.exists(config_path):
            print("Config not found, using defaults")
            return self.get_default_weights()
            
        with open(config_path, 'r') as f:
            data = json.load(f)
        
        # Track file modification time and increment version on reload
        import os.path as osp
        mtime = osp.getmtime(config_path)
        if mtime != self.config_mtime:
            self.config_version += 1
            self.config_mtime = mtime
            
        # Try specific exchange_symbol_timeframe, then fallback to symbol if not found
        # Issue 9: Standardized key is EXCHANGE_BASE_QUOTE_TF
        clean_symbol = symbol.split(':')[0].replace('/', '_').upper()
        key_ex = f"{exchange}_{clean_symbol}_{timeframe}" if (exchange and timeframe) else None
        key_tf = f"{clean_symbol}_{timeframe}"
        
        if key_ex and key_ex in data:
            self.config_data = data[key_ex]
            self.logger.debug(f"Loaded exchange-specific config for {key_ex}")
        elif key_tf in data:
            self.config_data = data[key_tf]
        elif symbol in data:
            self.config_data = data[symbol]
        elif 'default' in data:
            self.config_data = data['default']
        else:
            self.config_data = {}
        
        # Load Risk Settings
        risk = self.config_data.get('risk', {})
        from config import STOP_LOSS_PCT, TAKE_PROFIT_PCT
        self.sl_pct = risk.get('sl_pct', STOP_LOSS_PCT)
        self.tp_pct = risk.get('tp_pct', TAKE_PROFIT_PCT)
             
        return self.config_data.get('weights', self.get_default_weights())
    
    def reload_weights_if_changed(self):
        """Check if config file changed, reload if needed."""
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        if not os.path.exists(config_path):
            return
        
        import os.path as osp
        current_mtime = osp.getmtime(config_path)
        
        # Check if config file was modified
        if current_mtime != self.config_mtime:
            print(f"[CONFIG RELOAD] {self.symbol}_{self.timeframe} - Config file changed, reloading...")
            self.load_weights(self.symbol, self.timeframe, self.exchange)
    
    def is_enabled(self):
        """Check if this strategy config is enabled.
        Used to block NEW positions if config becomes disabled.
        """
        return self.config_data.get('enabled', True)

    def reload_config(self):
        """Reloads parameters from strategy_config.json"""
        self.weights = self.load_weights(self.symbol, self.timeframe, self.exchange)
        print(f"🔄 [{self.symbol} {self.timeframe}] Config reloaded.")

    def get_sizing_tier(self, score):
        # Default fallback (Use LOW tier to ensure min notional is met)
        # Low Tier: Lev 3, Cost 3 -> Notional 9 > 5 (Ok)
        # Old Fallback: Lev 1, Cost 5 -> Notional 5 (Borderline/Fail)
        res = { "leverage": 3, "cost_usdt": 3.0 } 
        
        if not hasattr(self, 'config_data') or 'tiers' not in self.config_data:
            import config
            # Use CONFIDENCE_TIERS from config.py as the primary source
            tiers = getattr(config, 'CONFIDENCE_TIERS', {})
            
            # Map weighted score to tier keys (high/medium/low)
            # Strategy score is typically 0-10+. CONFIDENCE_TIERS uses 'min_confidence' (0-1) or we map score.
            # Adaptation: Map Score to Confidence Level for lookup
            # Score 7+ -> High, 5-7 -> Medium, <5 -> Low
            
            # Since CONFIDENCE_TIERS is keyed by 'high', 'medium', 'low', we select based on score.
            target_tier_key = 'low'
            if score >= 7.0: target_tier_key = 'high'
            elif score >= 5.0: target_tier_key = 'medium'
            
            selected_tier = tiers.get(target_tier_key, tiers.get('low', res))
            
            # Ensure return has required keys
            return {
                "leverage": selected_tier.get('leverage', 3),
                "cost_usdt": selected_tier.get('cost_usdt', 3.0) 
            }
            
        tiers = self.config_data['tiers']
        
        # Check tiers in order from strategy_config.json
        selected_tier = res # default
             
        # GLOBAL OVERRIDE: Clamp to config safety settings
        from config import GLOBAL_MAX_LEVERAGE, GLOBAL_MAX_COST_PER_TRADE
        
        final_tier = selected_tier.copy()
        if 'leverage' in final_tier:
             final_tier['leverage'] = min(final_tier['leverage'], GLOBAL_MAX_LEVERAGE)
        
        if 'cost_usdt' in final_tier:
             final_tier['cost_usdt'] = min(final_tier['cost_usdt'], GLOBAL_MAX_COST_PER_TRADE)
             
        # Fallback if cost_usdt missing but tier selected
        if 'cost_usdt' not in final_tier and 'leverage' in final_tier:
             final_tier['cost_usdt'] = min(5.0, GLOBAL_MAX_COST_PER_TRADE)
             
        return final_tier

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

    def get_signal(self, row, use_adaptive=True):
        """
        Calculates LONG/SHORT score based on DYNAMIC signals from config.
        Checks if 'signal_{key}' exists in row and is True.
        
        Args:
            row: Single row of features (pandas Series or dict)
            use_adaptive: If True, apply adaptive weight adjustments from signal tracker
        """
        # Skip if explicitly disabled in config
        if not self.config_data.get('enabled', True):
            return {'side': 'SKIP', 'confidence': 0.0, 'comment': 'Disabled in config'}

        score_long = 0.0
        score_short = 0.0
        reasons_long = []
        reasons_short = []
        
        # Get base weights, optionally adjusted by adaptive learning
        w = self.weights.copy()
        
        if use_adaptive:
            try:
                from signal_tracker import tracker
                w = tracker.adjust_weights(w)
            except Exception:
                pass  # Fallback to base weights if tracker unavailable

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
                # Broadened polarity detection
                is_long = any(x in signal_name for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'Price_lt_BB', 'gt_50', 'gt_'])
                is_short = any(x in signal_name for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'Price_gt_BB', 'lt_50', 'lt_'])
                
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
        signal = {'side': 'SKIP', 'confidence': 0.0, 'comment': 'Wait', 'snapshot': None}
        
        # === NEURAL BRAIN INTEGRATION ===
        # Extract features for brain (must match NeuralBrain input size = 12)
        # Handle missing keys safely with defaults
        snapshot = {
            'norm_RSI_7': row.get('norm_RSI_7', 0.5),
            'norm_RSI_14': row.get('norm_RSI_14', 0.5),
            'norm_RSI_21': row.get('norm_RSI_21', 0.5),
            'norm_MACD': row.get('norm_MACD', 0.5),
            'norm_BB_Width': row.get('norm_BB_Width', 0.5),
            'norm_Price_in_BB': row.get('norm_Price_in_BB', 0.5),
            'norm_Volume': row.get('norm_Volume', 0.0),
            'norm_ADX': row.get('norm_ADX', 0.0),
            'norm_ATR': row.get('norm_ATR', 0.0),
            'state_pnl_pct': row.get('state_pnl_pct', 0.0),
            'state_leverage': row.get('state_leverage', 0.0),
            'state_equity_ratio': row.get('state_equity_ratio', 1.0)
        }
        
        # Convert to list for brain
        feature_vector = list(snapshot.values())
        
        # Add 5 dynamic context placeholders for v4.0 (Defaults for NEW trades)
        import numpy as np
        # Case: New trade - SL original = current target SL, moves = 0, pnl = 0
        sl_orig = row.get('sl', row.get('close', 0))
        entry = row.get('close', 1)
        side_mult = 1 # We don't know side yet in this logic block, but we can assume BUY for scaling
        # Actually, it's better to just use neutral 0.5 for placeholders at entry
        feature_vector.extend([0.5, 0.5, 0.0, 0.0, 0.0]) # [dist_orig, dist_final, moves, tightened, max_pnl]
        
        neural_score = 0.5
        if self.use_brain:
            try:
                # Replace NaNs with defaults just in case
                clean_vector = [0.5 if (pd.isna(x) if hasattr(pd, 'isna') else x is None) else x for x in feature_vector]
                neural_score = self.brain.predict(clean_vector)
            except Exception as e:
                print(f"⚠️ Brain error: {e}")
        
        # Merge Heuristic Score + Neural Score
        # Strategy: Brain acts as a Validator (Filter)
        # If Brain < 0.3 -> VETO (Block trade)
        # If Brain > 0.8 -> BOOST (Increase confidence)
        
        final_side = 'SKIP'
        final_conf = 0.0
        comment = 'Wait'

        if score_long >= entry_thresh:
            final_side = 'BUY'
            base_conf = min(score_long / 10.0, 1.0)
            
            # --- BRAIN INTERVENTION (Only if trained) ---
            if self.use_brain and self.brain.is_trained:
                if neural_score < 0.3:
                    final_side = 'SKIP'
                    comment = f"Brain VETO (Score {neural_score:.2f}) on Long"
                elif neural_score > 0.8:
                    final_conf = min(base_conf * 1.2, 1.0) # 20% Boost
                    comment = f"Long Score {score_long:.1f} + Brain {neural_score:.2f} 🚀"
                else:
                    final_conf = base_conf
                    comment = f"Long Score {score_long:.1f} ({','.join(reasons_long)})"
            else:
                final_conf = base_conf
                brain_status = "WaitData" if (self.use_brain and not self.brain.is_trained) else "Off"
                comment = f"Long Score {score_long:.1f} (Brain:{brain_status})"

        elif score_short >= entry_thresh:
            final_side = 'SELL'
            base_conf = min(score_short / 10.0, 1.0)
            
            # --- BRAIN INTERVENTION (Only if trained) ---
            if self.use_brain and self.brain.is_trained:
                if neural_score < 0.3:
                    final_side = 'SKIP'
                    comment = f"Brain VETO (Score {neural_score:.2f}) on Short"
                elif neural_score > 0.8:
                    final_conf = min(base_conf * 1.2, 1.0) # 20% Boost
                    comment = f"Short Score {score_short:.1f} + Brain {neural_score:.2f} 🚀"
                else:
                    final_conf = base_conf
                    comment = f"Short Score {score_short:.1f} ({','.join(reasons_short)})"
            else:
                final_conf = base_conf
                brain_status = "WaitData" if (self.use_brain and not self.brain.is_trained) else "Off"
                comment = f"Short Score {score_short:.1f} (Brain:{brain_status})"
                
        signal = {
            'side': final_side,
            'confidence': final_conf,
            'comment': comment,
            'snapshot': snapshot,         # Save features for training
            'neural_score': neural_score  # Save score for analysis
        }
            
        return signal
