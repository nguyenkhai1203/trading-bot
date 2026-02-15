# -*- coding: utf-8 -*-
"""
Simple Telegram Bot - Status & Summary only
Auto-sends report every 2 hours
"""
import asyncio
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

# Load environment variables from project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
load_dotenv(env_path)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import BINANCE_API_KEY, BINANCE_API_SECRET
from execution import Trader
from data_manager import MarketDataManager

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env")

# Setup exchange & trader
from exchange_factory import get_exchange_adapter
exchange = get_exchange_adapter()

trader = Trader(exchange, dry_run=True)
data_manager = MarketDataManager()


async def close():
    """Close module-level exchange and data manager connectors."""
    try:
        if hasattr(exchange, 'close'):
            await exchange.close()
    except Exception:
        pass
    try:
        # Ensure MarketDataManager's exchange is closed as well
        if hasattr(data_manager, 'close'):
            await data_manager.close()
    except Exception:
        pass

# ============== STATUS MESSAGE ==============
async def get_status_message() -> str:
    """Generate beautiful status message with P&L"""
    # Read positions from the shared JSON file - use absolute path for reliability
    script_dir = os.path.dirname(os.path.abspath(__file__))
    positions_file = os.path.join(script_dir, 'positions.json')
    
    # Try multiple times in case of file access conflicts
    all_positions = {}
    max_retries = 5
    
    # Log to file for debugging
    log_file = os.path.join(script_dir, 'telegram_debug.log')
    with open(log_file, 'a', encoding='utf-8') as log:
        log.write(f"{datetime.now()}: Starting position read attempt\n")
    
    for attempt in range(max_retries):
        try:
            if os.path.exists(positions_file) and os.path.getsize(positions_file) > 0:
                with open(positions_file, 'r', encoding='utf-8') as f:
                    all_positions = json.load(f)
                    with open(log_file, 'a', encoding='utf-8') as log:
                        log.write(f"{datetime.now()}: SUCCESS - Loaded {len(all_positions)} positions\n")
                    break
            else:
                all_positions = {}
                with open(log_file, 'a', encoding='utf-8') as log:
                    log.write(f"{datetime.now()}: File empty or missing\n")
                break
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
            with open(log_file, 'a', encoding='utf-8') as log:
                log.write(f"{datetime.now()}: Attempt {attempt + 1} failed: {e}\n")
            import asyncio
            await asyncio.sleep(0.25 * (attempt + 1))
    else:
        all_positions = {}
    
    # Separate active and pending positions
    active = {k: v for k, v in all_positions.items() if v.get('status') == 'filled'}
    pending_pos = {k: v for k, v in all_positions.items() if v.get('status') == 'pending'}
    
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
            sl = float(pos.get('sl') or 0.0)
            tp = float(pos.get('tp') or 0.0)
            
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
            
            # Try to get timeframe and confidence if available
            timeframe = pos.get('timeframe', '1h')
            conf = pos.get('confidence', 0)
            conf_str = f"{conf:.1f}" if conf else "Auto"
            
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            side_emoji = "📈" if side.upper() == 'BUY' else "📉"
            
            lines.append(f"{side_emoji} *{side.upper()} - {symbol} - {timeframe} - {conf_str}* ({leverage}x)")
            lines.append(f"   `{entry:.4f}` → `{current:.4f}` ({emoji}{pnl_pct:+.2f}%)")
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
            
            timeframe = pos.get('timeframe', '1h')
            tp = float(pos.get('tp') or 0.0)
            sl = float(pos.get('sl') or 0.0)
            
            side_emoji = "⏳"
            
            lines.append(f"{side_emoji} *{side.upper()} - {symbol} - {timeframe}* ({leverage}x)")
            lines.append(f"   Entry: `{price:.4f}` | Now: `{current:.4f}`")
            lines.append(f"   🎯 TP: {tp:.4f} | 🛡 SL: {sl:.4f}")
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
    
    # Broadcast to channel if command came from elsewhere (e.g. DM)
    if CHAT_ID and str(update.effective_chat.id) != str(CHAT_ID):
        try:
             await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
        except Exception as e:
             logging.error(f"Failed to broadcast status to channel: {e}")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await get_summary_message('month')
    await update.message.reply_text(msg, parse_mode='Markdown')

async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger reconciliation between persisted positions and exchange state."""
    await update.message.reply_text("🔁 Starting sync with exchange...", parse_mode='Markdown')
    try:
        summary = await trader.reconcile_positions()
        msg = f"✅ Sync complete. Recovered order_ids: {summary.get('recovered_order_ids',0)}, created_tp_sl: {summary.get('created_tp_sl',0)}"
    except Exception as e:
        msg = f"❌ Sync failed: {e}"
    await update.message.reply_text(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data == 'status':
        msg = await get_status_message()
        await query.edit_message_text(msg, parse_mode='Markdown')
        
        # Broadcast to channel if clicked in DM
        if CHAT_ID and str(query.message.chat.id) != str(CHAT_ID):
            try:
                await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            except Exception as e:
                logging.error(f"Failed to broadcast button status to channel: {e}")
        return
    elif query.data == 'summary_month':
        msg = await get_summary_message('month')
    elif query.data == 'summary_all':
        msg = await get_summary_message('all')
    else:
        msg = "Unknown"
    
    await query.edit_message_text(msg, parse_mode='Markdown')

import asyncio

# ... (imports) ...

# ============== AUTO REPORT ==============
async def periodic_report_loop(application: Application):
    """Loop for periodic reports (replacing JobQueue)"""
    while True:
        await asyncio.sleep(7200) # 2 hours
        try:
            if CHAT_ID:
                msg = await get_status_message()
                await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
        except Exception as e:
            print(f"⚠️ Auto-report failed: {e}")

async def post_init(application: Application) -> None:
    """Start background tasks."""
    if CHAT_ID:
        asyncio.create_task(periodic_report_loop(application))
        print(f"✅ Auto-report every 2h to {CHAT_ID}")

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
        app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("sync", sync_cmd))
        app.add_handler(CommandHandler("summary", summary_cmd))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_error_handler(error_handler)
        
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