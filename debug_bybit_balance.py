import ccxt.async_support as ccxt
import asyncio
import os
import json
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
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    
    try:
        # Test 1: UNIFIED
        print("\n--- Testing accountType: UNIFIED ---")
        try:
            bal_unified = await ex.fetch_balance(params={'accountType': 'UNIFIED'})
            print(f"USDT Total: {bal_unified.get('total', {}).get('USDT')}")
            # Print raw to see structure
            # print(json.dumps(bal_unified['info'], indent=2))
        except Exception as e:
            print("Unified failed:", e)

        # Test 2: CONTRACT (Classic)
        print("\n--- Testing accountType: CONTRACT ---")
        try:
            bal_contract = await ex.fetch_balance(params={'accountType': 'CONTRACT'})
            print(f"USDT Total: {bal_contract.get('total', {}).get('USDT')}")
        except Exception as e:
            print("Contract failed:", e)

        # Test 3: SPOT
        print("\n--- Testing accountType: SPOT ---")
        try:
            bal_spot = await ex.fetch_balance(params={'accountType': 'SPOT'})
            print(f"USDT Total: {bal_spot.get('total', {}).get('USDT')}")
        except Exception as e:
            print("Spot failed:", e)

    finally:
        await ex.close()

if __name__ == "__main__":
    asyncio.run(test())
