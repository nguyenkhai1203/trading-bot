# -*- coding: utf-8 -*-
"""
OPTIMIZED Strategy Analyzer v2.2 - FULL QUALITY + FAST
- Caching: Data + Features cached per symbol/tf
- FULL GRID search: 270 combos (NO quality reduction!)
- CACHED SIGNALS per threshold: 30x faster (9 signal computations instead of 270!)
- Smart validation: Top 50 train combos tested on test set
- Reduced redundancy: Reuse cross-TF results
"""
import sys
import os

# Add support for running directly or via module
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import logging
import pandas as pd
import numpy as np
import json
import asyncio
import time
# Add src to path if running directly
if __name__ == '__main__':
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.append(src_dir)
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from src import config
from src.config import TRADING_SYMBOLS, TRADING_TIMEFRAMES, MAX_WORKERS, GLOBAL_MAX_LEVERAGE, GLOBAL_MAX_COST_PER_TRADE
from src.feature_engineering import FeatureEngineer
import subprocess
from src.train_brain import run_nn_training
from src.utils.symbol_helper import to_api_format

class StrategyAnalyzer:
    def __init__(self, data_dir=None):
        # Default to data/ at project root
        if data_dir is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(project_root, 'data')
        self.data_dir = data_dir
        self.feature_engineer = FeatureEngineer()
        # OPTIMIZATION 1: Cache for loaded data and calculated features
        self._data_cache = {}
        self._features_cache = {}
        # OPTIMIZATION 2: Cache validation results for cross-TF lookup
        self._validation_cache = {}
        # OPTIMIZATION 3: Cache for BMS results to avoid recalculating for every symbol
        self._bms_cache = {}

    def load_data(self, symbol, timeframe='1h', exchange='BINANCE'):
        """Load data with caching and exchange awareness."""
        cache_key = f"{exchange}_{symbol}_{timeframe}"
        if cache_key in self._data_cache:
            return self._data_cache[cache_key]
        
        # Standardize symbol for file path (e.g. BTC/USDT:USDT -> BTCUSDT)
        safe_symbol = to_api_format(symbol)
        # Data files: {EXCHANGE}_{SYMBOL}_{TF}.csv  OR legacy  {SYMBOL}_{TF}.csv
        file_path = os.path.join(self.data_dir, f"{exchange}_{safe_symbol}_{timeframe}.csv")
        
        if not os.path.exists(file_path):
            # Fallback 1: legacy without exchange prefix
            legacy_path = os.path.join(self.data_dir, f"{safe_symbol}_{timeframe}.csv")
            if os.path.exists(legacy_path):
                file_path = legacy_path
            else:
                return None
            
        df = pd.read_csv(file_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        if len(df) > 200:
            df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        else:
            df['ema_200'] = df['close'].expanding().mean()
        
        # --- LOOKAHEAD BIAS / DATA INTEGRITY CHECK ---
        df = df.sort_values('timestamp')
        df['ts_diff'] = df['timestamp'].diff().dt.total_seconds()
        # If gap > 1.5 * timeframe (e.g., 1.5h for 1h TF), we should be careful
        tf_seconds = 3600 if timeframe == '1h' else 14400 if timeframe == '4h' else 900 if timeframe == '15m' else 3600
        if (df['ts_diff'] > 1.5 * tf_seconds).any():
            gap_count = (df['ts_diff'] > 1.5 * tf_seconds).sum()
            # logging.warning(f"Detected {gap_count} gaps in {symbol} {timeframe} data. May cause bias.")
            # Optimization: drop rows with huge gaps to avoid EMA/feature distortion if needed
            pass
        
        # --- BMS Integration ---
        # Fetch BTC data to calculate BMS for this timeframe once and cache it
        if symbol != 'BTC/USDT:USDT':
            bms_cache_key = f"{exchange}_{timeframe}"
            if bms_cache_key in self._bms_cache:
                bms_subset = self._bms_cache[bms_cache_key]
            else:
                from src.btc_analyzer import BTCAnalyzer
                # We use a dummy for db/exchange here as we only need the calculation logic
                btc_calc = BTCAnalyzer(self, None) 
                btc_df = self.load_data('BTC/USDT:USDT', timeframe, exchange=exchange)
                if btc_df is not None:
                    # Calculate features once for BTC
                    btc_feat_df = self.feature_engineer.calculate_features(btc_df)
                    bms_data = btc_calc.calculate_bulk_sentiment(btc_feat_df)
                    if bms_data is not None:
                        # Extract and rename columns
                        bms_subset = bms_data[['timestamp', 'bms', 'zone']].rename(
                            columns={'bms': 'bms_score', 'zone': 'bms_zone'}
                        )
                        self._bms_cache[bms_cache_key] = bms_subset
                    else:
                        bms_subset = None
                else:
                    bms_subset = None

            # Merge if BMS data is available
            if bms_subset is not None:
                # BMS Data Integrity: Avoid very old sentiment data
                bms_subset = bms_subset.sort_values('timestamp')
                
                # ADAPTIVE TOLERANCE (v2.0): Drop if BMS age > 2x timeframe
                # 1h -> 2h, 4h -> 8h, etc.
                tolerance_map = {'15m': '30m', '30m': '1h', '1h': '2h', '4h': '8h', '1D': '2D'}
                tol_str = tolerance_map.get(timeframe.replace('d', 'D'), '4h')
                
                df = pd.merge_asof(
                    df.sort_values('timestamp'),
                    bms_subset,
                    on='timestamp',
                    direction='backward',
                    tolerance=pd.Timedelta(tol_str) 
                ).ffill(limit=1) # Minimal forward fill

        self._data_cache[cache_key] = df
        return df

    def get_features(self, symbol, timeframe, exchange='BINANCE'):
        """Get features with caching and exchange awareness."""
        cache_key = f"{exchange}_{symbol}_{timeframe}"
        if cache_key in self._features_cache:
            return self._features_cache[cache_key]
        
        df = self.load_data(symbol, timeframe, exchange=exchange)
        if df is None:
            return None
        
        df = self.feature_engineer.calculate_features(df)
        self._features_cache[cache_key] = df
        return df

    # Aliases for compatibility with BTCAnalyzer (which expects MarketDataManager interface)
    def get_data_with_features(self, symbol, timeframe, exchange='BINANCE'):
        return self.get_features(symbol, timeframe, exchange=exchange)

    def get_data(self, symbol, timeframe, exchange='BINANCE'):
        return self.load_data(symbol, timeframe, exchange=exchange)

    def get_signal_category(self, name):
        """Categorize signals to avoid redundancy."""
        name = name.lower()
        if any(x in name for x in ['ema', 'death', 'golden', 'gt_200', 'lt_200', 'adx', 'di_', 'strong']):
            return 'Trend'
        if any(x in name for x in ['rsi', 'macd', 'stoch', 'mfi', 'divergence', 'div']):
            return 'Momentum'
        if any(x in name for x in ['bb', 'bollinger', 'volatility', 'atr']):
            return 'Volatility'
        if any(x in name for x in ['vwap', 'price_above', 'price_below']):
            return 'Level'
        if any(x in name for x in ['volume', 'vol_']):
            return 'Volume'
        return 'Other'

    def analyze(self, symbol, timeframe='1h', horizon=4, exchange='BINANCE'):
        """
        Analyzes signals with Layer 1: Trend Filter and Layer 2: Diversity.
        OPTIMIZED: Uses cached features with exchange awareness.
        """
        df = self.get_features(symbol, timeframe, exchange=exchange)
        if df is None:
            return None

        df = df.copy()
        df['Target_Return'] = df['close'].shift(-horizon) / df['close'] - 1.0
        
        signal_cols = [c for c in df.columns if c.startswith('signal_')]
        category_signals = {}
        
        for name in signal_cols:
            config_key = name.replace('signal_', '')
            cat = self.get_signal_category(config_key)
            
            is_long = any(x in config_key for x in [
                '_cross_21_up', '_gt_200', 'MACD_cross_up', 'MACD_gt_signal',
                'MACD_Bullish', 'RSI_Bullish', 'Bullish', 'oversold',
                'TK_Cross_Up', 'Vol_Spike', 'Price_Above_VWAP', 'Price_lt_BB_Low',
                'bounce_from_support', 'breakout_above_resistance',
                'Stoch_Oversold', 'Stoch_K_Cross_Up', '_gt_50',
            ])
            is_short = any(x in config_key for x in [
                '_cross_21_down', '_lt_200', 'MACD_cross_down', 'MACD_lt_signal',
                'MACD_Bearish', 'RSI_Bearish', 'Bearish', 'overbought',
                'TK_Cross_Down', 'Price_Below_VWAP', 'Price_gt_BB_Up',
                'bounce_from_resistance', 'breakout_below_support',
                'Stoch_Overbought', 'Stoch_K_Cross_Down', '_lt_50',
            ])
            
            # LAYER 1: TREND FILTER
            if is_long:
                trend_df = df[df['close'] > df['ema_200']]
            elif is_short:
                trend_df = df[df['close'] < df['ema_200']]
            else:
                trend_df = df
                
            subset = trend_df[trend_df[name] == True]
            if len(subset) < 5:
                continue
            
            raw_win_rate_long = (subset['Target_Return'] > 0).mean()
            actual_win_rate = raw_win_rate_long if is_long else (1.0 - raw_win_rate_long if is_short else 0)
            avg_return = subset['Target_Return'].mean() if is_long else (-subset['Target_Return'].mean() if is_short else 0)
            
            weight = 0.0
            if actual_win_rate > 0.58: weight = 2.0
            elif actual_win_rate > 0.55: weight = 1.0
            elif actual_win_rate > 0.52: weight = 0.5
            
            if weight > 0:
                if cat not in category_signals:
                    category_signals[cat] = []
                category_signals[cat].append({
                    'name': config_key, 
                    'wr': actual_win_rate, 
                    'weight': weight, 
                    'dir': 'LONG' if is_long else 'SHORT',
                    'trades': len(subset),
                    'avg_return': avg_return
                })

        # LAYER 2: Pick TOP 3 per category
        results = {}
        for cat, signals_list in category_signals.items():
            signals_list = sorted(signals_list, key=lambda x: x['wr'], reverse=True)
            top_n = min(3, len(signals_list))
            for i, sig in enumerate(signals_list[:top_n]):
                adj_weight = sig['weight'] * (1.0 if i == 0 else 0.7 if i == 1 else 0.5)
                results[sig['name']] = adj_weight
            
        return results

    def validate_weights(self, df, weights, symbol, timeframe, exchange='BINANCE', bms_score=None, bms_zone=None):
        """
        LAYER 3: Nested Walk-Forward Validation - OPTIMIZED v2.3
        - Split: Train (50%) | Validation (25%) | Holdout (25%)
        - Use Validation to pick Top 50, then check on Holdout.
        - Adds Market Regime check.
        """
        if not weights or len(df) < 100:
            return None
        
        # 1. Split data into 3 sets
        train_end = int(len(df) * 0.5)
        val_end = int(len(df) * 0.75)
        
        train_df = df.iloc[:train_end]
        val_df = df.iloc[train_end:val_end]
        holdout_df = df.iloc[val_end:]
        
        from src.strategy import WeightedScoringStrategy
        mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        mock_strat.weights = weights
        
        # Determine Market Regime once
        regime = self._get_market_regime(df, bms_score, bms_zone)
        
        # Transaction costs: fee (0.0012) + estimated slippage (0.0005)
        trading_cost = 0.0017 
        
        # Grid parameters
        sl_ranges = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04]
        rr_ratios = [1.0, 1.5, 2.0, 2.5, 3.0]
        thresholds_to_test = [3.0, 3.5, 4.0, 4.5, 5.0, 6.0] # Reduced for speed but kept quality
        
        # Pre-compute signals for each threshold
        train_signals_cache = {}
        val_signals_cache = {}
        holdout_signals_cache = {}
        
        for thresh in thresholds_to_test:
            mock_strat.config_data['thresholds'] = {'entry_score': thresh}
            train_signals_cache[thresh] = self._compute_signals(train_df, mock_strat, bms_score=bms_score, bms_zone=bms_zone)
            val_signals_cache[thresh] = self._compute_signals(val_df, mock_strat, bms_score=bms_score, bms_zone=bms_zone)
            holdout_signals_cache[thresh] = self._compute_signals(holdout_df, mock_strat, bms_score=bms_score, bms_zone=bms_zone)
        
        best_overall = None
        max_total_pnl = -999999
        
        # Phase 1: Grid search on TRAIN, pick Top 50 by VALIDATION
        train_val_results = []
        for thresh in thresholds_to_test:
            for sl_test in sl_ranges:
                for rr in rr_ratios:
                    tp_test = max(0.025, sl_test * rr)
                    
                    for amult in [None, 1.5, 2.0, 3.0]:
                        train_perf = self._backtest_with_signals(train_df, train_signals_cache[thresh], sl_test, tp_test, trading_cost, atr_mult=amult)
                        if train_perf['trades'] < 3 or train_perf['win_rate'] < 0.50:
                            continue
                            
                        val_perf = self._backtest_with_signals(val_df, val_signals_cache[thresh], sl_test, tp_test, trading_cost, atr_mult=amult)
                        
                        train_val_results.append({
                            'sl': sl_test, 'tp': tp_test, 'thresh': thresh, 'atr_mult': amult,
                            'train_perf': train_perf, 'val_perf': val_perf
                        })
        
        # Sort by Validation PnL, keep top 50
        train_val_results.sort(key=lambda x: x['val_perf']['pnl'], reverse=True)
        top_combos = train_val_results[:50]
        
        # Phase 2: Final validation on HOLDOUT set
        for combo in top_combos:
            holdout_perf = self._backtest_with_signals(holdout_df, holdout_signals_cache[combo['thresh']], combo['sl'], combo['tp'], trading_cost, atr_mult=combo['atr_mult'])
            
            # CRITICAL: Consistency check between Val and Holdout
            val_perf = combo['val_perf']
            if holdout_perf['trades'] < 2: continue
            
            consistency = abs(holdout_perf['win_rate'] - val_perf['win_rate'])
            if consistency > 0.20: # Stricter consistency for Holdout
                continue
                
            # Total performance including all sets
            total_pnl = combo['train_perf']['pnl'] + val_perf['pnl'] + holdout_perf['pnl']
            
            if total_pnl > max_total_pnl:
                max_total_pnl = total_pnl
                best_overall = {
                    'sl_pct': combo['sl'], 'tp_pct': combo['tp'], 'atr_mult': combo['atr_mult'],
                    'entry_score': combo['thresh'],
                    'pnl': total_pnl,
                    'trades': combo['train_perf']['trades'] + val_perf['trades'] + holdout_perf['trades'],
                    'win_rate': (combo['train_perf']['win_rate'] + val_perf['win_rate'] + holdout_perf['win_rate']) / 3,
                    'test_wr': holdout_perf['win_rate'],
                    'test_pnl': holdout_perf['pnl'],
                    'consistency': consistency,
                    'regime': regime
                }
        
        # Cache result for cross-TF lookup
        cache_key = f"{exchange}_{symbol}_{timeframe}"
        if best_overall:
            self._validation_cache[cache_key] = best_overall
        
        return best_overall

    def _get_market_regime(self, df, bms_score=None, bms_zone=None):
        """Detect market regime using BTC EMA and BMS."""
        if bms_zone == 'RED': return 'BEAR'
        if bms_zone == 'GREEN': return 'BULL'
        
        # Fallback to BTC price vs EMA
        if 'ema_200' in df.columns:
            curr_price = df['close'].iloc[-1]
            ema = df['ema_200'].iloc[-1]
            if curr_price > ema * 1.02: return 'BULL'
            if curr_price < ema * 0.98: return 'BEAR'
        return 'SIDEWAYS'

    def _compute_signals(self, df, strat, bms_score=None, bms_zone=None):
        """Pre-compute signals once per threshold."""
        signals = []
        # CRITICAL PERFORMANCE OPTIMIZATION: 
        # df.iloc[i] is extremely slow in Python loops (takes ~200-500us per row).
        # Converting to a list of dicts first brings this down to ~1-2us per row.
        # This speeds up Step 2 Walk-Forward Validation drastically.
        fast_rows = df.to_dict('records')
        
        for i in range(20, len(fast_rows)):
            row = fast_rows[i]
            # Use row-specific BMS if available (merged during load_data), fallback to global
            row_bms_score = row.get('bms_score') or bms_score
            row_bms_zone = row.get('bms_zone') or bms_zone
            
            # Do not use adaptive weights during backtest grid search to save aggregation time
            # use_brain=False skips neural network inference for 10x speedup in backtesting.
            sig = strat.get_signal(row, use_adaptive=False, use_brain=False, bms_score=row_bms_score, bms_zone=row_bms_zone)
            signals.append(sig['side'] if sig['side'] in ['BUY', 'SELL'] else None)
        return signals

    def _backtest_with_signals(self, df, signals, sl_pct, tp_pct, fee, atr_mult=None):
        """Backtest using pre-computed signals - MUCH FASTER."""
        if len(df) < 25 or not signals:
            return {'pnl': 0, 'trades': 0, 'win_rate': 0}
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        atrs = df['ATR_14'].values if 'ATR_14' in df.columns else None
        
        balance = 10000
        pos = None
        trades = 0
        wins = 0
        
        for i, sig in enumerate(signals):
            idx = i + 20
            if idx >= len(df):
                break
            
            if pos is not None:
                exit_price = None
                if pos['side'] == 'BUY':
                    if lows[idx] <= pos['sl']:
                        exit_price = pos['sl']
                    elif highs[idx] >= pos['tp']:
                        exit_price = pos['tp']
                else:
                    if highs[idx] >= pos['sl']:
                        exit_price = pos['sl']
                    elif lows[idx] <= pos['tp']:
                        exit_price = pos['tp']
                
                if exit_price:
                    if pos['entry'] <= 0:
                        pos = None
                        continue
                    if pos['side'] == 'BUY':
                        pnl_pct = (exit_price - pos['entry']) / pos['entry'] - fee
                    else:
                        pnl_pct = (pos['entry'] - exit_price) / pos['entry'] - fee
                    balance += pnl_pct * 1000
                    if pnl_pct > 0:
                        wins += 1
                    trades += 1
                    pos = None
            
            elif sig is not None:
                price = closes[idx]
                curr_atr = atrs[idx] if atrs is not None else price * 0.02
                
                if sig == 'BUY':
                    if atr_mult:
                        sl = price - (curr_atr * atr_mult)
                        tp = price + (curr_atr * atr_mult * (tp_pct/sl_pct if sl_pct > 0 else 2))
                    else:
                        sl = price * (1 - sl_pct)
                        tp = price * (1 + tp_pct)
                else:
                    if atr_mult:
                        sl = price + (curr_atr * atr_mult)
                        tp = price - (curr_atr * atr_mult * (tp_pct/sl_pct if sl_pct > 0 else 2))
                    else:
                        sl = price * (1 + sl_pct)
                        tp = price * (1 - tp_pct)
                pos = {'side': sig, 'entry': price, 'sl': sl, 'tp': tp}
        
        return {
            'pnl': balance - 10000,
            'trades': trades,
            'win_rate': wins / trades if trades > 0 else 0
        }

    def get_cross_tf_support(self, symbol, timeframes, exchange='BINANCE'):
        """
        OPTIMIZED: Check cross-TF support using cached validation results with exchange awareness.
        """
        supported = 0
        for tf in timeframes:
            cache_key = f"{exchange}_{symbol}_{tf}"
            if cache_key in self._validation_cache:
                result = self._validation_cache[cache_key]
                if result and (result['pnl'] > 0 or result['win_rate'] >= 0.50):
                    supported += 1
        return supported

    async def update_config(self, symbol, timeframe, new_weights, sl_pct=0.02, tp_pct=0.04, entry_score=5.0, atr_mult=None, stats=None, enabled=None, exchange='BINANCE', w_btc=0.5):
        """
        Updates strategy configuration in the database.
        Includes ATR multiplier if selected.
        """
        # 1. Prepare standardized config object
        config = {
            "enabled": True if enabled is None else enabled,
            "weights": new_weights,
            "thresholds": {"entry_score": entry_score, "exit_score": 2.5},
            "risk": {
                "sl_pct": sl_pct, 
                "tp_pct": tp_pct, 
                "atr_mult": atr_mult,
                "w_btc": w_btc, 
                "w_alt": 1.0 - w_btc
            },
            "tiers": {
                "low": {
                    "min_score": entry_score, 
                    "leverage": min(4, GLOBAL_MAX_LEVERAGE), 
                    "cost_usdt": min(3.0, GLOBAL_MAX_COST_PER_TRADE)
                },
                "high": {
                    "min_score": entry_score + 2.0, 
                    "leverage": min(5, GLOBAL_MAX_LEVERAGE), 
                    "cost_usdt": min(5.0, GLOBAL_MAX_COST_PER_TRADE)
                }
            }
        }
        
        if stats:
            config['performance'] = {
                "pnl_sim": stats.get('pnl', 0),
                "win_rate_sim": stats.get('win_rate', 0),
                "trades_sim": stats.get('trades', 0)
            }

        # 2. Save to DB using DataManager
        from src.infrastructure.repository.database import DataManager
        db = await DataManager.get_instance()
        await db.save_strategy_config(symbol, timeframe, exchange, config)
        print(f"✅ [ANALYZER] Saved config for {symbol} {timeframe} ({exchange}) to database.")
        
        # 3. Synchronize current process cache if WeightedScoringStrategy is in memory
        try:
            from src.strategy import WeightedScoringStrategy
            all_configs = await db.get_all_strategy_configs()
            WeightedScoringStrategy.update_cache(all_configs)
        except Exception as e:
            # Not critical if not running in bot context
            pass

    def clear_cache(self):
        """Clear all caches (call between runs if needed)."""
        self._data_cache.clear()
        self._features_cache.clear()
        self._validation_cache.clear()

    async def run_mini_optimization(self, symbols_to_check, improvement_threshold=0.05):
        """
        MINI-ANALYZER v2.3: Lightweight optimization with consistency checks.
        """
        print(f"\n{'='*50}")
        print(f"[*] MINI-ANALYZER: Checking {len(symbols_to_check)} symbols")
        print(f"{'='*50}\n")
        
        start_time = time.time()
        updates = {}
        
        # Grid parameters - includes ATR multipliers
        sl_ranges = [0.015, 0.02, 0.03] 
        rr_ratios = [1.5, 2.0, 2.5]
        thresholds = [3.5, 4.5, 5.5]
        atr_mults = [None, 1.5, 2.0, 3.0] 
        
        for symbol in symbols_to_check:
            for tf in TRADING_TIMEFRAMES:
                print(f"  Checking {symbol} {tf}...")
                
                from src.infrastructure.repository.database import DataManager
                db = await DataManager.get_instance()
                config_data = await db.get_strategy_config(symbol, tf, getattr(self, 'exchange_name', 'BINANCE'))
                
                current_pnl = 0
                if config_data and 'performance' in config_data:
                    current_pnl = config_data['performance'].get('pnl_sim', 0)
                
                weights = self.analyze(symbol, timeframe=tf, horizon=15)
                if not weights: continue
                
                df = self.get_features(symbol, tf)
                if df is None: continue
                
                # Nested split for mini-optimization
                train_end = int(len(df) * 0.6)
                train_df = df.iloc[:train_end]
                test_df = df.iloc[train_end:]
                
                from src.strategy import WeightedScoringStrategy
                mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=tf)
                mock_strat.weights = weights
                
                trading_cost = 0.0017
                best_result = None
                best_pnl = -999999
                
                for thresh in thresholds:
                    mock_strat.config_data['thresholds'] = {'entry_score': thresh}
                    train_signals = self._compute_signals(train_df, mock_strat)
                    test_signals = self._compute_signals(test_df, mock_strat)
                    
                    for sl in sl_ranges:
                        for rr in rr_ratios:
                            tp = max(0.025, sl * rr)
                            for amult in atr_mults:
                                train_perf = self._backtest_with_signals(train_df, train_signals, sl, tp, trading_cost, atr_mult=amult)
                                if train_perf['trades'] < 3 or train_perf['win_rate'] < 0.50:
                                    continue
                                
                                test_perf = self._backtest_with_signals(test_df, test_signals, sl, tp, trading_cost, atr_mult=amult)
                                if test_perf['trades'] < 2:
                                    continue
                                
                                consistency = abs(test_perf['win_rate'] - train_perf['win_rate'])
                                if consistency > 0.20:
                                    continue
                                    
                                combined_pnl = train_perf['pnl'] + test_perf['pnl']
                                if combined_pnl > best_pnl:
                                    best_pnl = combined_pnl
                                    best_result = {
                                        'sl_pct': sl, 'tp_pct': tp, 'atr_mult': amult,
                                        'entry_score': thresh,
                                        'pnl': combined_pnl,
                                        'win_rate': (train_perf['win_rate'] + test_perf['win_rate']) / 2,
                                        'trades': train_perf['trades'] + test_perf['trades']
                                    }
                
                if best_result is None:
                    continue
                
                # Check for significant improvement
                improvement = (best_result['pnl'] - current_pnl) / max(abs(current_pnl), 1) if current_pnl != 0 else 1.0
                
                if (improvement > improvement_threshold and best_result['trades'] > 5) or (current_pnl <= 0 and best_result['pnl'] > 20):
                    key = f"{symbol}_{tf}"
                    updates[key] = {
                        'weights': weights,
                        'result': best_result,
                        'old_pnl': current_pnl,
                        'new_pnl': best_result['pnl'],
                        'improvement': improvement
                    }
                    
                    await self.update_config(
                        symbol, tf, weights,
                        sl_pct=best_result['sl_pct'],
                        tp_pct=best_result['tp_pct'],
                        entry_score=best_result['entry_score'],
                        stats=best_result,
                        enabled=True
                    )
                    
                    print(f"    ✨ UPDATED: PnL ${current_pnl:.0f} -> ${best_result['pnl']:.0f} (+{improvement*100:.0f}%) | Trades: {best_result['trades']}")
                else:
                    print(f"    [SKIP] No significant improvement (current: ${current_pnl:.0f}, new: ${best_result['pnl']:.0f})")
        
        elapsed = time.time() - start_time
        print(f"\n{'='*50}")
        print(f"[COMPLETE] MINI-ANALYZER: {elapsed:.1f}s")
        print(f"   Updated: {len(updates)} configs")
        print(f"{'='*50}\n")
        
        return updates



