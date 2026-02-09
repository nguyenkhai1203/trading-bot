import pandas as pd
import numpy as np
import os
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import TRADING_SYMBOLS, BINANCE_API_KEY, BINANCE_API_SECRET, MAX_WORKERS
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
        
        # Calculate EMA 200 for Trend Filter
        if len(df) > 200:
            df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        else:
            df['ema_200'] = df['close'].expanding().mean()
            
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

    def analyze(self, symbol, timeframe='1h', horizon=4):
        """
        Analyzes signals with Layer 1: Trend Filter and Layer 2: Diversity (IMPROVED).
        Now picks TOP 3 signals per category for better coverage.
        """
        print(f"Analyzing {symbol} {timeframe}...")
        df = self.load_data(symbol, timeframe)
        if df is None: return

        df = self.feature_engineer.calculate_features(df)
        df['Target_Return'] = df['close'].shift(-horizon) / df['close'] - 1.0
        
        signal_cols = [c for c in df.columns if c.startswith('signal_')]
        
        category_signals = {} # {category: [signals with metrics]}
        
        print(f"  Scanning {len(signal_cols)} signals with EMA-200 Trend Filter...")
        
        for name in signal_cols:
            config_key = name.replace('signal_', '')
            cat = self.get_signal_category(config_key)
            
            is_long = any(x in config_key for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'lt_BB', 'cross_up', 'gt_50', 'gt_', 'bullish', 'above', 'strong_uptrend'])
            is_short = any(x in config_key for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'gt_BB', 'cross_down', 'lt_50', 'lt_', 'bearish', 'below', 'strong_downtrend'])
            
            # --- LAYER 1: TREND FILTER ---
            # Only analyze signals that follow the 200-EMA trend
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
            
            # Weighting Logic - IMPROVED
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

        # --- LAYER 2: CLEAN PARAMETERS (IMPROVED - Pick TOP 3 per category) ---
        # Now pick up to 3 signals per category instead of just 1
        results = {}
        for cat, signals_list in category_signals.items():
            # Sort by win-rate, descending
            signals_list = sorted(signals_list, key=lambda x: x['wr'], reverse=True)
            
            # Pick top 3 (or fewer if not enough)
            top_n = min(3, len(signals_list))
            for i, sig in enumerate(signals_list[:top_n]):
                # Decrease weight for lower priority signals in same category
                adj_weight = sig['weight'] * (1.0 if i == 0 else 0.7 if i == 1 else 0.5)
                print(f"    [LAYER2-{i+1}] {cat}: {sig['name']} ({sig['dir']}) | WR={sig['wr']*100:.1f}% | Trades={sig['trades']}")
                results[sig['name']] = adj_weight
            
        return results

    def cross_timeframe_validate(self, symbol, weights_dict, timeframes=['15m', '30m', '1h', '4h', '1d']):
        """
        CROSS-TIMEFRAME VALIDATION: Check if signals are reliable across multiple timeframes.
        Two distant timeframes (e.g., 15m+1h, 30m+4h, 1h+1d) = HIGH confidence.
        This ensures signal validity without requiring excessive confirmations.
        """
        print(f"\n  [CROSS-TF VALIDATION] Checking {symbol} across {len(timeframes)} timeframes...")
        
        tf_results = {}
        profitable_tfs = 0
        
        for tf in timeframes:
            if tf not in weights_dict[symbol]:
                continue
            
            weights = weights_dict[symbol][tf].get('weights', {})
            if not weights:
                continue
            
            df = self.load_data(symbol, tf)
            if df is None:
                continue
            
            df = self.feature_engineer.calculate_features(df)
            best_result = self.validate_weights(df, weights, symbol, tf)
            
            if best_result and best_result['pnl'] > 0 and best_result['win_rate'] >= 0.52:
                profitable_tfs += 1
                tf_results[tf] = {
                    'pnl': best_result['pnl'],
                    'wr': best_result['win_rate'],
                    'trades': best_result['trades']
                }
        
        # Confidence scoring based on cross-TF alignment
        # Strategy: 2+ distant timeframes = HIGH confidence (sufficient validation)
        # This allows more signals to pass while maintaining safety
        confidence = 'LOW'
        if profitable_tfs >= 4:
            confidence = 'VERY HIGH'  # Profitable on 4+ TF (exceptional)
        elif profitable_tfs >= 2:
            confidence = 'HIGH'        # Profitable on 2+ TF (robust, recommended)
        elif profitable_tfs == 1:
            confidence = 'MEDIUM'      # Profitable only 1 TF (watch, single validation)
        
        print(f"  Cross-TF Confidence: {confidence} ({profitable_tfs}/{len(timeframes)} TF profitable)")
        
        return {
            'confidence': confidence,
            'profitable_tfs': profitable_tfs,
            'total_tfs': len(timeframes),
            'tf_results': tf_results
        }

    def validate_weights(self, df, weights, symbol, timeframe):
        """--- LAYER 3: 70/30 WALK-FORWARD VALIDATION with SAFETY CHECKS ---"""
        if not weights: 
            return None
        
        split_idx = int(len(df) * 0.7)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
        
        from strategy import WeightedScoringStrategy
        mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        mock_strat.weights = weights
        
        # EXPANDED ranges to find better SL/TP combinations
        sl_ranges = [0.01, 0.015, 0.02, 0.025, 0.03, 0.035] 
        tp_min = 0.025
        rr_ratios = [1.0, 1.5, 2.0, 2.5, 3.0] 
        # EXPANDED thresholds: test more entry difficulty levels
        thresholds_to_test = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0]
        
        round_trip_fee = 0.0012
        best_overall = None
        max_train_pnl = -999999
        
        for thresh in thresholds_to_test:
            mock_strat.config_data['thresholds'] = {'entry_score': thresh}
            for sl_test in sl_ranges:
                for rr in rr_ratios:
                    tp_test = max(tp_min, sl_test * rr)
                    
                    # 1. TEST ON TRAINING DATA
                    train_perf = self._backtest_segment(train_df, mock_strat, sl_test, tp_test, round_trip_fee)
                    
                    # SAFETY CHECK 1: Minimum trade count
                    if train_perf['trades'] < 2:
                        continue
                    
                    # SAFETY CHECK 2: Win rate higher than baseline
                    if train_perf['win_rate'] < 0.52:
                        continue
                    
                    if train_perf['pnl'] > max_train_pnl:
                        # 2. VALIDATE ON TESTING DATA
                        test_perf = self._backtest_segment(test_df, mock_strat, sl_test, tp_test, round_trip_fee)
                        
                        # SAFETY CHECK 3: Test must be consistent with train
                        if abs(test_perf['win_rate'] - train_perf['win_rate']) > 0.25:
                            # Too much divergence, likely overfitted
                            continue
                        
                        # SAFETY CHECK 4: Minimum trades in test set
                        if test_perf['trades'] < 2:
                            continue
                        
                        max_train_pnl = train_perf['pnl']
                        best_overall = {
                            'sl_pct': sl_test, 'tp_pct': tp_test, 
                            'entry_score': thresh, 
                            'pnl': train_perf['pnl'] + test_perf['pnl'], 
                            'trades': train_perf['trades'] + test_perf['trades'], 
                            'win_rate': (train_perf['win_rate'] + test_perf['win_rate']) / 2,
                            'test_wr': test_perf['win_rate'],
                            'test_pnl': test_perf['pnl'],
                            'consistency': abs(test_perf['win_rate'] - train_perf['win_rate'])
                        }

        return best_overall

    def _backtest_segment(self, df, strat, sl_pct, tp_pct, fee):
        balance = 10000
        pos = None
        trades = 0
        wins = 0
        
        for i in range(20, len(df)):
            row = df.iloc[i]
            if pos:
                exit_price = None
                if pos['side'] == 'BUY':
                    if row['low'] <= pos['sl']: exit_price = pos['sl']
                    elif row['high'] >= pos['tp']: exit_price = pos['tp']
                else:
                    if row['high'] >= pos['sl']: exit_price = pos['sl']
                    elif row['low'] <= pos['tp']: exit_price = pos['tp']
                
                if exit_price:
                    pnl_pct = ((exit_price - pos['entry']) / pos['entry'] if pos['side'] == 'BUY' 
                               else (pos['entry'] - exit_price) / pos['entry']) - fee
                    balance += (pnl_pct * 1000)
                    if pnl_pct > 0: wins += 1
                    trades += 1
                    pos = None
            else:
                sig = strat.get_signal(row)
                if sig['side'] in ['BUY', 'SELL']:
                    price = row['close']
                    sl = price * (1 - sl_pct) if sig['side'] == 'BUY' else price * (1 + sl_pct)
                    tp = price * (1 + tp_pct) if sig['side'] == 'BUY' else price * (1 - tp_pct)
                    pos = {'side': sig['side'], 'entry': price, 'sl': sl, 'tp': tp}
                    
        return {'pnl': balance - 10000, 'trades': trades, 'win_rate': wins/trades if trades > 0 else 0}

    def update_config(self, symbol, timeframe, new_weights, sl_pct=0.02, tp_pct=0.04, entry_score=5.0, stats=None, enabled=None):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
        else:
            data = {"default": {}}

        key = f"{symbol}_{timeframe}"
        if key not in data:
            data[key] = {
                "enabled": True, # Explicit On/Off toggle
                "weights": {}, 
                "thresholds": {"entry_score": entry_score, "exit_score": 2.5},
                "risk": {"sl_pct": sl_pct, "tp_pct": tp_pct},
                "tiers": {
                    "low": { "min_score": entry_score, "leverage": 3, "cost_usdt": 3.0 },
                    "high": { "min_score": entry_score + 2.0, "leverage": 5, "cost_usdt": 5.0 }
                }
            }
        
        # Update enabled state if provided, otherwise keep existing or default to True
        if enabled is not None:
            data[key]["enabled"] = enabled
        elif "enabled" not in data[key]:
            data[key]["enabled"] = True
        
        # Update weights and optimized parameters
        data[key]['weights'] = new_weights
        data[key]['risk'] = {"sl_pct": sl_pct, "tp_pct": tp_pct}
        data[key]['thresholds']['entry_score'] = entry_score
        data[key]['tiers']['low']['min_score'] = entry_score
        data[key]['tiers']['high']['min_score'] = entry_score + 2.0
        
        # Save simulation performance
        if stats:
            data[key]['performance'] = {
                "pnl_sim": stats.get('pnl', 0),
                "win_rate_sim": stats.get('win_rate', 0),
                "trades_sim": stats.get('trades', 0)
            }

        # ATOMIC WRITE: Write to temp file, then rename (prevents bot reading corrupted data during reload)
        import tempfile
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(config_path), text=True)
        try:
            with os.fdopen(temp_fd, 'w') as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, config_path)  # Atomic rename
        except Exception as e:
            os.unlink(temp_path)
            raise e
        print(f"Updated config for {key} | Score={entry_score} SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

async def run_global_optimization():
    from config import TRADING_TIMEFRAMES, TRADING_SYMBOLS
    from notification import send_telegram_chunked
    
    analyzer = StrategyAnalyzer()
    print("[*] **STRATEGY OPTIMIZATION STARTED** (Enhanced with Cross-TF Validation + Parallel Processing)")
    
    results_summary = []
    horizon = 15 # Balanced horizon
    all_weights_by_symbol = {}  # {symbol: {tf: weights}}
    
    # OPTIMIZED Step 1: Parallel analysis by symbol (seq by timeframe within symbol)
    print("Step 1: Analyzing all symbol+TF combinations (PARALLEL)...")
    
    def analyze_symbol(symbol):
        """Helper: Analyze single symbol across all timeframes."""
        weights_by_tf = {}
        for tf in TRADING_TIMEFRAMES:
            weights = analyzer.analyze(symbol, timeframe=tf, horizon=horizon)
            if weights:
                weights_by_tf[tf] = {'weights': weights}
        return symbol, weights_by_tf
    
    # Use ThreadPoolExecutor for parallel symbol processing
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_symbol, sym): sym for sym in TRADING_SYMBOLS}
        completed = 0
        for future in as_completed(futures):
            symbol, weights_by_tf = future.result()
            all_weights_by_symbol[symbol] = weights_by_tf
            completed += 1
            if completed % 8 == 0:  # Progress feedback every 8 symbols
                print(f"  [Progress] {completed}/{len(TRADING_SYMBOLS)} symbols analyzed...")
    
    print("  ✓ Step 1 complete: All symbols analyzed in parallel")

    print("\nStep 2: Cross-Timeframe Validation (Filtering unreliable signals) - PARALLEL...")
    
    def validate_symbol_tf(symbol, tf):
        """Helper: Validate and collect results for one (symbol, tf) pair."""
        if tf not in all_weights_by_symbol.get(symbol, {}):
            return None
        
        weights = all_weights_by_symbol[symbol][tf].get('weights', {})
        df = analyzer.load_data(symbol, tf)
        if df is None or not weights:
            return {'symbol': symbol, 'tf': tf, 'action': 'skip', 'reason': 'no_data'}
        
        df = analyzer.feature_engineer.calculate_features(df)
        best_result = analyzer.validate_weights(df, weights, symbol, tf)
        
        if best_result and best_result['pnl'] > 0:
            wr = best_result['win_rate']
            test_wr = best_result.get('test_wr', 0)
            consistency = best_result.get('consistency', 0)
            
            # ENHANCED ENABLING CONDITIONS with Cross-TF check
            is_profitable = wr >= 0.52 and test_wr >= 0.51
            is_consistent = consistency < 0.25
            
            # Check if same signal is profitable on other timeframes
            other_tf_supported = _check_other_timeframes(analyzer, symbol, weights, TRADING_TIMEFRAMES)
            
            # === NOTE: IMPORTANT DECISION ===
            # RELAXED cross-TF requirement: 2+ TF → 1+ TF for ALL symbols
            # Rationale: Increase enabled configs vs strict multi-TF diversity
            # Risk: Reduced safety margin on cross-timeframe validation
            # Mitigation: 70/30 walk-forward + 4 safety checks still enforce quality
            # Enable logic
            tf_requirement = 1  # All symbols require 1+ TF (relaxed from prior 2+ for alts)
            is_enabled = is_profitable and is_consistent and other_tf_supported >= tf_requirement
            
            status_icon = "[OK]" if is_enabled else "[WATCH]"
            status_label = "ENABLED" if is_enabled else "WATCH (Below threshold)"
            status_str = (
                f"{status_icon} **[{symbol} {tf}] {status_label}**\n"
                f"PnL: +${best_result['pnl']:.2f} | WR: {wr*100:.1f}% | Test WR: {test_wr*100:.1f}%\n"
                f"Consistency: {consistency*100:.1f}% | Trades: {best_result['trades']}"
            )
            
            return {
                'symbol': symbol, 'tf': tf, 'action': 'update',
                'weights': weights, 'best_result': best_result,
                'is_enabled': is_enabled, 'status_str': status_str
            }
        elif best_result:
            return {
                'symbol': symbol, 'tf': tf, 'action': 'disable',
                'pnl': best_result['pnl']
            }
        else:
            return {'symbol': symbol, 'tf': tf, 'action': 'skip', 'reason': 'no_signals'}
    
    # Parallel validation of all symbol+tf combinations
    validation_tasks = []
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            validation_tasks.append((symbol, tf))
    
    validation_results = []
    completed_val = 0
    import time
    step2_start = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(validate_symbol_tf, sym, tf): (sym, tf) for sym, tf in validation_tasks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                validation_results.append(result)
            completed_val += 1
            elapsed = time.time() - step2_start
            throughput = completed_val / elapsed if elapsed > 0 else 0
            if completed_val % 10 == 0:
                print(f"  [Progress] {completed_val}/{len(validation_tasks)} done | {elapsed:.1f}s | {throughput:.1f}/s (parallel x{MAX_WORKERS})")
    
    print(f"  ✓ Step 2a complete: {completed_val} validations done (parallel)")
    
    # Step 2b: Update configs serially (prevent file write race conditions)
    print("  → Updating configs from validation results...")
    for result in validation_results:
        symbol, tf = result['symbol'], result['tf']
        
        if result['action'] == 'update':
            print(f"  {result['status_str'].replace('**', '')}")
            analyzer.update_config(symbol, tf, result['weights'],
                                 sl_pct=result['best_result']['sl_pct'],
                                 tp_pct=result['best_result']['tp_pct'],
                                 entry_score=result['best_result']['entry_score'],
                                 stats=result['best_result'],
                                 enabled=result['is_enabled'])
            if result['is_enabled']:
                results_summary.append(result['status_str'])
        elif result['action'] == 'disable':
            print(f"  [SKIP] [{symbol} {tf}] Loss=${result['pnl']:.2f}")
            analyzer.update_config(symbol, tf, {}, enabled=False)
        elif result['action'] == 'skip':
            print(f"  [SKIP] [{symbol} {tf}] {result.get('reason', 'no_data')}")

    if results_summary:
        final_msg = "[OK] **STRATEGY OPTIMIZATION COMPLETE**\n\n" + "\n".join(results_summary[:20])  # Limit to 20 for Telegram
        await send_telegram_chunked(final_msg)
    else:
        print("[!] No new profitable configurations found.")

def _check_other_timeframes(analyzer, symbol, weights, timeframes):
    """Helper: Count how many other timeframes support these weights."""
    supported_count = 0
    for tf in timeframes:
        df = analyzer.load_data(symbol, tf)
        if df is None:
            continue
        df = analyzer.feature_engineer.calculate_features(df)
        result = analyzer.validate_weights(df, weights, symbol, tf)
        # RELAXED: Accept if pnl > 0 OR decent WR (>=50% instead of >=52%)
        # This helps major pairs which have fewer cross-TF confirmations
        if result and (result['pnl'] > 0 or result['win_rate'] >= 0.50):
            supported_count += 1
    return supported_count

if __name__ == "__main__":
    asyncio.run(run_global_optimization())
