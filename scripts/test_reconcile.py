import asyncio
import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
load_dotenv()

from execution import Trader
from exchange_factory import get_active_exchanges_map

async def test_reconcile():
    print("🚦 Starting Manual Reconciliation Test...")
    ex_adapters = get_active_exchanges_map()
    
    for name, adapter in ex_adapters.items():
        print(f"\n--- Checking {name} ---")
        trader = None
        try:
            # Initialize Trader in live mode (to actually fetch from API)
            print(f"🔧 Initializing Trader for {name}...")
            trader = Trader(adapter, dry_run=False)
            
            print(f"⏰ Syncing time for {name}...")
            await trader.exchange.sync_time()
            
            print(f"📚 Loading markets for {name}...")
            await trader.exchange.load_markets()
            
            print(f"🔄 Running reconcile_positions for {name}...")
            # We wrap this in a try-except to catch inner errors
            try:
                summary = await trader.reconcile_positions(auto_fix=True)
                print(f"✅ Reconcile complete for {name}")
                print(f"📊 Summary:")
                print(json.dumps(summary, indent=4))
            except Exception as inner_e:
                print(f"❌ Inner error in reconcile_positions: {inner_e}")
                import traceback
                traceback.print_exc()
            
            print(f"📦 Active Positions after sync: {len(trader.active_positions)}")
            # Filter for CURRENT exchange positions only
            prefix = f"{name}_"
            for k, v in trader.active_positions.items():
                if k.startswith(prefix):
                    print(f"  - {k}: {v.get('side')} {v.get('qty')} @ {v.get('entry_price')} (Status: {v.get('status')})")

        except Exception as e:
            print(f"❌ Global error during {name} sync: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if trader and hasattr(trader, 'close'):
                print(f"🔌 Closing trader for {name}...")
                await trader.close()
            elif adapter and hasattr(adapter, 'close'):
                print(f"🔌 Closing adapter for {name}...")
                await adapter.close()

if __name__ == "__main__":
    try:
        asyncio.run(test_reconcile())
    except Exception as e:
        print(f"🔥 Script crashed: {e}")
        import traceback
        traceback.print_exc()