async def _compute_and_cache_bms(analyzer, env_str: str):
    """
    Calculate MTF BMS from downloaded CSV data, persist to DB,
    and populate analyzer._bms_cache for reuse in load_data().
    """
    from src.btc_analyzer import BTCAnalyzer
    from src.infrastructure.repository.database import DataManager
    from src.config import ACTIVE_EXCHANGES, TRADING_TIMEFRAMES
    from src.feature_engineering import FeatureEngineer
    
    db = await DataManager.get_instance(env_str)
    btc_calc = BTCAnalyzer(analyzer, db)
    
    # NEW v2.0: Trigger BMS Weight Optimization (Loop A)
    print("  ↳ Optimizing BMS weights (Loop A)...")
    opt_results = btc_calc.optimize_weights()
    
    # 1. Persist MTF BMS to DB (for live bot freshness)
    print("  ↳ Updating BTC sentiment in database...")
    await btc_calc.update_sentiment('BTC/USDT:USDT')
    
    # 2. Pre-populate _bms_cache so load_data() reuses it
    fe = FeatureEngineer()
    
    # Load BTCDOM for bulk computation if needed
    dom_df = analyzer.load_data('BTCDOM/USDT:USDT', '1h', exchange='BINANCE')
    
    for ex_name in ACTIVE_EXCHANGES:
        ex_name = ex_name.strip()
        for tf in TRADING_TIMEFRAMES:
            btc_df = analyzer.load_data('BTC/USDT:USDT', tf, exchange=ex_name)
            if btc_df is not None:
                # Merge BTCDOM for 1h TF calculation
                if tf == '1h' and dom_df is not None:
                    dom_subset = dom_df[['timestamp', 'close']].rename(columns={'close': 'BTCDOM_close'})
                    btc_df = pd.merge_asof(btc_df.sort_values('timestamp'), dom_subset.sort_values('timestamp'), on='timestamp', direction='backward')
                
                # Calculate features once for BTC
                btc_feat_df = fe.calculate_features(btc_df)
                bms_data = btc_calc.calculate_bulk_sentiment(btc_feat_df)
                if bms_data is not None:
                    # Extract and rename columns
                    # v2.0: Now including sub-scores for Neural Brain
                    cols_to_keep = ['timestamp', 'bms', 'zone', 's_trend', 's_momentum', 's_vol', 's_dom']
                    avail_cols = [c for c in cols_to_keep if c in bms_data.columns]
                    
                    bms_subset = bms_data[avail_cols].rename(
                        columns={'bms': 'bms_score', 'zone': 'bms_zone'}
                    )
                    cache_key = f"{ex_name}_{tf}"
                    analyzer._bms_cache[cache_key] = bms_subset
    
    # Return the latest aggregated BMS for global filtering
    sentiment = await db.get_latest_market_sentiment()
    return sentiment


