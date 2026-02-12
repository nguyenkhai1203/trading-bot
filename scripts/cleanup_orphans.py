"""
Script to CLEANUP ORPHANED orders matches check_orphans logic.
"""
import asyncio
import ccxt.async_support as ccxt
from dotenv import load_dotenv
import os
import json
import sys

# Ensure src path is in sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
load_dotenv()

async def cleanup_orphans():
    print("=" * 60)
    print("🧹 CLEANING UP ORPHANED ORDERS")
    print("=" * 60)

    # 1. Load Known IDs from positions.json
    known_ids = set()
    
    try:
        # Correct path to positions.json relative to script location
        json_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'positions.json')
        if os.path.exists(json_path):
            with open(json_path, 'r') as f:
                positions = json.load(f)
            print(f"📦 Loaded {len(positions)} positions from local file.")
            
            for key, pos in positions.items():
                if pos.get('order_id'): known_ids.add(str(pos.get('order_id')))
                if pos.get('sl_order_id'): known_ids.add(str(pos.get('sl_order_id')))
                if pos.get('tp_order_id'): known_ids.add(str(pos.get('tp_order_id')))
        else:
             print("⚠️ positions.json not found! CAUTION: This might delete ALL orders if not careful.")
             # Safety: If json missing, maybe don't delete anything automatically?
             # But orphans are defined as orders NOT in json.
             # Better safe: Ask user confirmation.
             
    except Exception as e:
        print(f"❌ Error loading positions.json: {e}")
        return

    print(f"ℹ️  Tracking {len(known_ids)} Order IDs locally (Do NOT delete these).")
    
    # 2. Exchange Setup
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    
    try:
        await exchange.load_time_difference()
        
        # Scan Symbols
        scan_symbols = set()
        # From Config
        try:
            from config import TRADING_SYMBOLS
            for sym in TRADING_SYMBOLS: scan_symbols.add(sym)
        except: pass
        
        # From Exchange Pos
        ex_positions = await exchange.fetch_positions()
        for p in ex_positions:
            if float(p['contracts']) > 0:
                scan_symbols.add(p['symbol'])
        
        sorted_symbols = sorted(list(scan_symbols))
        print(f"ℹ️  Scanning {len(sorted_symbols)} symbols...")
        
        orphans = []
        
        for sym in sorted_symbols:
            try:
                orders = await exchange.fetch_open_orders(sym)
                for o in orders:
                    oid = str(o['id'])
                    if oid not in known_ids:
                        orphans.append(o)
            except: pass

        if not orphans:
            print("\n✅ NO ORPHANS FOUND. Clean!")
            return

        print(f"\n🚨 FOUND {len(orphans)} ORPHANED ORDERS TO CANCEL:")
        for o in orphans:
            print(f"  - {o['symbol']} {o['type']} {o['side']} ID: {o['id']}")
            
        print("\n" + "!"*60)
        confirm = input(f"💥 DELETE THESE {len(orphans)} ORDERS? (Type 'yes' to confirm): ")
        if confirm.lower().strip() != 'yes':
            print("❌ Cancelled.")
            return

        print("\n🗑️  Deleting...")
        deleted = 0
        for o in orphans:
            try:
                await exchange.cancel_order(o['id'], o['symbol'])
                print(f"   ✅ Deleted {o['id']}")
                deleted += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"   ❌ Failed {o['id']}: {e}")
        
        print(f"\n✨ Done. Deleted {deleted}/{len(orphans)} orphans.")
        
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(cleanup_orphans())
