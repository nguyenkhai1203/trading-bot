"""
Unified Notification System
Provides consistent messaging across terminal and Telegram with color-coded emojis.

Emoji Standards:
- ⚪ Pending (white circle) - Limit order waiting
- 🟢 Profit (green circle) - Position in profit or TP hit
- 🔴 Loss (red circle) - Position in loss or SL hit
- ❌ Cancelled - Order cancelled
- 📈 LONG - Buy/Long position
- 📉 SHORT - Sell/Short position
"""

from typing import Tuple, Optional
from datetime import datetime

# Mode labels
def get_mode_label(dry_run: bool) -> str:
    """Get mode label for notifications."""
    return "🧪 TEST" if dry_run else "🟢 LIVE"

# Direction emojis
def get_direction_emoji(side: str) -> str:
    """Get direction emoji for trade side."""
    return "📈" if side.upper() in ['BUY', 'LONG'] else "📉"

def get_direction_label(side: str) -> str:
    """Get direction label for trade side."""
    return "LONG" if side.upper() in ['BUY', 'LONG'] else "SHORT"

# Format helpers
def format_symbol(symbol: str) -> str:
    """Format symbol for display (replace / with -)."""
    return symbol.replace('/', '-')

def format_price(price: float) -> str:
    """
    Format price with appropriate decimal places based on magnitude.
    - >= $1000: 2 decimals (e.g., BTC $50000.00)
    - >= $10: 3 decimals (e.g., ETH $3000.123)
    - >= $1: 4 decimals (e.g., SOL $100.1234)
    - < $1: 5 decimals (e.g., JUP $0.12345)
    """
    if price >= 1000:
        return f"{price:.2f}"
    elif price >= 10:
        return f"{price:.3f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.5f}"

def format_size(size: float, symbol: str) -> str:
    """
    Format position size with appropriate precision.
    - BTC/ETH: 4 decimals
    - Most altcoins: 2 decimals
    """
    base = symbol.split('/')[0]
    if base in ['BTC', 'ETH']:
        return f"{size:.4f}"
    else:
        return f"{size:.2f}"

def format_pnl(pnl: float, pnl_pct: float) -> str:
    """Format PnL with sign and appropriate precision."""
    sign = "+" if pnl >= 0 else ""
    # Use 2 decimals for USD, 1 decimal for percentage
    return f"{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)"

def format_duration(entry_time: datetime, exit_time: datetime) -> str:
    """Format trade duration."""
    delta = exit_time - entry_time
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# === NOTIFICATION FORMATTERS ===

def format_pending_order(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    score: float,
    leverage: int,
    dry_run: bool
) -> Tuple[str, str]:
    """
    Format pending limit order notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    mode = get_mode_label(dry_run)
    safe_symbol = format_symbol(symbol)
    
    terminal = (
        f"⚪ PENDING | [{symbol} {timeframe}] {dir_emoji} {dir_label} @ {format_price(entry_price)} | "
        f"SL: {format_price(sl_price)} | TP: {format_price(tp_price)}"
    )
    
    telegram = (
        f"{mode} | ⚪ PENDING\n"
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label}\n"
        f"Entry: {format_price(entry_price)}\n"
        f"SL: {format_price(sl_price)} | TP: {format_price(tp_price)}\n"
        f"Score: {score:.1f} | Leverage: {leverage}x"
    )
    
    return (terminal, telegram)


def format_position_filled(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    size: float,
    notional: float,
    sl_price: float,
    tp_price: float,
    dry_run: bool
) -> Tuple[str, str]:
    """
    Format position filled notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    mode = get_mode_label(dry_run)
    safe_symbol = format_symbol(symbol)
    base_currency = symbol.split('/')[0]
    
    terminal = (
        f"⚪ FILLED | [{symbol} {timeframe}] {dir_emoji} {dir_label} @ {format_price(entry_price)} | "
        f"Size: {format_size(size, symbol)} {base_currency}"
    )
    
    telegram = (
        f"{mode} | ⚪ FILLED\n"
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label}\n"
        f"Entry: {format_price(entry_price)}\n"
        f"Size: {format_size(size, symbol)} {base_currency} (${notional:.0f})\n"
        f"SL: {format_price(sl_price)} | TP: {format_price(tp_price)}"
    )
    
    return (terminal, telegram)


