"""
GLOBAL ORDER VISIBILITY TEST
Fetches every single open order on the account without any filtering.
"""
import asyncio
import ccxt.async_support as ccxt
from dotenv import load_dotenv
import os
import sys

# Ensure src path is in sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
load_dotenv()

async def verify_visibility():
    print("=" * 60)
    print("🔍 GLOBAL ORDER VISIBILITY TEST")
    print("=" * 60)
    
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    
    try:
        await exchange.load_time_difference()
        
        print("\n[1] Fetching ALL open orders (fapiPrivateGetOpenOrders)...")
        # Global fetch (symbol=None)
        raw_orders = await exchange.fapiPrivateGetOpenOrders()
        
        print(f"✅ Found {len(raw_orders)} raw orders on exchange.")
        
        if not raw_orders:
            print("❌ ZERO orders found. (Did you already cancel them?)")
            return

        symbols = set()
        conditional = 0
        basic = 0
        
        for o in raw_orders:
            oid = o.get('orderId')
            sym = o.get('symbol')
            otype = o.get('type', '').upper()
            side = o.get('side', '').upper()
            stop_p = o.get('stopPrice')
            
            symbols.add(sym)
            
            is_conditional = any(x in otype for x in ['STOP', 'TAKE', 'TRAIL'])
            if is_conditional:
                conditional += 1
                print(f" ✨ [COND] {sym} {side} {otype} | Stop: {stop_p} | ID: {oid}")
            else:
                basic += 1
                print(f" 📝 [BASIC] {sym} {side} {otype} | Price: {o.get('price')} | ID: {oid}")

        print("\nSummary:")
        print(f"  - Total Symbols with orders: {len(symbols)}")
        print(f"  - Basic Orders (Limit/Mkt): {basic}")
        print(f"  - Conditional Orders (SL/TP): {conditional}")
        print(f"  - TOTAL: {len(raw_orders)}")
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(verify_visibility())
