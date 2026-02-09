#!/usr/bin/env python3
"""
Bulk Data Downloader for Trading Bot
Downloads 5000+ candles per symbol/timeframe for robust backtesting
"""

import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import os
import sys
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from config import BINANCE_API_KEY, BINANCE_API_SECRET, USE_TESTNET

async def download_historical_data(symbol, timeframe, limit=5000):
    """Download OHLCV data for a symbol/timeframe pair."""
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            }
        })
        
        print(f"[*] Downloading {symbol} {timeframe} ({limit} candles)...")
        
        # Fetch historical data in batches (1000 per request max)
        all_ohlcv = []
        since = exchange.milliseconds() - (300 * 24 * 60 * 60 * 1000)  # 300 days back
        
        while len(all_ohlcv) < limit:
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                print(f"    {len(all_ohlcv)} candles fetched...", end='\r')
                await asyncio.sleep(0.2)  # Rate limit
            except Exception as e:
                print(f"    Error: {e}")
                break
        
        # Keep only latest 'limit' candles
        all_ohlcv = all_ohlcv[-limit:]
        
        # Convert to DataFrame
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # Save to CSV
        safe_symbol = symbol.replace('/', '').upper()
        filename = f"data/{safe_symbol}_{timeframe}.csv"
        os.makedirs('data', exist_ok=True)
        df.to_csv(filename, index=False)
        
        print(f"[OK] {symbol:12s} {timeframe:3s} -> {len(df):5d} candles saved to {filename}")
        
        await exchange.close()
        return True
        
    except Exception as e:
        print(f"[ERROR] {symbol} {timeframe}: {e}")
        return False

async def main():
    """Main download routine."""
    # Major pairs that need data expansion
    symbols = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT']
    
    # All timeframes including new 2h & 8h
    timeframes = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    
    print("=" * 80)
    print("TRADING BOT - BULK DATA DOWNLOADER")
    print(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {len(symbols)} symbols x {len(timeframes)} timeframes = {len(symbols)*len(timeframes)} files")
    print("=" * 80)
    print()
    
    tasks = []
    for symbol in symbols:
        for timeframe in timeframes:
            tasks.append(download_historical_data(symbol, timeframe, limit=5000))
            await asyncio.sleep(0.1)  # Stagger requests
    
    # Run downloads concurrently with rate limiting
    results = await asyncio.gather(*tasks)
    
    print()
    print("=" * 80)
    success_count = sum(results)
    print(f"[OK] Download Complete: {success_count}/{len(results)} files successfully downloaded")
    print(f"End: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Download cancelled by user")
    except Exception as e:
        print(f"[ERROR] {e}")
