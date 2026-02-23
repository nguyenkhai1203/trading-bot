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

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import BINANCE_API_KEY, BINANCE_API_SECRET, DRY_RUN
from execution import Trader
from data_manager import MarketDataManager
from analyzer import run_global_optimization
from notification import format_position_v2, format_portfolio_update_v2

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env")

# Setup exchanges & traders
from exchange_factory import get_active_exchanges_map
ex_adapters = get_active_exchanges_map()
data_manager = MarketDataManager(adapters=ex_adapters)
traders = {name: Trader(adapter, dry_run=DRY_RUN, data_manager=data_manager) for name, adapter in ex_adapters.items()}

# Backward compatibility (some functions might still use 'trader' global)
trader = list(traders.values())[0] if traders else None
exchange = list(ex_adapters.values())[0] if ex_adapters else None


async def close():
    """Close module-level exchange and data manager connectors."""
    try:
        for ad in ex_adapters.values():
            if hasattr(ad, 'close'):
                await ad.close()
    except Exception:
        pass
    try:
        # Ensure MarketDataManager's exchange is closed as well
        if hasattr(data_manager, 'close'):
            await data_manager.close()
    except Exception:
        pass

# ============== STATUS MESSAGE ==============
# ============== STATUS MESSAGE ==============
async def get_total_equity() -> float:
    """Calculate total equity across all exchanges+local history."""
    total = 1000 # initial seed from bot.py logic
    
    # 1. Closed trades PnL
    perf_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signal_performance.json')
    if os.path.exists(perf_file):
        try:
            with open(perf_file, 'r') as f:
                data = json.load(f)
            trades = data.get('trades', [])
            total += sum(float(t.get('pnl_usdt', 0) or 0) for t in trades)
        except: pass
    
    # 2. Unrealized PnL and account balances if possible
    for ex_name, t in traders.items():
        # Account balance (USDT)
        try:
            bal = await t.exchange.fetch_balance()
            total_usdt = float(bal.get('total', {}).get('USDT', 0) or bal.get('free', {}).get('USDT', 0) or 0)
            # If we are in dry run, we use the initial balance + realized, but what about active positions?
            # get_current_balance in bot.py adds unrealized to dry-run balance.
            if t.dry_run:
                # In dry run, we already added realized to 'total' above. 
                # Now add unrealized from memory.
                for pos in t.active_positions.values():
                    if pos.get('status') == 'filled':
                        # need live price
                        sym = pos.get('symbol')
                        ticker = await data_manager.fetch_ticker(sym, exchange=ex_name)
                        if ticker:
                            cur = float(ticker['last'])
                            entry = float(pos.get('entry_price', cur))
                            qty = float(pos.get('qty', 0))
                            side = pos.get('side', 'BUY').upper()
                            unrealized = (cur - entry) * qty if side in ['BUY', 'LONG'] else (entry - cur) * qty
                            total += unrealized
            else:
                # Live mode: fetch_balance 'total' usually includes unrealized pnl on most exchanges (e.g. Bybit Unified)
                total += total_usdt
        except: pass
        
    return total

