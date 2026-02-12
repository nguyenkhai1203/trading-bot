"""
CANCEL ALL OPEN ORDERS (Clean Slate)
"""
import asyncio
import ccxt.async_support as ccxt
from dotenv import load_dotenv
import os
import sys

# Ensure src path is in sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
load_dotenv()

async def cancel_all():
    print("=" * 60)
    print("🧨 CANCEL ALL OPEN ORDERS")
    print("=" * 60)
    
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    
    try:
        await exchange.load_time_difference()
        
        # 1. Fetch ALL open orders
        print("Fetching open orders...")
        try:
            orders = await exchange.fapiPrivateGetOpenOrders()
        except:
            orders = await exchange.fetch_open_orders()
            
        if not orders:
            print("✅ No open orders found. Nothing to cancel.")
            return

        print(f"📦 Found {len(orders)} open orders.")
        
        # Group by symbol
        symbols = set([o['symbol'] for o in orders])
        print(f"🎯 Targets: {', '.join(symbols)}")
        
        confirm = input("\n💥 DESTROY ALL ORDERS? (Type 'yes'): ")
        if confirm.lower().strip() != 'yes':
            print("❌ Cancelled.")
            return
            
        print("\n🗑️  Cancelling...")
        for sym in symbols:
            try:
                # Cancel All for Symbol
                await exchange.cancel_all_orders(sym)
                print(f"   ✅ Cancelled all for {sym}")
            except Exception as e:
                print(f"   ❌ Failed {sym}: {e}")
                
        print("\n✨ Done. All clear.")
        
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(cancel_all())
