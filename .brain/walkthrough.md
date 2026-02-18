# Project Walkthrough & GPS

This document provides a high-level map of the project's evolution and current mindset.

## 🧭 Project Mindset Map (Diagnostic & Debugging Guide)

If you encounter an error or need to modify a feature, refer to this table to know which document to read and which code module to fix.

| Functional Area | Documentation (.brain/) | Source Module | Core Logic & Functions | Purpose / What to check? |
| :--- | :--- | :--- | :--- | :--- |
| **Data & Candles** | `architecture.md` | `data_manager.py` | `update_data`, `Namespacing` | Data fetching issues, Rate limits, CSV storage. |
| **Signals & Entry** | `knowledge.md` | `bot.py`, `strategy.py` | `process_signal`, `check_confirm` | Indicator logic, weights, threshold levels. |
| **Order Execution** | `architecture.md` | `execution.py` (Trader) | `place_order`, `cancel_order` | Limit/Market orders, Exchange API errors, Retries. |
| **Position Management** | `architecture.md` | `execution.py` | `active_positions`, `_save_positions` | State in `positions.json`, data persistence. |
| **Exit (SL/TP)** | `knowledge.md` | `risk_manager.py` | `calculate_sl_tp`, `monitor_pos` | SL/TP calculation based on ROE targets. |
| **Exchange Sync** | `architecture.md` | `execution.py` | `reconcile_positions`, `ADOPT` | Cleaning "ghost" orders, adopting external trades. |
| **Exchange Connectivity**| `architecture.md` | `adapters/*.py` | `BybitAdapter`, `BinanceAdapter` | Symbol mapping, Exchange-specific API quirks. |
| **Weight Optimization** | `knowledge.md` | `analyzer.py` | `run_global_optimization` | Auto-updating `strategy_config.json`, Multi-TF sync. |
| **Neural Brain** | `architecture.md` | `neural_brain.py` | `predict_win_rate`, `MLP` | Veto/Boost logic based on Machine Learning. |
| **Notifications** | `architecture.md` | `notification.py` | `send_telegram`, `formatters` | Telegram alerts, Spam prevention (Rate limiting). |

---

## 🚀 Recent Major Updates

### 1. Multi-Exchange & Symbol Isolation (Feb 18, 2026)
- **Unified Key**: Transitioned to the `EXCHANGE_SYMBOL_TIMEFRAME` format for absolute state isolation between exchanges.
- **Order Adoption**: "Garbage collection" and "Adoption" mechanism allows the bot to recover from external orders or disconnection.

### 2. Neural Brain & RL Scoring
- **Numpy-based MLP**: Lightweight ML model for final trade validation.
- **Veto/Boost**: Reduces low-quality trades and boosts high-probability signals.

### 3. Execution Engine Hardening
- **Authoritative Sync**: Self-healing via continuous reconciliation with exchange data.
- **Algo Order Visibility**: Fixed major issues with "hidden" SL/TP orders on Binance Futures.

## 🛠️ Key Architectural Components
- **Data Layer**: Name-spaced by exchange and timeframe.
- **Execution Engine**: Async locks and Authoritative Sync.
- **Strategy Engine**: 40+ Technical indicators + Dynamic re-weighting.

---
*For daily progress, see [task.md](task.md).*
