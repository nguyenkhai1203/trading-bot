import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Exchange Credentials
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# Configuration
USE_TESTNET = os.getenv('USE_TESTNET', 'True').lower() in ('true', '1', 't')

# Trading Settings
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
TRADING_SYMBOLS = [
    'BTC/USDT:USDT',
    'ETH/USDT:USDT',
    'SOL/USDT:USDT',
    'BNB/USDT:USDT',
    'XRP/USDT:USDT',
    'DOGE/USDT:USDT',
    'ADA/USDT:USDT',
    'AVAX/USDT:USDT',
    'TRX/USDT:USDT',
    'LINK/USDT:USDT'
]

# Timeframes to run (Concurrent execution)
# 15m, 1h, 4h
TRADING_TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d']

LEVERAGE = 3            # Increased from 1 to 5
RISK_PER_TRADE = 0.05   # Increased from 1% to 2%
STOP_LOSS_PCT = 0.085    # Widened SL to 3% to handle volatility
TAKE_PROFIT_PCT = 0.15  # Increased TP to 9% (3:1 Reward/Risk)
