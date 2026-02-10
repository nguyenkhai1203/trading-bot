import aiohttp
import asyncio
import os
import time
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

async def send_telegram_message(message):
    global _last_send_time
    
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    
    if not token or not chat_id:
        # print(f"[TELEGRAM MOCK] {message}") 
        # Reduce spam in console
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
        async with aiohttp.ClientSession() as session:
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

async def send_telegram_chunked(message):
    """Splits long messages (>4000 chars) into chunks for Telegram."""
    if not message: return
    
    max_len = 3500 # Safe margin below 4096
    chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]
    
    for chunk in chunks:
        await send_telegram_message(chunk)
        await asyncio.sleep(1) # Rate limit safety

async def send_trade_notification(symbol, side, entry, exit, pnl, pnl_pct, reason):
    emoji = "🟢" if pnl > 0 else "🔴"
    msg = (
        f"{emoji} **TRADE CLOSED** {emoji}\n"
        f"Symbol: {symbol}\n"
        f"Side: {side.upper()}\n"
        f"Entry: {entry}\n"
        f"Exit: {exit}\n"
        f"PnL: ${pnl:.2f} ({pnl_pct:.2f}%)\n"
        f"Reason: {reason}"
    )
    await send_telegram_message(msg)
