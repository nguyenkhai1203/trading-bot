import aiohttp
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def send_telegram_message(message):
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    
    if not token or not chat_id:
        # print(f"[TELEGRAM MOCK] {message}") 
        # Reduce spam in console
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id, 
        'text': message,
        'parse_mode': 'Markdown'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=5) as response:
                if response.status != 200:
                    rt = await response.text()
                    print(f"Failed to send telegram: {response.status} - {rt}")
    except Exception as e:
        print(f"Telegram Error: {e}")

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
