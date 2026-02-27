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
import time
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

from notification import format_position_v2, format_portfolio_update_v2
from database import DataManager
from data_manager import MarketDataManager
from exchange_factory import create_adapter_from_profile
from execution import Trader

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env")

# Global context (loaded in post_init)
db = None
data_manager = None
traders = {} # {profile_id: Trader}
profiles = [] # List of profile dicts


async def close():
    """Close module-level DB and data manager connectors."""
    try:
        if data_manager:
            await data_manager.close()
    except Exception:
        pass

# ============== STATUS MESSAGE ==============
# ============== STATUS MESSAGE ==============
async def get_total_equity() -> float:
    """Calculate total equity across all active profiles using fresh exchange data."""
    total = 0.0
    for p in profiles:
        t = traders.get(p['id'])
        if not t: continue
        
        try:
            # 1. Get fresh balance from exchange (or DB metric if dry-run)
            bal = 0.0
            if t.dry_run:
                metric = await t.db.get_risk_metric(p['id'], 'starting_balance_day', p.get('environment', 'LIVE'))
                bal = float(metric) if metric else 1000.0
            else:
                # Fetch fresh balance from the exchange
                bal_data = await t.exchange.fetch_balance()
                bal = float(bal_data.get('total', {}).get('USDT', 0))
            
            # 2. Get fresh positions from exchange
            unrealized = 0.0
            ex_positions = []
            if t.dry_run:
                # For dry run, use memory state as source of truth
                ex_positions = list(t.active_positions.values())
            else:
                # For live, fetch directly from exchange for truth
                ex_positions = await t.exchange.fetch_positions()
            
            for pos in ex_positions:
                p_qty = abs(float(pos.get('contracts') or pos.get('amount') or pos.get('qty') or 0))
                if p_qty > 0:
                    symbol = pos.get('symbol')
                    # Find ticker for unrealized pnl
                    ticker = await data_manager.fetch_ticker(symbol, exchange=t.exchange_name)
                    if ticker:
                        cur = float(ticker['last'])
                        entry = float(pos.get('entryPrice') or pos.get('entry_price') or pos.get('avgPrice') or 0)
                        side = (pos.get('side') or '').upper()
                        # Normalize side
                        if side in ['LONG', 'BUY']:
                            unrealized += (cur - entry) * p_qty
                        elif side in ['SHORT', 'SELL']:
                            unrealized += (entry - cur) * p_qty
                            
            total += (bal + unrealized)
        except Exception as e:
            logging.error(f"Equity calc error for {p['name']}: {e}")
    return total

