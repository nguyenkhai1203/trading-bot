# -*- coding: utf-8 -*-
"""
Orphan Management Utility
Finds and optionally cancels orders on the exchange that are NOT tracked in the database.
Replaces legacy check_orphans.py and cleanup_orphans.py.
"""
import asyncio
import os
import sys
import argparse
from typing import List, Set

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from database import DataManager
from exchange_factory import create_adapter_from_profile
from utils.symbol_helper import to_raw_format

async def manage_orphans(env: str, execute: bool = False):
    print("=" * 60)
    print(f"🕵️  SCANNING FOR ORPHANED ORDERS [{env}]")
    print("=" * 60)

    db = await DataManager.get_instance(env)
    profiles = await db.get_profiles()
    
    if not profiles:
        print(f"❌ No active profiles found for {env} environment.")
        return

    # 1. Collect all known Order IDs from Database
    known_order_ids = set()
    known_symbols = set()
    trade_count = 0

    for p in profiles:
        active_trades = await db.get_active_positions(p['id'])
        trade_count += len(active_trades)
        for t in active_trades:
            known_symbols.add(t['symbol'])
            for key in ['exchange_order_id', 'sl_order_id', 'tp_order_id']:
                val = t.get(key)
                if val:
                    known_order_ids.add(str(val))

    print(f"📦 Tracking {trade_count} active trades with {len(known_order_ids)} order IDs across {len(profiles)} profiles.")

    # 2. Check each profile's exchange for orphans
    total_orphans = 0
    orphans_to_cancel = [] # List of (adapter, order_id, symbol)

    for p in profiles:
        print(f"\n🏦 Checking Profile: {p['name']} ({p['exchange']})")
        adapter = await create_adapter_from_profile(p)
        if not adapter:
            print(f"   ❌ Failed to create adapter for {p['name']}")
            continue

        try:
            # Sync time for CCXT
            if hasattr(adapter, 'sync_time'):
                await adapter.sync_time()

            # Fetch ALL open orders from exchange
            # Note: fetch_open_orders in adapters might be symbol-specific or global
            # For orphans, we want global if possible.
            try:
                # Some exchangers support global fetch_open_orders()
                open_orders = await adapter.exchange.fetch_open_orders()
            except:
                # Fallback: scan known symbols
                open_orders = []
                for sym in known_symbols:
                    try:
                        orders = await adapter.exchange.fetch_open_orders(sym)
                        open_orders.extend(orders)
                    except:
                        pass
            
            p_orphans = []
            for o in open_orders:
                oid = str(o['id'])
                if oid not in known_order_ids:
                    p_orphans.append(o)
                    orphans_to_cancel.append((adapter, oid, o['symbol']))
            
            if p_orphans:
                print(f"   🚨 Found {len(p_orphans)} orphan(s):")
                for o in p_orphans:
                    print(f"      - {o['symbol']} {o['side']} {o['type']} (ID: {o['id']})")
                total_orphans += len(p_orphans)
            else:
                print("   ✅ No orphans found.")

        except Exception as e:
            print(f"   ❌ Error checking {p['name']}: {e}")
        finally:
            # We don't close adapter here if we need to execute below, 
            # but we should ensure they are closed eventually.
            pass

    print("\n" + "=" * 60)
    if total_orphans == 0:
        print("✅ SYSTEM CLEAN. No orphaned orders found.")
    else:
        print(f"📊 Total Orphans Found: {total_orphans}")
        
        if execute:
            confirm = input(f"💥 CANCEL THESE {total_orphans} ORDERS? (Type 'yes' to confirm): ")
            if confirm.lower().strip() == 'yes':
                print("\n🗑️  Cancelling...")
                for adapter, oid, symbol in orphans_to_cancel:
                    try:
                        await adapter.exchange.cancel_order(oid, symbol)
                        print(f"   ✅ Cancelled {oid} ({symbol})")
                    except Exception as e:
                        print(f"   ❌ Failed {oid}: {e}")
                print("\n✨ Cleanup complete.")
            else:
                print("\n❌ Execution cancelled by user.")
        else:
            print("\n💡 Tip: Run with --execute to cancel these orders.")
    
    # Cleanup adapters
    for adapter, _, _ in orphans_to_cancel:
        try: await adapter.close()
        except: pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage orphaned exchange orders.")
    parser.add_argument("--env", type=str, default="LIVE", choices=["LIVE", "TEST"], help="Environment to scan (LIVE/TEST)")
    parser.add_argument("--execute", action="store_true", help="Actually cancel the found orphans")
    
    args = parser.parse_args()
    asyncio.run(manage_orphans(args.env, args.execute))
