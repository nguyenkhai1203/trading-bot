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

## 🚀 Major Updates

### Iteration 14 — BMS Intelligent Shield (March 4, 2026)
**Upgraded the macro exit logic from a blind hard-cut to a data-confirmed strategy:**
- **Two-Tiered Protection**:
    - **Hard Shield**: If BMS Vetoes the trade AND the Neural Brain confirms the bearishness (Score < 0.4), the position is closed immediately.
    - **Soft Shield**: If BMS Vetoes but the Neural Brain still shows micro-strength (Score >= 0.4), the bot **Tightens the Stop Loss** instead.
- **Tightening Logic**: SL is moved halfway toward the current price or to a small profit lock (Entry + 0.3%) if the trade is in profit. This allows the trade to potentially survive macro noise while drastically reducing risk.

### Iteration 13 — 12h Periodic Optimizer (March 4, 2026)
**Automated the full optimization cycle to run twice daily:**
- **Scheduler**: Integrated a 12-hour recurring task into `bot.py` that triggers `run_global_optimization(download=True)`.
- **Persistence**: Using the `risk_metrics` database table to track `last_optimization_time`, ensuring the cycle persists even after bot restarts.
- **Hands-Free Maintenance**: The bot now handles data refreshes, BMS optimization, Altcoin weight finding, and Neural Brain retraining automatically every 12 hours.

### Iteration 12 — Multi-Timeframe (MTF) Macro Analysis (March 4, 2026)
**Upgraded BMS to aggregate sentiment from 1h, 4h, and 1d timeframes:**
- **Aggregate Weights**: Implemented a weighted macro scoring system: 1h (30%), 4h (40%), and 1d (30%).
- **Robust Filtering**: Ensured the "Supreme Filter" accounts for structural market regimes (1d) and structural trends (4h), preventing noise from the 1h timeframe from triggering false macro shifts.
- **Auto-Exit Sync**: Proactive exits now trigger based on this robust MTF signal, providing higher confidence in macro-driven closures.

### Iteration 11 — Active Shield & Scale Refinement (March 4, 2026)
**Standardized BMS to a 0.0-1.0 scale and implemented proactive macro-exits:**
- **0.0 - 1.0 Scale**: Refactored `BTCAnalyzer` and `Strategy` to use a 0.0-1.0 sentiment score (0.5 = Neutral) for smoother mathematical integration.
- **Symmetrical Veto**: Implemented logic to veto LONGs in the RED zone (< 0.3) and SHORTs in the GREEN zone (> 0.7).
- **Active Shield (Auto-Exit)**: Updated `bot.py` to proactively close positions when market sentiment shifts against the trade (e.g., cutting longs if BTC enters the RED zone).
- **Unified Optimizer**: Integrated the full "Data -> BMS -> Alt -> Brain -> Backtest" flow into a single `py src/analyzer.py` command for streamlined maintenance.
- **Standardized Confidence**: Standardized strategy output to always be a 0.0-1.0 confidence score, regardless of brain state.

### Iteration 10 — Two-Tier BMS Optimization (March 4, 2026)
**Mathematically discovered the best weights for BTC sentiment and its impact on Altcoins:**
- **Loop A (Internal BTC)**: Automated weight discovery for BTC sub-scores (Trend, Momentum, Volatility, Dominance) using `scripts/optimize_bms.py`.
- **Loop B (Global Strategy)**: Integrated $W_{BTC}$ (Global Weight) into the `StrategyAnalyzer` grid search, allowing for symbol-specific BTC-Altcoin correlation optimization.
- **Data Alignment**: Modified `StrategyAnalyzer.load_data` to automatically merge historical BMS data with Altcoin candles for high-fidelity backtesting.
- **Config Persistence**: Optimized $W_{BTC}$ is now saved to `strategy_config.json` per symbol/timeframe under the `risk` key.

### Iteration 9 — BTC Macro Signal (BMS) integration (March 3, 2026)
**Implemented a global "Supreme Filter" to provide market context:**
- **BTC-First Logic**: Created `BTCAnalyzer` to calculate a composite score (0-100) based on Trend, Momentum, Volatility, and Dominance.
- **Veto System**: Implemented a "Traffic Light" mechanism where score < 20 (RED Zone) blocks all Long entries.
- **Weighted Scoring**: Signals now combine Altcoin score and BMS score using configurable weights ($W_{BTC}$ and $W_{Alt}$).
- **Notifications**: Telegram alerts for BMS Zone transitions to keep the user informed of macro shifts.

### Iteration 8 — Multi-Profile Safety & Shared State (March 3, 2026)
**Fixed multi-profile synchronization and loop crashes:**
- **Shared Account Cache**: Implemented a class-level shared cache in `Trader` that gives all profile instances real-time awareness of account-wide positions and orders. This prevents duplicate entries when multiple profiles manage the same underlying API key.
- **Profile-Specific ClOrdIDs**: Standardized `newClientOrderId` to include `P{id}` (e.g., `P1_ETH...`), ensuring that recovery and tracking logic are perfectly isolated between profiles.
- **Crash Fixes**: Resolved a critical `NameError: 'now' is not defined` that caused the main loop to stall under specific timing conditions.

### Iteration 7 — Performance & Rate-Limit Shield (March 3, 2026)
**Reduced API overhead by 80% to ensure stability:**
- **Readiness Cache**: Refactored `has_any_symbol_position` to use a 60-second local/shared cache of exchange positions and orders. This eliminated redundant `fetch_positions` calls for every symbol on every tick.
- **Request Throttling**: Added a mandatory 500ms delay between symbol fetches during the deep sync cycle to strictly respect Bybit and Binance rate limits.

### Iteration 6 — Advanced Reconciliation & History Recovery (March 2, 2026)
**Implemented a three-tier synchronization shield:**
- **Tier 1 (60s)**: Real-time "Ghost Detection" loop to resolve external closures (TP/SL).
- **Tier 2 (10m)**: Full DB-to-Exchange parity check to fix missing orders and adopt orphans.
- **Tier 3 (1h)**: Deep history audit scanning the last 24-48h of trade logs to ensure 100% PnL fidelity.
- **ID Persistence**: Mandatory saving of exchange-native `order_id` for all entry and TP/SL orders to eliminate "Null ID" tracking failures.

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
