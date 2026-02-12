"""
DUMP RAW STATE (Positions + Orders)
"""
import asyncio
import ccxt.async_support as ccxt
from dotenv import load_dotenv
import os
import sys
import json

# Ensure src path is in sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
load_dotenv()

async def dump_raw():
    print("=" * 60)
    print("🕵️  DUMPING RAW BINANCE STATE")
    print("=" * 60)
    
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    
    try:
        await exchange.load_time_difference()
        
        # 1. RAW POSITIONS
        print("\n[1] FETCHING RAW POSITIONS (fapiPrivateGetPositionRisk)...")
        try:
            # Get ALL positions (including zero) using V2
            try:
                raw_positions = await exchange.fapiPrivateGetPositionRiskV2()
            except AttributeError:
                # Fallback if V2 method not found in this version of CCXT
                print("⚠️  V2 method not found, trying fetch_positions directly...")
                raw_positions = await exchange.fetch_positions()
                # fetch_positions returns parsed data, need to adapt printing
                for p in raw_positions:
                     # Remap to raw-like structure for consistency if needed, or just print
                     print(f"  👉 ACTIVE: {p['symbol']} | Size: {p['contracts']} | Entry: {p['entryPrice']} | Side: {p['side']}")
                raw_positions = [] # Skip loop below

            
            active_count = 0
            for p in raw_positions:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    active_count += 1
                    print(f"  👉 ACTIVE: {p['symbol']} | Size: {amt} | Entry: {p['entryPrice']} | UniPnL: {p['unRealizedProfit']} | Side: {p['positionSide']}")
                    # Dump full json for these
                    # print(json.dumps(p, indent=2))
            
            if active_count == 0:
                print("  ✅ NO ACTIVE RAW POSITIONS FOUND (All amts are 0).")
                
        except Exception as e:
            print(f"❌ Failed to fetch positions: {e}")

        # 2. RAW OPEN ORDERS (Loop for accuracy)
        print("\n[2] FETCHING RAW OPEN ORDERS (Looping all markets)...")
        try:
            markets = await exchange.load_markets()
            # USE ALL SYMBOLS (Filter for USDT futures to save time if needed, but safe to scan all)
            all_symbols = [s for s in markets.keys() if '/USDT' in s]
            print(f"  Scanning ALL {len(all_symbols)} symbols (this may take a moment)...")
            
            check_list = all_symbols # Brute force check
            
            # 1. Try global first again (sometimes works)
            try:
                g_orders = await exchange.fapiPrivateGetOpenOrders()
                all_orders.extend(g_orders)
            except: pass
            
            # 2. Loop specific symbols to be sure
            for sym in check_list:
                try:
                    # Normalized symbol for CCXT
                    ccxt_sym = sym.replace(':USDT', '') 
                    s_orders = await exchange.fetch_open_orders(ccxt_sym)
                    # Deduplicate by ID
                    existing_ids = set(o['orderId'] for o in all_orders)
                    for o in s_orders:
                        if o['id'] not in existing_ids: # CCXT uses 'id' string, raw uses 'orderId' int/str
                            # Convert ccxt order to raw dict structure for consistency if needed, or just append
                            # We'll just append and handle printing
                            all_orders.append(o)
                except Exception as e:
                    # print(f"  Failed scan {sym}: {e}")
                    pass

            basic_orders = []
            conditional_orders = []
            
            seen_ids = set()
            
            for o in all_orders:
                # Handle raw vs ccxt structure
                oid = str(o.get('orderId') or o.get('id'))
                if oid in seen_ids: continue
                seen_ids.add(oid)
                
                otype = str(o.get('type', '')).upper()
                side = o.get('side', '').upper()
                sym = o.get('symbol', '')
                
                if any(x in otype for x in ['STOP', 'TAKE', 'TRAIL']):
                    conditional_orders.append(o)
                else:
                    basic_orders.append(o)
            
            print(f"  📦 TOTAL UNIQUE ORDERS: {len(basic_orders) + len(conditional_orders)}")
            
            print(f"\n  --- BASIC ORDERS (Limit/Market) [{len(basic_orders)}] ---")
            for o in basic_orders:
                print(f"    {o['symbol']} {o['side']} {o['type']} | Price: {o.get('price')} | ID: {o.get('id') or o.get('orderId')}")

            print(f"\n  --- CONDITIONAL ORDERS (SL/TP) [{len(conditional_orders)}] ---")
            for o in conditional_orders:
                stype = o.get('type', 'UNKNOWN')
                stop_p = o.get('stopPrice') or o.get('info', {}).get('stopPrice')
                print(f"    {o['symbol']} {o['side']} {stype} | Stop: {stop_p} | ID: {o.get('id') or o.get('orderId')}")
                
        except Exception as e:
            print(f"❌ Failed to fetch orders: {e}")

    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(dump_raw())