async def get_status_message(profile_filter: str = None, is_portfolio: bool = False) -> str:
    """Generate status message using SQLite data and multi-profile support."""
    total_balance = await get_total_equity()
    
    # Calculate daily PnL
    total_start = 0.0
    for p in profiles:
        t = traders.get(p['id'])
        if t:
            metric = await t.db.get_risk_metric(p['id'], 'starting_balance_day', p.get('environment', 'LIVE'))
            total_start += float(metric) if metric else 1000.0
            
    daily_pnl_pct = ((total_balance - total_start) / total_start * 100) if total_start > 0 else 0.0
    
    exchanges_payload = {}
    total_active = 0
    total_pending = 0
    
    filtered_profiles = [p for p in profiles if not profile_filter or profile_filter.lower() in p['name'].lower()]
    
    for p in filtered_profiles:
        t = traders.get(p['id'])
        if not t: continue
        
        label = f"{p['name']} ({p['environment']})"
        exchanges_payload[label] = {'active': [], 'pending': []}
        
        # 1. Fetch FRESH positions from exchange for LIVE profiles
        # For DRY_RUN, we still rely on memory as there is no remote exchange state.
        if not t.dry_run:
            try:
                ex_pos = await t.exchange.fetch_positions()
                for ep in ex_pos:
                    p_qty = abs(float(ep.get('contracts') or ep.get('amount') or ep.get('qty') or 0))
                    if p_qty > 0:
                        sym = ep['symbol']
                        ticker = await data_manager.fetch_ticker(sym, exchange=t.exchange_name)
                        cur = float(ticker['last']) if ticker else float(ep.get('entryPrice') or ep.get('entry_price') or 0)
                        entry = float(ep.get('entryPrice') or ep.get('entry_price') or ep.get('avgPrice') or 0)
                        side = ep['side'].upper()
                        if side == 'LONG': side = 'BUY'
                        elif side == 'SHORT': side = 'SELL'
                        
                        lev = ep.get('leverage') or ep.get('info', {}).get('leverage', 1)
                        
                        pnl_usd = (cur - entry) * p_qty if side == 'BUY' else (entry - cur) * p_qty
                        roe = ((cur - entry) / entry * 100 * float(lev)) if entry > 0 else 0
                        if side == 'SELL': roe = -roe
                        
                        # Find corresponding local pos for SL/TP if available
                        local_match = next((lp for lp in t.active_positions.values() if t._normalize_symbol(lp['symbol']) == t._normalize_symbol(sym)), {})
                        
                        exchanges_payload[label]['active'].append({
                            'symbol': sym, 'side': side, 'leverage': lev,
                            'entry_price': entry, 'current_price': cur,
                            'roe': roe, 'pnl_usd': pnl_usd, 
                            'tp': local_match.get('tp', 0), 'sl': local_match.get('sl', 0)
                        })
                        total_active += 1
                        
                # Also fetch pending orders for full visibility
                ex_orders = await t.exchange.fetch_open_orders()
                for eo in ex_orders:
                    # Logic to identify ENTRY orders (not SL/TP)
                    o_type = str(eo.get('type') or '').upper()
                    is_reduce = eo.get('reduceOnly') or eo.get('info', {}).get('reduceOnly') == 'true'
                    if 'STOP' not in o_type and 'TAKE' not in o_type and not is_reduce:
                        ticker = await data_manager.fetch_ticker(eo['symbol'], exchange=t.exchange_name)
                        cur = float(ticker['last']) if ticker else 0
                        exchanges_payload[label]['pending'].append({
                            'symbol': eo['symbol'], 'side': eo['side'].upper(), 'leverage': 1, # leverage unknown for order
                            'entry_price': eo.get('price') or eo.get('stopPrice') or 0, 
                            'current_price': cur,
                            'roe': 0, 'pnl_usd': 0, 'tp': 0, 'sl': 0,
                            'is_pending': True
                        })
                        total_pending += 1
            except Exception as e:
                logging.error(f"Error fetching fresh status for {p['name']}: {e}")
        else:
            # DRY RUN: Use memory state
            for pos in t.active_positions.values():
                if pos.get('status') == 'filled':
                    ticker = await data_manager.fetch_ticker(pos['symbol'], exchange=t.exchange_name)
                    cur = float(ticker['last']) if ticker else float(pos['entry_price'])
                    entry = float(pos['entry_price'])
                    qty = float(pos['qty'])
                    side = pos['side'].upper()
                    lev = pos.get('leverage', 1)
                    
                    pnl_usd = (cur - entry) * qty if side in ['BUY', 'LONG'] else (entry - cur) * qty
                    roe = ((cur - entry) / entry * 100 * lev) if entry > 0 else 0
                    if side in ['SELL', 'SHORT']: roe = -roe
                    
                    exchanges_payload[label]['active'].append({
                        'symbol': pos['symbol'], 'side': side, 'leverage': lev,
                        'entry_price': entry, 'current_price': cur,
                        'roe': roe, 'pnl_usd': pnl_usd, 'tp': pos.get('tp', 0), 'sl': pos.get('sl', 0)
                    })
                    total_active += 1
                elif pos.get('status') == 'pending':
                    ticker = await data_manager.fetch_ticker(pos['symbol'], exchange=t.exchange_name)
                    cur = float(ticker['last']) if ticker else float(pos['entry_price'])
                    exchanges_payload[label]['pending'].append({
                        'symbol': pos['symbol'], 'side': pos['side'].upper(), 'leverage': pos.get('leverage', 1),
                        'entry_price': pos['entry_price'], 'current_price': cur,
                        'roe': 0, 'pnl_usd': 0, 'tp': pos.get('tp', 0), 'sl': pos.get('sl', 0),
                        'is_pending': True
                    })
                    total_pending += 1
                
    if is_portfolio:
        return format_portfolio_update_v2(
            total_balance=total_balance,
            daily_pnl_pct=daily_pnl_pct,
            active_count=total_active,
            pending_count=total_pending,
            exchanges_data=exchanges_payload
        )
        
    # Manual grouping format
    now_str = datetime.now().strftime('%d/%m %H:%M')
    lines = [f"📊 *BOT STATUS (Multi-Profile)* - {now_str}", ""]
    
    for label, data in exchanges_payload.items():
        if not data['active'] and not data['pending']: continue
        lines.append(f"🏦 {label}")
        if data['active']:
            lines.append(f"🟢 ACTIVE ({len(data['active'])})")
            for p_msg in data['active']:
                lines.append(format_position_v2(**p_msg))
                lines.append("")
        if data['pending']:
            lines.append(f"🟡 PENDING ({len(data['pending'])})")
            for p_msg in data['pending']:
                # The formatter takes kwargs, we need to remove is_pending if it's there or handle it
                is_p = p_msg.pop('is_pending', False)
                lines.append(format_position_v2(**p_msg, is_pending=is_p))
                lines.append("")
                
    return "\n".join(lines) if len(lines) > 2 else "No active positions."

