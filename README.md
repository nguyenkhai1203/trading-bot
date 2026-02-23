# 🚀 Advanced Crypto Trading Bot

A multi-exchange, automated trading bot designed for high-frequency signal execution on Binance and Bybit Futures. 

## 🤖 Features & Capabilities

- **Multi-Exchange Execution**: Native support for **Binance Futures** and **Bybit V5 API**.
- **Wait-and-Patience Entry**: Uses smart limit orders to capture better entry prices (1-2% improvement) based on technical levels.
- **Dynamic Risk Scaling**: Automatically adjusts position size and leverage (8x-12x) based on signal confidence.
- **Neural Brain Integration**: Uses a lightweight RL-based scoring system to filter high-probability entries.
- **Authoritative Sync**: Periodically reconciles local state with the exchange to ensure 100% accuracy.
- **Telegram Command Center**: Fully remote control and status reporting via Telegram bot.
- **Isolated Margin Safety**: Forces isolated margin per position to prevent account-wide drawdown.

## 📋 Quick Start

### 1. Environment Setup
```bash
python3 -m venv .venv
.venv\Scripts\activate
source .venv/bin/activate
python3 src/self_test.py
```

### 2. Prepare Data & Maintenace
```bash
python3 scripts/download_data.py
python3 src/analyzer.py
python3 scripts/clean_positions.py  # Fix corrupted positions (NaN values)
```

### 3. Launch the Bot
```bash
python3 launcher.py
```
*Note: Launcher starts both the trading loop and the Telegram command bot.*

### 5. 🧪 Running Tests
The bot includes a comprehensive test suite (Unit, Integration, and Hardcore Anomalies).
```bash
python3 run_tests.py --ci
```
*This command executes all tests and checks for 100% stability.*

### 4. 🧠 Train the Neural Brain (Optional)
The bot includes a Neural Network that learns from trade performance. To activate it:
1. **Collect Data**: Run the bot in Dry Run or Live mode until you have at least 20-50 trades in `src/signal_performance.json`.
2. **Train**: Run `python3 src/train_brain.py`.
3. **Verify**: The bot will automatically start using the trained model for trade validation (Veto/Boost) on its next reload.

## 📂 Project Structure

- `src/`: Core logic (Bot, Execution, Strategy, Adapters)
- `scripts/`: Utilities for data management and diagnostics
- `data/`: Local cache of OHLCV market data
- `.brain/`: **Documentation & AI Context**
    - This directory contains the "Project Brain" — a persistent store of knowledge, architecture decisions, and development history.
    - It is designed to be read by both humans and AI coding assistants (like Antigravity) to maintain context over long development cycles.
    - **Note**: This folder is purely for documentation and is NOT used by the bot during live trading.

## 📚 Documentation & Technical Details

For in-depth information on system architecture, strategy mechanics, and troubleshooting, please refer to the **Project Brain**:

- [**System Architecture & Knowledge**](.brain/knowledge.md) - Deep dive into how it works and config settings.
- [**Recent Updates & Walkthroughs**](.brain/walkthrough.md) - Log of major features and recent changes.
- [**Development Roadmap**](.brain/task.md) - Active tasks and historical progress.

---
⚠️ **Disclaimer**: This is a production-grade trading tool. Always test in **Dry Run mode** (`DRY_RUN=True` in `.env`) before deploying real capital.
