# 🚀 Quick Reference Guide

## Common Commands

### Starting the Bot
```powershell
# Start bot (production/test mode depends on config)
D:/code/tradingBot/.venv/Scripts/python.exe src/bot.py
```

### Strategy Optimization
```powershell
# Analyze all symbols and update strategy config
D:/code/tradingBot/.venv/Scripts/python.exe src/analyzer.py
```

### Backtesting
```powershell
# Test specific symbol/timeframe
D:/code/tradingBot/.venv/Scripts/python.exe src/backtester.py --symbol BTC/USDT --timeframe 1h
```

### Data Management
```powershell
# Download fresh market data
D:/code/tradingBot/.venv/Scripts/python.exe download_data.py --symbols BTC ETH SOL --timeframes 15m 1h --limit 1000

# Clear positions (fresh start)
if (Test-Path d:\code\tradingBot\src\positions.json) { Remove-Item d:\code\tradingBot\src\positions.json }
if (Test-Path d:\code\tradingBot\src\trade_history.json) { Remove-Item d:\code\tradingBot\src\trade_history.json }
```

### Position Monitoring
```powershell
# Check active positions
Get-Content d:\code\tradingBot\src\positions.json | ConvertFrom-Json | Format-List

# Check trade history
Get-Content d:\code\tradingBot\src\trade_history.json | ConvertFrom-Json | Format-List
```

---

## Configuration Quick Check

### Test vs Live Mode
**File:** `src/bot.py` or when initializing Trader

```python
# Test mode (dry_run=True)
trader = Trader(manager.exchange, dry_run=True)

# Live mode (dry_run=False) - REAL MONEY!
trader = Trader(manager.exchange, dry_run=False)
```

### Leverage & Margin Settings
**File:** `src/strategy_config.json`

```json
"tiers": {
  "minimum": {"min_score": 2.0, "leverage": 8, "cost_usdt": 3},
  "low": {"min_score": 4.0, "leverage": 10, "cost_usdt": 4},
  "high": {"min_score": 7.0, "leverage": 12, "cost_usdt": 5}
}
```

### Signal Requirements
**File:** `src/config.py`

```python
# Require Fibonacci or Support/Resistance confirmation
REQUIRE_TECHNICAL_CONFIRMATION = False  # Set to True for stricter entries

# Use limit orders for better prices
USE_LIMIT_ORDERS = True
PATIENCE_ENTRY_PCT = 0.015  # 1.5% better than market
```

---

## Monitoring Bot Status

### Check if Bot is Running
```powershell
# Check running Python processes
Get-Process python -ErrorAction SilentlyContinue

# Check if positions file exists and has entries
if (Test-Path d:\code\tradingBot\src\positions.json) {
    $positions = Get-Content d:\code\tradingBot\src\positions.json | ConvertFrom-Json
    Write-Host "Active positions: $(($positions | Get-Member -MemberType NoteProperty).Count)"
}
```

### Position States
- **Pending**: Limit order placed, waiting for fill (in `trader.pending_orders`)
- **Active**: Position filled and tracking SL/TP (in `positions.json`)
- **Closed**: Position exited, moved to trade_history.json

### Telegram Notifications
Bot sends alerts for:
- ✅ **Entry**: When limit order is filled
- ⏳ **Pending**: When limit order is placed  
- ⚠️ **Cancelled**: When pending order invalidated
- 🟢 **Profit Exit**: Take profit or profitable stop loss
- 🔴 **Loss Exit**: Stop loss hit
- 🔧 **System**: Bot start, circuit breaker, errors

---

## Troubleshooting

### Bot Not Creating Positions
1. Check if `REQUIRE_TECHNICAL_CONFIRMATION = True` (may be too strict)
2. Verify symbols in `TRADING_SYMBOLS` have data in `data/` folder
3. Check `strategy_config.json` has `"enabled": true`
4. Ensure entry threshold not too high: `"entry_score_threshold": 2.0`

### Positions Not Closing
1. Verify SL/TP prices are correctly set
2. Check Telegram for exit notifications
3. In live mode, check exchange directly
4. Review `trade_history.json` for closed positions

### High Number of Cancelled Pending Orders
1. Normal behavior - bot validates signals every cycle
2. Signals reversing quickly = volatile market
3. Consider adjusting `entry_score_threshold` higher for stronger signals
4. Check if timeframe too low (15m more volatile than 1h)

---

## Performance Tips

1. **Run analyzer weekly** to optimize signal weights for current market conditions
2. **Review closed trades** in `trade_history.json` to identify best performing timeframes
3. **Monitor win rate** per symbol - disable consistently losing pairs
4. **Adjust tiers** if positions too small/large for your account size
5. **Use higher timeframes** (1h, 4h) for more stable signals
