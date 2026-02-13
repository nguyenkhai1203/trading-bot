# Trading Bot Constitution

## Core Principles

### I. Code Quality & Modularity
Every component MUST be independently testable and well-documented. Code is organized as:
- **Modular libraries** with single responsibility (strategy.py, risk_manager.py, execution.py)
- **Clear interfaces** between modules (defined parameters, return types documented)
- **No circular dependencies** - strict module hierarchy enforced
- **Logging on all critical paths** - every decision point must be traceable

Rationale: A trading bot handles real financial operations. Code clarity prevents catastrophic errors.

### II. Risk Management (NON-NEGOTIABLE)
Trading decisions MUST incorporate explicit risk controls:
- **Circuit breaker** - MUST halt trading if max drawdown (10%) or daily loss limit (3%) exceeded
- **Position sizing** - MUST use tier-based system: minimum/low/high confidence with fixed margin per tier
- **Stop Loss & Take Profit** - MUST be calculated and enforced on EVERY position
- **Leverage capping** - MUST respect configured leverage limits (8-12x range)
- **Cooldown enforcement** - MUST wait 2 hours after stop loss before re-entry

Rationale: Uncontrolled risk can lead to total account loss. These are guardrails, not suggestions.

### III. Signal Validation & Confidence Scoring
Trading signals MUST be validated before execution:
- **Weighted scoring system** - signals combined with explicit weights (40+ technical indicators)
- **Confidence threshold** - minimum 0.2 confidence required for entry
- **Multi-timeframe confirmation** - signals must align across timeframes (at least 1 supporting timeframe)
- **Technical confirmation optional** - entries can require Fibonacci/Support-Resistance alignment (configurable)
- **Dynamic tier sizing** - position size scales with confidence (higher confidence = higher tier)

Rationale: Prevents random entries; ensures signal quality aligns with position size.

### IV. Testing Standards (TDD)
Strategy optimization and backtesting are MANDATORY:
- **Backtest before deployment** - analyzer.py optimizes weights using historical data
- **Win rate threshold** - minimum 55% required on both train/test sets
- **Consistency check** - max 25% deviation between train/test performance
- **Per-symbol analysis** - each symbol/timeframe pair evaluated independently
- **Performance reports** - CSV output for every backtest run

Rationale: Prevents strategy drift; ensures profitability is validated before trading.

### V. Operational Resilience
Bot MUST handle failures gracefully:
- **Dry-run mode** - ALL new features MUST be tested in paper trading (dry_run=True) before live
- **Deep sync reconciliation** - every 10 minutes, verify all positions match exchange state
- **Self-healing** - auto-fix missing SL/TP, recreate broken orders
- **Graceful degradation** - Telegram failures don't crash trading bot
- **Comprehensive logging** - all trades logged to trade_history.json for audit

Rationale: Trading runs 24/7; must survive network failures, API errors, partial state loss.

### VI. User Experience Consistency
All interfaces (CLI, Telegram, logs) MUST provide clear, actionable information:
- **Unified notifications** - consistent format for order/position/error messages
- **State indicators** - clearly label demo (🧪 TEST) vs live (✅ REAL) mode
- **Verbose logging** - timestamp, symbol, decision rationale, confidence scores in every log
- **JSON config-driven** - strategy_config.json controls ALL trading parameters (no hardcoding)
- **CLI verification** - self_test.py validates environment before bot launch

Rationale: Transparency prevents user errors and builds trust in automated system.

### VII. Data Quality & Freshness
Market data MUST be accurate and timely:
- **Public data only** - CCXT library for exchange-agnostic data fetching
- **Multi-timeframe support** - simultaneously analyze 15m, 30m, 1h, 2h, 4h, 8h, 1d candles
- **Feature caching** - compute features once per cycle, reuse across all bots (performance optimization)
- **Timestamp synchronization** - sync server time with Binance every hour to prevent API rejections
- **Fallback mechanisms** - if data fetch fails, preserve last known state (assume position still alive)

Rationale: Stale data = incorrect signals = losses. Caching prevents redundant computation at scale.

### VIII. Performance Requirements
Bot deployment MUST meet these operational targets:
- **Heartbeat interval** - main loop runs every 5 seconds (configurable via HEARTBEAT_INTERVAL)
- **Concurrent symbols** - 3+ symbols × 7 timeframes = 21+ bots running in parallel
- **Latency cap** - order placement < 5 seconds from signal detection to exchange submission
- **Memory footprint** - bot.py must run on systems with < 500MB available memory
- **API rate limits** - respect Binance/CCXT rate limits (enableRateLimit=True enforced)

Rationale: Trading moves fast. Slow bots miss opportunities. Resource constraints are real in production.

## Configuration Management

All trading parameters MUST be defined in src/config.py or strategy_config.json:
- **TRADING_SYMBOLS** - list of perpetual futures pairs (e.g., BTC/USDT, ETH/USDT)
- **TRADING_TIMEFRAMES** - 7-timeframe default for comprehensive analysis
- **LEVERAGE** - 8-12x range, adjusted per tier
- **RISK_PER_TRADE** - 5% default (account-relative position sizing)
- **DRY_RUN** - boolean flag for paper trading (MUST be True initially)
- **Strategy weights** - loaded from strategy_config.json, hot-reloadable during operation

Environment variables in .env:
- **BINANCE_API_KEY, BINANCE_API_SECRET** - credentials (optional in dry-run)
- **TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID** - optional (bot continues without Telegram)
- DO NOT hardcode credentials or tokens in source files

## Development Workflow

### New Feature Addition
1. Implement in isolated module (e.g., new technical indicator in feature_engineering.py)
2. Add unit tests if logic is complex
3. Add to strategy weights with default=0 (disabled)
4. Backtest using backtester.py to validate
5. Enable in strategy_config.json after validation
6. Deploy in dry-run first, monitor for 24-48 hours
7. Gradual rollout to live trading

### Bug Fixes
- If fix affects trading logic, MUST backtest to verify no regression
- If fix affects data handling, MUST validate against 100+ historical candles
- All bug fixes logged in git commit with "fix:" prefix and issue reference

### Performance Optimization
- Caching is preferred over recomputation (see Feature Caching principle)
- Async/await used for I/O operations (API calls, file I/O)
- ThreadPoolExecutor with MAX_WORKERS=8 for symbol-level parallelism
- NO blocking operations in main event loop

## Governance

### Amendment Process
- All changes to this constitution MUST be documented with rationale
- Version number increments: MAJOR (principle removals), MINOR (principle additions), PATCH (clarifications)
- Ratification date is when original constitution adopted
- Last amended date updates on every change
- Changes require consensus among core maintainers

### Compliance Verification
- Every PR MUST verify:
  - No hardcoded credentials
  - Dry-run tested if trading logic changed
  - Backtests pass if strategy weights modified
  - Logging present on critical paths
  - No new hardcoded symbols/parameters (use config.py)
  
### Runtime Guidance
See [src/bot.py](src/bot.py) for runtime behavior
See [src/analyzer.py](src/analyzer.py) for optimization logic
See [README.md](README.md) for operational procedures

---

**Version**: 1.0.0 | **Ratified**: 2026-02-13 | **Last Amended**: 2026-02-13
