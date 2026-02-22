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

### Đợt 3 — Runtime Stability & Data Recovery (Feb 22, 2026)
- [x] Fix 12: `reconcile_positions` — bỏ redundant params, dùng adapter delegation.
- [x] Fix 13: `data_manager.py` — xóa duplicate `close()` call.
- [x] Fix 14: Ensure all `execution.py` API calls use Adapter instead of raw CCXT.
- [x] **New**: Implementation of "Airtight Phantom Win" protection logic.
- [x] **New**: Mandatory standard prefixes (`EXCHANGE_SYMBOL`) for all position/trade keys.
- [x] **New**: Unified root `/data/` path architecture.
- [x] **New**: Incremental OHLCV fetching logic in `download_data.py`.
- [x] **Recovery**: Restored standardized trade history in `signal_performance.json`.

### Đợt 4 — Core Sync & Adoption Bug Fixes (Feb 22, 2026)
- [x] **Bugfix**: Resolved "Zombie Position" bug by removing aggressive prefix stripping logic in `execution.py`.
- [x] **Bugfix**: Fixed Binance SHORT position adoption (`reconcile_positions` now uses `abs(qty) > 0`).
- [x] **Bugfix**: Standardized position extraction in `telegram_bot.py` to match internal state reliably.

### Future Improvements
- [ ] Add `/optimize` manual trigger via Telegram command
- [ ] Improve monthly/all-time summary reports
- [ ] Implement Summary Backtest phase in `run_global_optimization`
