import aiohttp
import asyncio
import os
import time
import ssl
import certifi
from dotenv import load_dotenv

load_dotenv()

# Rate limit protection - Telegram allows ~30 msg/sec but safer to throttle
_last_send_time = 0
_send_lock = None

def _get_lock():
    global _send_lock
    if _send_lock is None:
        _send_lock = asyncio.Lock()
    return _send_lock

async def send_telegram_message(message, exchange_name=None):
    global _last_send_time
    
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    
    # Prepend exchange prefix if provided
    if exchange_name:
        message = f"[{exchange_name}] {message}"
    
    
    if not token or not chat_id:
        if os.getenv('DRY_RUN', 'False').lower() == 'true':
            # print(f"[TELEGRAM MOCK] {message}") 
            return
        # print(f"[TELEGRAM WARN] Token or Chat ID missing")
        return

    # Rate limit: max 1 message per 0.5 second
    async with _get_lock():
        now = time.time()
        wait_time = max(0, 0.5 - (now - _last_send_time))
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        _last_send_time = time.time()

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id, 
        'text': message,
        'parse_mode': 'Markdown'
    }
    
    
    try:
        # Create SSL context with certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status == 429:  # Rate limited
                    retry_after = int(response.headers.get('Retry-After', 5))
                    print(f"⚠️ Telegram rate limited, waiting {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    # Retry once
                    async with session.post(url, json=payload, timeout=10) as retry_resp:
                        if retry_resp.status != 200:
                            print(f"❌ Telegram retry failed: {retry_resp.status}")
                elif response.status != 200:
                    rt = await response.text()
                    print(f"❌ Telegram failed: {response.status} - {rt}")
                    print(f"   Message was: {message[:100]}...")
    except Exception as e:
        print(f"❌ Telegram Error: {e}")
        print(f"   Message was: {message[:100]}...")

async def send_telegram_chunked(message, exchange_name=None):
    """Splits long messages (>4000 chars) into chunks for Telegram."""
    if not message: return
    
    max_len = 3500 # Safe margin below 4096
    chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]
    
    for chunk in chunks:
        await send_telegram_message(chunk, exchange_name)
        await asyncio.sleep(1) # Rate limit safety

async def send_trade_notification(symbol, side, entry, exit, pnl, pnl_pct, reason, exchange_name=None):
    emoji = "🟢" if pnl > 0 else "🔴"
    
    prefix = f"[{exchange_name}] " if exchange_name else ""
    
    msg = (
        f"{prefix}{emoji} **TRADE CLOSED** {emoji}\n"
        f"Symbol: {symbol}\n"
        f"Side: {side.upper()}\n"
        f"Entry: {entry}\n"
        f"Exit: {exit}\n"
        f"PnL: ${pnl:.2f} ({pnl_pct:.2f}%)\n"
        f"Reason: {reason}"
    )
    # We pass None here because we already embedded the prefix in the title line
    await send_telegram_message(msg)
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
    dry_run: bool,
    exchange_name: Optional[str] = None
) -> Tuple[str, str]:
    """
    Format pending limit order notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    safe_symbol = format_symbol(symbol)
    mode = get_mode_label(dry_run)
    ex_prefix = f"[{exchange_name.upper()}] " if exchange_name else ""
    terminal = (
        f"⚪ PENDING | [{symbol} {timeframe}] {dir_emoji} {dir_label} @ {format_price(entry_price)} | "
        f"SL: {format_price(sl_price)} | TP: {format_price(tp_price)}"
    )
    
    telegram = (
        f"{mode} | ⚪ PENDING\n"
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label}\n"
        f"Entry: {format_price(entry_price)}\n"
        f"SL: {format_price(sl_price)} | TP: {format_price(tp_price)}\n"
        f"Score: {score:.1f} | Leverage: {leverage}x ({int(score*10):d}%)"
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
    score: Optional[float],
    leverage: Optional[int],
    dry_run: bool,
    exchange_name: Optional[str] = None
) -> Tuple[str, str]:
    """
    Format position filled notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    safe_symbol = format_symbol(symbol)
    base_currency = symbol.split('/')[0]
    mode = get_mode_label(dry_run)
    ex_prefix = f"[{exchange_name.upper()}] " if exchange_name else ""
    terminal = (
        f"{ex_prefix}⚪ FILLED | [{symbol} {timeframe}] {dir_emoji} {dir_label} @ {format_price(entry_price)} | "
        f"Size: {format_size(size, symbol)} {base_currency}"
    )
    
    telegram = (
        f"{mode} | ⚪ FILLED\n"
        f"{safe_symbol} | {timeframe} | {dir_emoji} {dir_label} ({int(score*100) if score else 0}%)\n"
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
    dry_run: bool = False,
    exchange_name: Optional[str] = None
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
    
    ex_prefix = f"[{exchange_name.upper()}] " if exchange_name else ""
    terminal = (
        f"{ex_prefix}{status_emoji} [{symbol} {timeframe}] {reason_label} @ {format_price(exit_price)} | "
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
    dry_run: bool,
    exchange_name: Optional[str] = None
) -> Tuple[str, str]:
    """
    Format order cancelled notification.
    
    Returns:
        (terminal_message, telegram_message)
    """
    dir_emoji = get_direction_emoji(side)
    dir_label = get_direction_label(side)
    safe_symbol = format_symbol(symbol)
    mode = get_mode_label(dry_run)
    ex_prefix = f"[{exchange_name.upper()}] " if exchange_name else ""
    terminal = f"{ex_prefix}❌ [{symbol} {timeframe}] CANCELLED | Reason: {reason}"
    
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
    total_pnl_pct: float,
    exchange_name: Optional[str] = None,
    is_live_sync: bool = True
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
    ex_prefix = f"[{exchange_name.upper()}] " if exchange_name else ""
    sync_tag = "[SYNCED]" if is_live_sync else "[LOCAL CACHE]"
    terminal = (
        f"{ex_prefix}📊 {sync_tag} [STATUS] {active_count} active | "
        f"PnL: {format_pnl(total_pnl, total_pnl_pct)} | {status_icons}"
    )
    
    # Telegram detailed
    telegram_parts = [
        f"📊 POSITION STATUS UPDATE {sync_tag}\n",
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
