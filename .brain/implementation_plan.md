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

### 4. Cross-Timeframe Signal Validation (ANALYZER ENHANCEMENT)
- **Problem**: Analyzer was enabling single-timeframe signals that were unreliable and unlikely to repeat.
- **Solution**: 
  - Implement `cross_timeframe_validate()` method to test signals across 5 timeframes (15m, 30m, 1h, 4h, 1d)
  - Use **2-timeframe confirmation threshold** for HIGH confidence (not 3+ as initially designed)
  - **Philosophy**: 2 distant timeframes (15m+1h, 30m+4h, 1h+1d) = sufficient validation proof
  - **Benefit**: Allows more signals to pass while preventing overfitting to single timeframes

**Confidence Scoring**:
- VERY HIGH: 4+ timeframes profitable → Deploy with full position size
- HIGH: 2-3 timeframes profitable → Deploy (most reliable, 2+ REQUIRED)
- MEDIUM: 1 timeframe profitable → Mark as WATCH (needs manual review)
- LOW: 0 timeframes profitable → REJECT (no evidence)

**Implementation Details**:
1. Update `cross_timeframe_validate()` method (line 125 in analyzer.py)
   - Changed condition from `profitable_tfs >= 3` to `profitable_tfs >= 2` for HIGH confidence
   - Added comment explaining 2-distant-TF philosophy

2. Update `run_global_optimization()` (line 348 in analyzer.py)
   - Changed enabling condition: `other_tf_supported >= 2` (was `>= 1 OR high single-TF`)
   - Ensures all ENABLED configs have 2+ timeframe evidence
   - WATCHED status for single-TF signals to allow manual override

3. Update enabling logic status messages
   - ENABLED: "Multi-TF Confirmed" (2+ timeframes profitable)
   - WATCH: "Limited Timeframe Evidence" (1 timeframe profitable)

## Verification Success
- [x] `positions.json` now correctly shows all concurrent symbols across timeframes.
- [x] Telegram logs match local storage 1:1.
- [x] Bot no longer enters a coin if another timeframe already holds it.
## Reinforcement Learning (RL) 2.0 Upgrade

We upgraded the strategy layer with a lightweight Neural Network (MLP) to act as a "Veto/Boost" layer, improving trade quality based on historical performance.

### Components
1. **Neural Brain (`neural_brain.py`)**:
   - Lightweight MLP built using only `numpy` (inference <1ms).
   - 12-node input layer (capturing 40+ indicators normalized into 12 features).
   - Hidden layer (ReLU) and Sigmoid output layer.
2. **Strategy Integration (`strategy.py`)**:
   - `WeightedScoringStrategy` now queries the Brain for a confidence score (0.0 to 1.0).
   - **Veto Logic**: Score < 0.3 blocks the entry.
   - **Boost Logic**: Score > 0.8 provides a +20% confidence boost.
3. **Training System (`train_brain.py`)**:
   - Simple SGD optimizer that trains on `signal_performance.json` snapshots.
   - Targets: 1.0 for WINS, 0.0 for LOSSES.

### Verification Status
- [x] Brain successfully loads `brain_weights.json`.
- [x] Inference time verified at <0.5ms.
- [x] `train_brain.py` correctly parses snapshots and completes 100 epochs.

## Standardizing Exchange Symbols & Analyzer Logic (Current)

Following the Bybit integration, we standardized symbol handling and made the optimization layer fully exchange-aware to handle different fee structures and liquidity.

### Improvements
1. **Symbol Segregation**: 
   - `BINANCE_SYMBOLS` and `BYBIT_SYMBOLS` defined separately in `config.py`.
   - Allows targeting exchange-specific tokens (e.g., $HYPE on Bybit).
2. **Exchange-Aware Optimization**:
   - `StrategyAnalyzer` now saves weights with exchange prefixes (e.g., `BYBIT_BTC/USDT_1h`).
   - Prevents weights from one exchange overwriting another.
3. **Data Persistence**:
   - DataManager saves candles as `{EXCHANGE}_{SYMBOL}_{TF}.csv` for clear isolation.
4. **Bybit 8h Mapping**:
   - Automatically maps unsupported '8h' timeframe to '4h' for Bybit API compatibility.

### Verification Status
- [x] Analyzer saves separate sections in `strategy_config.json`.
- [x] DataManager fetches and saves data with exchange namespaces.
- [x] Bot instances use the correct exchange context for PnL and signal checks.
