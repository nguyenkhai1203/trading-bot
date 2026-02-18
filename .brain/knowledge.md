# Trading Bot Knowledge Base & Strategy Notes

## 📊 Strategy Design: Robust Weighted Scoring
- **Entry Logic**: Aggregates signals from RSI (7/14/21), EMA (9/21/50/200), MACD, Ichimoku, and Bollinger Bands.
- **Thresholds**: Defaults to 5.0 for entry, 2.5 for exit.
- **ROE-Targeting Risk**:
    - **Leverage**: Typically 3x - 10x.
    - **Stop Loss**: Set to ~1.7% price move (targets 5% ROE loss at 3x).
    - **Take Profit**: Set to ~4.0% price move (targets 12%+ ROE profit).
    - **Benefit**: Provides wide stops to handle market noise while strictly limiting account drawdown.

## 🧠 Optimization vs. Execution
- **Confidence Level (Filter)**: Statistical evidence across multiple timeframes.
- **Score (Sizing)**: Real-time signal strength for a specific candle.

---

## 🩸 Lessons Learned & "Vết sẹo" History

This section documents critical lessons learned through debugging sessions to ensure we never repeat past mistakes.

### 1. The "positions.json" Overwrite Disaster (Race Condition)
- **Lesson**: When running multiple timeframes (Multi-TF), independent bot instances were trying to read/write `positions.json` simultaneously, leading to newly opened positions being wiped out.
- **Solution**: Use a **Shared Trader Singleton** and `asyncio.Lock` per Symbol. Only one Trader object manages the state and file writes.

### 2. The "Double Entry" Incident (Global Symbol Guard)
- **Lesson**: Signals can appear simultaneously on the 15m and 1h charts. Without protection, the bot would open two separate positions for the same coin, doubling the risk.
- **Solution**: `has_any_symbol_position` must check the entire landscape (Exchange + Local state + Pending orders) before allowing a new entry.

### 3. "Price % vs ROE %" Confusion (Risk Management)
- **Lesson**: Initially, the bot calculated SL=5% as 5% of the coin price. With 10x leverage, a 5% price move results in a 50% account loss.
- **Solution**: Always calculate SL/TP based on **ROE Target**. Formula: `Price_SL = (ROE_Target / Leverage)`.

### 4. Timestamp Drift Error (Binance -1021)
- **Lesson**: If the local machine clock drifts by more than 1s, Binance rejects all orders. CCXT auto-sync isn't always reliable.
- **Solution**: Use a manual offset with a -5000ms safety buffer. Always subtract a buffer before assigning a timestamp to a request.

### 5. "Invisible" Algo Orders (Binance SL/TP)
- **Lesson**: Binance Futures treats SL/TP as `algoOrders`. Calling the standard `fetch_open_orders` will NOT return them, leading the bot to think they don't exist and creating redundant orders.
- **Solution**: Use the specific `/fapi/v1/algoOrder` endpoint or fallback to Position data to determine SL/TP state.

### 6. "Qty Invalid" (Bybit Precision)
- **Lesson**: Every exchange and pair has a different `qtyStep`. Incorrect decimal rounding causes immediate order rejection.
- **Solution**: Use `decimal` or the CCXT `amount_to_precision` helper exclusively. Never hardcode rounding.

### 7. Heartbeat Hang (Dry-Run Loop)
- **Lesson**: In Dry-run mode, fetching data for 125 pairs (25 coins x 5 TFs) every minute consumes excessive resources and triggers 429 rate limits.
- **Solution**: In Dry-run, prioritize using cached CSV data fetched during the last Analyzer run. Only fetch live data when strictly necessary.

### 8. "Order Not Found" during Cancellation (Bybit Conditional)
- **Lesson**: Bybit separates standard and conditional (trigger) orders into two queues. Trying to cancel a trigger order using the standard ID results in a 404.
- **Solution**: Retry cancellation with the `trigger=True` or `is_algo=True` flag if the first attempt fails.

---

## ⚙️ Operational Commands
1.  **Activate Environment**: `.venv\Scripts\activate`
2.  **Run Analyzer**: `python src/analyzer.py`
3.  **Run Bot**: `python src/bot.py`
4.  **Reset System**: `Remove-Item src/positions.json, src/trade_history.json`

## 🧠 Neural Brain (RL) Deep Dive
- **Veto (< 0.3)**: Blocks the order if the win probability is low, even with strong indicators.
- **Boost (> 0.8)**: Increases confidence if the model predicts a high success probability.
- **Training**: At least 20 trade snapshots are required before the model becomes active.
