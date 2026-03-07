# System Architecture & Technical Implementation

This document serves as the technical blueprint for the trading bot, documenting the implementation details of the Adapter Pattern, Singleton architectures, and synchronization logic.

## 1. Core Architecture: 3-Layer Scale
The system is decoupled into three distinct layers to ensure scalability and maintainability.

1.  **Data Layer (`MarketDataManager`)**: 
    - Handles centralized fetching and normalization.
    - **Exchange Namespacing**: Data is isolated as `{EXCHANGE}_{SYMBOL}_{TF}.csv`.
    - **Unified Path**: All OHLCV data is stored in the root `/data/` directory (shared between Bot and Analyzer) to prevent path mismatches.
    - **Incremental Fetching**: `download_data.py` uses existing CSV timestamps to only fetch new candles, optimizing API usage.
    - **Time Sync**: Implements manual offset with a -5000ms safety buffer to resolve Binance -1021 timestamp errors.
2.  **Logic Layer (`Strategy` / `Analyzer`)**: 
    - **Strategy Engine**: 40+ technical indicators weighted by performance.
    - **Analyzer**: Periodic re-optimization (2x daily) with Parallel Grid Search (8 workers).
    - **Neural Brain**: Lightweight NumPy-based MLP for Veto/Boost confirmed signals.
3.  **Execution Layer (Modular `Trader`)**: 
    - **Orchestrator Pattern**: The `Trader` class acts as a high-level orchestrator, delegating complex logic to specialized sub-managers.
    - **OrderExecutor**: Manages the full order lifecycle (placement, timeout recovery, limit-fill monitoring, and SL/TP creation).
    - **CooldownManager**: Handles symbol-level SL cooldowns and account-level margin throttling.
    - **Multi-Profile Dependency Injection**: `bot.py` loads profiles and injects a shared `DataManager` and account-level caches into each `Trader`.
    - **Safety Locks**: Symbol-level async mutexes (`asyncio.Lock`) serialize entry processes per profile.
    - **Authoritative Sync**: Database-first reality where `pos_key` is the primary identifier. The system reconciles DB status with the exchange on every restart.
    - **Prefixing**: `pos_key` format `P{ID}_{EXCHANGE}_{SYMBOL}_{TF}` links positions to specific user profiles.

---

## 2. Multi-Exchange Adapter Pattern
Standardizes behavior across fragmented exchange APIs.

### Base Interface (`BaseAdapter`)
Ensures unified method signatures for:
- `get_historical_candles`
- `place_smart_order`
- `cancel_order` (handles both Standard and Algo orders)
- `fetch_order` (with conditional retry logic)

### Bybit V5 Implementation
Transitioned to Bybit's **Parent-Child mechanism**:
- **Setup**: Isolated margin and leverage set before placement.
- **Atomic Entry**: SL/TP attached to the entry order via `params` field.
- **Auto-Cleanup**: Cancelling an entry order automatically purges attached SL/TP.
- **Mapping**: Maps unsupported timeframes (e.g., 8h -> 4h).

### Binance Futures Implementation
Handles the complexity of "Algo Orders":
- **Algo Cancellation**: Uses specialized `fapiPrivateDeleteAlgoOrder` for TP/SL.
- **Failover Logic**: Implements automatic fallback in `cancel_order`. If a standard cancellation fails with "Order not found," it automatically attempts the Algo endpoint (and vice versa).
- **Symbol Normalization**: Maps unified symbols to Binance internal formats.
- **Mandatory Prefixing**: All trade and position identifiers now use the `BINANCE_` prefix (e.g., `BINANCE_BTC_USDT`) for 100% disambiguation.

### 4. Dynamic Order Updates & Market Adaptation
The system employs a dual-logic approach to manage active positions based on both strategy confidence and technical market structure.

#### **A. TA-Driven Extensions (Market Structure)**
- **Logic**: Uses Technical Analysis to expand profits when momentum is strong.
- **TP Extensions**: If Resistance (for Buy) or Support (for Sell) is detected beyond the initial TP, the bot extends the target up to 1.5x the original distance.
- **ATR Fallback**: If structural levels (S/R) are unavailable, uses `ATR * ATR_EXT_MULTIPLIER` for dynamic expansion.
- **Profit Lock**: Automatically moves SL to a "Positive Profit Zone" once the trade reaches 80% of its target (Profit Lock Level).

