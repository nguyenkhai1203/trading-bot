import asyncio
import random
from data_fetcher import DataFetcher
from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES
import os
import pandas as pd

# Limit concurrency to avoid rate limits
SEMAPHORE_LIMIT = 3
semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

async def download_symbol_tf(symbol, tf, data_dir):
    async with semaphore:
        safe_symbol = symbol.replace('/', '').replace(':', '')
        filename = os.path.join(data_dir, f"{safe_symbol}_{tf}.csv")
        
        print(f"Downloading {symbol} {tf}...")
        fetcher = DataFetcher(symbol=symbol, timeframe=tf)
        try:
            # Random sleep to jitter requests
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            # Fetch 1000 candles
            df = await fetcher.fetch_ohlcv(limit=1000)
            if df is not None and not df.empty:
                df.to_csv(filename, index=False)
                print(f"  ✅ Saved {len(df)} rows to {filename}")
            else:
                print(f"  ❌ Failed to fetch data for {symbol} {tf}")
        except Exception as e:
            print(f"  💥 Error downloading {symbol} {tf}: {e}")
            # Wait a bit longer if error
            await asyncio.sleep(5)
        finally:
            await fetcher.close()

async def download_all_data():
    data_dir = 'data'
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        
    tasks = []
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            tasks.append(download_symbol_tf(symbol, tf, data_dir))
            
    # Run in batches or gathered with semaphore
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(download_all_data())
