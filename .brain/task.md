# Trading Bot — Task List

## ✅ Completed

### Infrastructure
- [x] Setup Environment & API Connection (ccxt, dotenv, pandas, numpy)
- [x] Basic connection test for Bybit/Binance
- [x] `MarketDataManager` with rate limit protection
- [x] Data Persistence (CSV) with exchange isolation
- [x] `WeightedScoringStrategy` (40+ indicators)
- [x] `Analyzer` for asset-specific weight optimization
- [x] Cross-Timeframe Validation (2-TF confirmation)
- [x] Neural Brain (RL MLP) integration
- [x] ROE-Scaled SL/TP (5% SL / 12%+ TP ROE targets)
- [x] Profit Lock (SL → Profit at 80% TP)
- [x] TA-based TP Extension (ATR/SR)
- [x] `BaseAdapter` interface (Unified API behavior)
- [x] `BybitAdapter` (Bybit V5, category:linear, conditional orders)
- [x] `BinanceAdapter` (Algo Orders, Symbol normalization)
- [x] Shared Trader Singleton + Async Locking
- [x] `_get_pos_key` (`EXCHANGE_SYMBOL_TIMEFRAME`) namespacing
- [x] Position Adoption Logic (stray order recovery)
- [x] Telegram notifications + rate limiting
- [x] Per-exchange optimization reports

### Bug Fixes (Đợt 1 - Feb 2026)
- [x] Fix 1: `tighten_sl` missing `timeframe` param
- [x] Fix 2: `log_trade` prioritize actual fees from `_exit_fees`
- [x] Fix 3: `reconcile_positions` extract actual fees to `_exit_fees`
- [x] Fix 4: Remove duplicate adoption block in `reconcile_positions`
- [x] Fix 5: `force_close_position` add `category: linear` for Bybit
- [x] Fix 6: `/status` crash `NameError: force_live`
- [x] Fix 7: Dead code removal in `telegram_bot.py`
- [x] Fix 8: Enrich `record_trade()` with PnL fields
- [x] Fix 9: Unify data store — `log_trade` → `signal_performance.json`
- [x] Fix 10: `get_current_balance()` read from unified store
- [x] Fix 11: `pnl_usd` → `pnl_usdt` in summary message

---

## 🔴 In Progress / Next

### Runtime Fixes (Đợt 2 - Feb 19, 2026)
- [ ] **Fix 12**: `reconcile_positions` — bỏ `params={'type': 'future'}` khi gọi `fetch_positions` để Adapter tự delegate params đúng cho từng sàn
- [ ] **Fix 13**: `data_manager.py` — xóa duplicate `close()` (L320-321) đang đè lên logic chuẩn (L113)
- [ ] **Fix 14**: Kiểm tra lại các nơi gọi raw `self.exchange.fetch_xxx` trong `execution.py` nên gọi qua Adapter thay vì CCXT object trực tiếp

### Future Improvements
- [ ] Add `/optimize` manual trigger via Telegram command
- [ ] Improve monthly/all-time summary reports
- [ ] Implement Summary Backtest phase in `run_global_optimization`
