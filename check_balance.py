import asyncio
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from exchange_factory import get_active_exchanges_map

async def check_balances():
    ex_adapters = get_active_exchanges_map()
    for name, adapter in ex_adapters.items():
        print(f"--- Checking {name} ---")
        try:
            # Sync time first
            if hasattr(adapter, 'sync_server_time'):
                await adapter.sync_server_time()
                
            # fetch_balance
            balance = await adapter.exchange.fetch_balance()
            
            usdt_bal = balance.get('USDT', {})
            total = usdt_bal.get('total', 0)
            free = usdt_bal.get('free', 0)
            print(f"USDT Total: {total}")
            print(f"USDT Free: {free}")
            
            # Check positions
            pos = await adapter.fetch_positions()
            active_pos = [p for p in pos if float(p.get('contracts', 0)) > 0]
            print(f"Active Positions: {len(active_pos)}")
            for p in active_pos:
                print(f"  {p['symbol']}: {p['side']} {p['contracts']} @ {p.get('entryPrice')}")
                
        except Exception as e:
            print(f"Error checking {name}: {e}")
            import traceback
            # traceback.print_exc()
    
    # Close connections
    for adapter in ex_adapters.values():
        await adapter.close()

if __name__ == "__main__":
    asyncio.run(check_balances())
