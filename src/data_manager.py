import asyncio
import os
import sys

# Add src to path if running directly or from root
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.append(src_dir)
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
import os
import time
from config import ACTIVE_EXCHANGE
# Adapters are now handled by exchange_factory

class MarketDataManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
             cls._instance = super(MarketDataManager, cls).__new__(cls)
             cls._instance.initialized = False
        return cls._instance

    def __init__(self, adapters=None):
        if hasattr(self, 'initialized') and self.initialized: return
        self.initialized = True
        
        # Initialize or use provided multiple Exchange Adapters
        if adapters:
            self.adapters = adapters
        else:
            from exchange_factory import get_active_exchanges_map
            self.adapters = get_active_exchanges_map()
        
        # Backward compatibility: set self.adapter/self.exchange to the first one
        if self.adapters:
            first_name = list(self.adapters.keys())[0]
            self.adapter = self.adapters[first_name]
            self.exchange = self.adapter
        else:
            self.adapter = None
            self.exchange = None
        
        self.data_store = {} # { 'EXCHANGE_symbol_timeframe': df }
        self.features_cache = {}  # { 'EXCHANGE_symbol_timeframe': df_with_features }
        self._last_ohlcv_update = 0.0  # Timestamp for throttled candle updates
        self.listeners = []
        self._isolated_margin_set = False
        self._update_counter = 0  # Track cycles for periodic disk save
        self._feature_engineer = None  # Lazy init shared FeatureEngineer
    
    def _get_feature_engineer(self):
        """Lazy load single shared FeatureEngineer."""
        if self._feature_engineer is None:
            from feature_engineering import FeatureEngineer
            self._feature_engineer = FeatureEngineer()
        return self._feature_engineer

    async def sync_server_time(self):
        """Delegate time sync to adapter."""
        return await self.adapter.sync_time()

    def get_synced_timestamp(self):
        """Delegate timestamp generation to adapter."""
        return self.adapter.get_synced_timestamp()

    async def set_isolated_margin_mode(self, symbols, exchange=None):
        """Sets margin mode to ISOLATED for given symbols (Live only)."""
        from config import DRY_RUN
        
        target_adapter = self.adapters.get(exchange) if exchange else self.adapter
        
        # Check apiKey via adapter proxy
        if DRY_RUN or not target_adapter or not target_adapter.apiKey:
            return
            
        failed_symbols = []
        for symbol in symbols:
            try:
                # If Binance and missing keys, skip margin setup (it will fail anyway)
                is_binance = target_adapter.name == 'BINANCE'
                from config import BINANCE_API_KEY
                if is_binance and (not BINANCE_API_KEY or 'your_' in BINANCE_API_KEY):
                    # Public mode: skip setup, don't fail
                    continue

                # Use Adapter method
                await target_adapter.set_margin_mode(symbol, 'ISOLATED')
                print(f"✅ [{target_adapter.name}] {symbol} set to ISOLATED margin")
            except Exception as e:
                err_str = str(e).lower()
                # Check for critical API errors that imply symbol is invalid for this key
                # Binance: -2014, -2015, "api-key format invalid", "permission denied"
                # Bybit: 10003 ("api key invalid"), 33004 ("api key expired"), "permission denied"
                if ("api-key" in err_str and "invalid" in err_str) or \
                   ("permission" in err_str and "denied" in err_str) or \
                   "code': -2014" in err_str or \
                   "code': 10003" in err_str or \
                   "code': 33004" in err_str:
                     print(f"⚠️ [{target_adapter.name}] Skipping {symbol}: Invalid permissions/key format.")
                     failed_symbols.append(symbol)
                elif "no need to change" in err_str or "already" in err_str:
                    pass # harmless
                else:
                    # Log other errors but maybe don't blacklist immediately unless repeated? 
                    # For now, user said "warn once and skip", so we blacklist on API errors.
                    pass
        
        return failed_symbols

    async def close(self):
        """Close all exchange connections."""
        if hasattr(self, 'adapters') and self.adapters:
            for name, adapter in self.adapters.items():
                print(f"🔌 Closing connection to {name}...")
                await adapter.close()
        elif hasattr(self, 'adapter') and self.adapter:
             await self.adapter.close()
             
        self.initialized = False

    async def fetch_ticker(self, symbol, exchange=None):
        """Fetch ticker deeply delegated to adapter."""
        adapter = self.adapters.get(exchange) if exchange else self.adapter
        return await adapter.fetch_ticker(symbol)

    async def fetch_ohlcv_with_retry(self, symbol, timeframe, limit=50, exchange=None):
        """Fetch OHLCV data deeply delegated to adapter."""
        adapter = self.adapters.get(exchange) if exchange else self.adapter
        return await adapter.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def update_tickers(self, symbols):
        """Fetch latest prices for all symbols across all exchanges."""
        curr_time = time.time()
        if not hasattr(self, '_last_ticker_update'): self._last_ticker_update = 0
        if curr_time - self._last_ticker_update < 1.0:
            return 0
        self._last_ticker_update = curr_time
        
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {
            'BINANCE': BINANCE_SYMBOLS,
            'BYBIT': BYBIT_SYMBOLS
        }
        
        total_updated = 0
        for name, adapter in self.adapters.items():
            try:
                allowed_symbols = exchange_symbol_map.get(name, symbols)
                current_symbols = [s for s in symbols if s in allowed_symbols]
                if not current_symbols: continue
                
                tickers = await adapter.fetch_tickers(current_symbols)
                for symbol, ticker_data in tickers.items():
                    last_price = ticker_data.get('last')
                    if last_price:
                        # Update the 'close' price of the latest candle for all timeframes
                        for key, df in self.data_store.items():
                            if key.startswith(f"{name}_{symbol}_") and not df.empty:
                                df.iloc[-1, df.columns.get_loc('close')] = last_price
                total_updated += len(tickers)
            except Exception as e:
                print(f"⚠️ [{name}] Ticker update failed: {e}")
        return total_updated

    async def update_data(self, symbols, timeframes, force=False):
        """
        Fetch latest candles from all active exchanges and merge with historical data.
        """
        from config import OHLCV_REFRESH_INTERVAL
        curr_time = time.time()
        if not force and (curr_time - self._last_ohlcv_update < OHLCV_REFRESH_INTERVAL):
            return False 

        self._last_ohlcv_update = curr_time
        MAX_CANDLES = 1000 
        self._update_counter += 1
        save_to_disk = (self._update_counter % 10 == 0)
        
        self.features_cache.clear()
        
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {
            'BINANCE': BINANCE_SYMBOLS,
            'BYBIT': BYBIT_SYMBOLS
        }
        
        for name, adapter in self.adapters.items():
            print(f"📡 [{name}] Updating market data...")
            # Filter symbols for this specific exchange
            allowed_symbols = exchange_symbol_map.get(name, symbols)
            current_symbols = [s for s in symbols if s in allowed_symbols]
            
            for symbol in current_symbols:
                for tf in timeframes:
                    key = f"{name}_{symbol}_{tf}"
                    try:
                        ohlcv = await adapter.fetch_ohlcv(symbol, tf, limit=50)
                        if not ohlcv: continue
                            
                        new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                        
                        old_df = self.data_store.get(key)
                        if old_df is None:
                            # Try disk
                            safe_symbol = symbol.replace('/', '').replace(':', '')
                            file_path = os.path.join('data', f"{name}_{safe_symbol}_{tf}.csv")
                            if os.path.exists(file_path):
                                old_df = pd.read_csv(file_path)
                                old_df['timestamp'] = pd.to_datetime(old_df['timestamp'])
                        
                        if old_df is not None:
                            combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last').reset_index(drop=True)
                            combined = combined.sort_values('timestamp').reset_index(drop=True).tail(MAX_CANDLES)
                        else:
                            combined = new_df
                        
                        # Validate before storing
                        is_valid, reason = self.validate_data(combined, symbol, tf)
                        if is_valid:
                            self.data_store[key] = combined
                            if save_to_disk:
                                safe_symbol = symbol.replace('/', '').replace(':', '')
                                os.makedirs('data', exist_ok=True)
                                combined.to_csv(os.path.join('data', f"{name}_{safe_symbol}_{tf}.csv"), index=False)
                        else:
                            # Keep old data if new merge is invalid
                            print(f"⚠️ [{name}] Skipped update for {symbol} {tf}: {reason}")
                            if key not in self.data_store and old_df is not None:
                                self.data_store[key] = old_df

                    except Exception as e:
                        print(f"⚠️ [{name}] Error updating {symbol} {tf}: {e}")
        
        if save_to_disk:
            print(f"💾 Data saved to disk (cycle {self._update_counter})")
        
        return True

    async def load_from_disk(self, symbols, timeframes):
        """Load historical data from disk only (for dry_run mode)."""
        loaded = 0
        for name, adapter in self.adapters.items():
            for symbol in symbols:
                for tf in timeframes:
                    key = f"{name}_{symbol}_{tf}"
                    safe_symbol = symbol.replace('/', '').replace(':', '')
                    file_path = os.path.join('data', f"{name}_{safe_symbol}_{tf}.csv")
                    
                    if os.path.exists(file_path):
                        try:
                            df = pd.read_csv(file_path)
                            df['timestamp'] = pd.to_datetime(df['timestamp'])
                            self.data_store[key] = df
                            loaded += 1
                        except Exception as e:
                            print(f"Error loading {file_path}: {e}")
        
        print(f"✅ Loaded {loaded} data files from disk")

    def get_data(self, symbol, timeframe, exchange='BINANCE'):
        return self.data_store.get(f"{exchange}_{symbol}_{timeframe}")

    def validate_data(self, df, symbol, timeframe):
        """
        Validates the integrity of the DataFrame.
        Returns (bool, str) -> (is_valid, error_reason)
        """
        if df is None:
            return False, "DataFrame is None"
        if df.empty:
            return False, "DataFrame is empty"
        
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        if not all(col in df.columns for col in required_cols):
            return False, f"Missing columns: {[c for c in required_cols if c not in df.columns]}"
            
        # Check for excessive NaNs in critical columns (last 5 rows are most important for signals)
        last_rows = df.tail(5)
        if last_rows['close'].isnull().any():
            return False, "NaN values found in recent close prices"
            
        return True, "OK"

    def get_data_with_features(self, symbol, timeframe, exchange='BINANCE'):
        """Get data with features computed (cached per cycle)."""
        key = f"{exchange}_{symbol}_{timeframe}"
        
        # Return cached features if available
        if key in self.features_cache:
            return self.features_cache[key]
        
        # Get raw data
        df = self.data_store.get(key)
        
        # Validate data before processing
        is_valid, reason = self.validate_data(df, symbol, timeframe)
        if not is_valid:
            # print(f"⚠️ [{symbol} {timeframe}] Data invalid: {reason}") # Optional: verify noise level
            return None
        
        # Compute features and cache
        try:
            fe = self._get_feature_engineer()
            df_with_features = fe.calculate_features(df.copy())
            self.features_cache[key] = df_with_features
            return df_with_features
        except Exception as e:
            print(f"❌ [{symbol} {timeframe}] Feature calculation failed: {e}")
            return None

    async def _execute_with_timestamp_retry(self, api_call, *args, **kwargs):
        """Delegate retry logic to adapter (exposed for Trader)."""
        from base_exchange_client import BaseExchangeClient
        return await BaseExchangeClient._execute_with_timestamp_retry(self.adapter, api_call, *args, **kwargs)
    # Fix 13: Removed duplicate close() that was overriding the correct close() at line 113.
    # The correct close() properly iterates all adapters and resets self.initialized = False.
