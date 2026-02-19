# Trading Bot Knowledge Base & Strategy Notes

## 📊 Strategy Design
- **Entry Logic**: Aggregates signals from RSI (7/14/21), EMA (9/21/50/200), MACD, Ichimoku, Bollinger Bands.
- **Thresholds**: 5.0 entry, 2.5 exit (dynamic per asset via Analyzer).
- **Risk per Trade**: Fixed margin ($3–$8) with leverage 3x–10x.
- **SL/TP**: ROE-based targets. Formula: `Price_SL = ROE_target / Leverage`.

## 🧠 Neural Brain (RL)
- **Architecture**: Lightweight Numpy MLP, no external dependencies.
- **Veto (< 0.3)**: Block order even with strong indicators.
- **Boost (> 0.8)**: Increase confidence for high-probability signals.
- **Training**: Requires ≥20 trade snapshots. Snapshots stored in `signal_performance.json`.

---

## 🩸 Lessons Learned & Bug History

### 1. Race Condition — `positions.json` Overwrite
- **Lesson**: Multiple TF bots writing `positions.json` simultaneously → corruption.
- **Solution**: Shared Trader Singleton + `asyncio.Lock` per Symbol.

### 2. Double Entry — Global Symbol Guard
- **Lesson**: 15m and 1h signal at same time → 2 positions for 1 coin.
- **Solution**: `has_any_symbol_position` checks local + exchange before any new entry.

### 3. "Price % vs ROE %" Confusion
- **Lesson**: SL=5% of price with 10x leverage = 50% account loss.
- **Solution**: Always use `Price_SL = ROE_target / Leverage`.

### 4. Timestamp Drift — Binance -1021
- **Lesson**: Clock drift > 1s → Binance rejects all orders.
- **Solution**: Manual offset with -5000ms safety buffer. `BaseExchangeClient.get_synced_timestamp()`.

### 5. "Invisible" Algo Orders — Binance SL/TP
- **Lesson**: Binance Futures SL/TP are `algoOrders`, NOT returned by `fetch_open_orders`.
- **Solution**: `BinanceAdapter.fetch_open_orders` merges `fapiPrivateGetOpenAlgoOrders` internally.

### 6. "Qty Invalid" — Bybit Precision
- **Lesson**: Wrong decimal rounding → immediate rejection.
- **Solution**: Use CCXT `amount_to_precision`. Never hardcode rounding.

### 7. Heartbeat Hang — Dry-Run Rate Limits
- **Lesson**: Fetching 125 pairs every minute in dry-run → 429 rate limits.
- **Solution**: Dry-run uses cached CSV data; only fetch live when strictly needed.

### 8. Conditional Order Not Found — Bybit Cancel
- **Lesson**: Bybit has separate queues for standard vs conditional (trigger) orders.
- **Solution**: `BybitAdapter.cancel_order` automatically retries with `trigger=True` on 404.

### 9. Bybit V5 Category Missing — Futures vs Spot Confusion
- **Lesson**: Without `category: linear`, Bybit V5 routes order to Spot market.
- **Solution**: Every `BybitAdapter` method injects `{'category': 'linear'}` by default.

### 10. `fetch_positions` Param Mismatch (Binance vs Bybit)
- **Lesson**: `params={'type': 'future'}` is Binance-specific. Passing it to Bybit fails silently or causes errors.
- **Solution**: Never pass exchange-specific params from generic `Trader` code. Let each Adapter handle its own params. Call `self.exchange.fetch_positions()` with no extra params.

### 11. Duplicate `close()` in `data_manager.py`
- **Lesson**: Two `close()` methods defined in `MarketDataManager` — the second (L320) overrides the first (L113), causing `self.initialized = False` to never be set, leading to stale adapter state on reinit.
- **Solution**: Remove duplicate `close()` at L320-321.

---

## ⚙️ Operational Commands
1. **Activate env**: `source .venv/bin/activate`
2. **Run bot**: `python3 src/bot.py`
3. **Run launcher**: `python3 launcher.py`
4. **Run analyzer**: `python3 src/analyzer.py`
5. **Run self-test**: `cd src && python3 self_test.py`
6. **Reset positions**: `rm src/positions.json`

## 📁 Key Data Files
| File | Purpose |
|------|---------|
| `src/signal_performance.json` | **Single Source of Truth** — Trade history + Brain training data |
| `src/positions.json` | Live position state |
| `src/strategy_config.json` | Per-symbol, per-TF strategy weights (auto-updated by Analyzer) |
| `data/*.csv` | Historical OHLCV cache |