#### **B. Emergency Adaptive Shielding (`tighten_sl`)**
- **Mental Model**: This is a **Safety Buffer**, NOT a primary strategy move. 
- **Trigger**: Activated only when the **Strategy Confidence** drops significantly (e.g., < 50% of entry confidence) or market delta shifts against the trade.
- **Mechanical Factor (0.5)**: Acts as an emergency shield by moving the SL 50% closer to the entry price to minimize drawdown when the underlying signal becomes "shaky."
- **Function**: `trader.tighten_sl(pos_key, factor=0.5)` - strictly for risk mitigation when technical entry conditions start to fade.

---

## 3. Position & Order Synchronization
Philosophy: **The Exchange is the Source of Truth.**

### Execution Sub-Managers (Phase 3 Refactor)
To manage complexity and eliminate circular dependencies, execution logic is split:
- **`OrderExecutor`**: Encapsulates API interactions, client_id generation, and background monitoring tasks (Limit orders). Implements automatic timeout recovery by polling exchange for client_id.
- **`CooldownManager`**: Manages circuit breakers. Symbol cooldown (2h) blocks re-entry after SL; Margin cooldown (15m) blocks account-level entries after "Insufficient Margin" errors. Supports persistence to DB risk metrics.

### Tiered State Reconciliation
The system employs a three-tier defense against state drift and historical inconsistency:
1.  **Ghost Detection (60s)**: `sync_with_exchange()` runs every minute to detect positions that were closed externally (TP/SL). It uses a **Precision-First Resolver** (`_infer_exit_reason`) that prioritizes exchange-native metadata (Bybit `stopOrderType`, etc.).
2.  **Full Reconciliation (10m)**: `reconcile_positions()` runs every 10 minutes to fix missing SL/TP orders, adopt orphans, and ensure 1:1 parity between DB and Exchange.
3.  **Deep History Sync (1h)**: `deep_history_sync()` scans the last 24-48 hours of actual trade history to find exits missed by real-time loops.

### Performance & Multi-Profile Safety
- **Shared Account Cache**: `Trader` uses a class-level `_shared_account_cache` to track account-wide positions and orders across all profiles. This prevents "blind spots" where Profile B enters a trade because it hasn't yet synced Profile A's new entry.
- **Request Throttling**: Readiness checks (`has_any_symbol_position`) now use the 60s local/shared cache instead of redundant API calls, reducing total requests by ~80%.
- **Authoritative Reality**: The database is the primary source of truth for metadata, but the Exchange is the absolute source for state. Standardized `sl_order_id` and `tp_order_id` keys ensure 1:1 mapping.
- **Airtight Finalization**: Uses `log_trade` + `_clear_db_position` for atomic state transitions.
- **History Sync Script**: `scripts/sync_history.py` allows for manual or batch reconciliation of the last 24-48 hours to fix legacy data discrepancies.

---

## 4. Notification System Reference
The `notification_helper.py` standardizes terminal and Telegram UI.

### Key Formatters
- `format_pending_order`: Shows entry distance, score, and leverage.
- `format_position_filled`: Detailed entry stats with notional values.
- `format_position_closed`: Duration, PnL tracking, and reason (TP/SL/Manual).
- `format_status_update`: Summary of all active positions with PnL color-coding.

### Implementation Pattern
```python
# Standard Usage Pattern
from notification_helper import format_position_filled
from notification import send_telegram_message

terminal_msg, telegram_msg = format_position_filled(symbol, tf, side, price, size, notional, sl, tp, dry_run)
print(terminal_msg)
await send_telegram_message(telegram_msg)
```

---

## 5. Neural Brain (RL) Implementation
- **Architecture**: MLP with pure NumPy.
- **Features**: 12 normalized inputs (Momentum, Trend, Volatility, System State).
- **Veto/Boost**:
  - `Probability < 0.3` -> Veto (Block Entry).
  - `Probability > 0.8` -> Boost (+20% Confidence).
- **Training**: SGD optimizer running on `signal_performance.json` snapshots.
- **Data Source**: The Brain only trains on **CLOSED** trades that contain a valid market state snapshot at entry. Pending or currently active trades are never used for training.
