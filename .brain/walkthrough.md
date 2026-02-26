# Project Walkthrough & GPS

Quick map to navigate and debug the project.

## 🧭 Diagnostic Map

| Functional Area | Source Module | Core Functions | When to check? |
| :--- | :--- | :--- | :--- |
| **Data & Candles** | `data_manager.py` | `update_data`, `fetch_ohlcv_with_retry` | Data stale, rate limit, CSV issues |
| **Signals & Entry** | `bot.py`, `strategy.py` | `run_step`, `get_signal` | Indicator/weight/threshold issues |
| **Order Execution** | `execution.py`| `place_order`, `cancel_order` | API errors, order not placed |
| **Position State** | `execution.py`| `active_positions`, `_update_db_position` | DB sync issues, slot ID (`pos_key`) mapping |
| **SL/TP** | `execution.py`, `risk_manager.py` | `tighten_sl`, `recreate_missing_sl_tp` | SL not updating, wrong prices |
| **Exchange Sync** | `execution.py`| `sync_from_db`, `reconcile_positions` | Restart recovery, ID synchronization |
| **Exchange APIs** | `adapters/*.py` | `BybitAdapter`, `BinanceAdapter` | Bybit/Binance API quirks |
| **Notifications** | `execution.py`, `notification.py` | `send_telegram_message` | Entry/Fill/Exit notifications |
| **Brain Training** | `signal_tracker.py`, `neural_brain.py` | `record_trade`, `predict_win_rate` | Missing training data, MLP logic |
| **Data Store** | `trading_bot.db` (SQLite) | — | SL/TP order IDs, Trade history, pos_key tracking |

---

## � Project Structure & Module Map

| Directory / File | Role | Key Features |
| :--- | :--- | :--- |
| **`/src`** | **Core Logic** | Main source code for the bot. |
| ├── `database.py` | Data Persistence | **Core**. SQLite/aiosqlite bridge. Singleton DataManager. |
| ├── `bot.py` | Heartbeat Loop | Manages main loop, Circuit Breaker, and coordinator. |
| ├── `execution.py`| Execution Engine | **Critical**. Handles entry/exit, Dynamic SL/TP, and Reconcile. |
| ├── `strategy.py` | Weighted Scoring | Calculates signals from indicators and optimized weights. |
| ├── `neural_brain.py`| AI Veto/Boost | NumPy-based MLP for signal filtering (Veto/Boost logic). |
| ├── `risk_manager.py` | Risk Control | Position scaling, leverage, and drawdown/daily loss limits. |
| ├── `data_manager.py` | Data Orchestrator | OHLCV fetching, CSV standardization, and feature caching. |
| ├── `feature_engineering.py` | Indicators | Calculates 40+ TA indicators (RSI, MACD, ATR, etc.). |
| ├── `analyzer.py` | Strategy Optimizer | Grid Search for weight/SL/TP optimization (Layer 1-3). |
| ├── `backtester.py`| Simulation Engine | High-fidelity backtest with fees and slippage. |
| ├── `schema.sql` | DB Schema | SQLite table definitions for profiles, trades, and logs. |
| ├── `adapters/` | Exchange Layer | `binance_adapter.py` and `bybit_adapter.py` for API quirks. |
| **`/scripts`** | **Maintenance** | Support tools (`check_orphans`, `diagnose`, `download_data`). |
| **`/data`** | **Database** | Standardized OHLCV CSV files in root directory. |

---

## �🚀 Major Updates

### Iteration 5 — Database & Multi-Profile Foundation (Feb 26, 2026)

**Transitioned to enterprise-grade data management:**
- **SQLite Engine**: Replaced legacy `.json` files with a robust SQLite database (`trading_bot.db`). Features WAL mode for concurrency and strict schema enforcement.
- **Multi-Profile Architecture**: The bot now loads multiple trading profiles (different accounts/exchanges) from the database and runs them concurrently in a single loop using Dependency Injection.
- **Concurrent Launcher**: New `launcher.py` manages the Trading Engine and Telegram interface as supervised sub-processes, ensuring better stability and resource management.
- **Improved Sync Logic**: Position recovery now queries the database first, then reconciles with the exchange, eliminating "zombie" positions and state drift.

### Iteration 4 — Core Sync & Adoption Bug Fixes (Feb 22, 2026)

**Fixed core synchronization and position identification issues:**
- **Zero-Zombie Position Tracking**: Removed old stripped prefix logic that caused infinite loops of "Zombie Positions" (positions without prefixes in `.json`), preventing missing stop-loss history reports (missed stop-losses for NEAR, FIL).
- **Short Position Adoption Fix**: Fixed `reconcile_positions` and Telegram status issues where SHORT positions (like TAO, SEI) were ignored. Changed filtering from `qty > 0` to `abs(qty) > 0` and added fallback for `amount`/`positionAmt` to handle inconsistent CCXT responses on Binance.
- **Robust SL/TP Status Matching**: The Telegram bot now correctly maps exchange positions to internal metadata (timeframe), eliminating "N/A" displays for bot-initiated trades.


### Iteration 3 — Airtight Stability & Data Standardization (Feb 22, 2026)

**Ensured absolute stability and data standardization:**
- **Airtight Phantom Win Logic**: Prevented recorded "WIN" results when positions simply disappear. The bot now mandatorily fetches trade history 3 times for validation.
- **Mandatory Prefixing**: Standardized all keys in `positions.json` and `signal_performance.json` to the format `EXCHANGE_SYMBOL` (e.g., `BYBIT_NEAR_USDT`), removing `/` characters that caused errors.
- **Unified Data Path**: Merged all OHLCV candle data paths into the root `/data/` folder, resolving path mismatch errors between the Bot and Analyzer.
- **Incremental Fetching**: Upgraded `download_data.py` to only download new candles since the last timestamp in the CSV, optimizing performance and bandwidth.

### Iteration 2 — Bug Fixes & Unified Data Store (Feb 19, 2026)

**11 complete fixes:**
- **Execution fixes**: `tighten_sl` timeframe, actual fees, duplicate adoption, Bybit `category:linear`.
- **Unified Store**: `signal_performance.json` as the Single Source of Truth, replacing `trade_history.json`.
- **Telegram fixes**: `/status` crash, dead code, field name `pnl_usdt`.
- **Brain enrichment**: `record_trade()` now saves full PnL + trade metadata.

**Test Results**: 17/19 pass (89.5%) — 2 failures were unrelated legacy issues.

### Iteration 1 — Multi-Exchange & Isolation (Feb 18, 2026)
- Unified Key `EXCHANGE_SYMBOL_TIMEFRAME` for absolute state isolation.
- Order Adoption: Recovery from external orders or reconnection.
- Bybit V5 symbol normalization + `:USDT` suffix handling.

### Previous Updates
- Neural Brain (lightweight MLP) with Veto/Boost logic.
- Authoritative Exchange-First Reality for `/status`.
- Algo Order visibility fix (Binance SL/TP hidden orders).

---

## 🏗️ Data Flow Architecture

```
Exchange (CCXT) 
    ↔ Adapter (BinanceAdapter / BybitAdapter)  ← inject params, retry logic
        ↔ Trader (execution.py)                 ← business logic, memory state
            ↔ DataManager (database.py)         ↔ SQLite (trading_bot.db)
                ↔ TradingBot (bot.py)           ← signal → order lifecycle
                    ↔ SignalTracker              ← record performance
                        ↔ AI logs table          ← MLP Brain preparation
```

---

*Docs: [architecture.md](architecture.md) | [knowledge.md](knowledge.md) | Progress: [task.md](task.md)*
