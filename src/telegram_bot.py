# -*- coding: utf-8 -*-
"""
Simple Telegram Bot - Status & Summary only
Auto-sends report every 2 hours
"""
import json
import logging
import os
import sys
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv
import ccxt
import telegram
import requests

load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import BINANCE_API_KEY, BINANCE_API_SECRET, USE_TESTNET
from execution import Trader
from data_manager import MarketDataManager

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env")

# Setup exchange & trader
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    'apiKey': BINANCE_API_KEY if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY else None,
    'secret': BINANCE_API_SECRET if BINANCE_API_SECRET and 'your_' not in BINANCE_API_SECRET else None,
})
if USE_TESTNET:
    exchange.set_sandbox_mode(True)

trader = Trader(exchange, dry_run=True)
data_manager = MarketDataManager()

# ============== STATUS MESSAGE ==============
async def get_status_message() -> str:
    """Generate beautiful status message with P&L"""
    positions = trader.get_active_positions()
    pending = trader.get_pending_orders()
    
    active = {k: v for k, v in positions.items() if v.get('status') == 'filled'}
    pending_pos = {k: v for k, v in positions.items() if v.get('status') == 'pending'}
    pending_pos.update(pending)
    
    now = datetime.now().strftime('%d/%m %H:%M')
    lines = [f"📊 *TRADING STATUS* - {now}", ""]
    
    total_pnl = 0
    total_pnl_pct = 0
    
    # Active positions
    if active:
        lines.append(f"🟢 *ACTIVE ({len(active)})*")
        lines.append("─" * 20)
        
        for key, pos in active.items():
            symbol = pos.get('symbol', key.split('_')[0])
            side = pos.get('side', 'N/A')
            entry = float(pos.get('entry_price', 0))
            qty = float(pos.get('qty') or pos.get('amount', 0))
            leverage = int(pos.get('leverage', 10))
            sl = float(pos.get('sl', 0))
            tp = float(pos.get('tp', 0))
            
            try:
                ticker = await data_manager.fetch_ticker(symbol)
                current = float(ticker['last']) if ticker else entry
            except:
                current = entry
            
            if side.upper() == 'BUY':
                pnl_pct = ((current - entry) / entry) * 100 * leverage
            else:
                pnl_pct = ((entry - current) / entry) * 100 * leverage
            
            # USD P&L based on notional value (entry * qty)
            notional = entry * qty
            pnl_usd = (pnl_pct / 100) * notional / leverage  # Actual USD gain
            total_pnl += pnl_usd
            total_pnl_pct += pnl_pct
            
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            side_emoji = "📈" if side.upper() == 'BUY' else "📉"
            
            lines.append(f"{side_emoji} *{symbol}* {side.upper()} {leverage}x")
            lines.append(f"   Entry: `{entry:.4f}` → Now: `{current:.4f}`")
            lines.append(f"   {emoji} *{pnl_pct:+.2f}%* (${pnl_usd:+.2f})")
            lines.append(f"   🎯 TP: {tp:.4f} | 🛡 SL: {sl:.4f}")
            lines.append("")
    else:
        lines.append("🟢 *ACTIVE*: _None_")
        lines.append("")
    
    # Pending orders
    if pending_pos:
        lines.append(f"🟡 *PENDING ({len(pending_pos)})*")
        lines.append("─" * 20)
        
        for key, pos in pending_pos.items():
            symbol = pos.get('symbol', key.split('_')[0])
            side = pos.get('side', 'N/A')
            price = float(pos.get('price') or pos.get('entry_price', 0))
            leverage = int(pos.get('leverage', 10))
            
            try:
                ticker = await data_manager.fetch_ticker(symbol)
                current = float(ticker['last']) if ticker else price
            except:
                current = price
            
            dist = ((current - price) / price) * 100 if price > 0 else 0
            side_emoji = "📈" if side.upper() == 'BUY' else "📉"
            
            lines.append(f"{side_emoji} *{symbol}* {side.upper()} {leverage}x")
            lines.append(f"   Limit: `{price:.4f}` | Now: `{current:.4f}` ({dist:+.2f}%)")
            lines.append("")
    else:
        lines.append("🟡 *PENDING*: _None_")
        lines.append("")
    
    # Summary
    lines.append("─" * 20)
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"{pnl_emoji} *TOTAL: {total_pnl_pct:+.2f}% (${total_pnl:+.2f})*")
    
    return "\n".join(lines)

async def get_summary_message(period: str) -> str:
    """Generate summary message"""
    try:
        json_path = os.path.join(os.path.dirname(__file__), 'signal_performance.json')
        with open(json_path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        return "❌ No trade history."
    
    trades = data.get('trades', [])
    now = datetime.now()
    
    if period == 'month':
        filtered = [t for t in trades if datetime.fromisoformat(t['timestamp']) >= now.replace(day=1)]
        title = f"📈 *{now.strftime('%B %Y')}*"
    else:
        filtered = trades
        title = "📈 *ALL TIME*"
    
    if not filtered:
        return f"{title}\n_No trades_"
    
    total = len(filtered)
    wins = sum(1 for t in filtered if t.get('result') == 'WIN')
    win_rate = (wins / total * 100) if total > 0 else 0
    total_pnl = sum(float(t.get('pnl_pct', 0)) for t in filtered)
    total_usd = sum(float(t.get('pnl_usd', 0)) for t in filtered)
    
    lines = [
        title, "─" * 20,
        f"📊 Trades: {total} | ✅ Wins: {wins} | ❌ Loss: {total - wins}",
        f"🎯 Win Rate: *{win_rate:.1f}%*",
        f"💰 P&L: *{total_pnl:+.2f}%* (${total_usd:+.2f})",
    ]
    return "\n".join(lines)

# ============== COMMANDS ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data='status')],
        [InlineKeyboardButton("📈 This Month", callback_data='summary_month'),
         InlineKeyboardButton("📈 All Time", callback_data='summary_all')],
    ]
    await update.message.reply_text(
        "🤖 *Trading Bot*\nSelect:", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode='Markdown'
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await get_status_message()
    await update.message.reply_text(msg, parse_mode='Markdown')

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await get_summary_message('month')
    await update.message.reply_text(msg, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data == 'status':
        msg = await get_status_message()
    elif query.data == 'summary_month':
        msg = await get_summary_message('month')
    elif query.data == 'summary_all':
        msg = await get_summary_message('all')
    else:
        msg = "Unknown"
    
    await query.edit_message_text(msg, parse_mode='Markdown')

# ============== AUTO REPORT ==============
async def auto_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send status every 2 hours"""
    if CHAT_ID:
        msg = await get_status_message()
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Error: {context.error}")

# ============== MAIN ==============
def main():
    # Clear webhook
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        requests.get(url, timeout=10)
    except:
        pass
    
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("summary", summary_cmd))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_error_handler(error_handler)
        
        # Auto report every 2 hours
        if CHAT_ID:
            app.job_queue.run_repeating(auto_report, interval=7200, first=10)
            print(f"✅ Auto-report every 2h to {CHAT_ID}")
        
        print("🤖 Telegram bot started")
        app.run_polling(drop_pending_updates=True)
        
    except telegram.error.Conflict:
        print("⚠️ Another bot instance running! Stop it or create new token.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()