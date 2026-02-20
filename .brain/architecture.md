# System Architecture & Technical Implementation

This document serves as the technical blueprint for the trading bot, documenting the implementation details of the Adapter Pattern, Singleton architectures, and synchronization logic.

## 1. Core Architecture: 3-Layer Scale
The system is decoupled into three distinct layers to ensure scalability and maintainability.

1.  **Data Layer (`MarketDataManager`)**: 
    - Handles centralized fetching and normalization.
    - **Exchange Namespacing**: Data is isolated as `{EXCHANGE}_{SYMBOL}_{TF}.csv` to prevent collisions.
    - **Time Sync**: Implements manual offset with a -5000ms safety buffer to resolve Binance -1021 timestamp errors.
2.  **Logic Layer (`Strategy` / `Analyzer`)**: 
    - **Strategy Engine**: 40+ technical indicators weighted by performance.
    - **Analyzer**: Periodic re-optimization (2x daily) with Parallel Grid Search (8 workers).
    - **Neural Brain**: Lightweight NumPy-based MLP for Veto/Boost confirmed signals.
3.  **Execution Layer (`Trader`)**: 
    - **Singleton Architecture**: Unified `Trader` singleton manages all timeframe bots to prevent state corruption.
    - **Safety Locks**: Symbol-level async mutexes (`asyncio.Lock`) serialize entry processes.
    - **Authoritative Sync**: Exchange-first reality where `positions.json` is treated as a cache of exchange state.

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
- **Failover Logic**: Implements automatic fallback in `cancel_order`. If a standard cancellation fails with "Order not found," it automatically attempts the Algo endpoint (and vice versa). This ensures robust cleanup during position closing or signal reversals.
- **Symbol Normalization**: Maps unified symbols to Binance internal formats (e.g., `BTC/USDT:USDT` -> `BTCUSDT`).

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

### Deep Sync Loop
- **Authoritative Reality**: Telegram `/status` fetches live data from exchanges.
- **Ghost Removal**: Proactively purges local positions if exchange data shows no contracts.
- **Order Adoption (NEW)**: Scans for stray entry orders on the exchange and adopts them into the local state if unidentified.
- **Wait-and-Patience**: Polling mechanism for limit fills (2s intervals) with automatic timeout cancellation.

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
