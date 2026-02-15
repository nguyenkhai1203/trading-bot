import pandas as pd
import numpy as np
import os
import sys

# Add src to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES, RISK_PER_TRADE, STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRADING_COMMISSION, SLIPPAGE_PCT
from feature_engineering import FeatureEngineer
from strategy import WeightedScoringStrategy
from data_fetcher import DataFetcher
import asyncio
from risk_manager import RiskManager # Need this for sizing logic if not already used heavily

class Backtester:
    def __init__(self, symbol, timeframe, initial_balance=10000, commission=TRADING_COMMISSION, slippage=SLIPPAGE_PCT):
        self.symbol = symbol
        self.timeframe = timeframe
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.commission = commission    # 0.06% per trade
        self.slippage = slippage        # 0.05% price impact per trade
        self.trades = []
        self.position = None 
        self.equity_curve = []
        
        self.feature_engineer = FeatureEngineer()
        self.strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe)
        # Mock Risk Manager for backtest sizing logic
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE)

        # Data Caching Config
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
        os.makedirs(self.data_dir, exist_ok=True)

    async def run(self):
        # Print Status
        wa = [k for k,v in self.strategy.weights.items() if v != 0]
        status = "[ACTIVE]" if wa else "[INACTIVE]"
        print(f"[{self.symbol}] Strategy Status: {status} ({len(wa)} parameters)")
        
        # Check cache first
        file_path = os.path.join(self.data_dir, f"{self.symbol.replace('/', '').replace(':', '')}_{self.timeframe}.csv")

        df = None
        # 1. Try Load from Disk
        if os.path.exists(file_path):
            print(f"[{self.symbol}] Loading data from cache: {file_path}")
            df = pd.read_csv(file_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # 2. If no cache, Fetch from API
        if df is None or df.empty:
            print(f"[{self.symbol}] Fetching historical data from API...")
            fetcher = DataFetcher(symbol=self.symbol, timeframe=self.timeframe)
            df = await fetcher.fetch_ohlcv(limit=1000) 
            await fetcher.close()
            
            if df is not None and not df.empty:
                print(f"[{self.symbol}] Saving data to cache...")
                df.to_csv(file_path, index=False)
        
        if df is None or df.empty:
            print(f"[{self.symbol}] No data found.")
            return

        # 1. Calculate Features
        df = self.feature_engineer.calculate_features(df)
        
        print(f"Running Backtest on {len(df)} candles...")
        
        for index, row in df.iterrows():
            if index < 50: # Warmup period for EMA/RSI
                continue
                
            timestamp = row['timestamp']
            current_price = row['close']
            
            # --- Check Exit Conditions ---
            if self.position:
                p = self.position
                # Check SL/TP
                if p['type'] == 'long':
                    if p.get('sl') is not None and row['low'] <= p['sl']:
                        self._close_position(timestamp, p['sl'], 'SL')
                    elif p.get('tp') is not None and row['high'] >= p['tp']:
                        self._close_position(timestamp, p['tp'], 'TP')
                elif p['type'] == 'short':
                    if p.get('sl') is not None and row['high'] >= p['sl']:
                        self._close_position(timestamp, p['sl'], 'SL')
                    elif p.get('tp') is not None and row['low'] <= p['tp']:
                        self._close_position(timestamp, p['tp'], 'TP')
                        
            # --- Check Entry Signals ---
            # Only enter if no position (simplification)
            if not self.position:
                signal_data = self.strategy.get_signal(row)
                side = signal_data['side']
                conf = signal_data['confidence']
                
                if side in ['BUY', 'SELL']: # Strategy determines threshold (5.0 score)
                    trade_side = 'long' if side == 'BUY' else 'short'
                    self._open_position(timestamp, current_price, trade_side, conf)

            self.equity_curve.append({'timestamp': timestamp, 'equity': self.balance})

        return self._print_results()

    def _open_position(self, time, price, side, confidence=0.5):
        # Risk Management logic now delegated to RiskManager completely
        sl_dist = self.strategy.sl_pct
        tp_dist = self.strategy.tp_pct
        
        sl = price * (1 - sl_dist) if side == 'long' else price * (1 + sl_dist)
        tp = price * (1 + tp_dist) if side == 'long' else price * (1 - tp_dist)
        
        # Get Tier
        score = confidence * 10
        # Access through strategy if available, else hardcode for backtest safety
        if hasattr(self.strategy, 'get_sizing_tier'):
            tier = self.strategy.get_sizing_tier(score)
        else:
             tier = { "leverage": 3, "cost_usdt": 3.0 } if score < 7 else { "leverage": 5, "cost_usdt": 8.0 }

        target_lev = tier.get('leverage', 3)
        target_cost = tier.get('cost_usdt', 3.0)

        # Position Size
        # Backtester needs to simulate margin usage. 
        # Size = (Cost * Lev) / Price
        qty = self.risk_manager.calculate_size_by_cost(price, target_cost, target_lev)
        
        cost = target_cost # Margin used
        
        self.position = {
            'type': side,
            'entry_price': price,
            'qty': qty,
            'sl': sl,
            'tp': tp,
            'entry_time': time
        }

    def _close_position(self, time, price, reason):
        p = self.position
        qty = p['qty']
        entry = p['entry_price']
        
        # REAL-WORLD FRICTION MODEL (Backtrader standard)
        # 1. Commission: Paid on BOTH Entry and Exit (Total 2x)
        # 2. Slippage: Price impact against us on BOTH Entry and Exit
        
        entry_friction = entry * self.slippage
        exit_friction = price * self.slippage
        
        # Effective Entry/Exit prices after slippage
        eff_entry = entry + entry_friction if p['type'] == 'long' else entry - entry_friction
        eff_exit = price - exit_friction if p['type'] == 'long' else price + exit_friction
        
        # Recalculate PnL with effective prices
        if p['type'] == 'long':
            raw_pnl = (eff_exit - eff_entry) * qty
        else:
            raw_pnl = (eff_entry - eff_exit) * qty
            
        # Commission Cost (Volume * Rate)
        # Note: Commission is based on Notional Value (Price * Qty)
        comm_entry = (qty * eff_entry) * self.commission
        comm_exit = (qty * eff_exit) * self.commission
        
        total_cost = comm_entry + comm_exit
        net_pnl = raw_pnl - total_cost
        
        self.balance += net_pnl
        self.trades.append({
            'symbol': self.symbol,
            'entry_time': p['entry_time'],
            'exit_time': time,
            'side': p['type'],
            'entry': entry,
            'exit': price,
            'pnl': net_pnl,
            'reason': reason
        })
        self.position = None

    def _calculate_metrics(self, df):
        """Calculate advanced performance metrics."""
        if df.empty:
            return None
        
        # Basic metrics
        wins = df[df['pnl'] > 0]
        losses = df[df['pnl'] <= 0]
        win_rate = len(wins) / len(df) * 100 if len(df) > 0 else 0
        total_pnl = df['pnl'].sum()
        avg_trade = total_pnl / len(df) if len(df) > 0 else 0
        
        # Profit Factor (Gross Profit / |Gross Loss|)
        gross_profit = wins['pnl'].sum() if len(wins) > 0 else 0
        gross_loss = abs(losses['pnl'].sum()) if len(losses) > 0 else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)
        
        # ROI %
        roi = ((self.balance - self.initial_balance) / self.initial_balance * 100) if self.initial_balance > 0 else 0
        
        # Max Drawdown (from equity curve)
        equity_values = [x['equity'] for x in self.equity_curve]
        if equity_values:
            running_max = np.maximum.accumulate(equity_values)
            drawdown = (equity_values - running_max) / running_max * 100
            max_drawdown = np.min(drawdown) if len(drawdown) > 0 else 0
        else:
            max_drawdown = 0
        
        # Advanced Risk Metrics (Sharpe & Sortino)
        # 1. Determine annualization factor (252 days * candles per day)
        if '15m' in self.timeframe: freq = 96 * 252
        elif '30m' in self.timeframe: freq = 48 * 252
        elif '1h' in self.timeframe: freq = 24 * 252
        elif '4h' in self.timeframe: freq = 6 * 252
        elif '1d' in self.timeframe: freq = 252
        else: freq = 252 # Default
        
        if len(df) > 1:
            # ROI per trade
            returns = df['pnl'] / self.initial_balance
            
            # Sharpe Ratio (Mean / StdFlow)
            mean_ret = np.mean(returns)
            std_ret = np.std(returns)
            sharpe = (mean_ret / std_ret) * np.sqrt(freq) if std_ret > 0 else 0
            
            # Sortino Ratio (Mean / Downside Deviation)
            # Downside Deviation = Sqrt(Mean(Negative_Returns^2)) [Target Return = 0]
            # This is more robust than std() for single values
            negative_returns = returns[returns < 0]
            
            if len(negative_returns) > 0:
                # Root Mean Square of negative returns (assuming target return 0)
                downside_dev = np.sqrt(np.mean(negative_returns**2))
                sortino = (mean_ret / downside_dev) * np.sqrt(freq) if downside_dev > 0 else 0
            else:
                # No downside volatility = Infinite Sortino (technically), but cap at high number
                sortino = 999.0 if mean_ret > 0 else 0.0
        else:
            sharpe = 0
            sortino = 0
        
        # Consecutive wins/losses
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        
        for pnl in df['pnl'].values:
            if pnl > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
        
        # Largest win/loss
        largest_win = wins['pnl'].max() if len(wins) > 0 else 0
        largest_loss = losses['pnl'].min() if len(losses) > 0 else 0
        
        return {
            'trades': len(df),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_trade': avg_trade,
            'largest_win': largest_win,
            'largest_loss': largest_loss,
            'profit_factor': profit_factor,
            'roi': roi,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe,
            'sortino_ratio': sortino,
            'max_consecutive_wins': max_consecutive_wins,
            'max_consecutive_losses': max_consecutive_losses,
            'balance': self.balance
        }

    def _print_results(self):
        if not self.trades:
            print(f"[{self.symbol} {self.timeframe}] No trades executed.")
            return {
                'symbol': self.symbol, 'tf': self.timeframe, 
                'trades': 0, 'win_rate': 0, 'pnl': 0, 'balance': self.balance,
                'avg_trade': 0, 'largest_win': 0, 'largest_loss': 0,
                'profit_factor': 0, 'roi': 0, 'max_drawdown': 0, 'sharpe_ratio': 0
            }

        df = pd.DataFrame(self.trades)
        metrics = self._calculate_metrics(df)
        
        # Save detailed trades to CSV
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        report_dir = os.path.join(base_dir, 'reports')
        if not os.path.exists(report_dir):
            os.makedirs(report_dir, exist_ok=True)
            
        report_path = os.path.join(report_dir, f"backtest_{self.symbol.replace('/', '')}_{self.timeframe}.csv")
        df.to_csv(report_path, index=False)
        
        print(f"\n{'='*70}")
        print(f"BACKTEST RESULTS: {self.symbol} ({self.timeframe})")
        print(f"{'='*70}")
        print(f"Trades:                {metrics['trades']}")
        print(f"Win Rate:              {metrics['win_rate']:.1f}%")
        print(f"Total PnL:             ${metrics['total_pnl']:.3f}")
        print(f"Avg Trade PnL:         ${metrics['avg_trade']:.3f}")
        print(f"Largest Win/Loss:      ${metrics['largest_win']:.3f} / ${metrics['largest_loss']:.3f}")
        print(f"Profit Factor:         {metrics['profit_factor']:.2f}x")
        print(f"ROI:                   {metrics['roi']:.2f}%")
        print(f"Max Drawdown:          {metrics['max_drawdown']:.2f}%")
        print(f"Sharpe Ratio:          {metrics['sharpe_ratio']:.2f}")
        print(f"Sortino Ratio:         {metrics['sortino_ratio']:.2f}")
        print(f"Max Consecutive W/L:   {metrics['max_consecutive_wins']} / {metrics['max_consecutive_losses']}")
        print(f"Final Balance:         ${metrics['balance']:.3f}")
        print(f"{'='*70}")
        print(f"Report saved to: {report_path}\n")
        
        # Return result summary
        return {
            'symbol': self.symbol, 
            'tf': self.timeframe, 
            'trades': metrics['trades'],
            'win_rate': round(metrics['win_rate'], 1),
            'pnl': round(metrics['total_pnl'], 3),
            'avg_trade': round(metrics['avg_trade'], 3),
            'largest_win': round(metrics['largest_win'], 3),
            'largest_loss': round(metrics['largest_loss'], 3),
            'profit_factor': round(metrics['profit_factor'], 2),
            'roi': round(metrics['roi'], 2),
            'max_drawdown': round(metrics['max_drawdown'], 2),
            'sharpe_ratio': round(metrics['sharpe_ratio'], 2),
            'sortino_ratio': round(metrics['sortino_ratio'], 2),
            'balance': round(metrics['balance'], 3)
        }

