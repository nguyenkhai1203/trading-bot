"""
Cancel all duplicate/orphan orders on Binance.
Keeps only the orders that match positions.json.
"""

import os
import sys
import json
import ccxt
import time
from dotenv import load_dotenv

def main():
    # Load env
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))
    
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    
    if not api_key:
        print("❌ Missing API credentials")
        return
    
    print("="*80)
    print("CLEANUP DUPLICATE ORDERS")
    print("="*80)
    
    # Read positions.json to get valid order IDs
    positions_file = os.path.join(script_dir, 'src', 'positions.json')
    try:
        with open(positions_file, 'r') as f:
            positions = json.load(f)
        
        valid_order_ids = set()
        for pos in positions.values():
            valid_order_ids.add(str(pos.get('order_id')))
            if pos.get('sl_order_id'):
                valid_order_ids.add(str(pos.get('sl_order_id')))
            if pos.get('tp_order_id'):
                valid_order_ids.add(str(pos.get('tp_order_id')))
        
        print(f"\n📄 Valid order IDs from positions.json: {len(valid_order_ids)}")
        for oid in sorted(valid_order_ids):
            print(f"  - {oid}")
    except Exception as e:
        print(f"❌ Error reading positions.json: {e}")
        return
    
    # Connect to Binance
    print("\n🌐 Connecting to Binance...")
    try:
        exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        # Sync time
        server_time = exchange.fetch_time()
        local_time = int(time.time() * 1000)
        offset = server_time - local_time
        exchange.options['timeDifference'] = offset
        print(f"✅ Time synced (offset: {offset}ms)")
        
        # Fetch all orders
        orders = exchange.fetch_open_orders()
        print(f"✅ Found {len(orders)} open orders\n")
        
        # Find orphan orders
        orphan_orders = []
        for order in orders:
            order_id = str(order['id'])
            if order_id not in valid_order_ids:
                orphan_orders.append(order)
        
        print(f"🔍 Analysis:")
        print(f"  - Valid orders: {len(orders) - len(orphan_orders)}")
        print(f"  - Orphan orders: {len(orphan_orders)}")
        
        if not orphan_orders:
            print("\n✅ No orphan orders found!")
            return
        
        print(f"\n⚠️  Orphan orders to cancel:")
        for o in orphan_orders:
            print(f"  - {o['symbol']} {o['type']} {o['side']} @ {o.get('price', 'N/A')} (ID: {o['id']})")
        
        # Ask for confirmation
        print(f"\n❓ Cancel {len(orphan_orders)} orphan orders? (y/n): ", end='')
        response = input().strip().lower()
        
        if response != 'y':
            print("❌ Cancelled by user")
            return
        
        # Cancel orphan orders
        print(f"\n🗑️  Cancelling {len(orphan_orders)} orders...")
        cancelled = 0
        for order in orphan_orders:
            try:
                exchange.cancel_order(order['id'], order['symbol'])
                print(f"  ✅ Cancelled {order['symbol']} {order['id']}")
                cancelled += 1
                time.sleep(0.2)  # Rate limit
            except Exception as e:
                print(f"  ❌ Failed to cancel {order['id']}: {e}")
        
        print(f"\n✅ Cleanup complete! Cancelled {cancelled}/{len(orphan_orders)} orders")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "="*80)

if __name__ == '__main__':
    main()