async def get_status_message(force_live: bool = False, is_portfolio: bool = False) -> str:
    """Generate authoritative status message or Portfolio Update in BOT STATUS v2 style."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    positions_file = os.path.join(script_dir, 'positions.json')
    
    # Load local metadata for fallback/enrichment
    local_data = {}
    try:
        if os.path.exists(positions_file) and os.path.getsize(positions_file) > 0:
            with open(positions_file, 'r', encoding='utf-8') as f:
                local_data = json.load(f)
                if 'active_positions' not in local_data:
                    local_data = {'active_positions': local_data, 'pending_orders': {}}
    except: pass

    local_active = local_data.get('active_positions', {})
    local_pending = local_data.get('pending_orders', {})
    last_sync = local_data.get('last_sync', 0)
    use_cache = not force_live and (time.time() - last_sync < 60)

    # 1. Calculate Portfolio Data
    total_balance = await get_total_equity()
    
    # Get daily start from RiskManager's persistent file
    daily_config_file = os.path.join(script_dir, 'daily_config.json')
    daily_pnl_pct = 0
    # Calculate daily PnL based on starting balance for each exchange
    if os.path.exists(daily_config_file):
        try:
            with open(daily_config_file, 'r') as f:
                d_full = json.load(f)
                
            total_start = 0
            for ex_name in traders.keys():
                d_data = d_full.get(ex_name, {})
                # Fallback to flat format for Binance
                if ex_name == 'BINANCE' and 'starting_balance_day' in d_full:
                    start_ex = d_full.get('starting_balance_day', total_balance / len(traders))
                else:
                    start_ex = d_data.get('starting_balance_day', total_balance / len(traders))
                total_start += start_ex
            
            if total_start > 0:
                daily_pnl_pct = ((total_balance - total_start) / total_start * 100)
        except: pass
    
    # 2. Gather Position Data grouped by Exchange
    exchanges_payload = {}
    total_active = 0
    total_pending = 0
    
    for ex_name, adapter in data_manager.adapters.items():
        ex_name_up = ex_name.upper()
        current_trader = traders.get(ex_name_up)
        if not current_trader: continue
        
        exchanges_payload[ex_name_up] = {'active': [], 'pending': []}
        
        # Determine positions and orders
        live_pos_list = []
        pending_list = []
        
        if use_cache or getattr(current_trader, 'dry_run', False):
            # Use local data for dry run or cached
            prefix = f"{ex_name_up}_"
            for k, p in local_active.items():
                if k.startswith(prefix) or (ex_name_up == 'BINANCE' and not k.startswith(('BINANCE_', 'BYBIT_'))):
                    status = str(p.get('status', '')).lower()
                    if status in ['filled', 'active']:
                        live_pos_list.append(p)
                    elif status == 'pending':
                        pending_list.append(p)
            for k, o in local_pending.items():
                if k.startswith(prefix):
                    pending_list.append(o)
        else:
            # Live authoritative fetch
            try:
                raw_p = await adapter.fetch_positions()
                live_pos_list = [p for p in raw_p if abs(float(p.get('contracts') or p.get('amount') or p.get('info', {}).get('size', 0))) > 0]
                raw_o = await adapter.fetch_open_orders()
                pending_list = [o for o in raw_o if not o.get('reduceOnly')]
            except: pass

        # Format and append ACTIVE
        for p in live_pos_list:
            sym = p.get('symbol')
            # Enrichment
            ticker = await data_manager.fetch_ticker(sym, exchange=ex_name_up)
            cur = float(ticker['last']) if ticker else float(p.get('entryPrice') or 0)
            entry = float(p.get('entryPrice') or p.get('entry_price') or 0)
            qty = float(p.get('contracts') or p.get('qty') or p.get('amount') or 0)
            # Leverage extraction
            lev_raw = p.get('leverage')
            if lev_raw is None and 'info' in p:
                lev_raw = p['info'].get('leverage') or p['info'].get('leverage_x')
            lev = int(float(lev_raw)) if lev_raw is not None else 1
            
            side = p.get('side', 'BUY').upper()
            
            # PnL calc
            if 'unrealizedPnl' in p and p['unrealizedPnl'] is not None:
                pnl_usd = float(p['unrealizedPnl'])
                roe = (pnl_usd / ((qty * entry) / lev) * 100) if (qty * entry) > 0 else 0
            else:
                pnl_usd = (cur - entry) * qty if side in ['BUY', 'LONG'] else (entry - cur) * qty
                roe = ((cur - entry) / entry * 100 * lev) if entry > 0 else 0
                if side in ['SELL', 'SHORT']: roe = -roe
            
            # SL/TP extraction: CCXT unified stopLoss/takeProfit
            sl = float(p.get('stopLoss') or 0)
            tp = float(p.get('takeProfit') or 0)
            
            # Fallback to info dict for exchange-specific keys
            if (sl == 0 or tp == 0) and 'info' in p:
                info = p['info']
                if sl == 0: sl = float(info.get('stopLoss') or info.get('stop_loss') or 0)
                if tp == 0: tp = float(info.get('takeProfit') or info.get('take_profit') or 0)

            if sl == 0 or tp == 0 or lev <= 1:
                norm_sym = current_trader._normalize_symbol(sym)
                prefix = f"{ex_name_up}_"
                potential_matches = []
                for l_key, l_pos in local_active.items():
                    l_sym = l_pos.get('symbol', '')
                    has_any_prefix = l_key.startswith(('BINANCE_', 'BYBIT_'))
                    is_ex_match = l_key.startswith(prefix) or (not has_any_prefix and ex_name_up == 'BINANCE')
                    
                    if is_ex_match and (current_trader._normalize_symbol(l_sym) == norm_sym or l_sym == sym):
                        potential_matches.append(l_pos)
                
                if potential_matches:
                    best_match = next((m for m in potential_matches if str(m.get('status')).lower() in ['filled', 'active']), potential_matches[0])
                    if lev <= 1: lev = int(best_match.get('leverage') or lev)
                    if sl == 0: sl = float(best_match.get('sl') or best_match.get('stop_loss') or 0)
                    if tp == 0: tp = float(best_match.get('tp') or best_match.get('take_profit') or 0)
            
            exchanges_payload[ex_name_up]['active'].append({
                'symbol': sym, 'side': side, 'leverage': lev,
                'entry_price': entry, 'current_price': cur,
                'roe': roe, 'pnl_usd': pnl_usd, 'tp': tp, 'sl': sl
            })
            total_active += 1

        # Format and append PENDING
        for o in pending_list:
            sym = o.get('symbol')
            side = o.get('side', 'BUY').upper()
            price = float(o.get('price') or o.get('stopPrice') or 0)
            # Need current price for pending format too
            ticker = await data_manager.fetch_ticker(sym, exchange=ex_name_up)
            cur = float(ticker['last']) if ticker else price
            
            # Leverage/SL/TP enrichment from local
            pk = current_trader._get_pos_key(sym, o.get('timeframe'))
            l_ord = local_pending.get(pk) or local_active.get(pk)
            
            # Robust fallback search if strict key fails
            if not l_ord:
                norm_sym = current_trader._normalize_symbol(sym)
                prefix = f"{ex_name_up}_"
                for l_key, l_val in {**local_pending, **local_active}.items():
                    has_any_prefix = l_key.startswith(('BINANCE_', 'BYBIT_'))
                    is_match = l_key.startswith(prefix) or (not has_any_prefix and ex_name_up == 'BINANCE')
                    if is_match and current_trader._normalize_symbol(l_val.get('symbol')) == norm_sym:
                        l_ord = l_val
                        break

            lev = 1
            sl = 0
            tp = 0
            if l_ord:
                lev = int(l_ord.get('leverage') or 1)
                sl = float(l_ord.get('sl') or 0)
                tp = float(l_ord.get('tp') or 0)

            exchanges_payload[ex_name_up]['pending'].append({
                'symbol': sym, 'side': side, 'leverage': lev,
                'entry_price': price, 'current_price': cur,
                'roe': 0, 'pnl_usd': 0, 'tp': tp, 'sl': sl
            })
            total_pending += 1

    # 3. Final String Generation
    if is_portfolio:
        return format_portfolio_update_v2(
            total_balance=total_balance,
            daily_pnl_pct=daily_pnl_pct,
            active_count=total_active,
            pending_count=total_pending,
            exchanges_data=exchanges_payload
        )
    
    # Status message v2 (Manual grouping if not calling Portfolio formatter directly)
    now_str = datetime.now().strftime('%d/%m %H:%M')
    cache_tag = " (CACHED)" if use_cache else ""
    lines = [f"📊 *BOT STATUS v2*{cache_tag} - {now_str}", ""]
    
    for ex_name, data in exchanges_payload.items():
        lines.append(f"🏦 {ex_name}")
        
        # Active Section
        active_list = data.get('active', [])
        lines.append(f"🟢 ACTIVE ({len(active_list)})")
        lines.append("────────────────────")
        if not active_list:
            lines.append("   _None_")
        else:
            for p in active_list:
                lines.append(format_position_v2(**p))
                lines.append("")
        
        # Pending Section  
        pending_list = data.get('pending', [])
        lines.append(f"🟡 PENDING ({len(pending_list)})")
        lines.append("────────────────────")
        if not pending_list:
            lines.append("   _None_")
        else:
            for p in pending_list:
                lines.append(format_position_v2(**p, is_pending=True))
                lines.append("")
        
        lines.append("")
        
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
    total_usd = sum(float(t.get('pnl_usdt', 0) or 0) for t in filtered)
    
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
    msg = await get_status_message(force_live=True)
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
        from config import RISK_PER_TRADE, LEVERAGE
        from risk_manager import RiskManager
        results = []
        for name, t in traders.items():
            # Get current balance from exchange or simulated
            try:
                bal_data = await t.exchange.fetch_balance()
                current_bal = float(bal_data.get('total', {}).get('USDT', 0) or bal_data.get('free', {}).get('USDT', 0) or 0)
            except:
                current_bal = 1000 # fallback
                
            rm = RiskManager(exchange_name=name, risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
            msg = rm.reset_peak(current_bal)
            results.append(f"🏦 {name}: {msg}")
            
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
        msg = await get_status_message(force_live=True)
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