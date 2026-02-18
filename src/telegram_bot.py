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
from analyzer import run_global_optimization

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
    """Generate authoritative status message by fetching live data from exchanges."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    positions_file = os.path.join(script_dir, 'positions.json')
    
    # 1. Load local metadata Draft
    local_data = {}
    try:
        if os.path.exists(positions_file) and os.path.getsize(positions_file) > 0:
            with open(positions_file, 'r', encoding='utf-8') as f:
                local_data = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load positions.json: {e}")

    local_active = local_data.get('active_positions', {})
    local_pending = local_data.get('pending_orders', {})

    now = datetime.now().strftime('%d/%m %H:%M')
    lines = [f"📊 *AUTHORITATIVE STATUS* - {now}", ""]
    
    # 2. Fetch LIVE data from all adapters
    is_virtual_any = False
    is_live_any = False
    
    for ex_name, adapter in data_manager.adapters.items():
        ex_name = ex_name.upper()
        
        # Detect Public Mode
        is_public = not adapter.exchange.apiKey or adapter.exchange.get('is_public_only', False)
        if is_public:
            is_virtual_any = True
            header = f"🏦 *{ex_name}* (VIRTUAL)"
        else:
            is_live_any = True
            header = f"🏦 *{ex_name}*"
            
        lines.append(header)
        
        try:
            live_positions = {}
            pending_entries = []
            
            if is_public:
                # --- PUBLIC MODE: Use Local Data as Source of Truth ---
                # Filter local_active for this exchange
                for l_key, l_pos in local_active.items():
                    if l_key.startswith(f"{ex_name}_"):
                        norm_sym = trader._normalize_symbol(l_pos.get('symbol'))
                        # Convert to same format as fetch_positions result for reuse
                        live_positions[norm_sym] = {
                            'symbol': l_pos.get('symbol'),
                            'side': l_pos.get('side'),
                            'entryPrice': float(l_pos.get('entry_price') or 0),
                            'contracts': float(l_pos.get('qty') or 0),
                            'leverage': int(l_pos.get('leverage') or 1),
                            'unrealizedPnl': 0, # Will calc below
                            'timeframe': l_pos.get('timeframe', 'N/A'),
                            'stopLoss': float(l_pos.get('sl') or 0),
                            'takeProfit': float(l_pos.get('tp') or 0)
                        }
                
                # Filter local_pending for this exchange
                for l_key, l_ord in local_pending.items():
                    if l_key.startswith(f"{ex_name}_"):
                        pending_entries.append(l_ord)
            else:
                # --- LIVE MODE: Authoritative Exchange Fetch ---
                live_positions_list = await adapter.fetch_positions()
                live_positions = {trader._normalize_symbol(p['symbol']): p for p in live_positions_list if trader._safe_float(p.get('contracts'), 0) > 0}
                
                live_orders = await adapter.fetch_open_orders()
                pending_entries = [o for o in live_orders if str(o.get('type')).upper() in ['LIMIT'] and not o.get('reduceOnly')]
            
            # --- ACTIVE POSITIONS ---
            lines.append("🟢 *ACTIVE POSITIONS*")
            lines.append("─" * 15)
            
            if live_positions:
                for norm_sym, ex_p in live_positions.items():
                    symbol = ex_p.get('symbol')
                    side = ex_p.get('side', 'N/A').upper()
                    entry = float(ex_p.get('entryPrice') or 0)
                    contracts = float(ex_p.get('contracts') or 0)
                    lev = int(ex_p.get('leverage') or 1)
                    
                    # Merge with local metadata if exists
                    timeframe = ex_p.get('timeframe', "N/A")
                    sl = float(ex_p.get('stopLoss') or 0)
                    tp = float(ex_p.get('takeProfit') or 0)
                    
                    if not is_public:
                        # Try to find matching metadata in local_active (for timeframe/SL/TP fallback)
                        for l_key, l_pos in local_active.items():
                            if trader._normalize_symbol(l_pos.get('symbol')) == norm_sym:
                                timeframe = l_pos.get('timeframe', timeframe)
                                if sl == 0: sl = float(l_pos.get('sl') or 0)
                                if tp == 0: tp = float(l_pos.get('tp') or 0)
                                break
                    
                    # Calculate P&L % using live ticker
                    ticker = await data_manager.fetch_ticker(symbol, exchange=ex_name)
                    current = float(ticker['last']) if ticker else entry
                    
                    if entry > 0:
                        pnl_pct = ((current - entry) / entry) * 100 * lev * (1 if side == 'BUY' else -1)
                    else:
                        pnl_pct = 0
                        
                    side_emoji = "📈" if side == 'BUY' else "📉"
                    pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
                    
                    lines.append(f"{side_emoji} *{symbol}* [{timeframe}] ({lev}x) | {pnl_emoji}*{pnl_pct:+.2f}%*")
                    lines.append(f"   `{entry:.6f}` → `{current:.6f}`")
                    if sl > 0 or tp > 0:
                        lines.append(f"   🎯 TP: {tp:.6f} | 🛡 SL: {sl:.6f}")
            else:
                lines.append("   _None_")

            # --- PENDING ORDERS ---
            lines.append("")
            lines.append("⏳ *PENDING ORDERS*")
            lines.append("─" * 15)
            
            if pending_entries:
                for o in pending_entries:
                    sym = o.get('symbol')
                    side = o.get('side', 'N/A').upper()
                    price = float(o.get('price') or 0)
                    qty = float(o.get('amount') or 0)
                    
                    lines.append(f"   {side} {sym} @ `{price:.6f}` (Qty: {qty})")
            else:
                lines.append("   _None_")
                
        except Exception as e:
            lines.append(f"❌ Error fetching {ex_name}: {str(e)[:100]}")
            
        lines.append("")

    lines.append("─" * 20)
    if is_live_any and is_virtual_any:
        lines.append("📡 *Hybrid Reality Active* (Live + Virtual)")
    elif is_virtual_any:
        lines.append("🛡️ *Virtual Reality Active* (Simulation Mode)")
    else:
        lines.append("📡 *Exchange-First Reality Active*")
    
    return "\n".join(lines)
    
    return "\n".join(lines)

    lines.append("─" * 20)
    lines.append("📡 *Signal Channel Mode Active*")
    
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
async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger global optimization and brain training."""
    await update.message.reply_text("🚀 Starting Global Optimization & Neural Brain Training...\n_This may take a few minutes._", parse_mode='Markdown')
    try:
        # We run it as a task to not block the bot if it takes long
        asyncio.create_task(run_global_optimization())
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to start optimization: {e}")

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
                # 1. Send Active Status (Hybrid Reality)
                status_msg = await get_status_message()
                await application.bot.send_message(chat_id=CHAT_ID, text=status_msg, parse_mode='Markdown')
                
                # 2. Send Performance Summary
                summary_msg = await get_summary_message('month')
                await application.bot.send_message(chat_id=CHAT_ID, text=summary_msg, parse_mode='Markdown')
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
        app.add_handler(CommandHandler("optimize", optimize_cmd))
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