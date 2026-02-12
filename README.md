# 🚀 Trading Bot - Quick Start Guide

## 📋 Getting Started (First Time Setup)

### 1. ⚙️ Environment Setup
```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Verify configuration
py src/self_test.py
```

### 2. � Download Market Data
**IMPORTANT: Must run this first before analyzer or backtest!**

```powershell
# Download data for all configured symbols
py download_data.py

# Or download specific symbols and timeframes
py download_data.py --symbols BTC ETH SOL LINK --timeframes 15m 1h 4h --limit 1000
```

**What it does:**
- Downloads historical OHLCV data from Binance
- Saves to `data/` folder for offline analysis
- Required for analyzer and backtester to work

### 3. 🔍 Run Strategy Analyzer
**Optimize signal weights and find profitable settings**

```powershell
py src/analyzer.py
```

**What it does:**
- Analyzes all symbol/timeframe combinations (~5 minutes)
- Tests 40+ technical indicators
- Updates `src/strategy_config.json` with best weights
- Enables profitable pairs (✅), disables losing pairs (❌)
- Sends Telegram summary

### 4. 🧪 Backtest (Optional Verification)
**Verify performance before going live**

```powershell
py src/backtester.py --symbol BTC/USDT --timeframe 1h
```

**Output:** CSV report in `reports/` folder

### 5. 🤖 Run Trading Bot

#### Simulation Mode (Dry Run)
You can test the bot without using real funds. This mode simulates orders locally.

**Using command-line arguments (Easiest):**
```powershell
py src/bot.py --dry-run
```

**Using environment variables:**
```powershell
$env:DRY_RUN="True"; py src/bot.py
```

**In Command Prompt:**
```cmd
set DRY_RUN=True && py src/bot.py
```

#### Real Trading (Live Mode)
To trade with real funds, ensuring your `.env` file contains valid Binance API keys.

```powershell
# Runs both trading bot AND Telegram bot (Recommended)
py launcher.py
```

> **Note:** launcher.py starts both bot.py and telegram_bot.py. By default, it runs in **Live Mode** (`DRY_RUN=False` in `src/config.py`).

---

## 🔄 Daily Workflow

```
1. Download fresh data → py download_data.py
2. Optimize strategy  → py src/analyzer.py
3. Run bot           → py src/bot.py
```

---

## 📂 Project Structure

```
tradingBot/
├── src/
│   ├── bot.py                    # Main trading loop
│   ├── analyzer.py               # Strategy optimizer
│   ├── backtester.py             # Historical validator
│   ├── execution.py              # Order execution engine
│   ├── strategy.py               # Signal generation
│   ├── data_manager.py           # Data fetching & caching
│   ├── base_exchange_client.py   # Unified exchange client
│   ├── self_test.py              # System health checker
│   ├── cli_tools.py              # CLI utilities
│   ├── strategy_config.json      # Signal weights & settings
│   └── positions.json            # Active positions
├── data/                         # Cached market data
├── reports/                      # Backtest results
├── download_data.py              # Data downloader
├── launcher.py                   # Bot + Telegram launcher
└── README.md                     # This file
```

---

## 🎯 How It Works

### Smart Limit Order System

**Entry Flow:**
1. **Signal Detection** → 40+ indicators analyze market
2. **Limit Order** → Placed at 1.5% better price than market
3. **Background Monitor** → Checks every 3 seconds
4. **Validation** → Cancels if signal invalidates
5. **Fill** → Moves to active position tracking

**No Arbitrary Timeouts:**
- Orders wait until filled or invalidated
- No 90-second timeout
- Better entry prices

### Dynamic Leverage Tiers

| Signal Score | Leverage | Margin | Notional |
|--------------|----------|--------|----------|
| 2.0 - 3.9    | 8x       | $3     | $24      |
| 4.0 - 6.9    | 10x      | $4     | $40      |
| 7.0+         | 12x      | $5     | $60      |

