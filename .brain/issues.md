# Trading Bot Issues — Status Log

Tài liệu theo dõi tất cả issues: nguyên nhân, giải pháp, và trạng thái.

---

## ✅ Đã sửa hoàn toàn (Fix 1–11)

| Fix | Mô tả | File | Dòng |
|-----|-------|------|------|
| 1 | `tighten_sl` thiếu `timeframe` → SL không cập nhật trên sàn | `execution.py` | ~1521 |
| 2 | `log_trade` dùng phí ước tính 0.06% → dùng phí thực từ `fetch_my_trades` | `execution.py` | ~1385 |
| 3 | `reconcile_positions` không extract phí thực vào `_exit_fees` | `execution.py` | ~2452 |
| 4 | Logic Adopt chạy 2 lần (Block 1 sơ sài + Block 2 đầy đủ) | `execution.py` | ~2198 |
| 5 | `force_close_position` thiếu `category: linear` cho Bybit V5 | `execution.py` | ~1672 |
| 6 | `/status` crash `NameError: force_live` chưa được define | `telegram_bot.py` | ~59 |
| 7 | Dead code sau `return` statement | `telegram_bot.py` | ~215 |
| 8 | `record_trade()` thiếu field: `pnl_usdt`, `entry_price`, `exit_price`, `qty`, v.v. | `signal_tracker.py` | ~68 |
| 9 | `log_trade` vẫn ghi vào `trade_history.json` thay vì unified store | `execution.py` | ~1407 |
| 10 | `get_current_balance()` đọc từ `trade_history.json` thay vì `signal_performance.json` | `bot.py` | ~1017 |
| 11 | `get_summary_message` dùng field `pnl_usd` sai tên → phải là `pnl_usdt` | `telegram_bot.py` | ~250 |

> **`signal_performance.json` giờ là Single Source of Truth.**  
> `trade_history.json` đã deprecated — không còn ghi vào file này nữa.

---

## 🔴 Issues mới phát hiện (runtime)

### 12. Bybit Fetch Positions Thất Bại
- **Lỗi**: `[Bybit] Fetch positions failed: bybit GET https://api.bybit.com/v5/position/list?settleCoin=USDT&limit=200&category=linear`
- **Nguyên nhân**: `reconcile_positions` trong `execution.py` gọi `fetch_positions` với params `{'type': 'future'}` — đây là Binance param, Bybit không hiểu. Bybit V5 cần `category=linear` (đã được `BybitAdapter.fetch_positions` xử lý), nhưng Trader gọi `self.exchange.fetch_positions` thay vì `self.exchange.fetch_positions()` (adapter method), dẫn đến raw CCXT gọi với sai params.
- **Giải pháp cần làm**:
    - [ ] **Fix 12**: Trong `reconcile_positions` (execution.py L2167), bỏ `params={'type': 'future'}` — để Adapter (Bybit/Binance) tự xử lý params mặc định. Gọi đơn giản: `await self._execute_with_timestamp_retry(self.exchange.fetch_positions)`

### 13. Binance `_execute_with_timestamp_retry` AttributeError
- **Lỗi**: `'binance' object has no attribute '_execute_with_timestamp_retry'`
- **Nguyên nhân**: Một số nơi trong `execution.py` truyền `self.exchange.fetch_xxx` (method của Adapter) nhưng lại gọi retry với object context sai — cụ thể khi `self.exchange` là adapter hợp lệ, nhưng `data_manager.py` L320 có `close()` bị duplicate (đè lên `close()` L113, làm mất `initialized=False` flag, dẫn đến adapter không reinit đúng sau lần đầu).
- **Cần kiểm tra thêm**: `data_manager.py` có 2 hàm `close()` (`L113` và `L320`), hàm sau đè lên trước và làm mất logic `self.initialized = False`.
- **Giải pháp**:
    - [ ] **Fix 13**: Xóa duplicate `close()` ở cuối `data_manager.py` (L320-321).
    - [ ] **Fix 14**: Trong `reconcile_positions`, gọi `self.exchange.fetch_positions()` không có extra params cho Bybit — delegate hoàn toàn cho Adapter.

---

## � Kiến trúc Data Store hiện tại

```
signal_performance.json  ← Single Source of Truth (PnL + Brain Training)
positions.json           ← Live position state
trade_history.json       ← DEPRECATED (không còn ghi, chỉ đọc nếu migrate)
```

---

## 📌 Ghi chú kỹ thuật

- **Bybit V5**: Mọi lệnh futures phải có `category: linear`. `BybitAdapter` tự động inject param này.
- **Binance Algo Orders**: SL/TP là "Algo Orders" — phải dùng `fapiPrivateGetOpenAlgoOrders`, không phải `fetch_open_orders` thông thường.
- **Pattern chuẩn**: `EXCHANGE_SYMBOL_TIMEFRAME` (ví dụ: `BYBIT_BTC_USDT_1h`).