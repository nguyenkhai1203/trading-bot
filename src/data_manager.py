import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from config import BINANCE_API_KEY, BINANCE_API_SECRET, USE_TESTNET

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
        self.listeners = []

    def _initialize_exchange(self):
        # ... logic similar to DataFetcher ...
        exchange_class = ccxt.binance # Default to Binance
        
        config = {
            'enableRateLimit': True,
            'options': {'defaultType': 'future', 'adjustForTimeDifference': True}
        }
        if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY:
            config['apiKey'] = BINANCE_API_KEY
            config['secret'] = BINANCE_API_SECRET
            
        exchange = exchange_class(config)
        if USE_TESTNET: exchange.set_sandbox_mode(True)
        return exchange

    async def fetch_ticker(self, symbol):
        try:
             ticker = await self.exchange.fetch_ticker(symbol)
             return ticker
        except Exception as e:
            print(f"Error fetching ticker {symbol}: {e}")
            return None

    async def update_data(self, symbols, timeframes):
        """
        Fetch data for all symbol/timeframe combinations sequentially (or semi-parallel)
        to respect rate limits.
        """
        for symbol in symbols:
            for tf in timeframes:
                key = f"{symbol}_{tf}"
                try:
                    ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
                    if ohlcv:
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        self.data_store[key] = df
                        
                        # SAVE TO DISK for Analyzer (Layer 2)
                        import os
                        os.makedirs('data', exist_ok=True)
                        safe_symbol = symbol.replace('/', '').replace(':', '')
                        file_path = os.path.join('data', f"{safe_symbol}_{tf}.csv")
                        df.to_csv(file_path, index=False)
                except Exception as e:
                    print(f"Fetch error {key}: {e}")
                
                # Small sleep between requests to avoid 429
                await asyncio.sleep(0.1) 

    def get_data(self, symbol, timeframe):
        return self.data_store.get(f"{symbol}_{timeframe}")

    async def close(self):
        await self.exchange.close()
