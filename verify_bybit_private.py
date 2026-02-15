import sys
import os
import asyncio
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from adapters.bybit_adapter import BybitAdapter

async def test_private_access():
    print("🔐 Testing Bybit Private API Access...")
    try:
        adapter = BybitAdapter()
        print(f"✅ Adapter initialized: {adapter.name}")
        
        # Try to fetch positions (requires valid keys)
        print("⏳ Fetching positions...")
        positions = await adapter.fetch_positions()
        print(f"✅ Positions fetched successfully. Count: {len(positions)}")
        
        # Try to fetch balance/open orders
        print("⏳ Fetching open orders...")
        orders = await adapter.fetch_open_orders()
        print(f"✅ Open orders fetched successfully. Count: {len(orders)}")
        
        await adapter.close()
        return True
    except Exception as e:
        print(f"❌ Private API Access Failed: {e}")
        return False

if __name__ == "__main__":
    try:
        asyncio.run(test_private_access())
    except Exception as e:
        print(f"❌ Script Error: {e}")
