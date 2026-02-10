# Contributing Guidelines & Code Style

## Python Style Guide

### Naming Conventions
```python
# Classes: PascalCase
class SignalTracker:
    pass

# Functions/Methods: snake_case
def calculate_sl_tp(entry_price, signal_type):
    pass

# Variables: snake_case
current_balance = 1000
entry_price = 50000.5

# Constants: UPPER_SNAKE_CASE
MAX_WORKERS = 8
SL_COOLDOWN_SECONDS = 7200

# Private methods/attributes: _underscore prefix
def _load_positions(self):
    pass
self._sl_cooldowns = {}
```

### Docstrings (Google Style)
```python
def calculate_position_size(self, account_balance, entry_price, stop_loss_price, leverage=None):
    """
    Calculate position size based on risk parameters.
    
    Args:
        account_balance: Current account balance in USDT
        entry_price: Entry price of the trade
        stop_loss_price: Stop loss price
        leverage: Optional leverage override (default: class leverage)
    
    Returns:
        float: Position size in base currency units
        
    Raises:
        ValueError: If entry_price or stop_loss_price is <= 0
    """
    pass
```

### Type Hints (Optional but Recommended for Public APIs)
```python
def check_market_condition(self, btc_change: float) -> str:
    """Returns: 'crash' | 'pump' | 'normal'"""
    pass

async def place_order(
    self, 
    symbol: str, 
    side: str, 
    qty: float, 
    timeframe: str = None,
    order_type: str = 'market'
) -> dict | None:
    pass
```

### File Organization
```
# 1. Standard library imports
import os
import json
import asyncio
from datetime import datetime

# 2. Third-party imports  
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt

# 3. Local imports
from config import TRADING_SYMBOLS, LEVERAGE
from notification import send_telegram_message
```

### Class Structure
```python
class MyClass:
    """Class description."""
    
    # 1. Class constants
    DEFAULT_TIMEOUT = 300
    
    def __init__(self, param1, param2):
        """Initialize."""
        # 2. Instance attributes
        self.param1 = param1
        self._private_attr = None
    
    # 3. Public methods (business logic)
    def process(self):
        pass
    
    async def async_process(self):
        pass
    
    # 4. Private/helper methods
    def _helper(self):
        pass
    
    # 5. Static/class methods (if any)
    @staticmethod
    def utility():
        pass
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        bot.py (Main)                        │
│  - Orchestrates 125 bots (25 symbols × 5 timeframes)        │
│  - Main loop: fetch data → evaluate signals → execute       │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
    ┌───────────▼───────────┐     ┌───────────▼───────────┐
    │   data_manager.py     │     │    execution.py       │
    │ - Centralized fetching│     │ - Place/close orders  │
    │ - Feature caching     │     │ - Position persistence│
    │ - Rate limit handling │     │ - SL/TP management    │
    └───────────────────────┘     └───────────┬───────────┘
                                              │
┌───────────────────────────────────────────────────────────┐
│                      strategy.py                           │
│  - WeightedScoringStrategy: Aggregate signals → score     │
│  - Config hot-reload from strategy_config.json            │
└───────────────────────────────────────────────────────────┘
                │
    ┌───────────▼───────────┐     ┌───────────────────────┐
    │ feature_engineering.py│     │   signal_tracker.py   │
    │ - RSI, EMA, MACD, BB  │     │ - Adaptive learning   │
    │ - ADX, Stochastic     │     │ - Loss counter        │
    │ - Fibonacci, S/R      │     │ - Performance stats   │
    └───────────────────────┘     └───────────────────────┘
                                              │
    ┌───────────────────────┐     ┌───────────▼───────────┐
    │   risk_manager.py     │     │     analyzer.py       │
    │ - Position sizing     │     │ - Weight optimization │
    │ - Circuit breaker     │     │ - Backtesting engine  │
    │ - SL/TP calculation   │     │ - Config updates      │
    └───────────────────────┘     └───────────────────────┘
```

## Key Files Reference

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `bot.py` | Main entry | `TradingBot`, `main()` |
| `execution.py` | Order management | `Trader` |
| `strategy.py` | Signal generation | `WeightedScoringStrategy` |
| `analyzer.py` | Optimization | `StrategyAnalyzer`, `run_global_optimization()` |
| `signal_tracker.py` | Adaptive learning | `SignalTracker` |
| `risk_manager.py` | Risk control | `RiskManager` |
| `feature_engineering.py` | Indicators | `FeatureEngineer` |
| `data_manager.py` | Data fetching | `MarketDataManager` |
| `notification.py` | Alerts | `send_telegram_message()` |
| `config.py` | Settings | Constants, env vars |

## Common Patterns

### 1. Position Key Format
```python
pos_key = f"{symbol}_{timeframe}"  # e.g., "BTC/USDT_1h"
```

### 2. Async Lock for Concurrency
```python
async with self._get_lock(symbol):
    # Critical section - only one coroutine at a time per symbol
    await self.place_order(...)
```

### 3. Atomic Config Write
```python
import tempfile
fd, temp_path = tempfile.mkstemp(suffix='.json', dir=config_dir)
os.write(fd, json.dumps(data).encode())
os.close(fd)
os.replace(temp_path, config_path)  # Atomic rename
```

### 4. Graceful Degradation
```python
try:
    result = risky_operation()
except Exception as e:
    self.logger.error(f"Error: {e}")
    return None  # Don't crash, continue with fallback
```

## Testing Checklist

Before committing:
1. `py -m py_compile src/*.py` - Syntax check
2. Test bot startup: `python src/bot.py` (dry_run mode)
3. Test analyzer: `python src/analyzer.py`
4. Verify positions.json structure after trades

## Comments Language
- Code comments: English
- User-facing messages/logs: Can include Vietnamese where appropriate
