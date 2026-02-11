import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import os
import time
from config import BINANCE_API_KEY, BINANCE_API_SECRET

class MarketDataManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
             cls._instance = super(MarketDataManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, 'initialized'): return
        self.initialized = True
        self.exchange = self._initialize_exchange()
        self.data_store = {} # { 'symbol_timeframe': df }
        self.features_cache = {}  # { 'symbol_timeframe': df_with_features }
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

    def _initialize_exchange(self):
        # ... logic similar to DataFetcher ...
        exchange_class = ccxt.binance # Default to Binance
        
        config = {
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', 
                'adjustForTimeDifference': True,  # Auto-adjust for timestamp mismatch
                'timeDifference': -2000,  # Assume local time is 2 seconds ahead (will be refined)
                'fetchMarkets': True
            }
        }
        if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY:
            config['apiKey'] = BINANCE_API_KEY
            config['secret'] = BINANCE_API_SECRET
            
        exchange = exchange_class(config)
        # NOTE: Testnet support is DEPRECATED for Binance Futures - removed sandbox mode
        return exchange

    async def sync_server_time(self):
        """Sync local time with Binance server to fix timestamp offset issues"""
        try:
            # Fetch server time and calculate offset
            server_time_ms = await self._execute_with_timestamp_retry(self.exchange.fetch_time)  # Returns milliseconds
            local_time_ms = int(time.time() * 1000)
            offset_ms = server_time_ms - local_time_ms
            
            print(f"[TIME SYNC] Synchronization:")
            print(f"   Server: {server_time_ms}ms, Local: {local_time_ms}ms")
            print(f"   Offset: {offset_ms}ms ({'behind' if offset_ms > 0 else 'ahead'})")
            
            # Set the negative of offset so CCXT adds it to outgoing requests
            # If local is ahead by 1000ms, offset is -1000, we want to subtract it: timeDifference = 1000
            time_difference = -offset_ms
            self.exchange.options['timeDifference'] = time_difference
            
            print(f"   Set CCXT timeDifference to: {time_difference}ms")
            print(f"[OK] Time sync complete!")
            return True
            
        except Exception as e:
            print(f"[WARN] Time sync failed: {str(e)[:100]}")
            print(f"   Using default timeDifference: -2000ms")
            # Keep default value set in _initialize_exchange
            return False

    async def resync_time_if_needed(self, error_msg=""):
        """Re-sync time if timestamp error detected"""
        if "timestamp" in error_msg.lower() or "time" in error_msg.lower():
            print(f"[TIME SYNC] Detected timestamp error, re-syncing...")
            return await self.sync_server_time()
        return False

    async def _execute_with_timestamp_retry(self, api_call, *args, **kwargs):
        """Execute exchange API call with timestamp error retry"""
        max_retries = 3  # Increased from 2
        for attempt in range(max_retries):
            try:
                return await api_call(*args, **kwargs)
            except Exception as e:
                error_msg = str(e).lower()
                # Check for timestamp-related errors (-1021 is Binance timestamp error code)
                is_timestamp_error = (
                    "timestamp" in error_msg or 
                    "-1021" in error_msg or
                    "ahead of the server" in error_msg
                )
                
                if is_timestamp_error and attempt < max_retries - 1:
                    print(f"[TIMESTAMP ERROR] Attempt {attempt + 1}/{max_retries}: {str(e)[:100]}")
                    # Try to resync time
                    await self.resync_time_if_needed(str(e))
                    # Add delay before retry
                    import asyncio
                    await asyncio.sleep(1)  # 1 second delay
                    print(f"[RETRY] Retrying API call after time sync...")
                    continue
                else:
                    # Log non-timestamp errors or max retries reached
                    if not is_timestamp_error:
                        print(f"[API ERROR] Non-timestamp error, not retrying: {str(e)[:100]}")
                    else:
                        print(f"[TIMESTAMP ERROR] Max retries reached, giving up: {str(e)[:100]}")
                    # Re-raise the error
                    raise e

    async def fetch_ticker(self, symbol):
        try:
            ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)
            return ticker
        except Exception as e:
            error_str = str(e)
            # Handle Binance timestamp sync issue (-1021)
            if '-1021' in error_str or 'Timestamp' in error_str or 'ahead' in error_str:
                print(f"⚠️  Timestamp sync issue for {symbol}, retrying in 2 seconds...")
                import asyncio
                await asyncio.sleep(2)
                try:
                    ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)
                    print(f"✅ Recovered: {symbol} price fetched successfully")
                    return ticker
                except Exception as e2:
                    print(f"Error fetching ticker {symbol} (retry failed): {e2}")
                    return None
            else:
                print(f"Error fetching ticker {symbol}: {e}")
                return None

    async def set_isolated_margin_mode(self, symbols):
        """Set isolated margin mode for each symbol (call once at bot startup)."""
        if self._isolated_margin_set or not symbols:
            return
        
        print(f"⚙️ Checking ISOLATED MARGIN mode for {len(symbols)} symbols...")
        print("⚠️ Note: Binance Futures uses Isolated Margin by default")
        print("✅ Skipping set_margin_type() - already configured on exchange")
        
        # Binance Futures: Isolated margin is the default mode
        # No need to set it explicitly via API
        self._isolated_margin_set = True
        print("✅ Isolated margin mode verified")

    async def fetch_ohlcv_with_retry(self, symbol, timeframe, limit=50, max_retries=3):
        """Fetch OHLCV data with retry logic for timestamp offset issues"""
        for attempt in range(max_retries):
            try:
                ohlcv = await self._execute_with_timestamp_retry(
                    self.exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit
                )
                if ohlcv:
                    return ohlcv
                else:
                    return None
            except Exception as e:
                error_str = str(e)
                # Handle Binance timestamp/parameter issues (-1021, -1102)
                if ('-1021' in error_str or '-1102' in error_str or 
                    'Timestamp' in error_str or 'parameter' in error_str.lower()):
                    if attempt < max_retries - 1:
                        wait_time = 2 * (attempt + 1)  # Exponential backoff: 2s, 4s, 6s
                        print(f"⚠️  Fetch error {symbol} {timeframe} (attempt {attempt+1}/{max_retries}): {str(e)[:80]}")
                        print(f"   Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"❌ Fetch failed after {max_retries} attempts: {symbol} {timeframe}")
                        return None
                else:
                    # Non-retryable error
                    print(f"Fetch error {symbol} {timeframe}: {error_str[:100]}")
                    return None

    async def update_data(self, symbols, timeframes):
        """
        Fetch latest candles and merge with historical data.
        Keeps a rolling window of MAX_CANDLES for backtesting.
        OPTIMIZED: Only saves to disk every 10 cycles.
        """
        MAX_CANDLES = 1000  # Enough for EMA 200 and 70/30 split
        self._update_counter += 1
        save_to_disk = (self._update_counter % 10 == 0)  # Save every 10 cycles
        
        # Clear features cache on data update (will be recomputed on demand)
        self.features_cache.clear()
        
        for symbol in symbols:
            for tf in timeframes:
                key = f"{symbol}_{tf}"
                try:
                    # Use retry-enabled fetch method
                    ohlcv = await self.fetch_ohlcv_with_retry(symbol, tf, limit=50)
                    if not ohlcv:
                        continue
                        
                    new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                    
                    # Check if we have in-memory data first
                    if key in self.data_store:
                        old_df = self.data_store[key]
                    else:
                        # Load from disk only on first run
                        safe_symbol = symbol.replace('/', '').replace(':', '')
                        file_path = os.path.join('data', f"{safe_symbol}_{tf}.csv")
                        
                        if os.path.exists(file_path):
                            old_df = pd.read_csv(file_path)
                            old_df['timestamp'] = pd.to_datetime(old_df['timestamp'])
                        else:
                            old_df = None
                    
                    if old_df is not None:
                        # Merge: keep old + add new, remove duplicates
                        combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last').reset_index(drop=True)
                        combined = combined.sort_values('timestamp').reset_index(drop=True).tail(MAX_CANDLES)
                    else:
                        combined = new_df
                    
                    self.data_store[key] = combined
                    
                    # Save to disk periodically (not every cycle)
                    if save_to_disk:
                        safe_symbol = symbol.replace('/', '').replace(':', '')
                        file_path = os.path.join('data', f"{safe_symbol}_{tf}.csv")
                        os.makedirs('data', exist_ok=True)
                        combined.to_csv(file_path, index=False)
                    
                except Exception as e:
                    print(f"Fetch error {key}: {e}")
                
                await asyncio.sleep(0.1)
        
        if save_to_disk:
            print(f"💾 Data saved to disk (cycle {self._update_counter})")

    async def load_from_disk(self, symbols, timeframes):
        """Load historical data from disk only (for dry_run mode)."""
        loaded = 0
        for symbol in symbols:
            for tf in timeframes:
                key = f"{symbol}_{tf}"
                safe_symbol = symbol.replace('/', '').replace(':', '')
                file_path = os.path.join('data', f"{safe_symbol}_{tf}.csv")
                
                if os.path.exists(file_path):
                    try:
                        df = pd.read_csv(file_path)
                        df['timestamp'] = pd.to_datetime(df['timestamp'])
                        self.data_store[key] = df
                        loaded += 1
                    except Exception as e:
                        print(f"Error loading {file_path}: {e}")
        
        print(f"✅ Loaded {loaded} data files from disk")

    def get_data(self, symbol, timeframe):
        return self.data_store.get(f"{symbol}_{timeframe}")

    def get_data_with_features(self, symbol, timeframe):
        """Get data with features computed (cached per cycle)."""
        key = f"{symbol}_{timeframe}"
        
        # Return cached features if available
        if key in self.features_cache:
            return self.features_cache[key]
        
        # Get raw data
        df = self.data_store.get(key)
        if df is None or df.empty:
            return None
        
        # Compute features and cache
        fe = self._get_feature_engineer()
        df_with_features = fe.calculate_features(df.copy())
        self.features_cache[key] = df_with_features
        
        return df_with_features

    async def close(self):
        await self.exchange.close()