def format_position_closed(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    reason: str,
    entry_time: Optional[datetime] = None,
    exit_time: Optional[datetime] = None,
    dry_run: bool = False
) -> Tuple[str, str]:
    """
    Format position closed notification.
    
    Args:
        reason: 'TP', 'SL', or other exit reason
    
    Returns:
        (terminal_message, telegram_message)
    """
    # Determine emoji based on PnL
    status_emoji = "🟢" if pnl >= 0 else "🔴"
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    mode = get_mode_label(dry_run)
    safe_symbol = format_symbol(symbol)
    
    # Reason label
    if reason.upper() == 'TP':
        reason_label = "TAKE PROFIT"
    elif reason.upper() == 'SL':
        reason_label = "STOP LOSS"
    else:
        reason_label = reason.upper()
    
    terminal = (
        f"{status_emoji} [{symbol} {timeframe}] {reason_label} @ {format_price(exit_price)} | "
        f"PnL: {format_pnl(pnl, pnl_pct)}"
    )
    
    telegram_parts = [
        f"{mode} | {status_emoji} {reason_label}",
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label}",
        f"Entry: {format_price(entry_price)} → Exit: {format_price(exit_price)}",
        f"PnL: {format_pnl(pnl, pnl_pct)}"
    ]
    
    # Add duration if available
    if entry_time and exit_time:
        duration = format_duration(entry_time, exit_time)
        telegram_parts.append(f"Duration: {duration}")
    
    telegram = "\n".join(telegram_parts)
    
    return (terminal, telegram)


def format_order_cancelled(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    reason: str,
    dry_run: bool
) -> Tuple[str, str]:
    """
    Format order cancelled notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    mode = get_mode_label(dry_run)
    safe_symbol = format_symbol(symbol)
    
    terminal = f"❌ [{symbol} {timeframe}] CANCELLED | Reason: {reason}"
    
    telegram = (
        f"{mode} | ❌ CANCELLED\n"
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label}\n"
        f"Entry: {format_price(entry_price)}\n"
        f"Reason: {reason}"
    )
    
    return (terminal, telegram)


def format_adaptive_trigger(
    loss_count: int,
    symbols: list,
    btc_change: Optional[float] = None
) -> str:
    """
    Format adaptive learning trigger (terminal only).
    
    Returns:
        terminal_message
    """
    return f"\n⚠️  [ADAPTIVE] {loss_count} consecutive losses detected"


def format_status_update(
    positions: list,
    total_pnl: float,
    total_pnl_pct: float
) -> Tuple[str, str]:
    """
    Format periodic status update.
    
    Args:
        positions: List of dicts with keys: symbol, timeframe, pnl, pnl_pct
    
    Returns:
        (terminal_message, telegram_message)
    """
    active_count = len(positions)
    
    # Count profit/loss/neutral positions
    profit_count = sum(1 for p in positions if p['pnl'] > 0)
    loss_count = sum(1 for p in positions if p['pnl'] < 0)
    neutral_count = active_count - profit_count - loss_count
    
    # Terminal summary
    status_icons = f"{profit_count}🟢 {loss_count}🔴 {neutral_count}⚪" if active_count > 0 else "None"
    terminal = (
        f"📊 [STATUS] {active_count} active | "
        f"PnL: {format_pnl(total_pnl, total_pnl_pct)} | {status_icons}"
    )
    
    # Telegram detailed
    telegram_parts = [
        "📊 POSITION STATUS UPDATE\n",
        f"Active: {active_count} positions",
        f"Total PnL: {format_pnl(total_pnl, total_pnl_pct)}\n"
    ]
    
    for pos in positions:
        emoji = "🟢" if pos['pnl'] > 0 else "🔴" if pos['pnl'] < 0 else "⚪"
        safe_symbol = format_symbol(pos['symbol'])
        telegram_parts.append(
            f"{emoji} {safe_symbol} {pos['timeframe']}: {format_pnl(pos['pnl'], pos['pnl_pct'])}"
        )
    
    telegram = "\n".join(telegram_parts)
    
    return (terminal, telegram)
