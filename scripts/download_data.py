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
    ACTIVE_EXCHANGES, BINANCE_SYMBOLS, BYBIT_SYMBOLS,
    MACRO_SYMBOLS
)

async def download_historical_data(symbol, timeframe, exchange_name='BINANCE', limit=5000, semaphore=None):
    """Download OHLCV data for a symbol/timeframe pair with 5k candle limit and rate-limit safety."""
    if semaphore:
        async with semaphore:
            return await _download_inner(symbol, timeframe, exchange_name, limit)
    else:
        return await _download_inner(symbol, timeframe, exchange_name, limit)

def get_timeframe_seconds(timeframe: str) -> int:
    unit = timeframe[-1]
    val = int(timeframe[:-1])
    if unit == 'm': return val * 60
    if unit == 'h': return val * 3600
    if unit == 'd': return val * 86400
    return 60

async def _download_inner(symbol, timeframe, exchange_name, limit):
    safe_symbol = symbol.split(':')[0].replace('/', '').upper()
    filename = f"data/{exchange_name}_{safe_symbol}_{timeframe}.csv"
    
    # Bybit-specific timeframe mapping for API compatibility
    api_timeframe = timeframe
    if exchange_name.upper() == 'BYBIT':
        mapping = {'8h': '4h'} # Bybit doesn't support 8h, but we can aggregate 4h later or just use 4h
        api_timeframe = mapping.get(timeframe, timeframe)

    existing_df = None
    since_ts = None
    
    # 1. Calculate Target Window
    now_ms = int(time.time() * 1000)
    tf_seconds = get_timeframe_seconds(timeframe)
    window_ms = limit * tf_seconds * 1000
    target_since = now_ms - window_ms

    if os.path.exists(filename):
        try:
            existing_df = pd.read_csv(filename)
            if not existing_df.empty:
                existing_df['timestamp'] = pd.to_datetime(existing_df['timestamp'])
                last_ts = int(existing_df['timestamp'].iloc[-1].timestamp() * 1000)
                
                # If the gap is too large (> 5000 candles), we just restart from target_since
                if last_ts < target_since:
                    print(f"  [RESET] {exchange_name} {symbol} {timeframe} data is too old. Restarting from 5k window.")
                    since_ts = target_since
                else:
                    # Incremental catch-up
                    since_ts = last_ts + 1
                    print(f"  [CATCHUP] {exchange_name} {symbol} {timeframe} from {existing_df['timestamp'].iloc[-1]}")
        except Exception:
            existing_df = None

    if since_ts is None:
        since_ts = target_since

    exchange = None
    try:
        ex_class = getattr(ccxt, exchange_name.lower())
        ex_config = {
            'enableRateLimit': True,
            'options': { 'defaultType': 'future', 'adjustForTimeDifference': True }
        }
        if exchange_name.upper() == 'BINANCE' and BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY:
            ex_config.update({'apiKey': BINANCE_API_KEY, 'secret': BINANCE_API_SECRET})
        elif exchange_name.upper() == 'BYBIT' and BYBIT_API_KEY and 'your_' not in BYBIT_API_KEY:
            ex_config.update({'apiKey': BYBIT_API_KEY, 'secret': BYBIT_API_SECRET})
            
        exchange = ex_class(ex_config)
        await exchange.load_time_difference()
        
        all_ohlcv = []
        retry_count = 0
        current_since = since_ts
        
        while True:
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, api_timeframe, since=current_since, limit=1000)
                if not ohlcv:
                    break
                
                all_ohlcv.extend(ohlcv)
                last_candle_ts = ohlcv[-1][0]
                
                # Break if we reached current time (within 1 period)
                if last_candle_ts >= (now_ms - tf_seconds * 1000):
                    break
                
                current_since = last_candle_ts + 1
                
                # Moderate delay
                await asyncio.sleep(1.0 if exchange_name.upper() == 'BYBIT' else 0.5)
                retry_count = 0 # Reset retries on success
                
                # Safety break for massive downloads
                if len(all_ohlcv) > 20000:
                    break
                    
            except ccxt.RateLimitExceeded:
                retry_count += 1
                wait = min(retry_count * 10, 60)
                print(f"  [LIMIT] {exchange_name} rate limit. Waiting {wait}s...")
                await asyncio.sleep(wait)
                if retry_count > 3: break
            except Exception as e:
                print(f"  [ERROR] {exchange_name} {symbol}: {e}")
                break
        
        if not all_ohlcv and existing_df is None:
            return 0
            
        if all_ohlcv:
            new_df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
            
            if existing_df is not None:
                df = pd.concat([existing_df, new_df]).drop_duplicates(subset=['timestamp'], keep='last').sort_values('timestamp')
            else:
                df = new_df
        else:
            df = existing_df
            
        # 5. Strict 5k limit
        df = df.tail(limit)
        
        os.makedirs('data', exist_ok=True)
        df.to_csv(filename, index=False)
        print(f"  [OK] {exchange_name} {symbol:12s} {timeframe:3s} -> {len(df):5d} candles (Last: {df['timestamp'].iloc[-1]})")
        return 2 if all_ohlcv else 1

    except Exception as e:
        print(f"  [FAILURE] {symbol} {timeframe}: {e}")
        return 0
    finally:
        if exchange:
            await exchange.close()

async def main():
    """Main download routine with Global Semaphore(4)."""
    timeframes = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    exchange_symbols = { 
        'BINANCE': list(set(BINANCE_SYMBOLS + MACRO_SYMBOLS)), 
        'BYBIT': BYBIT_SYMBOLS 
    }
    
    all_tasks_args = []
    for ex_name in ACTIVE_EXCHANGES:
        ex_name = ex_name.strip().upper()
        symbols = exchange_symbols.get(ex_name, [])
        for symbol in symbols:
            for tf in timeframes:
                all_tasks_args.append((symbol, tf, ex_name))
    
    # Randomize to spread load across exchanges
    import random
    random.shuffle(all_tasks_args)
    
    total = len(all_tasks_args)
    print(f"[*] Starting download with GLOBAL SEMAPHORE(4). Total tasks: {total}")
    
    semaphore = asyncio.Semaphore(4)
    tasks = [download_historical_data(*args, limit=5000, semaphore=semaphore) for args in all_tasks_args]
    
    results = await asyncio.gather(*tasks)
    
    success_count = sum(1 for r in results if r > 0)
    actually_updated = sum(1 for r in results if r == 2)
    
    print("\n" + "=" * 80)
    print(f"[OK] Complete: {success_count}/{total} successful, {actually_updated} files updated.")
    print(f"End: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
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
