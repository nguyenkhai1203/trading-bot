import sys
import os
import asyncio
import time
import json

# Add src to path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import ccxt.async_support as ccxt
from config import BINANCE_API_KEY, BINANCE_API_SECRET
from base_exchange_client import BaseExchangeClient

async def main():
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': False,
            'recvWindow': 60000,
            'warnOnFetchOpenOrdersWithoutSymbol': False
        }
    })

    client = BaseExchangeClient(exchange)
    
    try:
        print("⏰ Synchronizing time with Binance...")
        await client.sync_server_time()
        
        print("🔍 Fetching all open orders from Binance Futures...")
        
        # 1. Standard Orders (Basic Tab)
        std_orders_raw = await client._execute_with_timestamp_retry(exchange.fapiPrivateGetOpenOrders)
        
        # 2. Algo Orders (Conditional Tab)
        algo_orders_raw = await client._execute_with_timestamp_retry(exchange.fapiPrivateGetOpenAlgoOrders)
        
        print(f"\n--- [TOTAL SUMMARY] ---")
        print(f"Standard Open Orders (Basic): {len(std_orders_raw)}")
        print(f"Algo/Conditional Orders: {len(algo_orders_raw)}")
        
        if std_orders_raw:
            print(f"\n--- [STANDARD ORDERS (Basic Tab)] ---")
            for o in std_orders_raw:
                symbol = o.get('symbol')
                oid = o.get('orderId')
                type_ = o.get('type')
                side = o.get('side')
                price = o.get('price')
                stop_price = o.get('stopPrice', '0')
                print(f"Symbol: {symbol} | ID: {oid} | Type: {type_} | Side: {side} | Price: {price} | StopPrice: {stop_price}")
        
        if algo_orders_raw:
            print(f"\n--- [ALGO ORDERS (Conditional Tab)] ---")
            for o in algo_orders_raw:
                # Based on raw output: {'algoId': '...', 'symbol': '...', 'algoType': '...', 'side': '...', 'stopPrice': '...', ...}
                oid = o.get('algoId') or o.get('orderId')
                symbol = o.get('symbol')
                side = o.get('side')
                stop_price = o.get('stopPrice') or o.get('triggerPrice')
                type_ = o.get('algoType') or o.get('type')
                print(f"Symbol: {symbol} | ID: {oid} | Type: {type_} | Side: {side} | StopPrice: {stop_price}")

        # Check Active Positions
        positions = await client._execute_with_timestamp_retry(exchange.fetch_positions)
        active = [p for p in positions if float(p.get('contracts', 0) or 0) > 0 or float(p.get('size', 0) or 0) > 0]
        if active:
            print(f"\n--- [ACTIVE POSITIONS (With Sizes)] ---")
            for p in active:
                contracts = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if abs(contracts) > 0:
                    print(f"Symbol: {p['symbol']} | Size: {contracts} | Entry: {p['entryPrice']}")

    except Exception as e:
        import traceback
        print(f"❌ Error: {e}")
        traceback.print_exc()
    finally:
        await exchange.close()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
