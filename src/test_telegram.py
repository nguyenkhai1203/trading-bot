from notification import send_telegram_message
import asyncio

async def test():
    print("Testing Telegram Integration...")
    
    # 1. Entry Notification
    msg_entry = "🟢 [TEST] BOT ENTRY: BUY BTC/USDT @ 45000\nRisk: 1% | TP: 49000 | SL: 44000"
    await send_telegram_message(msg_entry)
    await asyncio.sleep(1)
    
    # 2. Exit Notification
    msg_exit = "🔴 [TEST] BOT EXIT: SELL BTC/USDT @ 46000\nPnL: +$25.00 (+2.2%)"
    await send_telegram_message(msg_exit)
    await asyncio.sleep(1)
    
    # 3. Circuit Breaker
    msg_cb = "🚨 [TEST] CIRCUIT BREAKER TRIGGERED: Daily Loss Limit Hit (-3%). System Halted."
    await send_telegram_message(msg_cb)
    
    print("Messages sent. Please check your Telegram.")

if __name__ == "__main__":
    asyncio.run(test())
