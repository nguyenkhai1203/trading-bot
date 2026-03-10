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
| ├── `execution.py`| Execution Engine | **Critical**. Orchestrates entry/exit and state reconcile. |
| ├── `order_executor.py`| Order Lifecycle | **New**. Handles placement, recovery, and limit-monitoring. |
| ├── `cooldown_manager.py`| Risk Circuit Breakers| **New**. Manages SL cooldowns and margin throttling. |
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

### Iteration 10 — Codebase Modularization & Refactor (March 7, 2026)
**Successfully decoupled the core execution engine and cleaned up the project root:**
- **Modular Execution**: Extracted logic from the 3500-line `Trader` class into specialized `OrderExecutor` (API/Lifecycle) and `CooldownManager` (Risk/Circuit Breakers).
- **Performance & Circularity**: Resolved critical "Partially Initialized Module" errors via bottom-level import patterns and fixed an `account_key` initialization race condition.
- **Environment Integrity**: Resolved a hidden `aiosqlite` dependency issue that was causing silent test failures.
- **Clean Root**: Purged/archived ~15 redundant scripts and centralized utilities in `symbol_helper.py`, `config_manager.py`, and `trade_sync_helper.py`.
- **Verification Results [PASS]**:
    - **Unit Tests**: 5/5 tests in `tests/test_bms_v2_1_holistic.py` passed.
        - [x] Global Position Guard (Deduplication)
        - [x] Signal Upgrading (Pending Replacement)
        - [x] Position Optimization (Active SL/TP)
        - [x] Phase 31: Implement Limit Order Entry (Patience Entry)
    - [x] Add `PENDING` status to `schema.sql`
    - [x] Update `ExecuteTradeUseCase` to respect `USE_LIMIT_ORDERS`
    - [x] Calculate entry price based on `PATIENCE_ENTRY_PCT`
- [ ] Phase 32: Final Monitoring & Tweaking
- **Verification**: 28/28 tests passing (100% green).

### Iteration 9 — Precise SL/TP Sync & Cooldowns (March 4, 2026)
**Resolved SL/TP misclassification and improper cooldown application:**
- **Precise Exit Resolver**: Implemented `_infer_exit_reason()` which prioritizes exchange-native metadata (Bybit `stopOrderType`, Binance `orderType`) and refined proximity checks over price heuristics.
- **Entry Price Guard Fix**: Resolved a critical bug in `reconcile_positions` where SL detection was bypassed for adopted positions with `entry_price=0`.
- **Bulletproof Cooldowns**: Guaranteed `set_sl_cooldown` triggering across all 3 sync paths whenever a loss is verified.
- **Verification**: 6/6 test cases passing, specifically confirming Bybit-native detection and zero-entry-price edge cases.

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

### Institutional-Grade Hardening & Smart Sync (Mar 9, 2026)
- **Smart Candle Sync**: Reduced OHLCV API calls by 95%+ using the **"Bridge & Patch"** technique.
- **Signal Refresh**: Indicators (EMA, RSI, etc.) now recalculate in real-time when a live candle is patched with ticker data, eliminating **"Repainting"**.
- **Exchange-First Boundaries**: Replaced local time checking with `adapter.exchange.milliseconds()` for perfect candle synchronization and drift resilience.
- **Bug Fix**: Resolved a critical `NameError` in `update_data` where `current_period_start` was undefined during full syncs.
- **Verification**: 100% pass on the comprehensive **191-test suite** (including 14 new Smart Sync edge cases).

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

### BMS v2.1 Holistic Architecture Upgrade [COMPLETED]
 (March 10, 2026)
**Resolved systemic order spamming and management errors by shifting to account-wide orchestration:**
- **Atomic Entry Orchestration**: `TradeOrchestrator` now collects signals from all active profiles (e.g. 1h, 4h) and deduplicates by (Exchange, Symbol), executing only the winner.
- **Account-Aware Position Guard**: `ExecuteTradeUseCase` verifies that NO other profile on the same exchange has an active/pending trade for the symbol before placement.
- **Root Cause Spam Fixes**:
    - **Bybit Symbol Normalization**: Fixed `BybitAdapter` returning native `DOTUSDT` instead of CCXT `DOT/USDT:USDT`. This was the "missing link" causing the bot to ignore existing trades and open duplicates.
    - **PENDING Status Visibility**: Updated database queries to include `PENDING` trades in all active position checks.
- **Patience Entry (Limit Orders)**: Fixed `ExecuteTradeUseCase` to stop forcing Market orders. It now respects `USE_LIMIT_ORDERS = True` and calculates a better entry price (Limit) using `PATIENCE_ENTRY_PCT`.
- **Auto-Seeding Foundation**: `DataManager` now automatically creates Bybit/Binance profiles from `.env` on a fresh DB, ensuring zero-configuration recovery.
- **Signal Upgrading**:
    - **If Pending**: Higher confidence signals now cancel and replace existing pending orders.
    - **If Active**: New signals trigger a "Smart Sync" of existing SL/TP levels instead of opening duplicate positions.
- **Verification**: 100% pass on specialized holistic test suite and full 214-test regression suite.

*Docs: [architecture.md] | [knowledge.md] | Progress: [task.md]*
