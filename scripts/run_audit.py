"""
System Audit Script
-------------------
Script này thực hiện "Live Check" các kết nối quan trọng của Bot để đảm bảo
Bot nhận diện đúng dữ liệu từ sàn.

Checks:
1. Time Sync (Độ lệch thời gian)
2. Balance (Số dư)
3. Positions (Vị thế đang mở) - Quan trọng: Check Leverage thực tế
4. Open Orders (Lệnh chờ)
"""
import asyncio
import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
load_dotenv()

import ccxt.async_support as ccxt
from execution import Trader

async def audit():
    print("🔍 STARING SYSTEM AUDIT...\n")
    
    # Init Exchange
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True
        },
        'enableRateLimit': True
    })
    
    # Pre-sync time
    await exchange.load_time_difference()

    
    try:
        # 1. TIME SYNC
        print("1️⃣  Checking Time Sync...")
        local_time = exchange.milliseconds()
        server_time = await exchange.fetch_time()
        diff = server_time - local_time
        print(f"   ✅ Server Time: {server_time}")
        print(f"   ✅ Local Time:  {local_time}")
        print(f"   ⏱️  Offset:      {diff}ms (Acceptable < 1000ms)\n")

        # 2. BALANCE
        print("2️⃣  Checking Balance...")
        balance = await exchange.fetch_balance()
        usdt_free = balance['USDT']['free']
        usdt_total = balance['USDT']['total']
        print(f"   💰 USDT Free:  ${usdt_free:.2f}")
        print(f"   💰 USDT Total: ${usdt_total:.2f}\n")

        # 3. POSITIONS
        print("3️⃣  Scanning Active Positions (Source of Truth)...")
        positions = await exchange.fetch_positions()
        active = [p for p in positions if float(p['contracts']) > 0]
        
        if not positions:
             print("   ℹ️  No Active Positions found on Exchange.\n")
        else:
            # Filter for active
            active = [p for p in positions if float(p['contracts']) > 0]
            
            for p in active:
                sym = p['symbol'] # CCXT Unified Symbol e.g. FIL/USDT:USDT
                # Clean symbol for other fetches if needed
                clean_sym = sym.split(':')[0] 
                
                side = p['side'].upper()
                amt = float(p['contracts'])
                entry = float(p['entryPrice'])
                leverage = p.get('leverage', 'N/A')
                margin_type = p.get('marginType', 'N/A')
                pnl = float(p['unrealizedPnl'])
                
                print(f"   🟢 {sym} | {side} x{leverage} ({margin_type})")
                print(f"      Size: {amt} | Entry: {entry} | PnL: ${pnl:.2f}\n")
                
                # Fetch Open Orders for this symbol
                try:
                    orders = await exchange.fetch_open_orders(clean_sym)
                    if orders:
                        print(f"      📋 Orders for {clean_sym}:")
                        for o in orders:
                            o_type = o['type']
                            o_side = o['side'].upper()
                            o_price = o.get('price') or o.get('stopPrice')
                            o_id = o['id']
                            o_reduce = o.get('reduceOnly', False)
                            print(f"         - [{o_side}] {o_type} @ {o_price} (ID: {o_id}) | Reduce: {o_reduce}")
                    else:
                        print(f"      ⚠️  NO OPEN ORDERS for {clean_sym} (Check SL/TP!)")
                except Exception as e:
                    print(f"      ❌ Failed to fetch orders for {clean_sym}: {e}")
                print("-" * 40)

        print("\n")
        
        print("✅ AUDIT COMPLETE. This is exactly what the Bot sees.")
        
    except Exception as e:
        print(f"❌ AUDIT FAILED: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(audit())
