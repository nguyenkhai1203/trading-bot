# Trading Bot Knowledge Base & Strategy Notes

## Reliable Signal Sources & References
- **Signal Sources**:
    - *[To be populated: specific Twitter/X accounts, Telegram channels, or TradingView authors]*
    - *[To be populated: Trading bot websites for reference strategies]*
- **Exchange APIs**:
    - [Bybit API Documentation](https://bybit-exchange.github.io/docs/v5/intro)
    - [Binance API Documentation](https://binance-docs.github.io/apidocs/spot/en/)
    - [CCXT Documentation](https://docs.ccxt.com/)

## Strategy Ideas ("Battle Tactics")
### 1. Momentum & Mean Reversion Mix
- **Entry**: RSI(14) < 30 (Oversold) + Price > EMA(200) (Long Term Trend).
- **Filter**: Volume spike > 1.5x average.
- **Exit**: RSI > 70 or Fixed RR (Risk:Reward) 1:2.

### 2. External Signal Integration
- *Plan*: Parse signals from [Source] via webhook or API.
- *Validation*: Cross-check external signal with internal indicators (e.g., Signal says BUY, check if RSI is not overbought).

## Development Guidelines (Prompt-Based Approach)
1. **Iterative Coding**: Generate code -> Test -> Fix.
2. **Safety First**: Always use `Dry Run` mode initially.
3. **Risk Management**: Never risk more than 1-2% per trade.
## Project Structure & Usage
- **Configuration**: `.env` for API keys, `src/config.py` for loading them.
- **Data Fetching**: `src/data_fetcher.py`. Supports public mode (no keys) and authenticated mode.
- **Feature Engineering**: `src/feature_engineering.py`. Calculates indicators (RSI, EMA).
- **Strategy**: `src/strategy.py`. Pure logic layer (Signal + Confidence).
- **Risk Management**: `src/risk_manager.py`. Position size + Circuit Breakers (Drawdown/Daily Loss).
- **Backtesting**: `src/backtester.py`. Run this file to simulate strategy performance.
- **Main Bot**: `src/bot.py`. The live trading loop (Dry Run enabled by default).

### How to Run
1.  **Activate Environment**: `.venv\Scripts\activate`
2.  **Backtest**: `python src/backtester.py`
3.  **Live/Dry Run**: `python src/bot.py`

### Backtest Observations
-   **Aggressive Backtest (5x Leverage, 2% Risk, 3% SL, 9% TP)**:
    -   **BTC (1h)**: **+13.3% Return**, 44% Win Rate. PROFITABLE.
    -   **ETH (1h)**: -9.5% Return, 19% Win Rate.
    -   *Insight*: Wider stops (3%) helped BTC survive volatility and hit the big 9% TP target. ETH requires different settings or strategy.

-   **Top 10 Crypto Backtest (Feb 2026)**:
    -   **BTC**: +13.3% (Profitable).
    -   **LINK**: +1.0% (Profitable).
    -   **ETH, SOL, BNB, ADA**: Loss (-9% to -17%).
    -   *Conclusion*: Strategy works best on trending assets like BTC. Altcoins are too volatile for current RSI settings; requires specific tuning or wider stops.

-   **Aggressive High-Risk Backtest (5x Lev, 8.5% SL, 15% TP)**:
    -   **Winners**: ADA (+6.6%), BNB (+3.2%).
    -   **Losers**: SOL (-40% Drawdown!), ETH (-7.7%).
    -   *Insight*: 8.5% Stop Loss is huge. It works for swing trading (ADA/BNB caught big moves) but devastates the account on choppy pairs like SOL.
    -   *Risk Warning*: Running 8 timeframes x 10 symbols = 80 Bots. This is high load. Recommend reducing to critical timeframes (1h, 4h).

-   **Weighted Scoring Strategy (Aggressive)**:
    -   **BTC**: **+21.5%** (Best Result Yet).
    -   **Altcoins**: **-90% to -99%** (Catastrophic Loss).
    -   *Cause*: High frequency trading on noise + Aggressive sizing (1.5x) + Mean Reversion logic fails on choppy alts.
    -   *Recommendation*: **Run ONLY on BTC**. Do not run on Alts with these settings.

-   **Analytical Optimization (ETH Test)**:
    -   **Before**: -99% Loss (131 trades).
    -   **After Analyzer**: **0 Trades** (0% PnL).
    -   *Insight*: The Analyzer found that *none* of the signals (EMA, MACD, etc.) had a Win Rate > 51% for ETH. It correctly assigned 0 weights. The bot stopped trading ETH, saving the account.
    -   *Verdict*: The Analyzer acts as a powerful "Safety Filter". If a coin is untradeable, it forbids trading.

-   **Fixed Margin Sizing**:
    -   **Low Confidence**: $3 USDT x 3 Leverage.
    -   **High Confidence**: $8 USDT x 5 Leverage.
    -   *Benefit*: Protects account from percentage-based drawdowns. Losses are capped at the fixed dollar amount.

## Data Handling & Rate Limits
-   **Problem**: "Too Many Requests" (HTTP 429) occurs when 15+ bots poll the API simultaneously.
-   **Solution**: `MarketDataManager` class.
-   **Centralized Fetching**: Fetches data for all symbols in a single loop, respecting rate limits.
-   **Shared Data**: Bots read from the Manager's memory (RAM), not the API directly.
-   **Disk vs RAM**: For live trading, we store data in RAM for speed. Disk storage is only for backtesting to save bandwidth.

## Reference & Audit: 3-Layer Architecture & Checklist

### 1. Overall Logic (Scalping - Low Latency/High Freq)
**Layer 1 - Data Ingestion**:
-   **Goal**: Real-time data collection (Orderbook, Trades, Funding Rate, Liquidation Map).
-   **Structure**: "OpenClaw Orchestrator" manages multiple "Claws" (fetchers) to normalize data from exchanges.

**Layer 2 - Decision Layer**:
-   **Goal**: AI Model Inference (Signal: LONG / SHORT / SKIP).
-   **Requirement**: Separate **Feature Engineering** (normalization) from **Model Serving** (logic/inference) to allow easy model swapping.

**Layer 3 - Execution & Risk**:
-   **Goal**: Validation & Execution.
-   **Flow**: Signal -> Risk Filter (Position Size, Drawdown Limit, Correlation Check) -> Order.
-   **Feedback**: Trade results feed back to the model to adjust confidence thresholds.

### 2. Audit Checklist (The "Definition of Done")
**A - Data Integrity**:
1.  Data source gaps? Missing data detection?
2.  Latency from exchange to feature engine?
3.  Cross-validate data between sources?
4.  Look-ahead bias in feature calculation?
5.  Handling exchange downtime?

**B - Model Quality**:
6.  Training data split?
7.  Out-of-sample vs In-sample Sharpe ratio?
8.  Inference latency (p95)?
9.  Concept drift detection?
11. Low confidence -> SKIP?

**C - Risk Management**:
11. **Max Drawdown** before Circuit Breaker?
12. **Daily/Weekly Loss Limit**?
