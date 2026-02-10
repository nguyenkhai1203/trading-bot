# 📝 Changelog

## [2.1] - February 10, 2026

### 🚀 Analyzer Optimization (36x Faster!)

**Performance improvement from ~3 hours → ~5 minutes for full 125-symbol analysis**

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| 125 Symbol/TF combos | ~3 hours | ~5 min | **36x faster** |
| validate_weights() | 15s/combo | 2.9s/combo | **5x faster** |
| Cross-TF check | ~30 min (redundant) | 0s (cached) | **∞ eliminated** |

**Key optimizations:**

1. **Data & Feature Caching**
   - `load_data()` and `calculate_features()` results cached per symbol/TF
   - Previously: Called 4-5 times for same data
   - Now: Calculated once, reused everywhere

2. **Coarse-to-Fine Parameter Search**
   - Previously: Full grid search (270 combinations per symbol/TF)
   - Now: 2-phase search
     - Phase 1: Coarse search (18 combos) → Find best region
     - Phase 2: Fine search (27 combos around best) → Optimize
   - Total: ~45 combos instead of 270 = **6x reduction**

3. **Cross-TF Validation Cache**
   - Previously: `_check_other_timeframes()` re-ran validation for ALL timeframes
   - Now: Results cached during Step 2, lookup in Step 3 is O(1)
   - Eliminated ~90% of redundant computation

4. **Vectorized Backtest**
   - Pre-extract NumPy arrays instead of pandas iloc access
   - Reduced overhead in inner loop

**Quality maintained:**
- Same PnL results ($262 vs $262 for test case)
- Same Win Rate (58.3% vs 58.3%)
- Same safety checks (min trades, consistency, walk-forward)

---

## [2.0] - February 10, 2026

### 🎯 Major Features Added

#### Smart Limit Order System
- **Intelligent Cancellation**: Orders cancelled only when technical analysis invalidates setup
- **No Arbitrary Timeouts**: Removed 90-second timeout; orders wait until filled or invalidated
- **Background Monitoring**: Orders checked every 3 seconds for fill status
- **Technical Validation**: Each cycle checks if signal still valid:
  - Cancels if signal reverses (BUY→SELL or vice versa)
  - Cancels if confidence drops below 0.2
  - Cancels if no signal detected
  - Keeps waiting if signal remains valid

**Benefits:**
- Better entry prices (1.5% patience from market)
- No premature cancellation of good setups
- Automatic cleanup of invalid pending orders

#### Dynamic Leverage System
- **Score-Based Tiers**: Leverage adjusts based on signal confidence
  - Score 2.0-3.9: 8x leverage, $3 margin
  - Score 4.0-6.9: 10x leverage, $4 margin
  - Score 7.0+: 12x leverage, $5 margin
- **Fixed Margin Mode**: Consistent $3-5 per trade regardless of account size
- **Conservative Risk**: Small capital per position for portfolio stability

#### Signal Confidence System
- **40+ Technical Indicators**: Comprehensive analysis across multiple dimensions
- **Weighted Scoring**: High-quality signals (bounces, breakouts) weighted 1.5x
- **Multi-Timeframe**: Validates signals across 5 timeframes
- **Technical Confirmation**: Optional Fibonacci/S/R confirmation requirement

### 🔧 Configuration Changes

#### New Config Options (`src/config.py`)
```python
USE_LIMIT_ORDERS = True           # Enable limit order execution
PATIENCE_ENTRY_PCT = 0.015        # 1.5% better price entry
REQUIRE_TECHNICAL_CONFIRMATION = False  # Fibo/S/R confirmation
```

#### Updated Strategy Config Structure
- **Simplified**: Single `default` config instead of 125+ symbol-specific entries
- **Tier System**: `minimum`, `low`, `high` tiers with score thresholds
- **Signal Weights**: All 40 signals now have intelligent weights

### 🐛 Bug Fixes
- Fixed tier fallback returning 1x leverage instead of proper defaults
- Fixed notification display showing risk% instead of margin cost
- Fixed `get_sizing_tier()` not checking tiers in correct order
- Fixed pending orders not being tracked properly

### 📊 Performance Improvements
- Consolidated config from 125+ entries to 1 default config
- Removed blocking timeout waits for limit orders
- Background task monitoring for better async performance

### 🗑️ Cleanup
- Removed temporary test files
- Removed old log files
- Simplified strategy configuration structure

---

## Previous Versions

### [1.0] - Initial Release
- Basic trading bot with market orders
- Static leverage system
- Fixed signal weights
- Manual configuration per symbol
