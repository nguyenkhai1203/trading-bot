import asyncio
import logging
from src.adapters.bybit_adapter import BybitAdapter
# Assuming base_exchange_client is in src root or importable via src path
try:
    from src.base_exchange_client import BaseExchangeClient
except ImportError:
    # Fallback or direct import if needed
    import sys
    sys.path.append('src')
    from base_exchange_client import BaseExchangeClient
import sys
import os

sys.path.append(os.path.join(os.getcwd(), 'src'))

async def test_adapter():
    logging.basicConfig(level=logging.DEBUG)
    print("🚀 Initializing BybitAdapter...")
    
    try:
        adapter = BybitAdapter()
        print(f"✅ Adapter initialized. Authenticated: {adapter.is_authenticated}")
        
        if not adapter.is_authenticated:
            print("❌ Adapter not authenticated! Check API keys.")
            return

        print("⚖️ Fetching Balance...")
        balance = await adapter.fetch_balance()
        
        print("\n--- Balance Result ---")
        print(f"Raw Result Keys: {list(balance.keys()) if balance else 'None'}")
        total = balance.get('total', {}).get('USDT', 0)
        free = balance.get('free', {}).get('USDT', 0)
        print(f"💰 Total USDT: {total}")
        print(f"🆓 Free USDT: {free}")
        
        if total == 0:
            print("⚠️ Total is 0! Dumping full 'info' field if available...")
            if 'info' in balance:
                import json
                print(json.dumps(balance['info'], indent=2))
                
        await adapter.close()
        
    except Exception as e:
        print(f"💥 Exception: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_adapter())
