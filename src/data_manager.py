import asyncio
import os
import sys
import time
import pandas as pd
import numpy as np
import logging

# Add src to path if running directly or from root
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.append(src_dir)

import ccxt.async_support as ccxt
from config import ACTIVE_EXCHANGE, OHLCV_REFRESH_INTERVAL

class MarketDataManager:
    """
    Singleton Market Data Manager.
    Handles OHLCV fetching, synchronization across exchanges, and SQLite caching.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
             cls._instance = super(MarketDataManager, cls).__new__(cls)
             cls._instance.initialized = False
        return cls._instance

    def __init__(self, db=None, adapters=None):
        if hasattr(self, 'initialized') and self.initialized: return
        self.initialized = True
        self.db = db
        self.logger = logging.getLogger("MarketDataManager")
        
        # Initialize or use provided multiple Exchange Adapters
        if adapters:
            self.adapters = adapters
        else:
            from exchange_factory import get_active_exchanges_map
            self.adapters = get_active_exchanges_map()
        
        # Backward compatibility: set self.adapter to the first one
        if self.adapters:
            first_name = list(self.adapters.keys())[0]
            self.adapter = self.adapters[first_name]
        else:
            self.adapter = None
        
        self.data_store = {} # { 'EXCHANGE_symbol_timeframe': df }
        self.features_cache = {}  # { 'EXCHANGE_symbol_timeframe': df_with_features }
        self._last_ohlcv_update = 0.0
        self._update_counter = 0
        self._feature_engineer = None

    def _get_feature_engineer(self):
        if self._feature_engineer is None:
            from feature_engineering import FeatureEngineer
            self._feature_engineer = FeatureEngineer()
        return self._feature_engineer

    async def sync_server_time(self):
        return await self.adapter.sync_time()

    def get_synced_timestamp(self):
        return self.adapter.get_synced_timestamp()

    async def close(self):
        if hasattr(self, 'adapters') and self.adapters:
            for name, adapter in self.adapters.items():
                self.logger.info(f"🔌 Closing connection to {name}...")
                await adapter.close()
        self.initialized = False

    async def fetch_ticker(self, symbol, exchange=None):
        adapter = self.adapters.get(exchange) if exchange else self.adapter
        return await adapter.fetch_ticker(symbol)

    async def update_tickers(self, symbols):
        """Fetch latest prices and update the last candle close in data_store."""
        curr_time = time.time()
        if not hasattr(self, '_last_ticker_update'): self._last_ticker_update = 0
        if curr_time - self._last_ticker_update < 1.0: return 0
        self._last_ticker_update = curr_time
        
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {'BINANCE': BINANCE_SYMBOLS, 'BYBIT': BYBIT_SYMBOLS}
        
        total_updated = 0
        for name, adapter in self.adapters.items():
            try:
                allowed = exchange_symbol_map.get(name, symbols)
                current = [s for s in symbols if s in allowed]
                if not current: continue
                
                tickers = await adapter.fetch_tickers(current)
                for symbol, ticker_data in tickers.items():
                    last_price = ticker_data.get('last')
                    if last_price:
                        for key, df in self.data_store.items():
                            if key.startswith(f"{name}_{symbol}_") and not df.empty:
                                df.iloc[-1, df.columns.get_loc('close')] = last_price
                total_updated += len(tickers)
            except Exception as e:
                self.logger.warning(f"[{name}] Ticker update failed: {e}")
        return total_updated

    async def update_data(self, symbols, timeframes, force=False):
        """Fetch latest candles, merge with in-memory store, and persist to SQLite."""
        curr_time = time.time()
        if not force and (curr_time - self._last_ohlcv_update < OHLCV_REFRESH_INTERVAL):
            return False 

        self._last_ohlcv_update = curr_time
        MAX_CANDLES = 1000 
        self._update_counter += 1
        self.features_cache.clear() # Reset features per cycle
        
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {'BINANCE': BINANCE_SYMBOLS, 'BYBIT': BYBIT_SYMBOLS}
        
        semaphore = asyncio.Semaphore(5)
        
        async def fetch_and_store(name, adapter, symbol, tf):
            async with semaphore:
                key = f"{name}_{symbol}_{tf}"
                try:
                    # Fetch last 50 candles
                    ohlcv = await adapter.fetch_ohlcv(symbol, tf, limit=50)
                    if not ohlcv: return
                        
                    new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                    
                    # Merge with in-memory
                    old_df = self.data_store.get(key)
                    if old_df is None:
                        combined = new_df
                    else:
                        combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last').reset_index(drop=True)
                        combined = combined.sort_values('timestamp').reset_index(drop=True).tail(MAX_CANDLES)
                    
                    # Validate before storing
                    is_valid, reason = self.validate_data(combined, symbol, tf)
                    if is_valid:
                        self.data_store[key] = combined
                        # ASYNC PERSIST TO SQLite
                        candles_list = []
                        for _, row in new_df.iterrows():
                            ts = int(row['timestamp'].timestamp() * 1000)
                            candles_list.append([ts, row['open'], row['high'], row['low'], row['close'], row['volume']])
                        
                        await self.db.upsert_candles(symbol, tf, candles_list)
                    else:
                        self.logger.warning(f"[{name}] Skipped update for {symbol} {tf}: {reason}")
                except Exception as e:
                    self.logger.error(f"[{name}] Error updating {symbol} {tf}: {e}")

        tasks = []
        for name, adapter in self.adapters.items():
            self.logger.info(f"📡 [{name}] Updating market data...")
            allowed = exchange_symbol_map.get(name, symbols)
            current = [s for s in symbols if s in allowed]
            
            for symbol in current:
                for tf in timeframes:
                    tasks.append(fetch_and_store(name, adapter, symbol, tf))
        
        if tasks:
            await asyncio.gather(*tasks)
            
        return True

    async def load_historical_data(self, symbols, timeframes, limit=500):
        """Load data from SQLite cache into memory."""
        loaded = 0
        for name, adapter in self.adapters.items():
            for symbol in symbols:
                for tf in timeframes:
                    key = f"{name}_{symbol}_{tf}"
                    try:
                        candles = await self.db.get_candles(symbol, tf, limit=limit)
                        if candles:
                            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                            self.data_store[key] = df
                            loaded += 1
                    except Exception as e:
                        self.logger.error(f"Error loading {key} from DB: {e}")
        self.logger.info(f"✅ Loaded {loaded} datasets from SQLite cache")

    def get_data(self, symbol, timeframe, exchange='BINANCE'):
        return self.data_store.get(f"{exchange}_{symbol}_{timeframe}")

    def validate_data(self, df, symbol, timeframe):
        if df is None or df.empty: return False, "Empty DataFrame"
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        if not all(col in df.columns for col in required_cols): return False, "Missing columns"
        if df.tail(5)['close'].isnull().any(): return False, "NaN in recent close"
        return True, "OK"

    def get_data_with_features(self, symbol, timeframe, exchange='BINANCE'):
        key = f"{exchange}_{symbol}_{timeframe}"
        if key in self.features_cache: return self.features_cache[key]
        
        df = self.data_store.get(key)
        is_valid, reason = self.validate_data(df, symbol, timeframe)
        if not is_valid: return None
        
        try:
            fe = self._get_feature_engineer()
            df_with_features = fe.calculate_features(df.copy())
            self.features_cache[key] = df_with_features
            return df_with_features
        except Exception as e:
            self.logger.error(f"[{symbol} {timeframe}] Feature calculation failed: {e}")
            return None
