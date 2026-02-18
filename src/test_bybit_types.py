import asyncio
import ccxt.async_support as ccxt
from config import BYBIT_API_KEY, BYBIT_API_SECRET

async def test():
    exchange = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
    })
    try:
        await exchange.load_markets()
        symbols = ['ADA/USDT', 'ADA/USDT:USDT']
        for sym in symbols:
            if sym in exchange.markets:
                m = exchange.markets[sym]
                print(f"Symbol: {sym} | Type: {m['type']} | Spot: {m['spot']} | Swap: {m.get('swap')}")
            else:
                print(f"Symbol: {sym} not found")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(test())