async def get_summary_message(period: str) -> str:
    """Generate summary message using SQLite trade history."""
    now = datetime.now()
    if period == 'month':
        start_date = now.replace(day=1, hour=0, minute=0, second=0).isoformat()
        title = f"📈 *{now.strftime('%B %Y')} Summary*"
    else:
        start_date = "2000-01-01T00:00:00"
        title = "📈 *ALL TIME Summary*"
        
    try:
        # Fetch trades from DB for all profiles
        all_trades = []
        for p in profiles:
            p_trades = await db.get_trade_history(p['id'], limit=1000)
            # Filter by date manually or update get_trade_history to support date filtering
            for t in p_trades:
                if t['exit_time'] >= start_date:
                    all_trades.append(t)
                    
        if not all_trades:
            return f"{title}\n_No trades found in this period._"
            
        total = len(all_trades)
        wins = sum(1 for t in all_trades if t.get('pnl_pct', 0) > 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        total_pnl = sum(float(t.get('pnl_pct', 0)) for t in all_trades)
        total_usd = sum(float(t.get('pnl_usdt', 0) or 0) for t in all_trades)
        
        lines = [
            title, "─" * 20,
            f"📊 Total Trades: {total}",
            f"✅ Wins: {wins} | ❌ Losses: {total - wins}",
            f"🎯 Win Rate: *{win_rate:.1f}%*",
            f"💰 Net P&L: *{total_pnl:+.2f}%* (${total_usd:+.2f})",
        ]
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"Summary generated error: {e}")
        return "❌ Error generating summary."

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
    msg = await get_summary_message('all')
    await update.message.reply_text(msg, parse_mode='Markdown')

async def profiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active profiles and their basic info."""
    if not profiles:
        await update.message.reply_text("No active profiles loaded.")
        return
        
    lines = ["👥 *Active Trading Profiles*", ""]
    for p in profiles:
        status_emoji = "🟢" if p['is_active'] else "🔴"
        env_emoji = "🧪" if p['environment'].upper() == 'TEST' else "💰"
        lines.append(f"{status_emoji} *{p['name']}* {env_emoji}")
        lines.append(f"   ID: `{p['id']}` | Exchange: {p['exchange']}")
        lines.append(f"   Strategy: {p['strategy_name']}")
        
        t = traders.get(p['id'])
        if t:
            metric = await t.db.get_risk_metric(p['id'], 'starting_balance_day', p.get('environment', 'LIVE'))
            bal = float(metric) if metric else 1000.0
            lines.append(f"   Day Start: ${bal:.2f}")
        lines.append("")
        
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger reconciliation between persisted positions and exchange state for ALL exchangers."""
    await update.message.reply_text("🔁 Starting full sync with ALL exchanges...", parse_mode='Markdown')
    results = []
    try:
        for ex_name, t in traders.items():
            try:
                summary = await t.reconcile_positions()
                results.append(f"✅ {ex_name}: Recovered {summary.get('recovered_order_ids',0)}, Created TP/SL: {summary.get('created_tp_sl',0)}")
            except Exception as ex_err:
                results.append(f"❌ {ex_name}: {ex_err}")
                
        msg = "\n".join(results)
    except Exception as e:
        msg = f"❌ Sync failed: {e}"
    await update.message.reply_text(msg)
async def reset_peak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually reset peak balance to current balance for all traders."""
    try:
        from risk_manager import RiskManager
        results = []
        for p_id, t in traders.items():
            try:
                bal_data = await t.exchange.fetch_balance()
                current_bal = float(bal_data.get('total', {}).get('USDT', 0) or bal_data.get('free', {}).get('USDT', 0) or 0)
            except Exception:
                current_bal = 1000

            rm = RiskManager(db=t.db, profile_id=t.profile_id, exchange_name=t.exchange_name)
            msg = await rm.reset_peak(current_bal)
            results.append(f"🏦 {t.exchange_name}: {msg}")

        await update.message.reply_text("\n".join(results))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to reset peak: {e}")

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
                # 1. Send Portfolio Update v2
                status_msg = await get_status_message(is_portfolio=True)
                await application.bot.send_message(chat_id=CHAT_ID, text=status_msg, parse_mode='Markdown')
                
                # 2. Send Performance Summary (Realized Trades)
                summary_msg = await get_summary_message('month')
                await application.bot.send_message(chat_id=CHAT_ID, text=summary_msg, parse_mode='Markdown')
        except Exception as e:
            print(f"⚠️ Auto-report failed: {e}")

async def post_init(application: Application) -> None:
    """Initialize DB, profiles and background tasks."""
    global db, data_manager, profiles, traders

    # 1. Resolve environment from .env
    env_str = 'TEST' if os.getenv('DRY_RUN', 'False').lower() == 'true' else 'LIVE'

    # 2. Initialize DB (async singleton)
    db = await DataManager.get_instance(env_str)
    data_manager = MarketDataManager(db)

    # 3. Load Profiles
    profiles = await db.get_profiles()

    # 4. Setup Traders
    for p in profiles:
        try:
            adapter = await create_adapter_from_profile(p)
            if adapter:
                traders[p['id']] = Trader(
                    adapter,
                    db=db,
                    profile_id=p['id'],
                    signal_tracker=None
                )
                print(f"✅ Profile loaded: {p['name']}")
        except Exception as e:
            print(f"❌ Failed to load profile {p['name']}: {e}")

    # 5. Start periodic reports
    if CHAT_ID:
        asyncio.create_task(periodic_report_loop(application))
        print(f"✅ Auto-report every 2h enabled.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        app.add_handler(CommandHandler("profiles", profiles_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("sync", sync_cmd))
        app.add_handler(CommandHandler("summary", summary_cmd))
        app.add_handler(CommandHandler("optimize", optimize_cmd))
        app.add_handler(CommandHandler("reset_peak", reset_peak_cmd))
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