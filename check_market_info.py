
import asyncio
import ccxt.async_support as ccxt
from config import BYBIT_API_KEY, BYBIT_API_SECRET
import json

async def main():
    bybit = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'swap'}
    })
    
    try:
        await bybit.load_markets()
        symbol = 'SOL/USDT'
        market = bybit.market(symbol)
        
        print(f"=== {symbol} Limits ===")
        print(f"Min Qty: {market['limits']['amount']['min']}")
        print(f"Max Qty: {market['limits']['amount']['max']}")
        print(f"Qty Step: {market['precision']['amount']}")
        print(f"Min Notional: {market['limits']['cost']['min']}")
        
        # Check specific raw info if possible
        if 'info' in market:
            print(f"Lot Size Filter: {market['info'].get('lotSizeFilter')}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await bybit.close()

if __name__ == "__main__":
    asyncio.run(main())
