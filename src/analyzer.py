import pandas as pd
import numpy as np
import os
import json
import asyncio
from config import TRADING_SYMBOLS, BINANCE_API_KEY, BINANCE_API_SECRET
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
            
            # Identify Polarity (Broadened to match Strategy.py)
            is_long = any(x in config_key for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'lt_BB', 'cross_up', 'gt_50', 'gt_'])
            is_short = any(x in config_key for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'gt_BB', 'cross_down', 'lt_50', 'lt_'])
            
            subset = df[df[name] == True]
            if len(subset) < 8: # Lowered from 10
                continue
            
            raw_win_rate_long = (subset['Target_Return'] > 0).mean()
            raw_avg_return = subset['Target_Return'].mean()
            
            actual_win_rate = 0.0
            actual_avg_return = 0.0
            
            if is_long:
                actual_win_rate = raw_win_rate_long
                actual_avg_return = raw_avg_return
            elif is_short:
                actual_win_rate = 1.0 - raw_win_rate_long
                actual_avg_return = -raw_avg_return
                
            # Award Weight (More Lenient)
            weight = 0.0
            if actual_win_rate > 0.54: weight = 2.0
            elif actual_win_rate > 0.51: weight = 1.0
            elif actual_win_rate > 0.50: weight = 0.5
            
            if weight == 0.0 and actual_avg_return > 0.002 and actual_win_rate > 0.40:
                weight = 1.2
            
            if weight > 0:
                dir_label = "LONG" if is_long else "SHORT" if is_short else "UNKN"
                print(f"    FOUND: {config_key} ({dir_label}) | WR={actual_win_rate*100:.1f}% | Ret={actual_avg_return*100:.2f}% -> W={weight}")
                results[config_key] = weight
            
        return results

    def validate_weights(self, df, weights, symbol, timeframe):
        """Finds the best SL/TP and Entry Threshold combination."""
        if not weights: return None
        
        from strategy import WeightedScoringStrategy
        mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        mock_strat.weights = weights
        
        # Ranges to test
        # Ranges to test (Based on USER ROE Targets: 5% SL, 10-20% TP)
        # At 3x Lev: SL Price % ≈ 1.7%, TP Price % ≈ 3.3% - 6.6%
        sl_ranges = [0.015, 0.02, 0.025] 
        tp_min = 0.033 # Min 10% ROE at 3x Leverage
        rr_ratios = [1.5, 2.0, 3.0] 
        thresholds_to_test = [3.0, 4.0, 5.0]
        
        commission_pct = 0.0006 # 0.06% Bybit/Binance Taker
        round_trip_fee = commission_pct * 2 # 0.12%
        
        best_result = None
        max_pnl = -999999
        
        for thresh in thresholds_to_test:
            mock_strat.config_data['thresholds'] = {'entry_score': thresh}
            
            for sl_test in sl_ranges:
                for rr in rr_ratios:
                    tp_test = max(tp_min, sl_test * rr)
                    
                    balance = 10000
                    pos = None
                    trades = 0
                    
                    for i in range(50, len(df)):
                        row = df.iloc[i]
                        price = row['close']
                        
                        if pos:
                            exit_price = None
                            if pos['side'] == 'BUY':
                                if row['low'] <= pos['sl']: exit_price = pos['sl']
                                elif row['high'] >= pos['tp']: exit_price = pos['tp']
                            else:
                                if row['high'] >= pos['sl']: exit_price = pos['sl']
                                elif row['low'] <= pos['tp']: exit_price = pos['tp']
                            
                            if exit_price:
                                pnl_pct = (exit_price - pos['entry']) / pos['entry'] if pos['side'] == 'BUY' else (pos['entry'] - exit_price) / pos['entry']
                                
                                # SUBTRACT FEES (0.12% round trip)
                                pnl_pct -= round_trip_fee
                                
                                balance += (pnl_pct * 1000)
                                pos = None
                                trades += 1
                        else:
                            sig = mock_strat.get_signal(row)
                            if sig['side'] in ['BUY', 'SELL']:
                                sl = price * (1 - sl_test) if sig['side'] == 'BUY' else price * (1 + sl_test)
                                tp = price * (1 + tp_test) if sig['side'] == 'BUY' else price * (1 - tp_test)
                                pos = {'side': sig['side'], 'entry': price, 'sl': sl, 'tp': tp}
                    
                    if trades >= 2 and balance > max_pnl: # Require at least 2 trades for validity
                        max_pnl = balance
                        best_result = {
                            'sl_pct': sl_test, 'tp_pct': tp_test, 
                            'entry_score': thresh, 'pnl': balance - 10000, 'trades': trades
                        }

        if best_result is None:
             # print(f"    [WARN] No trades triggered for {symbol} {timeframe} with given weights.")
             pass
             
        return best_result

    def update_config(self, symbol, timeframe, new_weights, sl_pct=0.02, tp_pct=0.04, entry_score=5.0):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
        else:
            data = {"default": {}}

        key = f"{symbol}_{timeframe}"
        if key not in data:
            data[key] = {
                "weights": {}, 
                "thresholds": {"entry_score": entry_score, "exit_score": 2.5},
                "risk": {"sl_pct": sl_pct, "tp_pct": tp_pct},
                "tiers": {
                    "low": { "min_score": entry_score, "leverage": 3, "cost_usdt": 3.0 },
                    "high": { "min_score": entry_score + 2.0, "leverage": 5, "cost_usdt": 5.0 }
                }
            }
        
        # Update weights and optimized parameters
        data[key]['weights'] = new_weights
        data[key]['risk'] = {"sl_pct": sl_pct, "tp_pct": tp_pct}
        data[key]['thresholds']['entry_score'] = entry_score
        data[key]['tiers']['low']['min_score'] = entry_score
        data[key]['tiers']['high']['min_score'] = entry_score + 2.0

        with open(config_path, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"Updated config for {key} | Score={entry_score} SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

async def run_global_optimization():
    from config import TRADING_TIMEFRAMES, TRADING_SYMBOLS
    from notification import send_telegram_chunked
    
    analyzer = StrategyAnalyzer()
    print("🔍 **STRATEGY OPTIMIZATION STARTED**")
    
    results_summary = []
    horizon = 15 # Balanced horizon
    
    print("Running Global Strategy Optimization...")
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            weights = analyzer.analyze(symbol, timeframe=tf, horizon=horizon)
            if weights:
                df = analyzer.load_data(symbol, tf)
                df = analyzer.feature_engineer.calculate_features(df)
                
                best_result = analyzer.validate_weights(df, weights, symbol, tf)
                
                if best_result and best_result['pnl'] > 0:
                    status_str = f"✅ [{symbol} {tf}] REAL RUNNING (+${best_result['pnl']:.2f} | Score={best_result['entry_score']} SL={best_result['sl_pct']*100:.1f}% TP={best_result['tp_pct']*100:.1f}%)"
                    print(status_str)
                    analyzer.update_config(symbol, tf, weights, 
                                         sl_pct=best_result['sl_pct'], 
                                         tp_pct=best_result['tp_pct'],
                                         entry_score=best_result['entry_score'])
                    results_summary.append(status_str)
                elif best_result:
                    print(f"❌ [{symbol} {tf}] NO MONEY (Loss={best_result['pnl']:.2f})")
                    analyzer.update_config(symbol, tf, {}) # Deactivate
                else:
                    print(f"🧪 [{symbol} {tf}] TEST (No trades)")
            else:
                print(f"  [SKIP] No strong signals for {symbol} {tf}")

    if results_summary:
        final_msg = "📊 **STRATEGY OPTIMIZATION COMPLETE**\n\n" + "\n".join(results_summary)
        await send_telegram_chunked(final_msg)
    else:
        print("No new profitable configurations found.")

if __name__ == "__main__":
    asyncio.run(run_global_optimization())
