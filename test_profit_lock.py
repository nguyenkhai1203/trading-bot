
import asyncio
import sys
import os
import pandas as pd
import numpy as np

# Add src dir to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from execution import Trader
from config import ENABLE_PROFIT_LOCK, PROFIT_LOCK_THRESHOLD, PROFIT_LOCK_LEVEL

class MockExchange:
    def __init__(self):
        self.name = 'BYBIT'
        self.is_public_only = False
    
    async def fetch_ticker(self, symbol):
        # Always return a price that makes the SL valid for the test
        return {'last': 100.0}
    
    async def cancel_order(self, order_id, symbol):
        print(f"    [API] Order {order_id} cancelled on Bybit.")
        return True
    
    async def create_order(self, symbol, type, side, qty, price=None, params=None):
        price_val = params.get('stopPrice', 'MARKET')
        print(f"    [API] {side} order created on Bybit for {qty} @ {price_val}")
        return {'id': f'order_{np.random.randint(1000, 9999)}'}

async def run_simulation():
    print("="*60)
    print("🚀 STARTING SIMULATION: Dynamic Risk Management v3.0")
    print("="*60)
    
    # 1. Initialize Trader with Mock Exchange
    mock_ex = MockExchange()
    trader = Trader(mock_ex, dry_run=False) # Use live logic with mock API
    
    # 2. Define a Sample Position (LONG)
    # Entry: 100, TP: 110, SL: 95. Target Profit: 10 units.
    # 80% Threshold: 100 + (10 * 0.8) = 108
    symbol = "BTC/USDT"
    pos_key = f"BYBIT_{symbol}_1h"
    trader.active_positions[pos_key] = {
        'symbol': symbol,
        'side': 'BUY',
        'entry_price': 100.0,
        'tp': 110.0,
        'sl': 95.0,
        'qty': 0.1,
        'status': 'filled',
        'sl_order_id': 'initial_sl_id',
        'tp_order_id': 'initial_tp_id'
    }
    
    print(f"\n🟢 Initial Position: {symbol} LONG")
    print(f"   Entry: 100.0 | SL: 95.0 | TP: 110.0")
    print(f"   Threshold to trigger Profit Lock: 108.0")
    
    # 3. Simulate Price Movement
    prices = [102.0, 105.0, 107.5, 108.5]
    
    for price in prices:
        print(f"\n⏱️ Price moves to: {price:.2f}")
        
        # We simulate resistance at 115 and ATR at 2.0
        # If price reaches 108.5, it should trigger
        resistance = 115.0
        atr = 2.0
        
        updated = await trader.adjust_sl_tp_for_profit_lock(
            pos_key, 
            price, 
            resistance=resistance, 
            support=None, 
            atr=atr
        )
        
        pos = trader.active_positions[pos_key]
        if updated:
            print(f"   ✅ UPDATE TRIGGERED!")
            print(f"   New SL: {pos['sl']} (Now in PROFIT territory!)")
            print(f"   New TP: {pos['tp']} (Extended to Resistance: 115.0)")
            print(f"   Status: profit_locked={pos.get('profit_locked')}")
        else:
            print(f"   ... Waiting for threshold (Current SL: {pos['sl']})")

    print("\n" + "="*60)
    print("🏁 SIMULATION COMPLETE")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(run_simulation())
