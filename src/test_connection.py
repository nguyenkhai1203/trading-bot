import ccxt
import sys
import os

# Ensure src is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import BYBIT_API_KEY, BYBIT_API_SECRET, USE_TESTNET

def test_connection():
    print(f"Testing Connection to Bybit (Testnet: {USE_TESTNET})...")
    
    try:
        exchange = ccxt.bybit({
            'apiKey': BYBIT_API_KEY,
            'secret': BYBIT_API_SECRET,
            'options': {
                'defaultType': 'future',
            }
        })
        
        if USE_TESTNET:
            exchange.set_sandbox_mode(True)

        if not BYBIT_API_KEY or abs(len(BYBIT_API_KEY) < 5):
            print("WARNING: API Key not set in .env. Connectivity test might fail or only show public data.")

        # Check connectivity by fetching time
        print("Fetching server time...")
        time = exchange.fetch_time()
        print(f"Server Time: {time}")

        # Load Markets to ensure we use correct symbols
        print("Loading Markets...")
        markets = exchange.load_markets()
        print(f"Loaded {len(markets)} markets.")
        
        # Find a BTC symbol
        symbol = 'BTC/USDT:USDT'
        if symbol not in markets:
            print(f"Symbol {symbol} not found. Trying to find a valid BTC linear perp...")
            for m in markets:
                if 'BTC' in m and 'USDT' in m and markets[m]['linear']:
                    symbol = m
                    break
        
        print(f"Testing Ticker for {symbol}...")
        try:
            ticker = exchange.fetch_ticker(symbol)
            print(f"Ticker Fetched! Last Price: {ticker['last']}")
        except Exception as e:
            print(f"Failed to fetch ticker: {e}")

        # Fetch Balance
        print("Fetching Balance (requires API keys)...")
        balance = exchange.fetch_balance()
        print("Balance fetched successfully!")
        
        # Print USDT balance if available
        if 'USDT' in balance:
            print(f"USDT Balance: {balance['USDT']}")
        else:
            print("Balance (Total):", balance['total'])
            
    except ccxt.AuthenticationError:
        print("Authentication Error: Please check your API keys in .env")
    except ccxt.NetworkError as e:
        print(f"Network Error: Could not connect to exchange. Details: {e}") 
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    test_connection()
