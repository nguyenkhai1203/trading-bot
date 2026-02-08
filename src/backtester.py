import pandas as pd
import numpy as np
import os
import sys

# Add src to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES, RISK_PER_TRADE, STOP_LOSS_PCT, TAKE_PROFIT_PCT
from feature_engineering import FeatureEngineer
from strategy import WeightedScoringStrategy
from data_fetcher import DataFetcher
import asyncio
from risk_manager import RiskManager # Need this for sizing logic if not already used heavily

class Backtester:
    def __init__(self, symbol, timeframe, initial_balance=10000, commission=0.0006):
        self.symbol = symbol
        self.timeframe = timeframe
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.commission = commission
        self.trades = []
        self.position = None 
        self.equity_curve = []
        
        self.feature_engineer = FeatureEngineer()
        self.strategy = WeightedScoringStrategy(symbol=symbol)
        # Mock Risk Manager for backtest sizing logic
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE)

        # Data Caching Config
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
        os.makedirs(self.data_dir, exist_ok=True)

    async def run(self):
        # Print Status
        wa = [k for k,v in self.strategy.weights.items() if v != 0]
        status = "🟢 ACTIVE" if wa else "🔴 INACTIVE (Safety Mode)"
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
                    if row['low'] <= p['sl']:
                        self._close_position(timestamp, p['sl'], 'SL')
                    elif row['high'] >= p['tp']:
                        self._close_position(timestamp, p['tp'], 'TP')
                elif p['type'] == 'short':
                    if row['high'] >= p['sl']:
                        self._close_position(timestamp, p['sl'], 'SL')
                    elif row['low'] <= p['tp']:
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

        self._print_results()

    def _open_position(self, time, price, side, confidence=0.5):
        # Risk Management logic now delegated to RiskManager completely
        sl_dist = STOP_LOSS_PCT
        tp_dist = TAKE_PROFIT_PCT
        
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
        
        # Calculate PnL
        if p['type'] == 'long':
            raw_pnl = (price - entry) * qty
        else:
            raw_pnl = (entry - price) * qty
            
        # Commission (Entry + Exit) assuming taker
        comm_cost = (qty * entry * self.commission) + (qty * price * self.commission)
        net_pnl = raw_pnl - comm_cost
        
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

    def _print_results(self):
        if not self.trades:
            print(f"[{self.symbol}] No trades executed.")
            return

        df = pd.DataFrame(self.trades)
        wins = df[df['pnl'] > 0]
        win_rate = len(wins) / len(df) * 100
        total_pnl = df['pnl'].sum()
        
        print(f"\n--- Results for {self.symbol} ({self.timeframe}) ---")
        print(f"Trades: {len(df)}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Total PnL: ${total_pnl:.2f}")
        print(f"Final Balance: ${self.balance:.2f}")

async def main():
    # Test on a few symbols/timeframes
    print("Running Backtest Batch...")
    # Test on all configured symbols
    from config import TRADING_SYMBOLS
    symbols = TRADING_SYMBOLS # Use the full list from config
    tfs = ['1h'] # Faster test
    
    for s in symbols:
        for tf in tfs:
            bt = Backtester(s, tf)
            await bt.run()

if __name__ == "__main__":
    asyncio.run(main())
