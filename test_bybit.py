import sys
import os
import asyncio
# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from adapters.bybit_adapter import BybitAdapter

async def test_connectivity():
    print("🔌 Testing Bybit Connectivity...")
    adapter = BybitAdapter()
    print(f"✅ Adapter initialized: {adapter.name}")
    
    print("⏳ Syncing time (Public API)...")
    success = await adapter.sync_time()
    
    if success:
        print("✅ Time sync successful!")
        print(f"📊 Markets loaded: {len(adapter.exchange.markets)}")
    else:
        print("❌ Time sync failed.")
        
    await adapter.close()

if __name__ == "__main__":
    try:
        asyncio.run(test_connectivity())
    except Exception as e:
        print(f"❌ Error: {e}")
