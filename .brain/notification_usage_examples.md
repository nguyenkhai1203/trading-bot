# Notification Helper - Usage Examples

## Import
```python
from notification_helper import (
    format_pending_order,
    format_position_filled,
    format_position_closed,
    format_order_cancelled,
    format_status_update
)
from notification import send_telegram_message
```

## Example 1: Pending Order
```python
terminal_msg, telegram_msg = format_pending_order(
    symbol="BTC/USDT",
    timeframe="1h",
    side="BUY",
    entry_price=50000.00,
    sl_price=49500.00,
    tp_price=51000.00,
    score=4.5,
    leverage=10,
    dry_run=False
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: ⚪ [BTC/USDT 1h] 📈 PENDING LONG @ 50000.00 | SL: 49500.00 | TP: 51000.00
Telegram: ✅ LIVE | ⚪ PENDING ORDER
          BTC-USDT | 1h | 📈 LONG
          Entry: 50000.00
          SL: 49500.00 | TP: 51000.00
          Score: 4.5 | Leverage: 10x
```

## Example 2: Position Filled
```python
terminal_msg, telegram_msg = format_position_filled(
    symbol="ETH/USDT",
    timeframe="4h",
    side="SELL",
    entry_price=3000.00,
    size=0.5,
    notional=1500.00,
    sl_price=3050.00,
    tp_price=2900.00,
    dry_run=True
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: ⚪ [ETH/USDT 4h] 📉 FILLED SHORT @ 3000.00 | Size: 0.5000 ETH
Telegram: 🧪 TEST | ⚪ POSITION OPENED
          ETH-USDT | 4h | 📉 SHORT
          Entry: 3000.00
          Size: 0.5000 ETH ($1500)
          SL: 3050.00 | TP: 2900.00
```

## Example 3: Take Profit Hit
```python
from datetime import datetime

terminal_msg, telegram_msg = format_position_closed(
    symbol="BTC/USDT",
    timeframe="1h",
    side="BUY",
    entry_price=50000.00,
    exit_price=51000.00,
    pnl=100.00,
    pnl_pct=10.0,
    reason="TP",
    entry_time=datetime(2026, 2, 12, 10, 0),
    exit_time=datetime(2026, 2, 12, 12, 15),
    dry_run=False
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: 🟢 [BTC/USDT 1h] TAKE PROFIT @ 51000.00 | PnL: +$100.00 (+10.0%)
Telegram: ✅ LIVE | 🟢 TAKE PROFIT
          BTC-USDT | 1h | 📈 LONG
          Entry: 50000.00 → Exit: 51000.00
          PnL: +$100.00 (+10.0%)
          Duration: 2h 15m
```

## Example 4: Stop Loss Hit
```python
terminal_msg, telegram_msg = format_position_closed(
    symbol="SOL/USDT",
    timeframe="15m",
    side="SELL",
    entry_price=100.00,
    exit_price=102.00,
    pnl=-50.00,
    pnl_pct=-5.0,
    reason="SL",
    entry_time=datetime(2026, 2, 12, 11, 0),
    exit_time=datetime(2026, 2, 12, 11, 45),
    dry_run=False
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: 🔴 [SOL/USDT 15m] STOP LOSS @ 102.00 | PnL: -$50.00 (-5.0%)
Telegram: ✅ LIVE | 🔴 STOP LOSS
          SOL-USDT | 15m | 📉 SHORT
          Entry: 100.00 → Exit: 102.00
          PnL: -$50.00 (-5.0%)
          Duration: 45m
```

## Example 5: Order Cancelled
```python
terminal_msg, telegram_msg = format_order_cancelled(
    symbol="LINK/USDT",
    timeframe="1h",
    side="BUY",
    entry_price=15.50,
    reason="Signal reversed to SHORT",
    dry_run=False
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: ❌ [LINK/USDT 1h] CANCELLED | Reason: Signal reversed to SHORT
Telegram: ✅ LIVE | ❌ CANCELLED
          LINK-USDT | 1h | 📈 LONG
          Entry: 15.50
          Reason: Signal reversed to SHORT
```

## Example 6: Status Update
```python
positions = [
    {"symbol": "BTC/USDT", "timeframe": "1h", "pnl": 100.0, "pnl_pct": 10.0},
    {"symbol": "ETH/USDT", "timeframe": "4h", "pnl": 50.0, "pnl_pct": 5.0},
    {"symbol": "SOL/USDT", "timeframe": "1h", "pnl": 0.0, "pnl_pct": 0.0},
    {"symbol": "LINK/USDT", "timeframe": "2h", "pnl": -25.0, "pnl_pct": -2.5}
]

terminal_msg, telegram_msg = format_status_update(
    positions=positions,
    total_pnl=125.0,
    total_pnl_pct=3.1
)

print(terminal_msg)
await send_telegram_message(telegram_msg)
```

**Output:**
```
Terminal: 📊 [STATUS] 4 active | PnL: +$125.00 (+3.1%) | 2🟢 1🔴 1⚪
Telegram: 📊 POSITION STATUS UPDATE
          
          Active: 4 positions
          Total PnL: +$125.00 (+3.1%)
          
          🟢 BTC-USDT 1h: +$100.00 (+10.0%)
          🟢 ETH-USDT 4h: +$50.00 (+5.0%)
          ⚪ SOL-USDT 1h: +$0.00 (+0.0%)
          🔴 LINK-USDT 2h: -$25.00 (-2.5%)
```

## Integration Guide

### Replace in bot.py
```python
# OLD
print(f"⏳ [{symbol} {timeframe}] Pending {side} order...")
await send_telegram_message(f"PENDING ORDER\n{symbol}...")

# NEW
from notification_helper import format_pending_order
terminal_msg, telegram_msg = format_pending_order(...)
print(terminal_msg)
await send_telegram_message(telegram_msg)
```

### Replace in execution.py
```python
# OLD
print(f"Position closed: {symbol}")
await send_telegram_message(f"CLOSED\nPnL: {pnl}")

# NEW
from notification_helper import format_position_closed
terminal_msg, telegram_msg = format_position_closed(...)
print(terminal_msg)
await send_telegram_message(telegram_msg)
```
