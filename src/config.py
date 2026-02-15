import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Exchange Credentials
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# Active Exchange (str): 'BINANCE' or 'BYBIT'
ACTIVE_EXCHANGE = os.getenv('ACTIVE_EXCHANGE', 'BINANCE').upper()

# Configuration
# NOTE: USE_TESTNET is DEPRECATED - Binance removed Testnet Futures support
# Bot will now ALWAYS use LIVE exchange (set dry_run=True in bot.py for simulation)
USE_TESTNET = False  # Deprecated - keep False for Live trading
DRY_RUN = True  # Set to True for paper trading (simulation mode)

# Trading Settings
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
TRADING_SYMBOLS = [
    'ETH/USDT',
    'BTC/USDT',
    'SOL/USDT',
    'XRP/USDT',
    'HYPE/USDT',
    # 'BNB/USDT',
    'BCH/USDT',
    'ADA/USDT',
    'SUI/USDT',
    'LINK/USDT',
    'AVAX/USDT',
    # 'LTC/USDT',
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
TIMEFRAMES = TRADING_TIMEFRAMES  # Alias for backward compatibility

# Parallel Processing
MAX_WORKERS = 8  # ThreadPoolExecutor workers for symbol-level parallelism

LEVERAGE = 5  # Updated: 8-12x range (mid-point)
RISK_PER_TRADE = 0.05
STOP_LOSS_PCT = 0.015   # Updated: 5% ROE / 3x Lev ≈ 1.67%
TAKE_PROFIT_PCT = 0.03  # Updated: 12% ROE / 3x Lev = 4.0%

# Backtesting Friction (Realism)
TRADING_COMMISSION = 0.0006 # 0.06% per trade (Taker)
SLIPPAGE_PCT = 0.0005       # 0.05% price impact per trade

# Runtime behavior flags
# When False the bot will NOT automatically create SL/TP orders. Use manual TP/SL placement.
AUTO_CREATE_SL_TP = True

# Patience Entry Settings
USE_LIMIT_ORDERS = True  # Use limit orders for better entry price
PATIENCE_ENTRY_PCT = 0.015  # 1.5% better entry price target
LIMIT_ORDER_TIMEOUT = 300  # 5 minutes timeout for limit orders (seconds)
REQUIRE_TECHNICAL_CONFIRMATION = False  # Require Fibo/S/R alignment before entry (disabled for now)

# Signal Quality Filter
MIN_WINRATE_THRESHOLD = 0.50  # Minimum winrate (50%) required to trade a signal
# Bot will check signal_performance.json and only trade signals with winrate >= this threshold
HEARTBEAT_INTERVAL = 5  # Main loop interval in seconds
OHLCV_REFRESH_INTERVAL = 60  # OHLCV data refresh interval in seconds (1 minute)

# Confidence-Based Position Sizing (Conservative Settings)
# Bot will allocate capital and leverage based on signal confidence
CONFIDENCE_TIERS = {
    "high": {
        "min_confidence": 0.70,  # 70%+ confidence
        "leverage": 5,          # Conservative max leverage
        "cost_usdt": 5.0         # $5 per trade (conservative)
    },
    "medium": {
        "min_confidence": 0.50,  # 50-70% confidence
        "leverage": 4,           # Medium leverage
        "cost_usdt": 4.0         # $4 per trade
    },
    "low": {
        "min_confidence": 0.30,  # 30-50% confidence
        "leverage": 3,           # Low leverage for safety
        "cost_usdt": 3.0         # $3 per trade (minimum)
    }
}
MIN_CONFIDENCE_TO_TRADE = 0.30  # Minimum 30% confidence to enter any trade

# Analyzer Thresholds (for future use)
MIN_WIN_RATE_TRAIN = 0.55      # Minimum win rate on training set to enable (55%)
MIN_WIN_RATE_TEST = 0.55       # Minimum win rate on test set to enable (55%)
MAX_CONSISTENCY = 0.25         # Maximum consistency (train/test difference)
MIN_CROSS_TF_SUPPORT = 1       # Minimum number of other timeframes that must be profitable
