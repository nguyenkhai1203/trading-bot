"""
Script to find ORPHANED orders (exist on Exchange but NOT in positions.json)
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

async def find_orphans():
    print("=" * 60)
    print("🕵️  SCANNING FOR ORPHANED ORDERS")
    print("=" * 60)

    # 1. Load Known IDs from positions.json
    known_ids = set()
    known_symbols = set()
    
    try:
        with open('src/positions.json', 'r') as f:
            positions = json.load(f)
            
        print(f"📦 Loaded {len(positions)} positions from local file.")
        
        for key, pos in positions.items():
            sym = pos.get('symbol').replace(':USDT', '') # Normalize
            known_symbols.add(sym)
            
            # Entry Order ID
            if pos.get('order_id'): known_ids.add(str(pos.get('order_id')))
            
            # SL/TP Order IDs
            if pos.get('sl_order_id'): known_ids.add(str(pos.get('sl_order_id')))
            if pos.get('tp_order_id'): known_ids.add(str(pos.get('tp_order_id')))
            
    except Exception as e:
        print(f"❌ Error loading positions.json: {e}")
        return

    print(f"ℹ️  Tracking {len(known_ids)} Order IDs locally.")
    
    # 2. Fetch Open Orders from Exchange (Config Scan Strategy)
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {
            'defaultType': 'future',
            'warnOnFetchOpenOrdersWithoutSymbol': False
        },
        'enableRateLimit': True
    })
    
    all_orders = []
    scan_symbols = set()
    
    try:
        await exchange.load_time_difference()
        
        # A) Load symbols from Config
        try:
            from config import TRADING_SYMBOLS
            print(f"ℹ️  Loading symbols from config.py...")
            for sym in TRADING_SYMBOLS:
                scan_symbols.add(sym)
        except ImportError:
            print("⚠️  Could not import TRADING_SYMBOLS from src.config")

        # B) Load symbols from Active Positions on Exchange
        try:
            ex_positions = await exchange.fetch_positions()
            for p in ex_positions:
                if float(p.get('contracts', 0)) > 0:
                    scan_symbols.add(p['symbol'])
        except Exception as e:
            print(f"❌ Failed to fetch exchange positions: {e}")

        # C) Load known symbols from positions.json
        for s in known_symbols:
            scan_symbols.add(s)
            
        # Deduplicate and sort
        sorted_symbols = sorted(list(scan_symbols))
        print(f"ℹ️  Targeting {len(sorted_symbols)} unique symbols: {', '.join(sorted_symbols)}")
        print(f"⏳ Scanning each symbol one by one (this may take a moment)...")
        
        for sym in sorted_symbols:
            try:
                orders = await exchange.fetch_open_orders(sym)
                all_orders.extend(orders)
                if orders:
                    print(f"   ✅ {sym}: Found {len(orders)} orders")
            except Exception as e:
                # print(f"❌ Error scanning {sym}: {e}")
                pass

        # Process found orders
        print(f"\n🔍 Analyzing {len(all_orders)} total open orders...")
        
        # Deduplicate orders
        seen_ids = set()
        unique_orders = []
        for o in all_orders:
            if o['id'] not in seen_ids:
                seen_ids.add(o['id'])
                unique_orders.append(o)
        
        orphans = []
        stop_orders = 0
        limit_orders = 0
        
        for o in unique_orders:
            oid = str(o['id'])
            symbol = o['symbol']
            o_type = o.get('type')
            
            if o_type in ['STOP_MARKET', 'TAKE_PROFIT_MARKET']:
                stop_orders += 1
            else:
                limit_orders += 1
            
            # Check if known
            is_known = oid in known_ids
            
            if not is_known:
                orphans.append(o)
                
    finally:
        await exchange.close()
        
    print("\n" + "=" * 60)
    print(f"📊 SUMMARY:")
    print(f"  Total Open Orders Found: {len(unique_orders)}")
    print(f"  Limit/Market Orders: {limit_orders}")
    print(f"  Stop/Conditional Orders: {stop_orders}")
    
    if orphans:
        print(f"\n🚨 FOUND {len(orphans)} ORPHANED ORDERS:")
        for o in orphans:
            print(f"  - {o['symbol']} {o['type']} {o['side']} @ {o.get('price') or o.get('stopPrice')} (ID: {o['id']})")
        
        print("\nRun 'py cleanup_orphans.py' to delete them (I can create this script for you).")
    else:
        print("\n✅ NO ORPHANS FOUND. All orders are tracked.")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(find_orphans())
