import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import os
import time
from config import BINANCE_API_KEY, BINANCE_API_SECRET, ACTIVE_EXCHANGE
from adapters.binance_adapter import BinanceAdapter
from adapters.bybit_adapter import BybitAdapter

class MarketDataManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
             cls._instance = super(MarketDataManager, cls).__new__(cls)
             cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if hasattr(self, 'initialized') and self.initialized: return
        self.initialized = True
        
        # Initialize Exchange Adapter based on Config
        print(f"🔌 Initializing Exchange Adapter: {ACTIVE_EXCHANGE}")
        if ACTIVE_EXCHANGE == 'BYBIT':
            self.adapter = BybitAdapter()
        else:
            # Default to Binance
            ccxt_exchange = self._initialize_exchange()
            self.adapter = BinanceAdapter(ccxt_exchange)
            
        self.exchange = self.adapter # Backward compatibility
        
        # self.exchange = self._initialize_exchange() # REMOVED
        # super().__init__(exchange)  # REMOVED
        
        self.data_store = {} # { 'symbol_timeframe': df }
        self.features_cache = {}  # { 'symbol_timeframe': df_with_features }
        self._last_ohlcv_update = 0  # Timestamp for throttled candle updates
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

    def _initialize_exchange(self):
        # ... logic similar to DataFetcher ...
        exchange_class = ccxt.binance # Default to Binance
        
        config = {
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', 
                'adjustForTimeDifference': False, # Disabled: Using manual offset in BaseExchangeClient
                'recvWindow': 60000,             # 60s max safety window
                'fetchMarkets': True,
                'warnOnFetchOpenOrdersWithoutSymbol': False # Suppress warning for global fetch
            }
        }
        if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY and len(BINANCE_API_KEY) > 10:
            config['apiKey'] = BINANCE_API_KEY
            config['secret'] = BINANCE_API_SECRET
        else:
            # If no key, we assume dry run and only use public endpoints
            print("⚠️ [WARN] No valid Binance API key found. Defaulting to public data only.")
            
        exchange = exchange_class(config)
        return exchange

    async def set_isolated_margin_mode(self, symbols):
        """Sets margin mode to ISOLATED for given symbols (Live only)."""
        from config import DRY_RUN
        # Check apiKey via adapter proxy
        if DRY_RUN or not self.adapter.apiKey:
            return
            
        for symbol in symbols:
            try:
                # Use Adapter method
                await self.adapter.set_margin_mode(symbol, 'ISOLATED')
                print(f"✅ {symbol} set to ISOLATED margin")
            except Exception as e:
                # Often fails if already set, which is fine
                pass

    async def fetch_ticker(self, symbol):
        """Fetch ticker deeply delegated to adapter."""
        return await self.adapter.fetch_ticker(symbol)

    async def fetch_ohlcv_with_retry(self, symbol, timeframe, limit=50, max_retries=3):
        """Fetch OHLCV data deeply delegated to adapter."""
        return await self.adapter.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def update_tickers(self, symbols):
        """Fetch latest prices for all symbols in 1 API call (Low weight)."""
        try:
            tickers = await self.adapter.fetch_tickers(symbols)
            for symbol, ticker_data in tickers.items():
                last_price = ticker_data.get('last')
                if last_price:
                    # Update the 'close' price of the latest candle for all timeframes
                    for key, df in self.data_store.items():
                        if key.startswith(f"{symbol}_") and not df.empty:
                            df.iloc[-1, df.columns.get_loc('close')] = last_price
            return len(tickers)
        except Exception as e:
            print(f"⚠️ [WARN] Ticker update failed: {e}")
            return 0

    async def update_data(self, symbols, timeframes, force=False):
        """
        Fetch latest candles and merge with historical data.
        OPTIMIZED: Only fetches if force=True or enough time passed (60s).
        """
        from config import OHLCV_REFRESH_INTERVAL
        curr_time = time.time()
        if not force and (curr_time - self._last_ohlcv_update < OHLCV_REFRESH_INTERVAL):
            return 

        self._last_ohlcv_update = curr_time
        MAX_CANDLES = 1000 
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
