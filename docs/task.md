# Trading Bot Repair Task List

## Active Objectives
- [x] Resolve "Order Spam" and SL/TP Duplication
- [x] Fix Leverage Setting Errors (400 Bad Request)
- [x] Resolve Order Visibility Discrepancy (38 vs 0 orders)

## Progress Tracker

### Emergency Fixes
- [x] CRITICAL: Fix Infinite SL/TP Spam Loop (Prevent recreation on fetch failure)
- [x] **Fix: Anti-Ghosting Protocol (3-cycle verification grace period)**
- [x] **Fix: Total Visibility for SL/TP (Support for standard + Algo orders)**
- [x] **Fix: Agnostic Symbol Matching (Support ARB/USDT vs ARBUSDT)**
- [x] **Fix: Safe Global "Reaper" (Dọn dẹp) logic for unrecognized orders**
- [x] **Fix: Suppress Leverage Error -4161 and fix REST 400 visibility**
- [x] **Fix: Timestamp Sync Safety (Manual Offset + Double-sync prevention)**
- [x] **Fix: Algo Order Visibility (Standardized id, orderId, algoId, clientAlgoId)**
- [x] **Fix: Creation Loop (Added 20s Cooldown after recreation)**
- [x] **Fix: Structural syntax in reconcile_positions**
- [x] **Cleanup: Removed redundant debug files (inspect_orders.py, orders_dump.txt)**
- [x] **Docs: Updated README.md and utility_scripts_guide.md with new tools/fixes**
- [x] **Knowledge: Added "Critical Stability Fixes" to .brain/knowledge.md**

### Stabilization
- [x] Implement Global Order Snapshot (Total visibility)
- [x] Implement Global Orphan Reaper (Safe account cleanup)
- [x] Implement "Blind Cancel" before Order Recreate (Idempotency)
- [x] **Implement "Total Visibility" for Algo Orders**

## Next Steps
- [x] Monitor bot for 24 hours to confirm stability.
- [x] Comprehensive Project Documentation Audit (README, Knowledge, Utility)
- [x] Investigation: Handling Market Reversals & Signal Flips
    - [x] Implement Universal Reaper (Cleanup 103+ orders)
    - [x] **FIX:** Optimize Reaper (Periodic 5-min Schedule + Batch Limit 20)
    - [x] Implement Active Signal Flip Exit (Force Close)
    - [x] Implement Safe Reversal Entry (Reduced size/Tighter SL)
## Phase 2: Multi-Exchange Expansion (Single Bot Architecture)
- [x] **Refactor:** Extract `BinanceAdapter` from `BaseExchangeClient`
    - [x] Create `src/adapters/base_adapter.py` (Interface)
    - [x] Create `src/adapters/binance_adapter.py` (Implementation)
    - [x] Update `bot.py` to use Adapter Pattern
- [x] **Data Isolation:** Implement Field-Based Data Separation
    - [x] Update `execution.py` to use `binance_SYMBOL` keys
    - [x] Verify `positions.json` integrity after migration
- [x] **Feature:** Implement `BybitAdapter`
    - [x] Implement Candle Fetching (Delegated to CCXT)
    - [x] Implement Order Execution (Unified Margin support via CCXT)
- [x] **Consolidate Branches**
    - [x] Merge `multi-exchange` into `version2.0` <!-- id: 40 -->
    - [x] Merge `main` (Stability Fixes) into `version2.0` <!-- id: 41 -->
    - [x] Push updated `version2.0` to remote <!-- id: 42 -->
- [ ] **Next Steps**
    - [ ] Deploy `version2.0` to production (if requested) <!-- id: 43 -->
    - [x] Implement a lean Neural Network layer (MLP) using `numpy`
    - [x] Integrate `Brain` into `Strategy.get_signal` (Observer Mode)
    - [x] Update `Bot` and `Trader` to pass feature snapshots
    - [x] Implement training loop (`src/train_brain.py`)
- [ ] **Validation:** Full System Test (Binance + Bybit)
