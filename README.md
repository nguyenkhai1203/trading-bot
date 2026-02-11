# 🚀 Trading Bot Operational Guide

This guide describes how to use the bot for different purposes and scenarios.

## 📋 Scenarios
.\.venv\Scripts\Activate.ps1
python .\src\bot.py

### 1. 🔍 Strategy Optimization (Daily/Weekly)
Use this to find the best signal weights, Stop Loss, and Take Profit for every symbol.
- **Goal**: Update `strategy_config.json` with profitable settings.
- **Action**: Run `python src/analyzer.py`.
- **Runtime**: ~5 minutes for 125 symbol/TF combinations (optimized with caching + coarse-to-fine search)
- **Outcome**: 
  - Profitable pairs are labeled `✅ REAL RUNNING`.
  - Losing pairs are deactivated (`❌ NO MONEY`).
  - Pairs with no trades are labeled `🧪 TEST`.
- **Notification**: You'll receive a Telegram summary of active symbols.

### 2. 🧪 Backtesting (Verification)
Verify the performance of a specific symbol and timeframe before enabling it.
- **Goal**: See historical performance.
- **Action**: `python src/backtester.py --symbol BTC/USDT --timeframe 1h`
- **Output**: Detailed results in console and a CSV report in `reports/`.

### 3. 🛡️ Demo Trading (Paper Trading)
Run the bot and get notifications without risking real money.
- **Goal**: Live testing in safe mode.
- **Configuration**: Ensure `dry_run=True` in `src/bot.py`.
- **Action**: Run `python src/bot.py`.
- **Notification**: Alerts will be labeled `🧪 TEST`.

### 4. 💸 Live Trading (Production)
Execute real trades on your exchange.
- **Goal**: Profit from live markets.
- **Configuration**: Set `dry_run=False` in `src/bot.py`.
- **Action**: Run `python src/bot.py`. `.\.venv\Scripts\python src/bot.py`
- **Notification**: Alerts will be labeled `✅ REAL RUNNING`.

---

## 📂 Project Structure

- `src/bot.py`: Main entry point for trading.
- `src/analyzer.py`: Strategy optimizer and config generator.
- `src/backtester.py`: Historical performance validator.
- `src/strategy_config.json`: The "brain" containing weights and risk settings.
- `reports/`: Folder containing all CSV trade logs.
- `data/`: Folder containing cached market data.

---

## 🎯 Entry & Exit System

### Limit Order Smart Execution
The bot uses limit orders with intelligent patience for better entry prices:

**Entry Flow:**
1. **Signal Detection** → Bot detects BUY/SELL signal with technical confirmation
2. **Limit Order Placement** → Orders placed at 1.5% better price than current market
3. **Background Monitoring** → Order monitored every 3 seconds for fill status
4. **Technical Validation** → Each cycle checks if signal still valid:
   - ❌ Cancel if signal reversed (BUY→SELL or SELL→BUY)
   - ❌ Cancel if no signal detected
   - ❌ Cancel if confidence drops below 0.2
   - ✅ Keep waiting if signal still valid
5. **Fill Confirmation** → Once filled, moves to active positions tracking

**No Arbitrary Timeouts:**
- Orders are NOT cancelled after fixed time (no 90s timeout)
- Orders only cancelled when technical analysis invalidates the setup
- Prevents premature cancellation of good setups

### Position Monitoring
**Active Position Flow:**
1. **SL/TP Tracking** → Checks every cycle if Stop Loss or Take Profit hit
2. **Exchange Sync** → In live mode, validates position still exists on exchange
3. **PnL Calculation** → Real-time profit/loss tracking with leverage
4. **Telegram Alerts** → Notifications for entries, fills, and exits

---

## ⚖️ Dynamic Leverage & Risk Management

### Tier-Based Position Sizing
Bot adjusts leverage and position size based on signal confidence score:

| Score Range | Leverage | Margin per Trade | Notional Value |
|-------------|----------|------------------|----------------|
| 2.0 - 3.9   | 8x       | $3               | $24            |
| 4.0 - 6.9   | 10x      | $4               | $40            |
| 7.0+        | 12x      | $5               | $60            |

**Key Features:**
- **Fixed Margin Mode**: Each trade uses $3-5 regardless of account size
- **Score-Based Sizing**: Higher confidence = higher leverage
- **Conservative Capital**: Small per-trade capital for risk management
- **Isolated Margin**: Each position independent (no cross-margin risk)

### Signal Confidence System
Signal score (0-10) calculated from 40+ technical indicators:

**Signal Categories (weighted 1.0-1.5):**
- **Fibonacci Levels**: Retracement bounces, key level alignment
- **Support/Resistance**: Price at S/R, bounces, breakouts
- **EMA**: Trend alignment, crosses (50, 100, 200 periods)
- **MACD**: Crossovers, histogram divergence
- **RSI**: Oversold/overbought, divergence
- **Ichimoku**: Cloud signals, Tenkan/Kijun crosses
- **Volume**: Spike confirmation, breakout validation
- **ADX**: Trend strength confirmation

**Technical Confirmation Required:**
- Must have Fibonacci, Support, or Resistance signal
- Prevents false entries from weak setups
- Can be disabled via `REQUIRE_TECHNICAL_CONFIRMATION = False`

---

## 🔧 Configuration

### Key Settings (`src/config.py`)
```python
# Entry System
USE_LIMIT_ORDERS = True           # Use limit orders for better prices
PATIENCE_ENTRY_PCT = 0.015        # 1.5% patience from market price
REQUIRE_TECHNICAL_CONFIRMATION = False  # Require Fibo/S/R confirmation

# Risk Management  
LEVERAGE = 10                     # Default leverage (overridden by tiers)
RISK_PER_TRADE = 0.01            # 1% max risk fallback

# Monitoring
TRADING_SYMBOLS = [...]          # 25 crypto pairs
TRADING_TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d']  # 5 timeframes
```

### Strategy Config (`src/strategy_config.json`)
Stores per-symbol or default configuration:
- **Signal Weights**: 40 indicators with 1.0-1.5 weights
- **Sizing Tiers**: Leverage and margin by score thresholds
- **Entry/Exit Thresholds**: Minimum scores for signals
- **SL/TP Percentages**: Stop loss and take profit targets

**Structure:**
```json
{
  "default": {
    "enabled": true,
    "weights": { "signal_name": 1.5, ... },
    "tiers": {
      "minimum": {"min_score": 2.0, "leverage": 8, "cost_usdt": 3},
      "low": {"min_score": 4.0, "leverage": 10, "cost_usdt": 4},
      "high": {"min_score": 7.0, "leverage": 12, "cost_usdt": 5}
    },
    "entry_score_threshold": 2.0,
    "exit_score_threshold": 1.0,
    "sl_pct": 0.03,
    "tp_pct": 0.08
  }
}
```

---

## 🛠️ Maintenance
- **Data Cleanup**: Periodically delete the `data/` folder if you want the bot to fetch fresh historical data for optimization.
- **Report Review**: Check `reports/global_backtest_summary.csv` weekly to see which assets are performing best.
- **Config Optimization**: Run analyzer weekly to update signal weights and SL/TP values.
- **Position Files**: 
  - `src/positions.json`: Active and pending positions
  - `src/trade_history.json`: Completed trades log