**Features:**
- Fixed margin mode ($3-5 per trade)
- Score-based sizing
- Isolated margin (no cross-margin risk)

### Signal Confidence System

**40+ Technical Indicators:**
- Fibonacci levels & retracements
- Support/Resistance zones
- EMA (50, 100, 200)
- MACD crossovers
- RSI divergence
- Ichimoku cloud
- Volume confirmation
- ADX trend strength

**Score Calculation:**
- Each indicator weighted 1.0-1.5x
- High-quality signals (bounces, breakouts) weighted higher
- Multi-timeframe validation
- Minimum score threshold: 2.0

---

## 🔧 Configuration

### Environment Variables (`.env`)
```bash
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_secret
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Key Settings (`src/config.py`)
```python
# Trading Pairs
TRADING_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'LINK/USDT']
TIMEFRAMES = ['15m', '30m', '1h', '2h', '4h']

# Entry System
USE_LIMIT_ORDERS = True
PATIENCE_ENTRY_PCT = 0.015        # 1.5% better price
REQUIRE_TECHNICAL_CONFIRMATION = False

# Risk Management
LEVERAGE = 10                     # Default (overridden by tiers)
AUTO_CREATE_SL_TP = False         # Manual SL/TP management
```

### Strategy Config (`src/strategy_config.json`)
Auto-generated by analyzer. Contains:
- Signal weights for 40 indicators
- Leverage tiers by score
- Entry/exit thresholds
- SL/TP percentages

---

## 🛠️ Maintenance & Tools

### System Health Check
```powershell
py src/self_test.py
```
Tests: API keys, connectivity, time sync, modules, positions

### CLI Tools
```powershell
# Rebuild positions from exchange
py -c "import asyncio; from src.cli_tools import rebuild_positions_from_open_orders; asyncio.run(rebuild_positions_from_open_orders())"
```

### Data Cleanup
```powershell
# Delete cached data to force fresh download
Remove-Item -Recurse -Force data/
```

### Position Files
- `src/positions.json` - Active & pending positions
- `src/trade_history.json` - Completed trades
- `src/cooldowns.json` - SL cooldown tracking

---

## 📊 Performance Optimizations

### Analyzer Speed
- **36x faster** (3 hours → 5 minutes)
- Coarse-to-fine parameter search
- Data/feature caching
- Parallel processing

### System Architecture
- **BaseExchangeClient**: Unified time sync
- **Singleton DataManager**: Prevents duplicate API calls
- **Per-position locks**: Prevents race conditions
- **TP safety checks**: Prevents -2021 errors

---

## 🐛 Troubleshooting

### Bot Not Creating Positions
1. Check data exists: `ls data/`
2. Verify `strategy_config.json` has `"enabled": true`
3. Check entry threshold not too high (default: 2.0)

### Positions Not Closing
1. Check `positions.json` for SL/TP prices
2. Review Telegram notifications
3. Check `trade_history.json`

### Self-Test Failures
1. **API Keys**: Check `.env` file
2. **Time Sync**: Network issue or API down
3. **Module Imports**: Run `pip install -r requirements.txt`

### Data Download Issues
1. Check API keys in `.env`
2. Verify internet connection
3. Check Binance API status

---

## ⚠️ Important Notes

### Binance Conditional Orders
- STOP_MARKET/TAKE_PROFIT_MARKET may not be retrievable via `fetch_order`
- Returns -2013 "Order does not exist" even when accepted
- This is a Binance API limitation

### Time Synchronization
- Bot auto-syncs with server time on startup
- Prevents -1021 timestamp errors
- Uses unified `BaseExchangeClient` for all components

### Margin & Leverage
- Bot enforces ISOLATED margin mode
- Sets leverage per-order automatically
- Leverage clamped to 5x-12x range

---

## 📚 Additional Resources

For detailed architecture and development info, see:
- `.brain/knowledge.md` - System architecture & components
- `.brain/walkthrough.md` - Recent refactoring details
- `.brain/task.md` - Development history