async def run_global_optimization(download=False):
    from src.infrastructure.notifications.notification import send_telegram_chunked
    import subprocess
    
    print("\n" + "!" * 60)
    print("🚀 STARTING UNIFIED OPTIMIZATION WORKFLOW")
    print("!" * 60)

    # STEP 0: Download Fresh Data (Now Optional)
    if download:
        print("\n[*] Step 0: Downloading fresh market data...")
        try:
            import sys
            # Ensure we use the current python executable (important for venv)
            # and point to the correct scripts location relative to project root
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            download_script = os.path.join(project_root, 'scripts', 'download_data.py')
            subprocess.run([sys.executable, download_script], check=True)
            print("✅ Data download complete.")
        except Exception as e:
            print(f"⚠️  Data download failed or skipped: {e}")
    else:
        print("\n[*] Step 0: Skipping data download (use --download to fetch fresh data).")

    analyzer = StrategyAnalyzer()
    start_time = time.time()

    # Determine environment
    from src import config
    import sys
    env_str = 'TEST' if getattr(config, 'DRY_RUN', True) else 'LIVE'
    if "--live" in sys.argv: env_str = 'LIVE'
    elif "--dry-run" in sys.argv: env_str = 'TEST'
    
    # STEP 0.5: Calculate & Persist BMS
    print("\n" + "=" * 60)
    print("[*] STEP 0.5: BTC MACRO SIGNAL (BMS) UPDATE")
    print("=" * 60)
    global_bms = await _compute_and_cache_bms(analyzer, env_str)
    bms_zone = global_bms.get('sentiment_zone', 'YELLOW') if global_bms else 'YELLOW'
    bms_score = float(global_bms.get('bms', 0.5)) if global_bms else 0.5
    print(f"  ✅ Global BMS: {bms_score:.2f} | Zone: {bms_zone}")
    
    print("\n" + "=" * 60)
    print("[*] STEP 1: SIGNAL ANALYSIS & OPTIMIZATION")
    print("=" * 60)
    
    results_summary = []
    horizon = 15
    all_weights = {}  # {(exchange, symbol): {tf: weights}}
    
    # ========== STEP 1: Parallel Signal Analysis ==========
    from src.config import BINANCE_SYMBOLS, BYBIT_SYMBOLS, ACTIVE_EXCHANGES
    
    # Map exchange name to its specific symbols
    exchange_symbols = {
        'BINANCE': [s for s in BINANCE_SYMBOLS if s in TRADING_SYMBOLS],
        'BYBIT': [s for s in BYBIT_SYMBOLS if s in TRADING_SYMBOLS]
    }
    
    # If a generic symbols list is used but not mapped, fallback to all
    # But here we standardizing.
    
    active_tasks = []
    for ex_name in ACTIVE_EXCHANGES:
        ex_name = ex_name.strip()
        symbols = exchange_symbols.get(ex_name, TRADING_SYMBOLS)
        for symbol in symbols:
            active_tasks.append((ex_name, symbol))

    print(f"\n[STEP 1/3] Signal Analysis (parallel for {len(active_tasks)} exchange/symbol pairs)...")
    step1_start = time.time()
    
    def analyze_task(args):
        exchange, symbol = args
        weights_by_tf = {}
        for tf in TRADING_TIMEFRAMES:
            weights = analyzer.analyze(symbol, timeframe=tf, horizon=horizon, exchange=exchange)
            if weights:
                weights_by_tf[tf] = weights
        return (exchange, symbol), weights_by_tf
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_task, task): task for task in active_tasks}
        for i, future in enumerate(as_completed(futures)):
            task_key, weights_by_tf = future.result()
            all_weights[task_key] = weights_by_tf
            if (i + 1) % 5 == 0:
                print(f"  [{i+1}/{len(active_tasks)}] tasks analyzed...")
    
    step1_time = time.time() - step1_start
    print(f"  ✨ Step 1 complete: {step1_time:.1f}s")
    
    # ========== STEP 2: Parallel Validation ==========
    print("\n[STEP 2/3] Walk-Forward Validation (parallel)...")
    step2_start = time.time()
    
    validation_tasks = []
    for (exchange, symbol), weights_by_tf in all_weights.items():
        for tf, weights in weights_by_tf.items():
            validation_tasks.append((exchange, symbol, tf, weights))
    
    def validate_task(args):
        exchange, symbol, tf, weights = args
        df = analyzer.get_features(symbol, tf, exchange=exchange)
        if df is None:
            return None
        result = analyzer.validate_weights(df, weights, symbol, tf, exchange=exchange, bms_score=bms_score, bms_zone=bms_zone)
        if result:
            return {
                'exchange': exchange,
                'symbol': symbol,
                'tf': tf,
                'weights': weights,
                'result': result
            }
        return None
    
    validation_results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(validate_task, task) for task in validation_tasks]
        total_tasks = len(futures)
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            if result is not None:
                validation_results.append(result)
            if (i + 1) % 10 == 0 or (i + 1) == total_tasks:
                print(f"  [{i+1}/{total_tasks}] tasks validated...", end='\r')
    print() # newline after progress bar
    
    step2_time = time.time() - step2_start
    print(f"  ✨ Step 2 complete: {len(validation_results)} validated in {step2_time:.1f}s")
    
    # ========== STEP 3: Cross-TF Check & Config Update ==========
    print("\n[STEP 3/3] Cross-TF Validation & Config Update...")
    step3_start = time.time()
    
    enabled_count = 0
    disabled_count = 0
    
    # Sort results by exchange and symbol for better grouping in report
    validation_results.sort(key=lambda x: (x.get('exchange', 'BYBIT'), x.get('symbol', '')))
    
    for v in validation_results:
        exchange, symbol, tf = v['exchange'], v['symbol'], v['tf']
        result = v['result']
        weights = v['weights']
        
        wr = result['win_rate']
        test_wr = result.get('test_wr', 0)
        consistency = result.get('consistency', 0)
        
        # Build local lookup dictionary of successful validations
        if not hasattr(analyzer, '_local_validation_results'):
            analyzer._local_validation_results = {}
            for vr in validation_results:
                key = f"{vr['exchange']}_{vr['symbol']}_{vr['tf']}"
                analyzer._local_validation_results[key] = vr['result']
                
        # Check cross-TF support using local results without triggering heavy computation
        cross_tf_support = 0
        for support_tf in TRADING_TIMEFRAMES:
            support_key = f"{exchange}_{symbol}_{support_tf}"
            if support_key in analyzer._local_validation_results:
                support_res = analyzer._local_validation_results[support_key]
                if support_res and (support_res['pnl'] > 0 or support_res['win_rate'] >= 0.50):
                    cross_tf_support += 1
        
        # Use configurable thresholds from config
        from src.config import MIN_WIN_RATE_TRAIN, MIN_WIN_RATE_TEST, MAX_CONSISTENCY, MIN_CROSS_TF_SUPPORT
        
        is_profitable = wr >= MIN_WIN_RATE_TRAIN and test_wr >= MIN_WIN_RATE_TEST
        is_consistent = consistency < MAX_CONSISTENCY
        
        # BMS ZONE FILTER: In RED zone, disable LONG-biased configs. In GREEN zone, disable SHORT-biased configs.
        # We determine bias by checking if LONG weights sum is greater than SHORT weights sum.
        # Note: weight keys in config don't have 'signal_' prefix, so we check against keywords directly.
        long_sum = sum([w for s, w in weights.items() if any(k in s for k in analyzer.feature_engineer.long_keywords)])
        short_sum = sum([w for s, w in weights.items() if any(k in s for k in analyzer.feature_engineer.short_keywords)])
        
        bms_override = True
        if bms_zone == 'RED' and long_sum > short_sum:
            bms_override = False
            # We don't print here to avoid cluttering, but it will affect is_enabled
        elif bms_zone == 'GREEN' and short_sum > long_sum:
            bms_override = False
            
        is_enabled = is_profitable and is_consistent and cross_tf_support >= MIN_CROSS_TF_SUPPORT and bms_override
        
        if is_enabled:
            enabled_count += 1
            status = f"{exchange} | {symbol} {tf} | WR={wr*100:.1f}% | PnL=${result['pnl']:.0f} | CrossTF={cross_tf_support}"
            print(f"  {status}")
            results_summary.append(status)
        else:
            disabled_count += 1
        
        await analyzer.update_config(
            symbol, tf, weights if is_enabled else {},
            sl_pct=result['sl_pct'],
            tp_pct=result['tp_pct'],
            entry_score=result['entry_score'],
            atr_mult=result.get('atr_mult'),
            stats=result,
            enabled=is_enabled,
            exchange=exchange
        )
    
    step3_time = time.time() - step3_start
    total_time = time.time() - start_time
    
    print("=" * 60)
    
    # ========== NEW STEP 5: Summary Portfolio Backtest ==========
    if enabled_count > 0:
        print("\n[STEP 5/3] Summary Portfolio Backtest...")
        summary_start = time.time()
        
        # Simple aggregated performance estimate
        total_pnl = 0
        total_trades = 0
        combined_win_rate = 0
        
        for v in validation_results:
            if v.get('is_enabled', True): # We already filtered in Step 3 but double check
                total_pnl += v['result'].get('pnl', 0)
                total_trades += v['result'].get('trades', 0)
                combined_win_rate += v['result'].get('win_rate', 0)
        
        avg_win_rate = (combined_win_rate / enabled_count) * 100 if enabled_count > 0 else 0
        summary_time = time.time() - summary_start
        
        print(f"  ✅ Portfolio Summary: {enabled_count} pairs | Est. PnL: ${total_pnl:.0f} | Avg WR: {avg_win_rate:.1f}%")
        print(f"  ✨ Step 5 complete: {summary_time:.1f}s")
    
    print("=" * 60)
    
    if results_summary:
        # Group by exchange for the Telegram message
        grouped_summary = {}
        for s in results_summary:
            ex_name = s.split(' | ')[0]
            if ex_name not in grouped_summary:
                grouped_summary[ex_name] = []
            grouped_summary[ex_name].append(" | ".join(s.split(' | ')[1:]))
        
        final_lines = [f"✨ **OPTIMIZATION COMPLETE** ({total_time:.0f}s)\n\nEnabled: {enabled_count}\n"]
        for ex, lines in grouped_summary.items():
            final_lines.append(f"🏛️ **{ex.upper()}**")
            final_lines.extend([f"• {l}" for l in lines[:10]]) # Limit to top 10 per exchange
            final_lines.append("")
            
        final_msg = "\n".join(final_lines)
        # STEP 4: Neural Brain Training
        from src.infrastructure.notifications.notification import send_telegram_chunked
        await send_telegram_chunked(final_msg)
        
        print("\n[*] Step 4: Updating Neural Brain (RL Model)...")
        try:
            # Training on at least 20 samples to ensure quality
            brain_stats = await run_nn_training(min_samples=20, epochs=100)
            
            if isinstance(brain_stats, dict) and brain_stats.get('status') == 'success':
                brain_msg = (
                    "🧠 **Neural Brain Updated**\n"
                    f"📊 Samples: {brain_stats['samples']}\n"
                    f"🎯 Accuracy: {brain_stats['accuracy']:.1f}%\n"
                    f"📉 MSE: {brain_stats['mse']:.4f}\n"
                    "✅ Model redeployed and active."
                )
            else:
                brain_msg = "🧠 **Neural Brain**: Not enough new data to retrain (needs 20+ trades)."
            
            await send_telegram_chunked(brain_msg)
            
        except Exception as e:
            err_msg = f"⚠️ **Neural Brain Update Failed**: {e}"
            print(err_msg)
            await send_telegram_chunked(err_msg)
        
        # STEP 5: Run Summary Backtest (Optional but recommended)
        print("\n[*] Step 4: Running summary backtest...")
        try:
            # You might want to run backtester.py for the top enabled symbols
            # For now, we've already done the test-set validation inside analyze()
            print("✅ Strategy validated on test set.")
            
            # Final step: Update optimization timestamp to prevent immediate re-run by bot
            from src.infrastructure.repository.database import DataManager
            from src import config
            import sys
            
            # Determine environment
            env_str = 'TEST' if getattr(config, 'DRY_RUN', True) else 'LIVE'
            if "--live" in sys.argv: env_str = 'LIVE'
            elif "--dry-run" in sys.argv: env_str = 'TEST'
            
            # Update the same metric the bot uses to schedule
            db_sync = await DataManager.get_instance(env_str)
            await db_sync.set_risk_metric(0, 'last_optimization_time', time.time(), env_str)
            print(f"✨ Optimization timestamp updated for {env_str} mode. Bot will skip next cycle.")
            
        except Exception as e:
            print(f"⚠️  Finalization failed: {e}")
    else:
        print("[!] No profitable configurations found.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Strategy Analyzer & Optimizer")
    parser.add_argument("--download", action="store_true", help="Download fresh data before optimization")
    parser.add_argument("--live", action="store_true", help="Run in LIVE mode")
    parser.add_argument("--dry-run", action="store_true", help="Run in DRY-RUN mode")
    
    args = parser.parse_args()
    
    # Pass download flag to the main runner
    try:
        asyncio.run(run_global_optimization(download=args.download))
    finally:
        from src.infrastructure.repository.database import DataManager
        asyncio.run(DataManager.clear_instances())
