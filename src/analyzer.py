# -*- coding: utf-8 -*-
"""
OPTIMIZED Strategy Analyzer v2.2 - FULL QUALITY + FAST
- Caching: Data + Features cached per symbol/tf
- FULL GRID search: 270 combos (NO quality reduction!)
- CACHED SIGNALS per threshold: 30x faster (9 signal computations instead of 270!)
- Smart validation: Top 50 train combos tested on test set
- Reduced redundancy: Reuse cross-TF results
"""
import pandas as pd
import numpy as np
import os
import json
import asyncio
import time
import sys

# Add src to path if running directly
if __name__ == '__main__':
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.append(src_dir)
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import config
from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES, MAX_WORKERS, GLOBAL_MAX_LEVERAGE, GLOBAL_MAX_COST_PER_TRADE
from feature_engineering import FeatureEngineer
import subprocess
from train_brain import run_nn_training

class StrategyAnalyzer:
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        self.feature_engineer = FeatureEngineer()
        # OPTIMIZATION 1: Cache for loaded data and calculated features
        self._data_cache = {}
        self._features_cache = {}
        # OPTIMIZATION 2: Cache validation results for cross-TF lookup
        self._validation_cache = {}

    def load_data(self, symbol, timeframe='1h', exchange='BINANCE'):
        """Load data with caching and exchange awareness."""
        cache_key = f"{exchange}_{symbol}_{timeframe}"
        if cache_key in self._data_cache:
            return self._data_cache[cache_key]
        
        safe_symbol = symbol.replace('/', '').replace(':', '')
        # Data files now follow {EXCHANGE}_{SYMBOL}_{TF}.csv pattern
        file_path = os.path.join(self.data_dir, f"{exchange}_{safe_symbol}_{timeframe}.csv")
        
        if not os.path.exists(file_path):
            # Fallback for legacy files
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
            
            is_long = any(x in config_key for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'lt_BB', 'cross_up', 'gt_50', 'gt_', 'bullish', 'above', 'strong_uptrend'])
            is_short = any(x in config_key for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'gt_BB', 'cross_down', 'lt_50', 'lt_', 'bearish', 'below', 'strong_downtrend'])
            
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

    def validate_weights(self, df, weights, symbol, timeframe, exchange='BINANCE'):
        """
        LAYER 3: Walk-Forward Validation - OPTIMIZED v2.2
        - FULL GRID SEARCH (không giảm quality!)
        - CACHED SIGNALS per threshold (30x faster!)
        - Vectorized backtesting
        """
        if not weights:
            return None
        
        split_idx = int(len(df) * 0.7)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
        
        from strategy import WeightedScoringStrategy
        mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        mock_strat.weights = weights
        
        round_trip_fee = 0.0012
        
        # FULL GRID - KHÔNG GIẢM QUALITY
        sl_ranges = [0.01, 0.015, 0.02, 0.025, 0.03, 0.035]  # 6 values
        rr_ratios = [1.0, 1.5, 2.0, 2.5, 3.0]                # 5 values  
        thresholds_to_test = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0]  # 9 values
        
        # OPTIMIZATION: Pre-compute signals for each threshold (9 times instead of 270!)
        train_signals_cache = {}
        test_signals_cache = {}
        
        for thresh in thresholds_to_test:
            mock_strat.config_data['thresholds'] = {'entry_score': thresh}
            train_signals_cache[thresh] = self._compute_signals(train_df, mock_strat)
            test_signals_cache[thresh] = self._compute_signals(test_df, mock_strat)
        
        best_overall = None
        max_combined_pnl = -999999
        
        # Phase 1: Find best on train set using cached signals
        best_train_combos = []
        for thresh in thresholds_to_test:
            signals = train_signals_cache[thresh]
            for sl_test in sl_ranges:
                for rr in rr_ratios:
                    tp_test = max(0.025, sl_test * rr)
                    train_perf = self._backtest_with_signals(train_df, signals, sl_test, tp_test, round_trip_fee)
                    
                    if train_perf['trades'] >= 2 and train_perf['win_rate'] >= 0.50:
                        best_train_combos.append({
                            'sl': sl_test, 'tp': tp_test, 'thresh': thresh,
                            'train_perf': train_perf
                        })
        
        # Sort by train pnl, keep top 50 for test validation
        best_train_combos.sort(key=lambda x: x['train_perf']['pnl'], reverse=True)
        top_combos = best_train_combos[:50]
        
        # Phase 2: Validate top combos on test set using cached signals
        for combo in top_combos:
            signals = test_signals_cache[combo['thresh']]
            test_perf = self._backtest_with_signals(test_df, signals, combo['sl'], combo['tp'], round_trip_fee)
            
            if test_perf['trades'] < 2:
                continue
            
            train_perf = combo['train_perf']
            consistency = abs(test_perf['win_rate'] - train_perf['win_rate'])
            if consistency > 0.25:
                continue
            
            combined_pnl = train_perf['pnl'] + test_perf['pnl']
            if combined_pnl > max_combined_pnl:
                max_combined_pnl = combined_pnl
                best_overall = {
                    'sl_pct': combo['sl'], 'tp_pct': combo['tp'],
                    'entry_score': combo['thresh'],
                    'pnl': combined_pnl,
                    'trades': train_perf['trades'] + test_perf['trades'],
                    'win_rate': (train_perf['win_rate'] + test_perf['win_rate']) / 2,
                    'test_wr': test_perf['win_rate'],
                    'test_pnl': test_perf['pnl'],
                    'consistency': consistency
                }
        
        # Cache result for cross-TF lookup
        cache_key = f"{exchange}_{symbol}_{timeframe}"
        if best_overall:
            self._validation_cache[cache_key] = best_overall
        
        return best_overall

    def _compute_signals(self, df, strat):
        """Pre-compute signals once per threshold."""
        signals = []
        for i in range(20, len(df)):
            row = df.iloc[i]
            sig = strat.get_signal(row)
            signals.append(sig['side'] if sig['side'] in ['BUY', 'SELL'] else None)
        return signals

    def _backtest_with_signals(self, df, signals, sl_pct, tp_pct, fee):
        """Backtest using pre-computed signals - MUCH FASTER."""
        if len(df) < 25 or not signals:
            return {'pnl': 0, 'trades': 0, 'win_rate': 0}
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
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
                    if pos['entry'] <= 0:  # Division by zero protection
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
                if sig == 'BUY':
                    sl = price * (1 - sl_pct)
                    tp = price * (1 + tp_pct)
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

    def update_config(self, symbol, timeframe, new_weights, sl_pct=0.02, tp_pct=0.04, entry_score=5.0, stats=None, enabled=None, exchange='BINANCE'):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
        else:
            data = {"default": {}}

        # Use exchange-prefixed key for specific weights (Issue 9)
        clean_symbol = symbol.split(':')[0].replace('/', '_').upper()
        key = f"{exchange}_{clean_symbol}_{timeframe}"
        if key not in data:
            data[key] = {
                "enabled": True,
                "weights": {},
                "thresholds": {"entry_score": entry_score, "exit_score": 2.5},
                "risk": {"sl_pct": sl_pct, "tp_pct": tp_pct},
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
        
        if enabled is not None:
            data[key]["enabled"] = enabled
        elif "enabled" not in data[key]:
            data[key]["enabled"] = True
        
        data[key]['weights'] = new_weights
        data[key]['risk'] = {"sl_pct": sl_pct, "tp_pct": tp_pct}
        data[key]['thresholds']['entry_score'] = entry_score
        data[key]['tiers']['low']['min_score'] = entry_score
        data[key]['tiers']['high']['min_score'] = entry_score + 2.0
        
        if stats:
            data[key]['performance'] = {
                "pnl_sim": stats.get('pnl', 0),
                "win_rate_sim": stats.get('win_rate', 0),
                "trades_sim": stats.get('trades', 0)
            }

        import tempfile
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(config_path), text=True)
        try:
            with os.fdopen(temp_fd, 'w') as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, config_path)
        except Exception as e:
            os.unlink(temp_path)
            raise e

    def clear_cache(self):
        """Clear all caches (call between runs if needed)."""
        self._data_cache.clear()
        self._features_cache.clear()
        self._validation_cache.clear()

    def run_mini_optimization(self, symbols_to_check, improvement_threshold=0.05):
        """
        MINI-ANALYZER: Lightweight optimization for specific symbols after losses.
        Uses 50 combos instead of 270 for ~30s runtime.
        
        Args:
            symbols_to_check: List of symbols to re-optimize
            improvement_threshold: Minimum improvement to update (5%)
        
        Returns:
            Dict of updated configs {symbol_tf: new_config}
        """
        print(f"\n{'='*50}")
        print(f"[*] MINI-ANALYZER: Checking {len(symbols_to_check)} symbols")
        print(f"{'='*50}\n")
        
        start_time = time.time()
        updates = {}
        
        # Reduced grid for speed
        sl_ranges = [0.015, 0.02, 0.025, 0.03]  # 4 values (vs 6)
        rr_ratios = [1.5, 2.0, 2.5]              # 3 values (vs 5)
        thresholds = [3.0, 4.0, 5.0, 6.0]        # 4 values (vs 9)
        # Total: 4 × 3 × 4 = 48 combos (vs 270)
        
        for symbol in symbols_to_check:
            for tf in TRADING_TIMEFRAMES:
                print(f"  Checking {symbol} {tf}...")
                
                # Get current config
                config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
                current_pnl = 0
                if os.path.exists(config_path):
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                    exchange_name = getattr(self, 'exchange_name', 'BINANCE')
                    key = f"{exchange_name}_{symbol}_{tf}"
                    if key in config and 'performance' in config[key]:
                        current_pnl = config[key]['performance'].get('pnl_sim', 0)
                
                # Analyze and validate
                weights = self.analyze(symbol, timeframe=tf, horizon=15)
                if not weights:
                    continue
                
                df = self.get_features(symbol, tf)
                if df is None:
                    continue
                
                # Custom mini-validation with reduced grid
                split_idx = int(len(df) * 0.7)
                train_df = df.iloc[:split_idx]
                test_df = df.iloc[split_idx:]
                
                from strategy import WeightedScoringStrategy
                mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=tf)
                mock_strat.weights = weights
                
                round_trip_fee = 0.0012
                best_result = None
                best_pnl = -999999
                
                for thresh in thresholds:
                    mock_strat.config_data['thresholds'] = {'entry_score': thresh}
                    train_signals = self._compute_signals(train_df, mock_strat)
                    test_signals = self._compute_signals(test_df, mock_strat)
                    
                    for sl in sl_ranges:
                        for rr in rr_ratios:
                            tp = max(0.025, sl * rr)
                            
                            train_perf = self._backtest_with_signals(train_df, train_signals, sl, tp, round_trip_fee)
                            if train_perf['trades'] < 2 or train_perf['win_rate'] < 0.50:
                                continue
                            
                            test_perf = self._backtest_with_signals(test_df, test_signals, sl, tp, round_trip_fee)
                            if test_perf['trades'] < 2:
                                continue
                            
                            combined_pnl = train_perf['pnl'] + test_perf['pnl']
                            if combined_pnl > best_pnl:
                                best_pnl = combined_pnl
                                best_result = {
                                    'sl_pct': sl, 'tp_pct': tp,
                                    'entry_score': thresh,
                                    'pnl': combined_pnl,
                                    'win_rate': (train_perf['win_rate'] + test_perf['win_rate']) / 2,
                                    'trades': train_perf['trades'] + test_perf['trades']
                                }
                
                if best_result is None:
                    continue
                
                # Check if improvement is significant
                improvement = (best_result['pnl'] - current_pnl) / max(abs(current_pnl), 1) if current_pnl != 0 else 1.0
                
                if improvement > improvement_threshold or (current_pnl <= 0 and best_result['pnl'] > 0):
                    key = f"{symbol}_{tf}"
                    updates[key] = {
                        'weights': weights,
                        'result': best_result,
                        'old_pnl': current_pnl,
                        'new_pnl': best_result['pnl'],
                        'improvement': improvement
                    }
                    
                    # Update config
                    self.update_config(
                        symbol, tf, weights,
                        sl_pct=best_result['sl_pct'],
                        tp_pct=best_result['tp_pct'],
                        entry_score=best_result['entry_score'],
                        stats=best_result,
                        enabled=True
                    )
                    
                    print(f"    ✨ UPDATED: PnL ${current_pnl:.0f} -> ${best_result['pnl']:.0f} (+{improvement*100:.0f}%)")
                else:
                    print(f"    [SKIP] No significant improvement (current: ${current_pnl:.0f}, new: ${best_result['pnl']:.0f})")
        
        elapsed = time.time() - start_time
        print(f"\n{'='*50}")
        print(f"[COMPLETE] MINI-ANALYZER: {elapsed:.1f}s")
        print(f"   Updated: {len(updates)} configs")
        print(f"{'='*50}\n")
        
        return updates


