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
# NOTE: USE_TESTNET is DEPRECATED - Binance removed Testnet Futures support
# Bot will now ALWAYS use LIVE exchange (set dry_run=True in bot.py for simulation)
USE_TESTNET = False  # Deprecated - keep False for Live trading

# Trading Settings
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
TRADING_SYMBOLS = [
    'ETH/USDT',
    'BTC/USDT',
    'SOL/USDT',
    'XRP/USDT',
    'HYPE/USDT',
    'BNB/USDT',
    'BCH/USDT',
    'ADA/USDT',
    'SUI/USDT',
    'LINK/USDT',
    'AVAX/USDT',
    'LTC/USDT',
    'NEAR/USDT',
    'FET/USDT',
    'DOT/USDT',
    'STX/USDT',
    'TAO/USDT',
    'FTM/USDT',
    'OP/USDT',
    'ARB/USDT',
    'INJ/USDT',
    'TIA/USDT',
    'JUP/USDT',
    'SEI/USDT',
    'FIL/USDT'
]

# Timeframes to run (Concurrent execution)
# 7 timeframes for comprehensive multi-timeframe analysis
TRADING_TIMEFRAMES = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']

# Parallel Processing
MAX_WORKERS = 8  # ThreadPoolExecutor workers for symbol-level parallelism

LEVERAGE = 8  # Updated: 8-12x range (mid-point)
RISK_PER_TRADE = 0.05
STOP_LOSS_PCT = 0.017   # Updated: 5% ROE / 3x Lev ≈ 1.67%
TAKE_PROFIT_PCT = 0.04  # Updated: 12% ROE / 3x Lev = 4.0%

# Patience Entry Settings
USE_LIMIT_ORDERS = True  # Use limit orders for better entry price
PATIENCE_ENTRY_PCT = 0.015  # 1.5% better entry price target
LIMIT_ORDER_TIMEOUT = 300  # 5 minutes timeout for limit orders (seconds)
REQUIRE_TECHNICAL_CONFIRMATION = False  # Require Fibo/S/R alignment before entry (disabled for now)
