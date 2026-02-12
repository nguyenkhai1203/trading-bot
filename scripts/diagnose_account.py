"""
Diagnostic script to dump everything from Binance account to find "hidden" orders.
"""
import asyncio
import os
import sys
import json
import ccxt.async_support as ccxt
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
load_dotenv()
from config import BINANCE_API_KEY, BINANCE_API_SECRET

async def diagnose():
    print("🔍 STARTING FULL ACCOUNT DIAGNOSIS...")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True}
    })
    
    try:
        await exchange.load_time_difference()
        
        # 1. Basic Account Info
        print("\n--- [1] Account Configuration ---")
        try:
            account_config = await exchange.fapiPrivateGetAccountConfig()
            print(f"Hedge Mode: {account_config.get('dualSidePosition')}")
            print(f"Multi-Asset Mode: {account_config.get('multiAssetsMargin')}")
        except Exception as e: print(f"Error: {e}")

        # 2. Balances (only non-zero)
        print("\n--- [2] Open Positions (Positions with Risk) ---")
        try:
            positions = await exchange.fetch_positions()
            active_pos = [p for p in positions if float(p.get('contracts', 0)) > 0]
            if not active_pos:
                print("No active positions found.")
            for p in active_pos:
                print(f"📍 {p['symbol']} | {p['side']} | Size: {p['contracts']} | Entry: {p['entryPrice']}")
        except Exception as e: print(f"Error: {e}")

        # 3. Open Orders (Standard)
        print("\n--- [3] Open Orders (Global fetch) ---")
        try:
            open_orders = await exchange.fapiPrivateGetOpenOrders()
            print(f"Total Open Orders Found: {len(open_orders)}")
            for o in open_orders:
                print(f"📋 {o['symbol']} | {o['type']} | {o['side']} | Price: {o.get('price','N/A')} | Stop: {o.get('stopPrice','N/A')} | ID: {o['orderId']}")
        except Exception as e: print(f"Error: {e}")

        # 4. Check Coin-M as well (just in case user is confused)
        print("\n--- [4] Checking COIN-M Futures ---")
        coin_exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY, 'secret': BINANCE_API_SECRET,
            'options': {'defaultType': 'delivery'}
        })
        try:
            coin_orders = await coin_exchange.fetch_open_orders()
            print(f"Total COIN-M Open Orders: {len(coin_orders)}")
            for o in coin_orders:
                print(f"📋 {o['symbol']} | {o['id']}")
        except Exception: pass
        finally: await coin_exchange.close()

        print("\n--- [5] Checking SPOT Orders ---")
        spot_exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY, 'secret': BINANCE_API_SECRET,
            'options': {'defaultType': 'spot'}
        })
        try:
            spot_orders = await spot_exchange.fetch_open_orders()
            print(f"Total SPOT Open Orders: {len(spot_orders)}")
            for o in spot_orders:
                print(f"📋 {o['symbol']} | {o['id']}")
        except Exception: pass
        finally: await spot_exchange.close()

        print("\n--- DIAGNOSIS END ---")
        
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(diagnose())
