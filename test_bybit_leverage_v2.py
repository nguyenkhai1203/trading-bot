import ccxt.async_support as ccxt
import asyncio
import os
from dotenv import load_dotenv

async def test():
    load_dotenv()
    api_key = os.getenv('BYBIT_API_KEY')
    api_secret = os.getenv('BYBIT_API_SECRET')
    
    if not api_key:
        print("Missing Bybit Keys")
        return

    ex = ccxt.bybit({
        'apiKey': api_key,
        'secret': api_secret,
        'options': {'defaultType': 'swap'},
    })
    
    symbol = 'LTC/USDT'
    leverage = 5
    
    print(f"Testing with {symbol}, lev={leverage}")
    
    try:
        await ex.load_markets()
        print("Markets loaded.")
        
        ltc_markets = [id for id, m in ex.markets.items() if 'LTC' in id]
        print(f"LTC related markets: {ltc_markets}")
        
        # Test variation 1: Positional params
        print("\n--- Test 1: Positional params {'category': 'linear'} ---")
        try:
            res = await ex.set_leverage(leverage, symbol, {'category': 'linear'})
            print("Success:", res)
        except Exception as e:
            print("Failed:", e)
            
        # Test variation 2: Keyword params
        print("\n--- Test 2: Keyword params=extra ---")
        try:
            res = await ex.set_leverage(leverage, symbol, params={'category': 'linear'})
            print("Success:", res)
        except Exception as e:
            print("Failed:", e)

        # Test variation 3: No category (let CCXT infer)
        print("\n--- Test 3: No category ---")
        try:
            res = await ex.set_leverage(leverage, symbol)
            print("Success:", res)
        except Exception as e:
            print("Failed:", e)

        # Test variation 4: Native Bybit symbol LTCUSDT
        native_symbol = 'LTCUSDT'
        print(f"\n--- Test 4: Native symbol {native_symbol} ---")
        try:
            res = await ex.set_leverage(leverage, native_symbol, {'category': 'linear'})
            print("Success:", res)
        except Exception as e:
            print("Failed:", e)

        # Test variation 5: CCXT Linear symbol LTC/USDT:USDT
        ccxt_linear = 'LTC/USDT:USDT'
        print(f"\n--- Test 5: CCXT Unified symbol {ccxt_linear} ---")
        try:
            res = await ex.set_leverage(leverage, ccxt_linear, {'category': 'linear'})
            print("Success:", res)
        except Exception as e:
            print("Failed:", e)

    finally:
        await ex.close()

if __name__ == "__main__":
    asyncio.run(test())
