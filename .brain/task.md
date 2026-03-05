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

### Bug Fixes (Iteration 1 - Feb 2026)
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

### Iteration 3 — Runtime Stability & Data Recovery (Feb 22, 2026)
- [x] Fix 12: `reconcile_positions` — removed redundant params, using adapter delegation.
- [x] Fix 13: `data_manager.py` — removed duplicate `close()` call.
- [x] Fix 14: Ensure all `execution.py` API calls use Adapter instead of raw CCXT.
- [x] **New**: Implementation of "Airtight Phantom Win" protection logic.
- [x] **New**: Mandatory standard prefixes (`EXCHANGE_SYMBOL`) for all position/trade keys.
- [x] **New**: Unified root `/data/` path architecture.
- [x] **New**: Incremental OHLCV fetching logic in `download_data.py`.
- [x] **Recovery**: Restored standardized trade history in `signal_performance.json`.

### Iteration 4 — Core Sync & Adoption Bug Fixes (Feb 22, 2026)
- [x] **Bugfix**: Resolved "Zombie Position" bug by removing aggressive prefix stripping logic in `execution.py`.
- [x] **Bugfix**: Fixed Binance SHORT position adoption (`reconcile_positions` now uses `abs(qty) > 0`).
- [x] **Bugfix**: Standardized position extraction in `telegram_bot.py` to match internal state reliably.

### Iteration 5 — Database & Multi-Profile Integration (Feb 26, 2026)
- [x] Implement SQLite Schema (`schema.sql`) for profiles and trades
- [x] Create `DataManager` (Singleton, WAL mode, aiosqlite)
- [x] Migrate `RiskManager` and `Trader` to DB persistence
- [x] Data Migration Script (`migrate_json_to_sql.py`) for legacy JSON recovery
- [x] Refactor `bot.py` for Multi-profile Dependency Injection
- [x] Implement Process-based `launcher.py` for concurrent execution
- [x] Fix critical sync bugs and status mapping (`filled` -> `ACTIVE`)

### Iteration 6 — Advanced Reconciliation & History Recovery (March 2, 2026)
- [x] Add 60s "Ghost Detection" loop in `sync_with_exchange`
- [x] Implement 10m "Full Reconciliation" for SL/TP consistency and orphan adoption
- [x] Create 1h "Deep History Sync" for exhaustive trade audit
- [x] Mandatory Exchange Order ID persistence for all entries

### Iteration 7 — Performance & Rate-Limit Shield (March 3, 2026)
- [x] Implement account-level state caching in `Trader`
- [x] Optimize `has_any_symbol_position` to use cached state (80% API reduction)
- [x] Add 500ms request throttling for background history fetches

### Iteration 8 — Multi-Profile Safety & Shared State (March 3, 2026)
- [x] Implement `Trader._shared_account_cache` (class-level) for profile cross-talk
- [x] Update `place_order` to proactively sync shared state
- [x] Standardize `newClientOrderId` with profile-specific prefixes (`P{id}_...`)
- [x] Fix `NameError` crash in main loop

### Iteration 9 — Precise SL/TP Sync & Cooldowns (March 4, 2026)
- [x] Implement `_infer_exit_reason` precision-first resolver
- [x] Fix entry_price=0 logic guard in `reconcile_positions`
- [x] Integrate resolver into Ghost, Reconcile, and Deep Sync paths
- [x] Ensure mandatory SL cooldown triggering across all paths
- [x] Verify with 6/6 passing automated tests

---

## 🏗 Future & In-Progress

### Phase 3: Integration & UX Polish
- [ ] Add Profile/Exchange labeling to Telegram notifications
- [ ] Terminal color-coding for multi-profile differentiation
- [ ] Map adapter-specific status codes to uniform DB states

### Iteration 10 — Sizing Safety & Cap Enforcement (March 4, 2026)
- [x] Fix logic bug in `get_sizing_tier` (ignore score issue)
- [x] Enforce `GLOBAL_MAX` caps universally in sizing logic
- [x] Align `config.py` tiers with global margin limits
- [x] Verify tiered sizing and caps with test script
- [ ] Neural Brain training automation via DB logs

### Phase 4: Verification & Launch
- [ ] 24h Dry-run stress test with 5+ concurrent profiles
- [ ] Final security audit (API key isolation checks)
- [ ] Full regression testing on signal generation logic

