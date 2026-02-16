import os
from dotenv import load_dotenv

# Load environment variables
# Load environment variables from project root
# Root is one level up from src/
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
load_dotenv(env_path)

# Exchange Credentials
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# Active Exchanges (list): ['BINANCE', 'BYBIT']
ACTIVE_EXCHANGES = os.getenv('ACTIVE_EXCHANGES', 'BINANCE,BYBIT').upper().split(',')
# For backward compatibility
ACTIVE_EXCHANGE = ACTIVE_EXCHANGES[0] if ACTIVE_EXCHANGES else 'BINANCE'

# Configuration
# NOTE: USE_TESTNET is DEPRECATED - Binance removed Testnet Futures support
# Bot will now ALWAYS use LIVE exchange (set dry_run=True in bot.py for simulation)
USE_TESTNET = False  # Deprecated - keep False for Live trading
DRY_RUN = False  # Set to True for paper trading (simulation mode)

# Trading Settings
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
# Symbols to trade
# Symbols to trade (Perpetual Futures format for Bybit/CCXT)
# Binance supports broad list
BINANCE_SYMBOLS = [
    'ETH/USDT', 'BTC/USDT', 'SOL/USDT', 'XRP/USDT', 'BNB/USDT', 
    'BCH/USDT', 'ADA/USDT', 'SUI/USDT', 'LINK/USDT', 'AVAX/USDT', 
    'LTC/USDT', 'NEAR/USDT', 'FET/USDT', 'DOT/USDT', 'STX/USDT', 
    'TAO/USDT', 'FTM/USDT', 'OP/USDT', 'ARB/USDT', 'INJ/USDT', 
    'TIA/USDT', 'JUP/USDT', 'SEI/USDT', 'FIL/USDT'
]

# Bybit supports unique gems like HYPE (Updated User Request: Top 20 Stable High-Vol)
BYBIT_SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'BNB/USDT',
    'DOGE/USDT', 'ADA/USDT', 'TRX/USDT', 'AVAX/USDT', 'LINK/USDT',
    'DOT/USDT', 'POL/USDT', 'LTC/USDT', 'BCH/USDT', 'UNI/USDT',
    'XLM/USDT', 'NEAR/USDT', 'ATOM/USDT', 'APT/USDT', 'ARB/USDT'
]

# Unified list for general manager usage (union of both)
TRADING_SYMBOLS = list(set(BINANCE_SYMBOLS + BYBIT_SYMBOLS))

# Timeframes to run (Concurrent execution)
# 7 timeframes for comprehensive multi-timeframe analysis
TRADING_TIMEFRAMES = [ '15m', '30m', '1h', '2h', '4h', '8h', '1d']
TIMEFRAMES = TRADING_TIMEFRAMES  # Alias for backward compatibility

# Parallel Processing
MAX_WORKERS = 8  # ThreadPoolExecutor workers for symbol-level parallelism

LEVERAGE = 5  # Initial target leverage
RISK_PER_TRADE = 0.05
GLOBAL_MAX_LEVERAGE = 5        # Absolute max leverage allowed (User safety)
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
GLOBAL_MAX_COST_PER_TRADE = 5.0 # Absolute max USDT margin per position
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
        "leverage": 5,          # Max leverage
        "cost_usdt": 5.0         # $4 per trade (Base Max)
    },
    "medium": {
        "min_confidence": 0.50,  # 50-70% confidence
        "leverage": 4,           # Medium leverage
        "cost_usdt": 4.0         # $4 per trade (Base)
    },
    "low": {
        "min_confidence": 0.30,  # 30-50% confidence
        "leverage": 3,           # Low leverage for safety
        "cost_usdt": 3.0         # $3 per trade (Lower than base)
    }
}
MIN_CONFIDENCE_TO_TRADE = 0.30  # Minimum 30% confidence to enter any trade

# Trailing / Profit Lock-in Settings (v3.0)
ENABLE_PROFIT_LOCK = True
PROFIT_LOCK_THRESHOLD = 0.8     # 80% of the way to TP
PROFIT_LOCK_LEVEL = 0.1         # Lock in 10% of the target profit
MAX_TP_EXTENSIONS = 2           # Limit number of times TP can be moved
ATR_EXT_MULTIPLIER = 1.5        # Fallback ATR multiplier for TP extension

# Analyzer Thresholds (for future use)
MIN_WIN_RATE_TRAIN = 0.55      # Minimum win rate on training set to enable (55%)
MIN_WIN_RATE_TEST = 0.55       # Minimum win rate on test set to enable (55%)
MAX_CONSISTENCY = 0.25         # Maximum consistency (train/test difference)
MIN_CROSS_TF_SUPPORT = 1       # Minimum number of other timeframes that must be profitable
