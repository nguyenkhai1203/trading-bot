# System Architecture & Technical Implementation

This document serves as the technical blueprint for the trading bot, documenting the implementation details of the Adapter Pattern, Singleton architectures, and synchronization logic.

## 1. Core Architecture: Clean Architecture (4-Layer)
The system is being transitioned from a 3-layer setup to a strict **Clean Architecture** to ensure industrial-grade maintainability and performance.

1.  **Domain Layer (`src/domain/`)**: 
    - **Entities**: Pure Pydantic models (Trade, Position, Order) ensuring type safety and consistency.
    - **Services**: Pure business logic (Risk calculation, SL/TP dynamic adjustment) independent of any external frameworks or APIs.
2.  **Application Layer (`src/application/`)**: 
    - **Use Cases**: Orchestrates the flow of data between the Domain and Infrastructure.
    - **Trading Orchestrator**: Replaces the legacy `Trader` god-object logic.
    - **Optimization Orchestrator**: Manages the strategy re-tuning process.
3.  **Infrastructure Layer (`src/infrastructure/`)**: 
    - **Adapters**: Concrete implementations for Bybit and Binance, mapping raw API responses to Domain Models.
    - **State Management**: Centralized `AccountSyncService` that acts as a shared cache provider to minimize API requests and ensure cross-profile atomic state.
    - **Persistence**: Database (SQLite) and File implementations.
4.  **Presentation/Entry Layer (`src/`)**: 
    - **CLI Wrappers**: `bot.py` and `analyzer.py` provide the user interface while delegating all logic to the Application layer.

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
- **AccountSyncService**: A centralized provider that tracks account-wide positions and orders across all profiles. This prevents "blind spots" where Profile B enters a trade because it hasn't yet synced Profile A's new entry.
- **Smart Data Sync (Bridge & Patch)**: Redesigned `MarketDataManager` to minimize API overhead by 95%+.
    - **Boundaries**: Full OHLCV fetches occur *only* at candle closures, using Exchange Server Time for 100% precision.
    - **Bridging**: Batch Tickers patch Open/High/Low/Close of the "live" candle in memory every few seconds.
    - **Indicator Refresh**: TA features are re-calculated instantly on the patched data, ensuring signals do not "repaint" and are always reflective of the absolute latest price.
- **Request Throttling**: Uses a shared high-performance cache (`to_dict('records')` optimization) instead of redundant API calls, reducing total requests by ~80%.
- **Vectorized Backtesting**: Replaces `df.iterrows()` with vectorized or dict-record loops to increase backtest speed by 100x.

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
116: 
117: ---
118: 
119: ## 6. High-Efficiency Data Management
120: To prevent rate-limiting (e.g., Bybit `10006`) and maximize loop speed, the system uses a tiered fetching strategy:
121: 
122: ### A. Ticker Caching (2s TTL)
123: - **Mechanism**: `MarketDataManager.fetch_ticker` first checks a memory cache before calling the exchange.
124: - **Benefit**: Eliminates redundant API calls for the same symbol across multiple profiles/tasks within the same heartbeat cycle.
125: 
126: ### B. Smart Candle Sync (Bridge & Patch)
127: - **Source of Truth**: The heavy `fetch_ohlcv` call is ONLY performed at the start of a new candle period (e.g., exactly at 14:00, 14:15).
128: - **Real-time Bridging**: Between candle boundaries, the bot uses the lightweight Batch Ticker API (`fetchTickers()`) to "patch" the latest close, high, and low of the active candle in memory.
129: - **Bandwidth Savings**: Reduces OHLCV API traffic by ~95% while keeping all indicators and trading signals 100% real-time.
130: 
131: ### C. Active Symbol Prioritization
132: - **Logic**: Symbols with active positions or pending orders bypass the background staggered update queue. They are updated every cycle (10-15s) to ensure ultra-sensitive TP/SL monitoring, while inactive symbols are updated every 60-120s.
133: 
134: ### D. Timeframe Deduplication
135: - **Mechanism**: Fetch only the lowest required timeframe (e.g., 1h). Indicators for higher timeframes (4h, 1d) share the same underlying price feed, avoiding redundant requests for the same token.
