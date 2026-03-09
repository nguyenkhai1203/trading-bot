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
from src.config import ACTIVE_EXCHANGE, OHLCV_REFRESH_INTERVAL

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
            from src.infrastructure.adapters.exchange_factory import get_active_exchanges_map
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
        self._ticker_cache = {}    # { 'EXCHANGE_symbol': {'last': price, 'timestamp': ts} }
        self._stagger_index = 0    # Current group index for staggered updates
        self._cooldown_manager = None
        self._active_symbols_provider = None # Callable returning list of symbols
        self._ohlcv_sync_state = {} # { key: last_period_timestamp }
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

    def set_cooldown_manager(self, cm):
        self._cooldown_manager = cm
        
    def set_active_symbols_provider(self, provider):
        self._active_symbols_provider = provider

    async def fetch_ticker(self, symbol, exchange=None):
        ex_name = exchange or (list(self.adapters.keys())[0] if self.adapters else None)
        if not ex_name: return None
        
        # Check cache (TTL 2s)
        cache_key = f"{ex_name}_{symbol}"
        cached = self._ticker_cache.get(cache_key)
        if cached and (time.time() - cached['timestamp'] < 2.0):
            return {'last': cached['last'], 'symbol': symbol}

        adapter = self.adapters.get(ex_name)
        if not adapter: return None
        
        ticker = await adapter.fetch_ticker(symbol)
        if ticker and 'last' in ticker:
            self._ticker_cache[cache_key] = {'last': float(ticker['last']), 'timestamp': time.time()}
        return ticker

    async def update_tickers(self, symbols):
        """Fetch latest prices and update the last candle close in data_store."""
        curr_time = time.time()
        if not hasattr(self, '_last_ticker_update'): self._last_ticker_update = 0
        if curr_time - self._last_ticker_update < 1.0: return 0
        self._last_ticker_update = curr_time
        
        from src import config
        exchange_symbol_map = {'BINANCE': config.BINANCE_SYMBOLS, 'BYBIT': config.BYBIT_SYMBOLS}
        
        total_updated = 0
        for name, adapter in self.adapters.items():
            try:
                allowed = exchange_symbol_map.get(name, symbols)
                current = [s for s in symbols if s in allowed]
                if not current: continue
                
                tickers = await adapter.fetch_tickers(current)
                curr_ts = time.time()
                for symbol, ticker_data in tickers.items():
                    last_price = ticker_data.get('last')
                    if last_price:
                        # Update Cache
                        self._ticker_cache[f"{name}_{symbol}"] = {'last': float(last_price), 'timestamp': curr_ts}
                        
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
        
        # Reduced throttle to 10s to allow staggered groups to run faster
        STAGGER_INTERVAL = 10 
        if not force and (curr_time - self._last_ohlcv_update < STAGGER_INTERVAL):
            return False 

        self._last_ohlcv_update = curr_time
        MAX_CANDLES = 1000 
        self._update_counter += 1
        
        from src import config
        exchange_symbol_map = {'BINANCE': config.BINANCE_SYMBOLS, 'BYBIT': config.BYBIT_SYMBOLS}
        
        # 1. Prioritization Logic
        # Active symbols (positions/orders) update EVERY cycle (~10-15s)
        # Background symbols update in STAGGERED groups (~40-60s)
        active_symbols = set()
        if self._active_symbols_provider:
            try:
                active_symbols = set(self._active_symbols_provider())
            except:
                pass
        
        GROUP_COUNT = 4
        all_symbols = sorted(list(symbols))
        background_symbols = [s for s in all_symbols if s not in active_symbols]
        
        group_size = (len(background_symbols) + GROUP_COUNT - 1) // GROUP_COUNT
        start_idx = (self._stagger_index % GROUP_COUNT) * group_size
        end_idx = start_idx + group_size
        background_batch = background_symbols[start_idx:end_idx]
        self._stagger_index += 1
        
        # Combined batch: Priorities + Staggered group
        current_batch = sorted(list(active_symbols.union(set(background_batch))))
        
        self.logger.info(f"🔄 Data Sync: {len(active_symbols)} Priorities + Group {self._stagger_index % GROUP_COUNT} ({len(background_batch)} symbols)")
        
        semaphore = asyncio.Semaphore(5)
        
        async def fetch_and_store(name, adapter, symbol, timeframes):
            async with semaphore:
                try:
                    # 2. Skip if symbol is in SL cooldown
                    if self._cooldown_manager and self._cooldown_manager.is_in_cooldown(name, symbol):
                        self.logger.debug(f"⏳ Skipping {symbol} (SL Cooldown)")
                        return
                    
                    # 3. Smart Sync: Only fetch if a new candle period has started
                    # Otherwise, update_tickers() already handles live bridging.
                    main_tf = timeframes[0]
                    key_base = f"{name}_{symbol}_{main_tf}"
                    
                    # Convert '1h', '15m' to seconds
                    unit = main_tf[-1]
                    val = int(main_tf[:-1])
                    seconds = val * 60 if unit == 'm' else val * 3600 if unit == 'h' else val * 86400 if unit == 'd' else 60
                    
                    now_ts = time.time()
                    current_period_start = (int(now_ts) // seconds) * seconds
                    last_sync_period = self._ohlcv_sync_state.get(key_base, 0)
                    
                    # If we already synced THIS period start, and we have data, skip heavy fetch
                    if current_period_start <= last_sync_period and key_base in self.data_store:
                        return

                    # 4. Timeframe Deduplication: Fetch only the lowest timeframe (e.g., 1h)
                    ohlcv = await adapter.fetch_ohlcv(symbol, main_tf, limit=50)
                    if not ohlcv: return
                    
                    self._ohlcv_sync_state[key_base] = current_period_start
                        
                    new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                    last_price = float(new_df.iloc[-1]['close'])
                    
                    for tf in timeframes:
                        key = f"{name}_{symbol}_{tf}"
                        # If it's the main TF, merge properly
                        if tf == main_tf:
                            old_df = self.data_store.get(key)
                            if old_df is None: combined = new_df
                            else:
                                combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last').reset_index(drop=True)
                                combined = combined.sort_values('timestamp').reset_index(drop=True).tail(MAX_CANDLES)
                        else:
                            # If it's a higher TF (4h), just update the last candle close with the 1h close
                            combined = self.data_store.get(key)
                            if combined is not None and not combined.empty:
                                combined.iloc[-1, combined.columns.get_loc('close')] = last_price
                            else:
                                # Fallback: if 4h is empty, we must fetch it once
                                h_ohlcv = await adapter.fetch_ohlcv(symbol, tf, limit=50)
                                if h_ohlcv:
                                    combined = pd.DataFrame(h_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                    combined['timestamp'] = pd.to_datetime(combined['timestamp'], unit='ms')
                        
                        if combined is not None:
                            is_valid, reason = self.validate_data(combined, symbol, tf)
                            if is_valid:
                                self.data_store[key] = combined
                                if key in self.features_cache: del self.features_cache[key]
                                
                                # DB Persist (only for the fetched TF)
                                if tf == main_tf:
                                    candles_list = []
                                    for _, row in new_df.iterrows():
                                        ts = int(row['timestamp'].timestamp() * 1000)
                                        candles_list.append([ts, row['open'], row['high'], row['low'], row['close'], row['volume']])
                                    await self.db.upsert_candles(symbol, tf, candles_list)
                except Exception as e:
                    self.logger.error(f"[{name}] Error updating {symbol}: {e}")

        tasks = []
        for name, adapter in self.adapters.items():
            allowed = [s for s in current_batch if s in exchange_symbol_map.get(name, [])]
            for symbol in allowed:
                # Group all timeframes for this symbol together
                tasks.append(fetch_and_store(name, adapter, symbol, timeframes))
        
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
