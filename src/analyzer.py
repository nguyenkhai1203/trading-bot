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
        
        # Calculate EMA 200 for Trend Filter
        if len(df) > 200:
            df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        else:
            df['ema_200'] = df['close'].expanding().mean()
            
        return df

    def get_signal_category(self, name):
        """Categorize signals to avoid redundancy."""
        name = name.lower()
        if any(x in name for x in ['ema', 'death', 'golden', 'gt_200', 'lt_200']):
            return 'Trend'
        if any(x in name for x in ['rsi', 'macd']):
            return 'Momentum'
        if any(x in name for x in ['bb', 'bollinger']):
            return 'Volatility'
        return 'Other'

    def analyze(self, symbol, timeframe='1h', horizon=4):
        """
        Analyzes signals with Layer 1: Trend Filter and Layer 2: Diversity.
        """
        print(f"Analyzing {symbol} {timeframe}...")
        df = self.load_data(symbol, timeframe)
        if df is None: return

        df = self.feature_engineer.calculate_features(df)
        df['Target_Return'] = df['close'].shift(-horizon) / df['close'] - 1.0
        
        signal_cols = [c for c in df.columns if c.startswith('signal_')]
        
        category_best = {} # {category: {name, wr, weight}}
        
        print(f"  Scanning {len(signal_cols)} signals with EMA-200 Trend Filter...")
        
        for name in signal_cols:
            config_key = name.replace('signal_', '')
            cat = self.get_signal_category(config_key)
            
            is_long = any(x in config_key for x in ['oversold', 'up', 'golden', 'gt_200', 'gt_signal', 'lt_BB', 'cross_up', 'gt_50', 'gt_'])
            is_short = any(x in config_key for x in ['overbought', 'down', 'death', 'lt_200', 'lt_signal', 'gt_BB', 'cross_down', 'lt_50', 'lt_'])
            
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
            
            # Weighting Logic
            weight = 0.0
            if actual_win_rate > 0.58: weight = 2.0
            elif actual_win_rate > 0.55: weight = 1.0
            elif actual_win_rate > 0.52: weight = 0.5
            
            if weight > 0:
                # Update category best (Diversity)
                if cat not in category_best or actual_win_rate > category_best[cat]['wr']:
                    category_best[cat] = {'name': config_key, 'wr': actual_win_rate, 'weight': weight, 'dir': 'LONG' if is_long else 'SHORT'}

        # --- LAYER 2: CLEAN PARAMETERS ---
        # Construct final weights from diverse categories
        results = {}
        for cat, best in category_best.items():
            print(f"    [CLEAN] Picked best {cat}: {best['name']} ({best['dir']}) | WR={best['wr']*100:.1f}%")
            results[best['name']] = best['weight']
            
        return results

    def validate_weights(self, df, weights, symbol, timeframe):
        """--- LAYER 3: 70/30 WALK-FORWARD VALIDATION ---"""
        if not weights: return None
        
        split_idx = int(len(df) * 0.7)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
        
        from strategy import WeightedScoringStrategy
        mock_strat = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        mock_strat.weights = weights
        
        sl_ranges = [0.015, 0.02, 0.025] 
        tp_min = 0.033 
        rr_ratios = [1.5, 2.0, 3.0] 
        thresholds_to_test = [3.0, 4.0, 5.0]
        
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
                    
                    if train_perf['trades'] >= 2 and train_perf['pnl'] > max_train_pnl:
                        # 2. VALIDATE ON TESTING DATA (The "Future")
                        test_perf = self._backtest_segment(test_df, mock_strat, sl_test, tp_test, round_trip_fee)
                        
                        max_train_pnl = train_perf['pnl']
                        best_overall = {
                            'sl_pct': sl_test, 'tp_pct': tp_test, 
                            'entry_score': thresh, 
                            'pnl': train_perf['pnl'] + test_perf['pnl'], 
                            'trades': train_perf['trades'] + test_perf['trades'], 
                            'win_rate': (train_perf['win_rate'] + test_perf['win_rate']) / 2,
                            'test_wr': test_perf['win_rate'],
                            'test_pnl': test_perf['pnl']
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
                    wr = best_result['win_rate']
                    test_wr = best_result.get('test_wr', 0)
                    
                    # More rigorous enabling condition
                    is_enabled = wr >= 0.55 and test_wr >= 0.52
                    
                    status_icon = "🚀" if is_enabled else "⚠️"
                    status_label = "ENABLED (Tactical)" if is_enabled else "PROFITABLE BUT UNSTABLE"
                    
                    status_str = (
                        f"{status_icon} **[{symbol} {tf}] {status_label}**\n"
                        f"Overall PnL: +${best_result['pnl']:.2f} | WR: {wr*100:.1f}%\n"
                        f"Test WR: {test_wr*100:.1f}% | Trades: {best_result['trades']}\n"
                        f"SL: {best_result['sl_pct']*100:.1f}% | TP: {best_result['tp_pct']*100:.1f}%"
                    )
                    print(f"  {status_str.replace('**', '')}")
                    
                    analyzer.update_config(symbol, tf, weights, 
                                         sl_pct=best_result['sl_pct'], 
                                         tp_pct=best_result['tp_pct'],
                                         entry_score=best_result['entry_score'],
                                         stats=best_result,
                                         enabled=is_enabled)
                    
                    if is_enabled:
                        results_summary.append(status_str)
                elif best_result:
                    print(f"❌ [{symbol} {tf}] NO MONEY (Loss={best_result['pnl']:.2f})")
                    analyzer.update_config(symbol, tf, {}, enabled=False) # Deactivate
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
