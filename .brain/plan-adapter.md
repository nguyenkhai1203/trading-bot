# AI AGENT INSTRUCTION: BYBIT TRADING BOT INTEGRATION

**Requirement:** Add Bybit as a secondary exchange to optimize trading execution (superior TP/SL/Leverage handling) while maintaining Binance for its reliability. The system must support multiple exchanges with varying data formats and ordering methods. Therefore, an **Adapter Pattern** is required to streamline trading and maximize bot efficiency.

---

## 1. Project Architecture
Apply the **Adapter Pattern** to decouple **Analysis Logic** from **Exchange Execution**.

* **Core:** Shared logic for calculating signals based on OHLCV data.
* **Adapters:** Create a `BybitAdapter` that inherits from a `BaseAdapter`.
* **Data Normalization:** All data returned from the Adapter (candles, prices, order statuses) must be normalized into a **Standard Object** before being passed to the Core.

---

## 2. Module: Data Acquisition (Candles & Prices)
* **Specifications:** 25 Tokens | 8 Timeframes | 2000 candles per set.
* **Bybit V5 Fetch Technique:** * Bybit limits requests to 1000 candles. To retrieve 2000, the Agent must perform **2 fetches** (pagination using the `since` parameter or `startCursor`).
    * Use `asyncio` to fetch 25 tokens in parallel to avoid bottlenecks, maintaining a **Rate Limit < 10 req/s**.
* **5-Second Monitoring Loop:**
    * Avoid individual calls. Use `fetch_tickers()` (without a symbol parameter) to retrieve prices for the entire market in a **single request**.

---

## 3. Module: Position & Order Management (Execution)
Transition from Binance's fragmented logic to **Bybit’s Parent-Child mechanism**:

* **Setup:** Must call `set_margin_mode('ISOLATED')` and `set_leverage()` before placing an order.
* **Order Placement:** Use `create_order` with the `params` field to:
    * Directly attach `takeProfit` and `stopLoss`.
    * Set `tpslMode='Full'` to ensure the entire position is closed upon hitting TP/SL.
    * Set `tpOrderType='Market'` and `slOrderType='Market'` for high-priority exits.
* **Auto-Cleanup Mechanism:** The AI Agent does not need separate code to delete TP/SL when an Entry order is canceled. Simply issuing `cancel_order(entry_id)` will cause Bybit to automatically cancel all attached child orders.

---

## 4. Module: Synchronization
* **Fetch Open Orders:** Every 5–10 seconds, call `fetch_open_orders()` to retrieve the actual list of active orders on the exchange.
* **Mapping:** Compare the `order_id` from the exchange with the local database.
    * If an ID disappears from the exchange without being recorded by the bot: Update status (Filled/Cancelled).
    * If market volatility occurs (reversal signal): Use `set_trading_stop` to move TP/SL for open positions.

---



Act as a Senior Crypto Trading Bot Developer. I need you to write a Python class named `BybitAdapter` using the `ccxt.async_support` library. This class must inherit from a `BaseAdapter` and follow these technical requirements:

### 1. Market Data Methods
- `get_historical_candles(symbol, timeframe, count=2000)`: 
    - Implement pagination. Bybit limits to 1000 per request, so perform 2 calls to get 2000.
    - Return a standardized OHLCV list.
- `quick_price_check()`:
    - Use `fetch_tickers()` to get all market prices in one call.
    - Return a dictionary of {symbol: last_price} for my 25 tokens.

### 2. Execution Methods
- `place_smart_order(symbol, side, amount, price, tp, sl, leverage)`:
    - First, call `set_margin_mode` to 'ISOLATED' and `set_leverage`.
    - Place a Limit order using `create_order`.
    - Inside `params`, attach `takeProfit` and `stopLoss`.
    - Set `tpslMode` to 'Full' and exit order types to 'Market'.
- `cancel_smart_order(order_id, symbol)`:
    - Cancel the entry order and assume the exchange handles TP/SL cleanup.

### 3. Sync & Update
- `sync_local_data()`:
    - Call `fetch_open_orders()` and `fetch_closed_orders()`.
    - Normalize the output into a JSON format: `{"id": str, "status": str, "filled": float, "remaining": float}`.
- `update_position_tpsl(symbol, new_tp, new_sl)`:
    - Use `set_trading_stop` to modify TP/SL for an active position.

### 4. Safety First
- Implement `ccxt.BaseError` handling for Rate Limits and Insufficient Funds.
- Use `decimal` or CCXT's `amount_to_precision` to ensure price/quantity accuracy.