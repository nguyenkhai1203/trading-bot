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
    'BTC/USDT',
    'ETH/USDT',
    'SOL/USDT',
    'BNB/USDT',
    'XRP/USDT',
    'DOGE/USDT',
    'ADA/USDT',
    'TRX/USDT',
    'LINK/USDT'
]

# Timeframes to run (Concurrent execution)
# 15m, 1h, 4h
TRADING_TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d']

LEVERAGE = 3
RISK_PER_TRADE = 0.05
STOP_LOSS_PCT = 0.017   # Updated: 5% ROE / 3x Lev ≈ 1.67%
TAKE_PROFIT_PCT = 0.04  # Updated: 12% ROE / 3x Lev = 4.0%
