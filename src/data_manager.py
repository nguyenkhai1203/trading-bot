import asyncio
import os
import sys
import time
import pandas as pd
import numpy as np
import logging
from typing import Any, Dict, List, Optional, Callable, Set

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
                updated_keys = set()
                for symbol, ticker_data in tickers.items():
                    last_price = ticker_data.get('last')
                    if last_price:
                        last_price = float(last_price)
                        # Update Cache
                        self._ticker_cache[f"{name}_{symbol}"] = {'last': last_price, 'timestamp': curr_ts}
                        
                        for key, df in self.data_store.items():
                            if key.startswith(f"{name}_{symbol}_") and not df.empty:
                                self.data_store[key] = self._patch_current_candle(df, last_price)
                                updated_keys.add(key)
                
                # Refresh features for all patched DataFrames so signals (EMA, RSI) reflect the live price
                # We clear the features_cache so the next get_data_with_features() call triggers a recalc.
                fe = self._get_feature_engineer()
                if fe:
                    for key in updated_keys:
                        # Clear cache so get_data_with_features() doesn't return stale indicators
                        self.features_cache.pop(key, None)
                        
                        # Trigger immediate recalc in data_store if needed for shared state
                        # Note: We keep the raw data in data_store, and features in the cache.
                
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
                    if self._cooldown_manager and self._cooldown_manager.is_in_cooldown(name, symbol, 0):
                        self.logger.debug(f"⏳ Skipping {symbol} (SL Cooldown)")
                        return
                    
                    for tf in timeframes:
                        key = f"{name}_{symbol}_{tf}"
                        
                        # 3. Smart Sync: Only fetch if a new candle period has started
                        # Otherwise, update_tickers() handles live bridging for all timeframes.
                        if not self._should_fetch_new_candle(name, symbol, tf, adapter):
                            continue

                        # 4. Fetch OHLCV for this specific timeframe
                        ohlcv = await adapter.fetch_ohlcv(symbol, tf, limit=50)
                        if not ohlcv: continue
                        
                        seconds = self._get_timeframe_seconds(tf)
                        # We use exchange time if available for precision
                        now_ts_raw = (adapter.exchange.milliseconds() / 1000.0) if hasattr(adapter.exchange, 'milliseconds') and adapter.exchange.milliseconds() else time.time()
                        current_period_start = (int(now_ts_raw) // seconds) * seconds
                        self._ohlcv_sync_state[key] = current_period_start
                            
                        new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                        
                        old_df = self.data_store.get(key)
                        if old_df is None: 
                            combined = new_df
                        else:
                            # Merge new data and drop duplicates
                            combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last').reset_index(drop=True)
                            combined = combined.sort_values('timestamp').reset_index(drop=True).tail(MAX_CANDLES)
                        
                        is_valid, reason = self.validate_data(combined, symbol, tf)
                        if is_valid:
                            self.data_store[key] = combined
                            # Invalidate features cache so recalc happens on next access
                            self.features_cache.pop(key, None)
                            
                            # DB Persist
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

    def _get_timeframe_seconds(self, timeframe: str) -> int:
        unit = timeframe[-1]
        val = int(timeframe[:-1])
        if unit == 'm': return val * 60
        if unit == 'h': return val * 3600
        if unit == 'd': return val * 86400
        if unit == 'w': return val * 604800
        return 60

    def _patch_current_candle(self, df: pd.DataFrame, last_price: float) -> pd.DataFrame:
        """Patch the last row of the candle with the current price, updating high/low extremes."""
        if df is None or df.empty: return df
        last_idx = -1
        # Use col index for safety
        col_close = df.columns.get_loc('close')
        col_high = df.columns.get_loc('high')
        col_low = df.columns.get_loc('low')
        
        df.iloc[last_idx, col_close] = last_price
        if last_price > df.iloc[last_idx, col_high]:
            df.iloc[last_idx, col_high] = last_price
        if last_price < df.iloc[last_idx, col_low]:
            df.iloc[last_idx, col_low] = last_price
        return df

    def _should_fetch_new_candle(self, name: str, symbol: str, timeframe: str, adapter: Any) -> bool:
        """Determines if a full OHLCV fetch is needed based on boundary or data staleness."""
        key = f"{name}_{symbol}_{timeframe}"
        seconds = self._get_timeframe_seconds(timeframe)
        
        # 1. Sync boundary check (Exchange time)
        now_ts = (adapter.exchange.milliseconds() / 1000.0) if hasattr(adapter.exchange, 'milliseconds') and adapter.exchange.milliseconds() else time.time()
        current_period_start = (int(now_ts) // seconds) * seconds
        last_sync_period = self._ohlcv_sync_state.get(key, 0)
        
        if current_period_start > last_sync_period:
            return True
            
        # 2. Freshness Check
        df = self.data_store.get(key)
        if df is None or df.empty: return True
        
        last_candle_ts = df.iloc[-1]['timestamp'].timestamp()
        ticker_data = self._ticker_cache.get(f"{name}_{symbol}", {})
        ticker_age = now_ts - ticker_data.get('timestamp', 0)
        
        # Stale if data is older than 2 periods OR ticker hasn't updated in 30s
        if (now_ts - last_candle_ts > 2 * seconds) or (ticker_age > 30):
            return True
            
        return False

    def prune_caches(self, active_symbols: list):
        """Prune caches for symbols no longer in the active list to prevent memory bloat."""
        active_set = set(active_symbols)
        
        def _key_matches_any_symbol(k: str) -> bool:
            return any(sym in k for sym in active_set)
        
        # 1. Prune ticker cache
        stale_tickers = [k for k in list(self._ticker_cache.keys()) if not _key_matches_any_symbol(k)]
        for k in stale_tickers: 
            self._ticker_cache.pop(k, None)
            
        # 2. Prune features cache
        stale_features = [k for k in list(self.features_cache.keys()) if not _key_matches_any_symbol(k)]
        for k in stale_features:
            self.features_cache.pop(k, None)
            
        if stale_tickers or stale_features:
            self.logger.info(f"💾 Pruned {len(stale_tickers)} tickers and {len(stale_features)} feature sets from memory.")
