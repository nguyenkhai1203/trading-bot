# 🚀 Trading Bot Operational Guide

This guide describes how to use the bot for different purposes and scenarios.

## 📋 Scenarios

### 1. 🔍 Strategy Optimization (Daily/Weekly)
Use this to find the best signal weights, Stop Loss, and Take Profit for every symbol.
- **Goal**: Update `strategy_config.json` with profitable settings.
- **Action**: Run `python src/analyzer.py`.
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

## 🛠️ Maintenance
- **Data Cleanup**: Periodically delete the `data/` folder if you want the bot to fetch fresh historical data for optimization.
- **Report Review**: Check `reports/global_backtest_summary.csv` weekly to see which assets are performing best.
