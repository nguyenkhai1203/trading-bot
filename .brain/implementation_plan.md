# Fee-Aware Strategy Optimization

We discovered that the optimizer (analyzer.py) was ignoring trading commissions, which led to "Active" strategies that looked profitable in validation but lost money in real backtests due to fee accumulation.

## Proposed Changes

### [Bot & Automation]

#### [MODIFY] [bot.py](file:///d:/code/tradingBot/src/bot.py)
- Move `current_price` assignment to the start of the `run_bot` loop so it's available for exit checks.
- Add a background task to run the optimization (`analyzer.analyze`) periodically (e.g., once every 24h) if the user wants full automation.
- Ensure the bot reloads its configuration after optimization.

#### [MODIFY] [backtester.py](file:///d:/code/tradingBot/src/backtester.py)
- Ensure consistency in commission rates between the backtester and analyzer.

## Verification Plan

### Automated Tests
- Run `python src/analyzer.py` and verify that low-quality symbols (which were previously "Active" but losing) are now correctly labeled as `NO MONEY` or `TEST`.
- Run `python src/backtester.py` and check if the total PnL for active symbols stays closer to the optimizer's expectations.
