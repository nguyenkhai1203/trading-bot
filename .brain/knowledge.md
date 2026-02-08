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

## System Architecture: 3-Layer Scale
1.  **Data Layer (`MarketDataManager`)**: Centralized fetching to prevent 429 Rate Limits. Shares RAM among all TF bots.
2.  **Logic Layer (`Strategy` / `Analyzer`)**: Periodically (12h) optimizes weights based on win-rate trends.
3.  **Execution Layer (`Trader`)**: 
    - **Shared Memory**: Unified `active_positions` across all timeframes.
    - **Safety Locks**: Async mutex per symbol to prevent race conditions during order placement.
    - **Persistence**: `positions.json` mirrors the exchange state.

## Operational Commands
1.  **Activate Environment**: `.venv\Scripts\activate`
2.  **Reset System**: `Remove-Item -Recurse -Force data, reports`
3.  **Run Live/Dry Bot**: `python src/bot.py`
4.  **Run Manual Backtest**: `python src/backtester.py`

## Performance Observations (Feb 2026)
- **Shared Memory Fix**: Resolved the "disappearing positions" bug where TF bots would overwrite `positions.json`.
- **Global Guard**: Prevents overlapping symbols (e.g., BTC 15m and BTC 1h entering at the same time), ensuring risk is concentrated logically.
- **Formatting**: Logs are now limited to 3 decimal places for readable scalping notifications.
