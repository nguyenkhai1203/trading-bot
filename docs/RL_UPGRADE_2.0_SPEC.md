# 🤖 Trading Bot RL Upgrade 2.0: Master Specification

This document outlines the architectural transition of the trading bot from a **Heuristic Adaptive System** to a professional **Reinforcement Learning (RL) Framework**.

## 🎯 The Vision 2.0
The 2.0 upgrade aims to move beyond manual "point-scoring" logic. We are building a bot that:
1. **Sees accurately** (Standardized data).
2. **Knows its own status** (Portfolio awareness).
3. **Optimizes for quality**, not just quantity (Sharpe Ratio).
4. **Accounts for real-world friction** (Slippage & Fees).

---

## 🏛️ Architectural Pillars

### 1. The "Observer" (Observation Space)
*   **Feature Scaling (Normalization)**: Using `StandardScaler` to ensure signals like RSI (0-100) and Volume (Millions) are translated to a common range [-3, 3]. This prevents the bot from being "blinded" by large numbers.
*   **Portfolio State**: Integrating account balance, unrealized PnL, and current exposure into the decision matrix. The bot should trade differently when at a loss vs. when at a profit.

### 2. The "Brain" (Neural Decision Layer)
*   **Non-Linear Logic**: Moving from simple "Weights + Weights" to a **Neural Network (MLP)**. This allows the bot to understand complex conditions (e.g., "RSI is high BUT Volatility is low, so stay in").
*   **Inspiration from TensorFlow**: Implementing a lightweight neural layer using `numpy` for zero-latency execution.

### 3. The "Judge" (Reward Function & Backtesting)
*   **Sharpe/Sortino Ratio**: Instead of just "Did I make money?", the bot asks "How much risk did I take to make that money?".
*   **Backtrader Standards**: Implementing a "Real-World Friction Model". 
    *   **Commissions**: Subtracting 0.06% (market) or 0.01% (limit) from every trade.
    *   **Slippage**: Simulating 0.05% price decay on large orders.

---

## 🚀 Priority Roadmap

| Phase | Component | Key Feature | Output |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **Data & State** | Feature Scaling & Portfolio Feed | `StandardScaler` integrated into `FeatureEngineer`. |
| **Phase 2** | **Risk Logic** | Sharpe Ratio & Slippage Models | Updated `Analyzer` and `Backtester`. |
| **Phase 3** | **Neural Brain** | NP-Based Multi-Layer Perceptron | First non-linear decision maker. |

---

## 🛠️ Verification Standards
- **Standardized Features**: Mean of 0, Variance of 1.
- **Profitability vs. Drawdown**: Optimization must favor configurations with the lowest drawdown per dollar earned.
- **Latency Check**: Order placement must remain under **100ms**.
