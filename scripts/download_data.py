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
import time
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, 
    BYBIT_API_KEY, BYBIT_API_SECRET,
    ACTIVE_EXCHANGES, BINANCE_SYMBOLS, BYBIT_SYMBOLS
)

async def download_historical_data(symbol, timeframe, exchange_name='BINANCE', limit=5000):
    """Download OHLCV data for a symbol/timeframe pair with 1h freshness check."""
    # Strip :USDT suffix -> BTC/USDT:USDT -> BTCUSDT
    safe_symbol = symbol.split(':')[0].replace('/', '').upper()
    filename = f"data/{exchange_name}_{safe_symbol}_{timeframe}.csv"
    
    existing_df = None
    since_ts = None
    
    # [v2.5] Incremental Fetch Logic & Freshness check
    if os.path.exists(filename):
        mtime = os.path.getmtime(filename)
        age_seconds = time.time() - mtime
        if age_seconds < 3600:  # 1 hour
            print(f"  [SKIP] {exchange_name} {symbol} {timeframe} is fresh ({age_seconds/60:.1f}m old)")
            return 1  # Skipped (fresh)
            
        try:
            existing_df = pd.read_csv(filename)
            if not existing_df.empty:
                existing_df['timestamp'] = pd.to_datetime(existing_df['timestamp'])
                # Get the last timestamp in ms to fetch from
                last_ts = existing_df['timestamp'].iloc[-1].timestamp() * 1000
                since_ts = int(last_ts)
                print(f"  [INCREMENTAL] {exchange_name} {symbol} {timeframe} has {len(existing_df)} candles. Fetching from {existing_df['timestamp'].iloc[-1]}")
        except Exception as e:
            print(f"  [WARN] Could not read existing {filename}: {e}. Will re-download full history.")
            existing_df = None

    try:
        # Bybit-specific timeframe mapping
        if exchange_name.upper() == 'BYBIT':
            mapping = {'8h': '4h'}
            timeframe = mapping.get(timeframe, timeframe)

        ex_class = getattr(ccxt, exchange_name.lower())
        config = {
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            }
        }
        if exchange_name.upper() == 'BINANCE':
            if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY:
                config['apiKey'] = BINANCE_API_KEY
                config['secret'] = BINANCE_API_SECRET
        elif exchange_name.upper() == 'BYBIT':
            if BYBIT_API_KEY and 'your_' not in BYBIT_API_KEY:
                config['apiKey'] = BYBIT_API_KEY
                config['secret'] = BYBIT_API_SECRET
            
        exchange = ex_class(config)
        
        # [v2.4] Robust Time Sync for Binance/Bybit
        # Fetch server time and calculate offset to prevent -1021 error
        if exchange_name.upper() in ['BINANCE', 'BYBIT']:
            exchange.options['recvWindow'] = 60000 # Max safety window
            await exchange.load_time_difference()
            print(f"  [TIME] {exchange_name} offset: {exchange.options.get('timeDifference', 0)}ms")
        
        print(f"[*] Downloading {symbol} {timeframe} ({limit} candles)...")
        
        # Fetch historical data in batches (1000 per request max)
        all_ohlcv = []
        # If incremental, fetch from `since_ts`. Else fetch 300 days back.
        since = since_ts if since_ts else (exchange.milliseconds() - (300 * 24 * 60 * 60 * 1000))
        
        # For incremental updates, we just need to catch up, not necessarily fetch 5000 candles from `since_ts`.
        target_limit = limit if not since_ts else 5000 
        
        while len(all_ohlcv) < target_limit:
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                # Next batch from the last candle's timestamp + 1ms to avoid dupes
                since = ohlcv[-1][0] + 1
                if since_ts:
                    print(f"    {len(all_ohlcv)} new candles fetched...", end='\r')
                else:
                    print(f"    {len(all_ohlcv)} candles fetched...", end='\r')
                    
                # Break early if we only got a partial page (meaning we've hit the present)
                if len(ohlcv) < 1000:
                    break
                    
                delay = 1.0 if exchange_name.upper() == 'BYBIT' else 0.2
                await asyncio.sleep(delay)
            except Exception as e:
                print(f"    Error: {e}")
                break
        
        if not all_ohlcv:
            if existing_df is not None:
                print(f"  [INFO] {exchange_name} {symbol:12s} {timeframe:3s} -> No new data, keeping existing.")
                return 1 # Skipped/Fresh
            else:
                print(f"  [WARN] {exchange_name} {symbol:12s} {timeframe:3s} -> NO DATA fetched")
                return False

        new_df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
        
        if existing_df is not None:
            # Merge and deduplicate based on timestamp
            df = pd.concat([existing_df, new_df])
            df = df.drop_duplicates(subset=['timestamp'], keep='last')
            df = df.sort_values('timestamp')
        else:
            df = new_df
            
        # Enforce maximum historical limit
        df = df.tail(limit)
        
        # Save to CSV
        os.makedirs('data', exist_ok=True)
        df.to_csv(filename, index=False)
        
        print(f"[OK] {exchange_name} {symbol:12s} {timeframe:3s} -> {len(df):5d} candles saved to {filename}")
        return 2  # Actually downloaded
        
    except Exception as e:
        print(f"[ERROR] {symbol} {timeframe}: {e}")
        return 0  # Failed
    finally:
        if 'exchange' in locals() and exchange:
            await exchange.close()

async def main():
    """Main download routine."""
    import time
    timeframes = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    
    # Map exchange name to its specific symbols
    exchange_symbols = {
        'BINANCE': BINANCE_SYMBOLS,
        'BYBIT': BYBIT_SYMBOLS
    }
    
    all_tasks = []
    for ex_name in ACTIVE_EXCHANGES:
        ex_name = ex_name.strip().upper()
        symbols = exchange_symbols.get(ex_name, [])
        for symbol in symbols:
            for timeframe in timeframes:
                all_tasks.append((symbol, timeframe, ex_name))
    
    total = len(all_tasks)
    success_count = 0
    
    # Dynamic batch size based on exchange
    # Bybit is stricter with rate limits, so we process it slower or in smaller batches
    # If mixed, we stick to conservative limits
    is_bybit_active = 'BYBIT' in [x.upper() for x in ACTIVE_EXCHANGES]
    batch_size = 4 if is_bybit_active else 10
    
    print(f"[*] Starting download with batch size: {batch_size}")
    
    for i in range(0, total, batch_size):
        batch = all_tasks[i:i+batch_size]
        print(f"\n[Batch {i//batch_size + 1}/{(total + batch_size - 1)//batch_size}] Processing {len(batch)} downloads...")
        
        # Create tasks
        tasks = []
        for symbol, timeframe, ex_name in batch:
            limit = 5000
            tasks.append(download_historical_data(symbol, timeframe, ex_name, limit))
            
        results = await asyncio.gather(*tasks)
        # 2=downloaded, 1=skipped(fresh), 0=error
        actual_downloads = sum(1 for r in results if r == 2)
        success_count += sum(1 for r in results if r > 0)
        
        # Only sleep if we made real API calls this batch
        if i + batch_size < total and actual_downloads > 0:
            has_bybit = any(t[2] == 'BYBIT' for t in batch)
            wait_time = 5 if has_bybit else 2
            print(f"    Sleeping {wait_time}s to respect rate limits...")
            await asyncio.sleep(wait_time)
    
    print()
    print("=" * 80)
    print(f"[OK] Download Complete: {success_count}/{total} files successfully downloaded")
    print(f"End: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Download cancelled by user")
    except Exception as e:
        print(f"[ERROR] {e}")