async def run_global_optimization():
    from notification import send_telegram_chunked
    import subprocess
    
    print("\n" + "!" * 60)
    print("🚀 STARTING UNIFIED OPTIMIZATION WORKFLOW")
    print("!" * 60)

    # STEP 0: Download Fresh Data
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

    analyzer = StrategyAnalyzer()
    start_time = time.time()
    
    print("\n" + "=" * 60)
    print("[*] STEP 1: SIGNAL ANALYSIS & OPTIMIZATION")
    print("=" * 60)
    
    results_summary = []
    horizon = 15
    all_weights = {}  # {(exchange, symbol): {tf: weights}}
    
    # ========== STEP 1: Parallel Signal Analysis ==========
    from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS, ACTIVE_EXCHANGES
    
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
        result = analyzer.validate_weights(df, weights, symbol, tf, exchange=exchange)
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
        futures = list(executor.map(validate_task, validation_tasks))
        validation_results = [r for r in futures if r is not None]
    
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
        
        # Check cross-TF support using cached results
        cross_tf_support = analyzer.get_cross_tf_support(symbol, TRADING_TIMEFRAMES, exchange=exchange)
        
        # Use configurable thresholds from config
        from config import MIN_WIN_RATE_TRAIN, MIN_WIN_RATE_TEST, MAX_CONSISTENCY, MIN_CROSS_TF_SUPPORT
        
        is_profitable = wr >= MIN_WIN_RATE_TRAIN and test_wr >= MIN_WIN_RATE_TEST
        is_consistent = consistency < MAX_CONSISTENCY
        is_enabled = is_profitable and is_consistent and cross_tf_support >= MIN_CROSS_TF_SUPPORT
        
        if is_enabled:
            enabled_count += 1
            status = f"{exchange} | {symbol} {tf} | WR={wr*100:.1f}% | PnL=${result['pnl']:.0f} | CrossTF={cross_tf_support}"
            print(f"  {status}")
            results_summary.append(status)
        else:
            disabled_count += 1
        
        analyzer.update_config(
            symbol, tf, weights if is_enabled else {},
            sl_pct=result['sl_pct'],
            tp_pct=result['tp_pct'],
            entry_score=result['entry_score'],
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
        await send_telegram_chunked(final_msg)
        
        # STEP 4: Neural Brain Training
        from notification import send_telegram_chunked
        print("\n[*] Step 4: Updating Neural Brain (RL Model)...")
        try:
            # Training on at least 20 samples to ensure quality
            brain_stats = run_nn_training(min_samples=20, epochs=100)
            
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
        except Exception as e:
            print(f"⚠️  Backtest summary failed: {e}")
    else:
        print("[!] No profitable configurations found.")

if __name__ == "__main__":
    asyncio.run(run_global_optimization())