async def main():
    print("[*] Starting Global Backtest Session (ENABLED CONFIGS ONLY)...")
    from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES
    import json
    
    # Load strategy config to get ENABLED status
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
    try:
        with open(config_path, 'r') as f:
            strategy_config = json.load(f)
    except:
        strategy_config = {}
    
    all_results = []
    enabled_results = []
    watched_results = []
    
    for s in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            key = f"{s}_{tf}"
            config = strategy_config.get(key, {})
            is_enabled = config.get('enabled', False)
            status = config.get('status', 'UNKNOWN')
            
            # Skip if neither ENABLED nor WATCHED
            if not is_enabled and 'WATCH' not in status:
                continue
            
            bt = Backtester(s, tf)
            res = await bt.run()
            if res:
                res['status'] = status.split('(')[0].strip() if '(' in status else 'N/A'  # Extract status
                all_results.append(res)
                
                if is_enabled:
                    enabled_results.append(res)
                else:
                    watched_results.append(res)
    
    # Print ENABLED Results First
    if enabled_results:
        print("\n" + "="*140)
        print("[OK] ENABLED CONFIGS (DEPLOYING)")
        print("="*140)
        print(f"{'SYMBOL':<15} {'TF':<6} {'TRADES':<8} {'WIN%':<8} {'PROFIT':<12} {'AVG':<10} {'ROI':<8} {'SHARPE':<8} {'MAXDD%':<8} {'PF':<8}")
        print("-" * 140)
        total_pnl = 0
        total_roi = 0
        for r in enabled_results:
            avg_display = f"${r['avg_trade']:.3f}" if r['avg_trade'] else "$0.000"
            print(f"{r['symbol']:<15} {r['tf']:<6} {r['trades']:<8} {r['win_rate']:<8} ${r['pnl']:<11} {avg_display:<10} {r['roi']:<8} {r['sharpe_ratio']:<8} {r['max_drawdown']:<8} {r['profit_factor']:<8}")
            total_pnl += r['pnl']
            total_roi += r['roi']
        print("="*140)
        avg_roi = (total_roi / len(enabled_results)) if enabled_results else 0
        print(f"{'TOTAL (ENABLED)':<15} {'-':<6} {sum([r['trades'] for r in enabled_results]):<8} {'-':<8} ${total_pnl:<11} {'-':<10} {avg_roi:<8}")
        print("="*140)
    
    # Print WATCHED Results (Optional)
    if watched_results:
        print("\n" + "="*140)
        print("[WATCH] CONFIGS (SINGLE TF, NEEDS REVIEW)")
        print("="*140)
        print(f"{'SYMBOL':<15} {'TF':<6} {'TRADES':<8} {'WIN%':<8} {'PROFIT':<12} {'AVG':<10} {'ROI':<8} {'SHARPE':<8} {'MAXDD%':<8} {'PF':<8}")
        print("-" * 140)
        for r in watched_results:
            avg_display = f"${r['avg_trade']:.3f}" if r['avg_trade'] else "$0.000"
            print(f"{r['symbol']:<15} {r['tf']:<6} {r['trades']:<8} {r['win_rate']:<8} ${r['pnl']:<11} {avg_display:<10} {r['roi']:<8} {r['sharpe_ratio']:<8} {r['max_drawdown']:<8} {r['profit_factor']:<8}")
        print("="*140)
    
    # Save Global Summary (ENABLED + WATCHED)
    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports', 'global_backtest_summary.csv')
        summary_df.to_csv(summary_path, index=False)
        print(f"\n[OK] Summary: {len(enabled_results)} ENABLED + {len(watched_results)} WATCHED")
        print(f"[OK] Report: {summary_path}\n")

if __name__ == "__main__":
    asyncio.run(main())
