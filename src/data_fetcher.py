import ccxt.async_support as ccxt  # Use async version for performance/WS integration
import asyncio
import pandas as pd
import os
import time
from config import BINANCE_API_KEY, BINANCE_API_SECRET

class DataFetcher:
    def __init__(self, exchange_id='binance', symbol='BTC/USDT', timeframe='1h'):
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = self._initialize_exchange()

    def _initialize_exchange(self):
        exchange_class = getattr(ccxt, self.exchange_id)
        
        # Check if keys are set and look valid
        is_placeholder = 'your_api_key' in str(BINANCE_API_KEY) or 'your_api_secret' in str(BINANCE_API_SECRET)
        has_keys = BINANCE_API_KEY and len(BINANCE_API_KEY) > 10 and BINANCE_API_SECRET and not is_placeholder
        
        config = {
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            }
        }
        
        if has_keys:
            config['apiKey'] = BINANCE_API_KEY
            config['secret'] = BINANCE_API_SECRET
        else:
            print("WARNING: Invalid or missing Binance API keys. Running in Public Data Only mode.")

        exchange = exchange_class(config)
        
        # NOTE: Testnet support is DEPRECATED for Binance Futures - removed sandbox mode
        return exchange

    async def fetch_ticker(self):
        """Fetches the latest ticker data."""
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            return ticker
        except Exception as e:
            print(f"Error fetching ticker: {e}")
            return None

    async def start_stream(self, callback):
        """Simulates a WebSocket stream by polling ticker/OHLCV."""
        print(f"Starting simulated data stream for {self.symbol}...")
        while True:
            try:
                ticker = await self.fetch_ticker()
                if ticker:
                    await callback(ticker)
                await asyncio.sleep(1) # Poll every 1 second
            except Exception as e:
                print(f"Stream error: {e}")
                await asyncio.sleep(5)

    async def fetch_ohlcv(self, limit=100):
        """Fetches OHLCV data."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            print(f"Error fetching OHLCV: {e}")
            return None

    async def fetch_historical_data(self, days=30):
        """Fetches historical data and saves to CSV."""
        # Calculate start time
        since = self.exchange.milliseconds() - (days * 24 * 60 * 60 * 1000)
        all_ohlcv = []
        
        print(f"Fetching historical data for {self.symbol} since {pd.to_datetime(since, unit='ms')}...")
        
        while since < self.exchange.milliseconds():
            try:
                ohlcv = await self.exchange.fetch_ohlcv(self.symbol, self.timeframe, since=since, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                # Small delay to respect rate limits
                # await asyncio.sleep(0.1) 
            except Exception as e:
                print(f"Error fetching historical data: {e}")
                break
        
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # Save to CSV
        filename = f"data_{self.symbol.replace('/', '_').replace(':', '_')}_{self.timeframe}.csv"
        df.to_csv(filename, index=False)
        print(f"Saved {len(df)} rows to {filename}")
        return df

    async def close(self):
        await self.exchange.close()

# Example usage for testing
if __name__ == '__main__':
    async def main():
        fetcher = DataFetcher()
        print("Fetching Ticker...")
        ticker = await fetcher.fetch_ticker()
        print(f"Ticker: {ticker['last'] if ticker else 'Error'}")
        
        print("\nFetching OHLCV...")
        df = await fetcher.fetch_ohlcv(limit=5)
        print(df)
        
        # Uncomment to test history download
        # await fetcher.fetch_historical_data(days=1)
        
        await fetcher.close()

    try:
        asyncio.run(main())
    except Exception as e:
        print(e)
