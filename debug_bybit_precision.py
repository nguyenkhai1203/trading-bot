
import asyncio
import os
import sys

# Ensure src is in path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from adapters.bybit_adapter import BybitAdapter
from config import BYBIT_API_KEY, BYBIT_API_SECRET

async def main():
    print("🔍 Initializing BybitAdapter...")
    try:
        adapter = BybitAdapter()
    except Exception as e:
        print(f"❌ Failed to init adapter: {e}")
        return

    print("⏳ Syncing time & Loading markets...")
    await adapter.exchange.load_markets()
    
    # Check both standard and CCXT Swap naming convention
    symbols = [
        'ADA/USDT:USDT', 
        'XRP/USDT:USDT',
        'BTC/USDT:USDT',
        'ETH/USDT:USDT'
    ]
    
    for symbol in symbols:
        print(f"\n--- {symbol} ---")
        try:
            market = adapter.exchange.market(symbol)
        except Exception as e:
            print(f"Skipping {symbol}: {e}")
            continue
            
        # 1. Check ID and Type
        print(f"ID: {market.get('id')}")
        print(f"Type: {market.get('type')}")
        
        # 2. Check Precision
        precision = market.get('precision', {})
        print(f"Precision: {precision}")
        
        # 3. Check RAW INFO (The Source of Truth)
        # Bybit V5: params usually in 'info' -> 'lotSizeFilter'
        info = market.get('info', {})
        lot_size_filter = info.get('lotSizeFilter', {})
        print(f"RAW lotSizeFilter: {lot_size_filter}")
        print(f"RAW qtyStep: {lot_size_filter.get('qtyStep')}")
        
        # 4. Limits
        print(f"Amount Limits: {market.get('limits', {}).get('amount', {})}")
        
        # 4. Test amount_to_precision
        test_qty = 32.373456
        precision_str = adapter.exchange.amount_to_precision(symbol, test_qty)
        print(f"Test Qty {test_qty} -> Precision Str: '{precision_str}'")
        
        # 5. Check if string logic works
        print(f"CCXT Version: {adapter.exchange.version}")
        
    await adapter.close()

if __name__ == "__main__":
    if not BYBIT_API_KEY:
        print("⚠️ BYBIT_API_KEY missing. Script might fail if auth needed for some endpoints.")
    asyncio.run(main())
