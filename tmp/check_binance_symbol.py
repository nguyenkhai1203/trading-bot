
import asyncio
import ccxt.async_support as ccxt
import os
from dotenv import load_dotenv

async def check_binance_symbol():
    load_dotenv()
    api_key = os.getenv('BINANCE_API_KEY')
    secret = os.getenv('BINANCE_API_SECRET')
    
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'options': {'defaultType': 'future'}
    })
    
    symbol_with_suffix = 'ETH/USDT:USDT'
    symbol_standard = 'ETH/USDT'
    
    print(f"Checking Binance symbol: {symbol_with_suffix}")
    try:
        # Check if symbol is in markets
        markets = await exchange.load_markets()
        print(f"Symbol '{symbol_with_suffix}' in markets: {symbol_with_suffix in markets}")
        print(f"Symbol '{symbol_standard}' in markets: {symbol_standard in markets}")
        
        # Try fetch_ohlcv
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol_with_suffix, timeframe='15m', limit=1)
            print(f"fetch_ohlcv('{symbol_with_suffix}'): SUCCESS (Got {len(ohlcv)} candles)")
        except Exception as e:
            print(f"fetch_ohlcv('{symbol_with_suffix}'): FAILED - {e}")
            
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol_standard, timeframe='15m', limit=1)
            print(f"fetch_ohlcv('{symbol_standard}'): SUCCESS (Got {len(ohlcv)} candles)")
        except Exception as e:
            print(f"fetch_ohlcv('{symbol_standard}'): FAILED - {e}")
            
    finally:
        await exchange.close()

if __name__ == '__main__':
    asyncio.run(check_binance_symbol())
