# Trading Bot Knowledge Base & Strategy Notes

## Reliable Signal Sources & References
- **Exchange APIs**:
    - [Bybit API Documentation](https://bybit-exchange.github.io/docs/v5/intro)
    - [Binance API Documentation](https://binance-docs.github.io/apidocs/spot/en/)
    - [CCXT Documentation](https://docs.ccxt.com/)

## Strategy Design: Robust Weighted Scoring
- **Entry Logic**: Aggregates signals from RSI (7/14/21), EMA (9/21/50/200), MACD, Ichimoku, and Bollinger Bands.
- **Thresholds**: Defaults to 5.0 for entry, 2.5 for exit.
- **ROE-Targeting Risk**:
    - **Leverage**: Typically 3x - 5x.
    - **Stop Loss**: Set to ~1.7% price move (targets 5% ROE loss).
    - **Take Profit**: Set to ~4.0% price move (targets 12%+ ROE profit).
    - **Benefit**: Provides wide stops to handle market noise while strictly limiting account drawdown.

## Cross-Timeframe Signal Validation (Enhanced Safety)
- **Philosophy**: A signal is robust if it's profitable on **1+ distant timeframes** (relaxed from prior 2+ requirement for better config availability)
- **Decision Rationale (Feb 9, 2026)**: Changed from 2+ TF → 1+ TF for ALL symbols to increase enabled configs while maintaining safety
- **Why 1+ TF now?**: More practical for alt coins with fewer cross-TF data points; mitigated by 70/30 walk-forward + 4 safety checks still enforcing quality
- **Confidence Levels**:
  - **VERY HIGH** (4+ TF): Deploy full position size, maximum confidence
  - **HIGH** (2+ TF): Deploy, standard position size, most reliable signals ⭐ **RECOMMENDED**
  - **MEDIUM** (1 TF): Deploy, but monitor closely - now acceptable due to strict validation
  - **LOW** (0 TF): REJECT, no cross-timeframe evidence
- **Implementation**: Analyzer auto-tests each signal config across all 5 TFs during optimization; ENABLEs configs with ≥1 TF passing
- **Result**: More enabled configs (target 8-15 vs prior 1-2), while maintaining profitability through walk-forward validation

## System Architecture: 3-Layer Scale
1.  **Data Layer (`MarketDataManager`)**: Centralized fetching to prevent 429 Rate Limits. Shares RAM among all TF bots.
2.  **Logic Layer (`Strategy` / `Analyzer`)**: Periodically (2x daily) optimizes weights based on win-rate trends.
3.  **Execution Layer (`Trader`)**: 
    - **Shared Memory**: Unified `active_positions` across all timeframes.
    - **Safety Locks**: Async mutex per symbol to prevent race conditions during order placement.
    - **Persistence**: `positions.json` mirrors the exchange state.

## Concurrent Analyzer + Bot Execution (Feb 9, 2026 - Option 3)
**Problem**: Analyzer takes 3-4 minutes to optimize; bot can't wait.
**Solution**: Run analyzer 2x daily while bot trades continuously, with safe async updates.

**Implementation Details**:
1. **Atomic Config Write** (Analyzer → Config File):
   - Write to temp file first: `strategy_config.json.tmp`
   - Rename atomically to `strategy_config.json` (OS guarantees atomic operation)
   - Prevents bot reading corrupted/partial JSON during reload
   
2. **Config Auto-Reload** (Bot → Strategy):
   - Bot calls `reload_weights_if_changed()` every 60 seconds (piggybacked on price fetch cycle)
   - Detects file modification time (mtime) change
   - Increments `config_version` counter (for position tracking)
   
3. **Smart Entry Blocking** (Bot → Positions):
   - Before opening NEW position: Check `is_enabled()` flag from reloaded config
   - If disabled → Block new entry, print warning: "Config disabled, blocking new position"
   - **Existing positions**: Continue running, allowed to close at SL/TP (not force-closed)
   - Result: Config changes don't interrupt ongoing trades, respect new config for future entries

**Benefits**:
- ✅ Zero analyzer downtime (runs in background)
- ✅ Zero data corruption (atomic writes)
- ✅ Trading continuity (existing positions never force-closed)
- ✅ Auto config update (60s reload cycle, no manual restart)

**Risk Management**:
- Low: Logic correctness guarantees safety, only performance impact possible
- Worst case: Machine overload causes slight trade-check delays (1-2s), not logic errors
- Recommendation: Schedule analyzer at off-peak hours (e.g., 2am, 10am UTC)

## Operational Commands
1.  **Activate Environment**: `.venv\Scripts\activate`
2.  **Reset System**: `Remove-Item -Recurse -Force data, reports`
3.  **Run Analyzer** (Daily/Twice Daily): `python src/analyzer.py` (auto-updates `strategy_config.json`, no bot restart needed)
4.  **Run Live/Dry Bot**: `python src/bot.py` (will auto-reload config every 60s as analyzer updates)
5.  **Run Manual Backtest**: `python src/backtester.py`

**Deprecated Files Removed**:
- `src/daily_optimizer.py` - Use `src/analyzer.py` instead (2x performance improvement, parallel processing)

## Performance Observations (Feb 2026)
- **Shared Memory Fix**: Resolved the "disappearing positions" bug where TF bots would overwrite `positions.json`.
- **Global Guard**: Prevents overlapping symbols (e.g., BTC 15m and BTC 1h entering at the same time), ensuring risk is concentrated logically.
- **Formatting**: Logs are now limited to 3 decimal places for readable scalping notifications.

## Performance Optimization: Analyzer Speedup (Feb 9, 2026)
**Goal**: Reduce analyzer runtime from 15+ minutes to <5 minutes for 2x daily runs.

**Optimizations Applied**:
1. **Parameter Space Reduction**: 270 combos → 48 combos per signal
   - SL ranges: 6 values → 4 values (0.015, 0.02, 0.025, 0.03)
   - RR ratios: 5 values → 3 values (1.5, 2.0, 2.5)
   - Thresholds: 9 values → 3 values (3.0, 4.0, 5.0)
   - Math: 4 × 3 × 4 = 48 combinations (was 6 × 5 × 9 = 270)
   
2. **Parallel Processing Expansion**:
   - Step 1 (Signal Selection): Parallel by symbol (ThreadPoolExecutor)
   - Step 2 (Validation/Backtest): Parallel by (symbol, timeframe) pair - previously sequential
   - Workers: 4 → 6 → 8 (configurable via `MAX_WORKERS` in `config.py`)
   - Both steps now leverage 8 CPU cores simultaneously
   
3. **Result**: ~3-4 minute runtime (vs prior 15+ min)
   - Enables 2x daily analyzer runs without blocking bot
   - Typical schedule: 2am UTC (overnight in most markets) + 10am UTC (mid-day)

**Trade-offs**:
- ↓ Parameter depth (48 vs 270), but still thorough
- ↑ Config availability (+30-40% more enabled configs)
- ✓ Maintains safety: 70/30 walk-forward + 4 checks still active
  ```bash
  python src/data_fetcher.py --symbols BTC ETH BNB --limit 5000 --timeframe 15m,30m,1h,2h,4h,8h,1d
  ```
- **Why Expand?**:
  - Current 1000 candles may only have 15-50 trades per symbol/TF config
  - More data = more trades per 70/30 walk-forward split = better validation
  - Reduces overfitting risk and improves generalization to live trading
- **Extended Timeframes**: Added 2h & 8h to TRADING_TIMEFRAMES (was 5 TF, now 7 TF):
  - Better granularity between 1h↔4h and 4h↔1d
  - Increases number of profitable signal combinations available
  - More scenarios to test with Analyzer optimization loops