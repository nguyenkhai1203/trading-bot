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
        Fetch latest candles and merge with historical data.
        Keeps a rolling window of MAX_CANDLES for backtesting.
        """
        import os
        MAX_CANDLES = 1000  # Enough for EMA 200 and 70/30 split
        
        for symbol in symbols:
            for tf in timeframes:
                key = f"{symbol}_{tf}"
                try:
                    # Fetch only 50 latest candles (enough for signals)
                    ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=50)
                    if not ohlcv:
                        continue
                        
                    new_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                    
                    # Load existing historical data
                    safe_symbol = symbol.replace('/', '').replace(':', '')
                    file_path = os.path.join('data', f"{safe_symbol}_{tf}.csv")
                    
                    if os.path.exists(file_path):
                        old_df = pd.read_csv(file_path)
                        old_df['timestamp'] = pd.to_datetime(old_df['timestamp'])
                        
                        # Merge: keep old + add new, remove duplicates
                        combined = pd.concat([old_df, new_df]).drop_duplicates(subset='timestamp', keep='last')
                        combined = combined.sort_values('timestamp').tail(MAX_CANDLES)
                    else:
                        combined = new_df
                    
                    self.data_store[key] = combined
                    
                    # Save back to disk
                    os.makedirs('data', exist_ok=True)
                    combined.to_csv(file_path, index=False)
                    
                except Exception as e:
                    print(f"Fetch error {key}: {e}")
                
                await asyncio.sleep(0.1) 

    def get_data(self, symbol, timeframe):
        return self.data_store.get(f"{symbol}_{timeframe}")

    async def close(self):
        await self.exchange.close()
