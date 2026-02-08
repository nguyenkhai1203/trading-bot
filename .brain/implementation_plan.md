# System Reliability & Multi-Timeframe Synchronization

Following the initial deployment, we identified race conditions and data synchronization issues when running multiple timeframes (15m, 30m, 1h, 4h, 1d) concurrently.

## Implemented Solutions

### 1. Unified Trader Architecture
- **Problem**: Individual bot instances had their own `Trader` objects, causing corrupted `positions.json` updates and independent (conflicting) position tracking.
- **Solution**: Refactored `bot.py` to use a **Shared Trader Singleton**. All timeframe bots now feed into a single memory-mapped position manager.

### 2. Multi-TF Race Condition Guard
- **Problem**: Two bots (e.g., 15m and 1h) could detect a signal for the same symbol at the exact same millisecond, leading to double-buying.
- **Solution**: Implemented `asyncio.Lock` (per symbol) in `Trader`. The entry process (Signal Check -> Global Guard -> Place Order) is now strictly serialized.

### 3. ROE-Targeted SL/TP
- **Problem**: SL/TP percentages were being interpreted as price movements, resulting in targets that were too far away (e.g., 30% TP).
- **Solution**: Scaled targets based on the active leverage (e.g., 5% ROE / 3x Lev = 1.7% Price SL).

## Verification Success
- [x] `positions.json` now correctly shows all concurrent symbols across timeframes.
- [x] Telegram logs match local storage 1:1.
- [x] Bot no longer enters a coin if another timeframe already holds it.
