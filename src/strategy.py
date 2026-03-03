import pandas as pd
import numpy as np
import logging
import json
import os
import time

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

        raise NotImplementedError

# Constants for signal categorization to avoid string parsing in loops
LONG_KEYWORDS = {
    '_cross_21_up', '_gt_200', 'MACD_cross_up', 'MACD_gt_signal',
    'MACD_Bullish', 'RSI_Bullish', 'Bullish', 'oversold',
    'TK_Cross_Up', 'Vol_Spike', 'Price_Above_VWAP', 'Price_lt_BB_Low',
    'bounce_from_support', 'breakout_above_resistance',
    'Stoch_Oversold', 'Stoch_K_Cross_Up', '_gt_50',
}

SHORT_KEYWORDS = {
    '_cross_21_down', '_lt_200', 'MACD_cross_down', 'MACD_lt_signal',
    'MACD_Bearish', 'RSI_Bearish', 'Bearish', 'overbought',
    'TK_Cross_Down', 'Price_Below_VWAP', 'Price_gt_BB_Up',
    'bounce_from_resistance', 'breakout_below_support',
    'Stoch_Overbought', 'Stoch_K_Cross_Down', '_lt_50',
}


class WeightedScoringStrategy(Strategy):
    # Static cache to allow synchronous access to DB-backed configs
    _config_cache = {} 
    
    def __init__(self, symbol="default", timeframe="1h", exchange=None):
        super().__init__(name=f"WeightedScoringStrategy_{symbol}_{timeframe}")
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange
        self.logger = logging.getLogger(__name__)
        self.config_version = 0
        
        # Load config from static cache or fallback to default
        self.config_data = self._get_cached_config(symbol, timeframe, exchange)
        self.weights = self.config_data.get('weights', self.get_default_weights())
        
        # RL BRAIN
        try:
            self.brain = NeuralBrain(input_size=17)
            self.use_brain = True
        except Exception as e:
            self.logger.warning(f"Failed to init NeuralBrain: {e}")
            self.use_brain = False
            
        self.long_signals_cache = set()
        self.short_signals_cache = set()
        self._precalculate_signal_categories()

    def _precalculate_signal_categories(self):
        """Pre-processes weights to separate long and short signals for faster scoring."""
        self.long_signals_cache = set()
        self.short_signals_cache = set()
        for sig in self.weights.keys():
            sig_lower = sig.lower()
            if any(k.lower() in sig_lower for k in LONG_KEYWORDS):
                self.long_signals_cache.add(sig)
            elif any(k.lower() in sig_lower for k in SHORT_KEYWORDS):
                self.short_signals_cache.add(sig)

    def _get_cached_config(self, symbol, timeframe, exchange):
        """Fetch config from static cache with fallbacks."""
        # Standardized normalization
        clean_symbol = symbol.split(':')[0].replace('/', '_').upper() if symbol != 'default' else 'default'
        
        # 1. Exact match (Standardized DB format should use BTC/USDT, but we handle both)
        norm_sym = symbol.replace('_', '/') 
        
        keys_to_try = [
            (norm_sym, timeframe, exchange),
            (symbol, timeframe, exchange),
            ('default', 'default', 'default')
        ]
        
        for k in keys_to_try:
            if k in self._config_cache:
                return self._config_cache[k]
        
        # 2. Key-based match (legacy support)
        key_ex = f"{exchange}_{clean_symbol}_{timeframe}" if (exchange and timeframe) else None
        if key_ex and key_ex in self._config_cache:
            return self._config_cache[key_ex]
            
        return {}

    @classmethod
    def update_cache(cls, all_configs: list):
        """Populate the static cache from DB rows."""
        new_cache = {}
        for c in all_configs:
            # Index by the primary columns
            new_cache[(c['symbol'], c['timeframe'], c['exchange'])] = c
        cls._config_cache = new_cache

    def load_weights(self, symbol, timeframe, exchange=None):
        """Legacy placeholder: weights are now loaded via static cache + _get_cached_config."""
        self.config_data = self._get_cached_config(symbol, timeframe, exchange)
        return self.config_data.get('weights', self.get_default_weights())
    
    def is_enabled(self):
        return self.config_data.get('enabled', True)

    def reload_config(self):
        """Reloads parameters from cache."""
        self.config_data = self._get_cached_config(self.symbol, self.timeframe, self.exchange)
        self.weights = self.config_data.get('weights', self.get_default_weights())
        self._precalculate_signal_categories()
        print(f"🔄 [{self.symbol} {self.timeframe}] Config reloaded from cache.")

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
            target_tier_key = 'low'
            if score >= 7.0: target_tier_key = 'high'
            elif score >= 5.0: target_tier_key = 'medium'
            
            selected_tier = tiers.get(target_tier_key, tiers.get('low', res))
            
            return {
                "leverage": selected_tier.get('leverage', 3),
                "cost_usdt": selected_tier.get('cost_usdt', 3.0) 
            }
            
        tiers = self.config_data['tiers']
        selected_tier = res # default
             
        from config import GLOBAL_MAX_LEVERAGE, GLOBAL_MAX_COST_PER_TRADE
        
        final_tier = selected_tier.copy()
        if 'leverage' in final_tier:
             final_tier['leverage'] = min(final_tier['leverage'], GLOBAL_MAX_LEVERAGE)
        
        if 'cost_usdt' in final_tier:
             final_tier['cost_usdt'] = min(final_tier['cost_usdt'], GLOBAL_MAX_COST_PER_TRADE)
             
        if 'cost_usdt' not in final_tier and 'leverage' in final_tier:
             final_tier['cost_usdt'] = min(5.0, GLOBAL_MAX_COST_PER_TRADE)
             
        return final_tier

    def get_dynamic_risk_params(self, row):
        """
        Calculates SL and TP percentages. 
        If dynamic SLTP is enabled, uses ATR for volatility-based protection.
        """
        import config
        
        # Default from config
        sl_pct = getattr(config, 'STOP_LOSS_PCT', 0.02)
        tp_pct = getattr(config, 'TAKE_PROFIT_PCT', 0.04)
        
        # Dynamic ATR-based SLTP
        if getattr(config, 'ENABLE_DYNAMIC_SLTP', False):
            atr = row.get('ATR_14')
            close = row.get('close')
            
            if atr and close and close > 0:
                # Use ATR multiplier from config (default 1.5x ATR)
                mult = getattr(config, 'ATR_TRAIL_MULTIPLIER', 1.5)
                dynamic_sl = (atr * mult) / close
                
                # Take Profit usually 2-3x the SL for positive risk/reward
                dynamic_tp = dynamic_sl * 2.0
                
                # Apply safety bounds (don't go below 0.5% or above 10%)
                sl_pct = max(0.005, min(dynamic_sl, 0.10))
                tp_pct = max(0.01, min(dynamic_tp, 0.20))
                
        return sl_pct, tp_pct

    def get_default_weights(self):
        # Keys MUST match FeatureEngineer signal column names (without 'signal_' prefix)
        return {
            # --- LONG signals ---
            "EMA_9_cross_21_up":     1.5,   # signal_EMA_9_cross_21_up
            "EMA_50_gt_200":         2.0,   # signal_EMA_50_gt_200 (golden cross state)
            "MACD_cross_up":         1.5,   # signal_MACD_cross_up
            "MACD_gt_signal":        1.0,   # signal_MACD_gt_signal (histogram > 0)
            "RSI_14_oversold":       1.0,   # signal_RSI_14_oversold
            "RSI_14_gt_50":          0.8,   # signal_RSI_14_gt_50
            "Ichimoku_TK_Cross_Up":  1.3,   # signal_Ichimoku_TK_Cross_Up
            "Vol_Spike":             1.0,   # signal_Vol_Spike
            "Price_Above_VWAP":      0.9,   # signal_Price_Above_VWAP
            "bounce_from_support":   1.2,   # signal_bounce_from_support
            "RSI_Bullish_Div":       0.8,   # signal_RSI_Bullish_Div
            "MACD_Bullish_Div":      0.8,   # signal_MACD_Bullish_Div
            "Stoch_Oversold":        0.7,   # signal_Stoch_Oversold
            "Stoch_K_Cross_Up":      0.9,   # signal_Stoch_K_Cross_Up
            "Price_lt_BB_Low":       0.8,   # signal_Price_lt_BB_Low (mean-reversion buy)
            "breakout_above_resistance": 1.1, # signal_breakout_above_resistance

            # --- SHORT signals ---
            "EMA_9_cross_21_down":   1.5,   # signal_EMA_9_cross_21_down
            "EMA_50_lt_200":         2.0,   # signal_EMA_50_lt_200 (death cross state)
            "MACD_cross_down":       1.5,   # signal_MACD_cross_down
            "MACD_lt_signal":        1.0,   # signal_MACD_lt_signal
            "RSI_14_overbought":     1.0,   # signal_RSI_14_overbought
            "RSI_14_lt_50":          0.8,   # signal_RSI_14_lt_50
            "Ichimoku_TK_Cross_Down":1.3,   # signal_Ichimoku_TK_Cross_Down
            "Price_Below_VWAP":      0.9,   # signal_Price_Below_VWAP
            "bounce_from_resistance":1.2,   # signal_bounce_from_resistance
            "RSI_Bearish_Div":       0.8,   # signal_RSI_Bearish_Div
            "MACD_Bearish_Div":      0.8,   # signal_MACD_Bearish_Div
            "Stoch_Overbought":      0.7,   # signal_Stoch_Overbought
            "Stoch_K_Cross_Down":    0.9,   # signal_Stoch_K_Cross_Down
            "Price_gt_BB_Up":        0.8,   # signal_Price_gt_BB_Up (mean-reversion sell)
            "breakout_below_support":1.1,   # signal_breakout_below_support
        }

    def get_signal(self, row, tracker=None, use_adaptive=True, use_brain=True, bms_score=None, bms_zone=None):
        """
        Calculates LONG/SHORT score based on DYNAMIC signals from config.
        Checks if 'signal_{key}' exists in row and is True.
        
        Args:
            row: Single row of features (pandas Series or dict)
            use_adaptive: If True, apply adaptive weight adjustments from signal tracker
            use_brain: If True, include Neural Brain scores
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
        
        if use_adaptive and tracker:
            try:
                w = tracker.adjust_weights(w)
            except Exception:
                pass  # Fallback to base weights if tracker error

        # Iterate through all weights in config
        for signal_name, weight in w.items():
            if weight == 0: continue
            
            # Construct column name internally used in FeatureEngineer
            col_name = f"signal_{signal_name}"
            
            # Check if this signal is active in the current row
            if col_name in row and row[col_name]:
                # Use pre-calculated sets for extreme speed (O(1) member check)
                if signal_name in self.long_signals_cache:
                    score_long += weight
                    reasons_long.append(signal_name)
                elif signal_name in self.short_signals_cache:
                    score_short += weight
                    reasons_short.append(signal_name)
                    
        # === BMS WEIGHTING (INTELLIGENT SHIELD) ===
        # Formula: FinalScore = (AltScore * w_alt_adj) + (BMS * 10 * w_btc_adj)
        w_btc = getattr(self, 'w_btc', 0.5)
        w_alt = getattr(self, 'w_alt', 0.5)
        
        if bms_score is not None:
            w_btc_adj = w_btc
            w_alt_adj = w_alt

            # 1. Zone Adjustments
            if bms_zone == 'GREEN':
                # Boost BTC weight in GREEN zone (risk-on)
                w_btc_adj = min(w_btc * 1.2, 0.9)
                w_alt_adj = 1.0 - w_btc_adj
                # Veto Short signals in GREEN zone
                score_short = 0.0
            elif bms_zone == 'RED':
                # RED zone is defensive (risk-off)
                # Veto Long signals in RED zone
                score_long = 0.0
                # In RED zone, we don't boost BTC weight, we just keep it or shift to exit
            
            # 2. Score Calculation
            score_long = (score_long * w_alt_adj) + (bms_score * 10.0 * w_btc_adj)
            score_short = (score_short * w_alt_adj) + ((1.0 - bms_score) * 10.0 * w_btc_adj)

        # === DECISION ===
        thresholds = self.config_data.get('thresholds', {'entry_score': 5.0, 'exit_score': 2.5})
        entry_thresh = thresholds['entry_score']
        
        confidence = 0.0
        signal = {'side': 'SKIP', 'confidence': 0.0, 'comment': 'Wait', 'snapshot': None}
        
        # === NEURAL BRAIN INTEGRATION ===
        # Skip brain inference during backtests for maximum speed
        if not use_brain:
            if score_long >= entry_thresh:
                signal = {'side': 'BUY', 'confidence': min(score_long / 10.0, 1.0), 'comment': f'Score {score_long:.1f}', 'snapshot': None}
            elif score_short >= entry_thresh:
                signal = {'side': 'SELL', 'confidence': min(score_short / 10.0, 1.0), 'comment': f'Score {score_short:.1f}', 'snapshot': None}
            return signal

        # Extract features for brain (must match NeuralBrain input size = 17)
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

        # Case: New trade - SL original = current target SL, moves = 0, pnl = 0
        sl_orig = row.get('sl', row.get('close', 0))
        entry = row.get('close', 1)
        side_mult = 1 # We don't know side yet in this logic block, but we can assume BUY for scaling
        # Actually, it's better to just use neutral 0.5 for placeholders at entry
        feature_vector.extend([0.5, 0.5, 0.0, 0.0, 0.0]) # [dist_orig, dist_final, moves, tightened, max_pnl]
        
        neural_score = 0.5
        if self.use_brain and use_brain:

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
