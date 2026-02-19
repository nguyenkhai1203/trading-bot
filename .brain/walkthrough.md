# Project Walkthrough & GPS

Bản đồ nhanh để navigate và debug dự án.

## 🧭 Diagnostic Map

| Functional Area | Source Module | Core Functions | Khi nào xem? |
| :--- | :--- | :--- | :--- |
| **Data & Candles** | `data_manager.py` | `update_data`, `fetch_ohlcv_with_retry` | Data stale, rate limit, CSV issues |
| **Signals & Entry** | `bot.py`, `strategy.py` | `run_step`, `get_signal` | Indicator/weight/threshold issues |
| **Order Execution** | `execution.py` | `place_order`, `cancel_order` | API errors, order not placed |
| **Position State** | `execution.py` | `active_positions`, `_save_positions` | `positions.json` corruption |
| **SL/TP** | `execution.py`, `risk_manager.py` | `tighten_sl`, `recreate_missing_sl_tp` | SL not updating, wrong prices |
| **Exchange Sync** | `execution.py` | `reconcile_positions`, adopt logic | Ghost orders, missing positions |
| **Exchange APIs** | `adapters/*.py` | `BybitAdapter`, `BinanceAdapter` | Bybit/Binance API quirks |
| **Notifications** | `telegram_bot.py`, `notification.py` | `get_status_message`, formatters | Telegram crash, wrong display |
| **Brain Training** | `signal_tracker.py`, `neural_brain.py` | `record_trade`, `predict_win_rate` | Missing training data, MLP logic |
| **Data Store** | `signal_performance.json` | — | PnL history, brain snapshot data |

---

## 🚀 Major Updates

### Đợt 2 — Bug Fixes & Unified Data Store (Feb 19, 2026)

**11 fixes hoàn chỉnh:**
- **Execution fixes**: `tighten_sl` timeframe, actual fees, duplicate adoption, Bybit `category:linear`
- **Unified Store**: `signal_performance.json` là Single Source of Truth thay cho `trade_history.json`
- **Telegram fixes**: `/status` crash, dead code, field name `pnl_usdt`
- **Brain enrichment**: `record_trade()` giờ lưu đầy đủ PnL + trade metadata

**Kết quả test**: 17/19 pass (89.5%) — 2 fail là issues cũ không liên quan.

### Đợt 1 — Multi-Exchange & Isolation (Feb 18, 2026)
- Unified Key `EXCHANGE_SYMBOL_TIMEFRAME` cho absolute state isolation
- Order Adoption: recovery từ external orders hoặc reconnect
- Bybit V5 symbol normalization + `:USDT` suffix handling

### Trước đó
- Neural Brain (MLP lightweight) với Veto/Boost logic
- Authoritative Exchange-First Reality cho `/status`
- Algo Order visibility fix (Binance SL/TP hidden orders)

---

## 🏗️ Kiến trúc Data Flow

```
Exchange (CCXT) 
    → Adapter (BinanceAdapter / BybitAdapter)  ← inject params, retry logic
        → Trader (execution.py)                 ← business logic, position state
            → TradingBot (bot.py)               ← signal → order lifecycle
                → SignalTracker                  ← record trade + brain training
                    → signal_performance.json    ← Single Source of Truth
```

---

*Chi tiết issues: [issues.md](issues.md) | Progress: [task.md](task.md)*
