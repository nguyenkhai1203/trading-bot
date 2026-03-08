# CONTRIBUTING_TESTS.md - Guidelines for Writing Unit & Integration Tests

## Goal
Achieve high test coverage and reliability for the trading-bot project:
- >80% coverage for core logic (analyzer, strategy, neural_brain, risk/cooldown)
- >60% coverage for execution & reconciliation (Trader, OrderExecutor, sync logic)
- 100% coverage for critical paths (order placement, reconciliation, force close, margin handling)

## Folder Structure
tests/
├── conftest.py               # Global fixtures (mock_exchange, mock_db, tmp_path, etc.)
├── test_analyzer.py          # Signal analysis, validation, weights optimization
├── test_strategy.py          # WeightedScoringStrategy, get_signal, apply_bms_weighting
├── test_neural_brain.py      # RL model scoring, training, feature importance
├── test_trader.py            # Trader class (execution, sync, adopt ghost, reconcile)
├── test_order_executor.py    # Order placement, cancellation, SL/TP setup
├── test_cooldown_manager.py  # Cooldown logic, margin error handling
├── test_data_manager.py      # DB ops, caching, WAL mode
├── test_btc_analyzer.py      # BMS calculation, merge, zone logic
├── test_feature_engineering.py
└── test_utils.py             # symbol_helper, etc.

## Naming Convention
- Test file: `test_[module].py` (e.g., `test_trader.py`)
- Test function: `test_[method]_[scenario]`  
  Examples:
  - `test_reconcile_positions_ghost_detected_and_resolved`
  - `test_place_order_limit_fallback_to_market_after_timeout`
  - `test_get_signal_red_zone_veto_long_signal`

## Must-Have Test Types per Function/Module
1. **Happy path** – Normal input → expected output
2. **Edge cases** – min/max values, empty DataFrame, 0 trades, None/NaN
3. **Invalid input** – Raise ValueError, TypeError, AssertionError
4. **Exception handling** – NetworkError, RateLimitExceeded, OrderNotFound → proper retry/backoff or graceful fail
5. **State mutation** – DB insert/update/delete → assert row count/state changed
6. **Async correctness** – `@pytest.mark.asyncio`, await coroutines, check locks/race conditions
7. **Mock external dependencies**:
   - ccxt: mock `ccxt.async_support.Exchange`
   - Database: use `:memory:` SQLite or mock DataManager methods
   - Telegram: mock `send_telegram_message`
8. **Performance baseline** (critical functions): assert execution time < threshold (e.g., <0.5s for 1000 rows)
9. **Property-based testing** (hypothesis) for data-heavy functions:
   ```python
   from hypothesis import given, strategies as st

   @given(st.floats(min_value=1000, max_value=100000))
   def test_sl_calculation(price):
       sl = calculate_sl(price, 0.02)
       assert 0 < sl < price
   ```

## How to Run Tests
- **Run all tests**: `pytest`
- **Run specific file**: `pytest tests/test_trader.py`
- **Run with output**: `pytest -v -s`
- **Check Coverage** (requires `pytest-cov`): `pytest --cov=src tests/`

## Best Practices
- **Parametrization**: Use `@pytest.mark.parametrize` for multiple data inputs.
- **Single Responsibility**: One test function should test one specific logic path.
- **Mock vs Real**: NEVER call real exchange APIs. Always use the `mock_exchange` fixture.
- **Wait Times**: Avoid `time.sleep()`. Use `asyncio.sleep()` or mock the clock.
- **Snapshot Testing**: Use `pytest-snapshot` for complex outputs like optimized weights or large JSON configs to detect unintended regressions.
- **Chaos Testing**: Simulate random network failures or exchange timeouts in integration tests to verify gracefully logic (retry-loops, circuit breakers).
- **Concurrency & Locking**: When testing `asyncio` locks, assert that the lock is actually acquired and released, and check for deadlocks in multi-task scenarios.
- **Hypothesis (Property-based)**: Use for any math-heavy transformation (normalization, pnl calculation) to find edge cases where floats might overflow or return NaN.
