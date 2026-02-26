import asyncio
import logging
import numpy as np
import os
import sys
import time
import json

# Add src to path if running directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Enhanced Terminal Logging with Timestamps
import builtins
from datetime import datetime
_orig_print = builtins.print
def custom_print(*args, **kwargs):
    now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    _orig_print(f"{now}", *args, **kwargs)
builtins.print = custom_print

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    BYBIT_API_KEY, BYBIT_API_SECRET,
    TRADING_SYMBOLS, 
    TRADING_TIMEFRAMES, 
    LEVERAGE,
    RISK_PER_TRADE,
    STOP_LOSS_PCT, 
    TAKE_PROFIT_PCT,
    USE_LIMIT_ORDERS,
    PATIENCE_ENTRY_PCT,
    DRY_RUN,
    ACTIVE_EXCHANGE,
    MIN_CONFIDENCE_TO_TRADE
)
from data_manager import MarketDataManager
from strategy import WeightedScoringStrategy 
from risk_manager import RiskManager
from execution import Trader
from notification import (
    send_telegram_message,
    format_pending_order,
    format_position_filled,
    format_position_closed,
    format_order_cancelled,
    format_status_update,
    format_adaptive_trigger
)
from signal_tracker import tracker as signal_tracker

# ...
class BalanceTracker:
    """
    Manages shared balance state across multiple bots to prevent race conditions.
    Ensures that when one bot reserves funds for an order, others see the reduced balance immediately.
    """
    def __init__(self):
        self.balances = {} # {exchange_name: {'total': 0.0, 'reserved': 0.0, 'locked': 0.0}}
        
    def update_balance(self, exchange_name, total_equity):
        if exchange_name not in self.balances:
            self.balances[exchange_name] = {'total': 0.0, 'reserved': 0.0}
        
        # We only update TOTAL. Reserved is tracked separately until cleared.
        # However, a fresh fetch from exchange might already reflect used funds if orders were filled.
        # But for 'reserved' (pending orders not yet placed), we must persist reservation.
        self.balances[exchange_name]['total'] = total_equity
        
    def get_available(self, exchange_name):
        if exchange_name not in self.balances: return 0.0
        b = self.balances[exchange_name]
        return max(0.0, b['total'] - b['reserved'])

    def reserve(self, exchange_name, amount):
        """Try to reserve funds. Returns True if successful."""
        avail = self.get_available(exchange_name)
        if avail >= amount:
            self.balances[exchange_name]['reserved'] += amount
            return True
        return False

    def release(self, exchange_name, amount):
        """Release reserved funds (e.g. after order placement or failure)."""
        if exchange_name in self.balances:
            self.balances[exchange_name]['reserved'] = max(0.0, self.balances[exchange_name]['reserved'] - amount)

    def reset_reservations(self):
        """Reset all reservations. Should be called periodically to clear leaks."""
        for ex in self.balances:
            if self.balances[ex]['reserved'] > 0:
                print(f"🧹 [BALANCE] Clearing leaked reservations for {ex}: ${self.balances[ex]['reserved']:.2f} -> $0.00")
            self.balances[ex]['reserved'] = 0.0

class TradingBot:
    def __init__(self, symbol, timeframe, data_manager, trader, balance_tracker=None):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_manager = data_manager 
        self.balance_tracker = balance_tracker
# ...
        # Features are now computed and cached in data_manager (shared across all bots)
        self.strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe, exchange=trader.exchange_name) 
        # Weights are loaded automatically in __init__ now
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
        self.trader = trader
        self.logger = logging.getLogger(__name__)
        self.running = True
        
    def get_tier_config(self, score):
        """Get sizing tier based on confidence score."""
        return self.strategy.get_sizing_tier(score)

    async def run_monitoring_cycle(self):
        """
        Handles existing positions, pending order invalidation, and SL/TP monitoring.
        Returns True if a position/order was closed/cancelled, False otherwise.
        """
        try:
            df = self.data_manager.get_data(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
            if df is None or df.empty:
                return False

            last_row = df.iloc[-1]
            current_price = float(last_row['close'])
            pos_key = self.trader._get_pos_key(self.symbol, self.timeframe)
            
            # 1. CHECK PENDING ORDERS
            pending_order = self.trader.pending_orders.get(pos_key)
            if not pending_order:
                # Dry run check
                existing_pos = self.trader.active_positions.get(pos_key)
                if existing_pos and existing_pos.get('status') == 'pending':
                    pending_order = {
                        'side': existing_pos.get('side'),
                        'price': existing_pos.get('entry_price'),
                        'symbol': existing_pos.get('symbol')
                    }
            
            if pending_order:
                df_check = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df_check is not None and not df_check.empty:
                    last_row_check = df_check.iloc[-1]
                    signal_data = self.strategy.get_signal(last_row_check)
                    current_signal_side = signal_data['side']
                    pending_side = pending_order['side']
                    
                    should_cancel = False
                    cancel_reason = ""
                    if current_signal_side == 'SELL' and pending_side == 'BUY':
                        should_cancel = True
                        cancel_reason = "Signal reversed to SELL"
                    elif current_signal_side == 'BUY' and pending_side == 'SELL':
                        should_cancel = True
                        cancel_reason = "Signal reversed to BUY"
                    elif current_signal_side is None:
                        should_cancel = True
                        cancel_reason = "Signal disappeared"
                    
                    if should_cancel:
                        print(f"❌ [{self.symbol} {self.timeframe}] CANCELING: {cancel_reason}")
                        await self.trader.cancel_pending_order(pos_key, reason=cancel_reason)
                        terminal_msg, telegram_msg = format_order_cancelled(
                            self.symbol, self.timeframe, pending_side, pending_order['price'], 
                            cancel_reason, self.trader.dry_run, self.trader.exchange_name
                        )
                        print(terminal_msg)
                        await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                        return True
                return False

            # 2. MONITOR ACTIVE POSITION
            existing_pos = self.trader.active_positions.get(pos_key)
            if existing_pos and existing_pos.get('status') == 'filled':
                side = existing_pos.get('side', 'BUY').upper()
                entry_price = float(existing_pos.get('entry_price', 0))
                leverage = int(existing_pos.get('leverage', 1))
                
                unrealized_pnl_pct = ((current_price - entry_price) / entry_price * 100 * leverage)
                if side in ['SELL', 'SHORT']: unrealized_pnl_pct = -unrealized_pnl_pct

                # 2.1 SL/TP Check
                sl = float(existing_pos.get('sl', 0))
                tp = float(existing_pos.get('tp', 0))
                
                exit_reason = None
                if side in ['BUY', 'LONG']:
                    if sl > 0 and current_price <= sl: exit_reason = 'SL'
                    elif tp > 0 and current_price >= tp: exit_reason = 'TP'
                else:
                    if sl > 0 and current_price >= sl: exit_reason = 'SL'
                    elif tp > 0 and current_price <= tp: exit_reason = 'TP'
                
                if exit_reason:
                    if self.trader.dry_run:
                        # Simulation mode exit
                        print(f"🔴 [{self.trader.exchange_name}] [{self.symbol}] {exit_reason} hit internally (Dry Run). Closing...")
                        result = 'WIN' if exit_reason == 'TP' else 'LOSS'
                        # Use ROE for dry run reporting consistency
                        pnl_pct = (current_price - entry_price) / entry_price * leverage
                        if side in ['SELL', 'SHORT']: pnl_pct = -pnl_pct
                        
                        signals_used = existing_pos.get('signals_used', [])
                        snapshot = existing_pos.get('snapshot', {})
                        
                        btc_change = 0.0
                        try:
                            btc_df = self.data_manager.get_data('BTC/USDT', '1h', exchange=self.trader.exchange_name)
                            if btc_df is not None and not btc_df.empty:
                                btc_change = (btc_df.iloc[-1]['close'] - btc_df.iloc[-2]['close']) / btc_df.iloc[-2]['close']
                        except: pass
                        
                        await signal_tracker.record_trade(self.symbol, self.timeframe, side, signals_used, result, pnl_pct, btc_change, snapshot)
                        await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason=exit_reason)
                        return True
                    else:
                        print(f"⏳ [{self.trader.exchange_name}] [{self.symbol}] Price crossed {exit_reason} internally. Triggering proactive sync...")
                        await self.trader.reconcile_positions(auto_fix=True, force_verify=True)
                        return True

                # 2.2 Profit Lock
                await self.trader.adjust_sl_tp_for_profit_lock(
                    pos_key, current_price, 
                    resistance=last_row.get('resistance_level'), 
                    support=last_row.get('support_level'), 
                    atr=last_row.get('ATR_14')
                )

                # 2.3 Signal Reversal
                df_rev = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df_rev is not None and not df_rev.empty:
                    last_row_rev = df_rev.iloc[-1]
                    rev_signal_data = self.strategy.get_signal(last_row_rev)
                    rev_side = rev_signal_data['side']
                    rev_score = rev_signal_data['confidence'] * 10
                    exit_thresh = self.strategy.config_data.get('thresholds', {}).get('exit_score', 2.5)
                    
                    opp_side = 'SELL' if side == 'BUY' else 'BUY'
                    if rev_side == opp_side and rev_score >= exit_thresh:
                        print(f"🔄 [{self.symbol} {self.timeframe}] SIGNAL REVERSED to {rev_side} (Score {rev_score:.1f}). Force closing...")
                        success = await self.trader.force_close_position(pos_key, reason=f"Signal Flip to {rev_side}")
                        if success:
                            qty = existing_pos.get('qty', 0)
                            pnl_usd = (unrealized_pnl_pct / 100) * (qty * entry_price) / leverage
                            terminal_msg, telegram_msg = format_position_closed(
                                self.symbol, self.timeframe, side, entry_price, current_price, 
                                pnl_usd, unrealized_pnl_pct, f"Signal Flip ({rev_side})", 
                                dry_run=self.trader.dry_run, exchange_name=self.trader.exchange_name
                            )
                            print(terminal_msg)
                            await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                            return True
                return False
            return False
        except Exception as e:
            self.logger.error(f"Error in monitoring cycle for {self.symbol} {self.timeframe}: {e}")
            return False

    async def get_new_entry_signal(self):
        """
        Evaluates the strategy and returns signal data if a valid entry signal exists.
        Returns: signal_data dict or None
        """
        try:
            # 1. GUARDS
            if self.trader.is_in_cooldown(self.symbol):
                return None
            
            skip, reason = signal_tracker.should_skip_symbol(self.symbol, min_wr=0.3, min_trades=3)
            if skip:
                return None

            df = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
            if df is None or df.empty:
                return None
            
            last_row = df.iloc[-1]
            signal_data = self.strategy.get_signal(last_row)
            
            if signal_data['side'] in ['BUY', 'SELL']:
                # 3. CONFIDENCE THRESHOLD CHECK
                if signal_data['confidence'] < MIN_CONFIDENCE_TO_TRADE:
                    return None
                    
                if hasattr(self.strategy, 'is_enabled') and not self.strategy.is_enabled():
                    return None
                
                # Technical confirmation
                technical_confirm = False
                confirm_signals = []
                for k, l in [('signal_at_fibo_key_level', "Fibo"), ('signal_bounce_from_support', "Support"), 
                            ('signal_bounce_from_resistance', "Resistance"), ('signal_price_at_support', "At Support"), 
                            ('signal_price_at_resistance', "At Resistance")]:
                    if last_row.get(k, False):
                        technical_confirm = True
                        confirm_signals.append(l)
                
                from config import REQUIRE_TECHNICAL_CONFIRMATION
                if REQUIRE_TECHNICAL_CONFIRMATION and not technical_confirm:
                    return None
                
                signal_data['technical_info'] = f" ({', '.join(confirm_signals)})" if confirm_signals else ""
                signal_data['last_row'] = last_row
                return signal_data
            
            return None
        except Exception as e:
            self.logger.error(f"Error evaluating signal for {self.symbol} {self.timeframe}: {e}")
            return None

    async def execute_entry(self, signal_data, current_equity):
        """
        Calculates position size and executes the trade.
        """
        try:
            side = signal_data['side']
            conf = signal_data['confidence']
            last_row = signal_data['last_row']
            current_price = float(last_row['close'])
            
            # PRE-TRADE CHECK (Only for Live)
            if not self.trader.dry_run:
                state = await self.trader.verify_symbol_state(self.symbol)
                if state and (state['active_exists'] or state['order_exists']):
                    await self.trader.reconcile_positions()
                    return False

            # REVERSAL REDUCTION
            last_trade_side = signal_tracker.get_last_trade_side(self.symbol)
            is_reversal = last_trade_side and last_trade_side != side
            
            score = conf * 10
            tier = self.get_tier_config(score)
            target_lev = tier.get('leverage', LEVERAGE)
            
            if is_reversal:
                target_lev = max(3, int(target_lev * 0.6))
            
            # Risk params
            sl_pct, tp_pct = self.strategy.get_dynamic_risk_params(last_row)
            if is_reversal: sl_pct *= 0.8
            
            sl_price = current_price * (1 - sl_pct) if side == 'BUY' else current_price * (1 + sl_pct)
            tp_price = current_price * (1 + tp_pct) if side == 'BUY' else current_price * (1 - tp_pct)
            
            # Placement
            # tier contains cost_usdt and leverage
            qty = self.risk_manager.calculate_size_by_cost(current_price, tier.get('cost_usdt', 10), target_lev)
            if qty <= 0: return False
            
            print(f"🎯 [{self.symbol} {self.timeframe}] EXECUTING {side} x{target_lev} | Conf: {conf:.2f}")

            # Sanitize snapshot before passing to trader
            safe_snapshot = {}
            for k, v in last_row.to_dict().items():
                if hasattr(v, 'isoformat'): safe_snapshot[k] = v.isoformat()
                elif isinstance(v, (np.integer, np.floating)): safe_snapshot[k] = v.item()
                else: safe_snapshot[k] = v

            return await self.trader.place_order(
                symbol=self.symbol, side=side, qty=qty, timeframe=self.timeframe, 
                order_type='limit' if USE_LIMIT_ORDERS else 'market',
                price=current_price * (1 - PATIENCE_ENTRY_PCT) if side == 'BUY' and USE_LIMIT_ORDERS else current_price * (1 + PATIENCE_ENTRY_PCT) if side == 'SELL' and USE_LIMIT_ORDERS else None,
                sl=sl_price, tp=tp_price, leverage=target_lev, signals_used=signal_data.get('signals',''),
                entry_confidence=conf, snapshot=safe_snapshot
            )
        except Exception as e:
            self.logger.error(f"Error executing entry for {self.symbol} {self.timeframe}: {e}")
            return False

    async def run_step(self, current_equity=None, circuit_breaker_triggered=False):
        """
        Main execution step for a single (symbol, timeframe) bot.
        """
        # Circuit breaker is checked ONCE in main(), passed here as flag
        if circuit_breaker_triggered:
            if self.running:
                self.logger.warning(f"🛑 [CIRCUIT BREAKER] Stopping bot for {self.symbol} {self.timeframe}")
            self.running = False
            return

        try:
            # 1. Monitoring (SL/TP, reversal, pending logic)
            await self.run_monitoring_cycle()
            
            # 2. Evaluation & Execution
            already_in_this_symbol = await self.trader.has_any_symbol_position(self.symbol)
            pos_key = self.trader._get_pos_key(self.symbol, self.timeframe)
            
            if already_in_this_symbol:
                if pos_key not in self.trader.active_positions and pos_key not in self.trader.pending_orders:
                    return
                return

            signal_data = await self.get_new_entry_signal()
            if signal_data:
                # Available Equity
                if self.balance_tracker:
                     current_equity = self.balance_tracker.get_available(self.trader.exchange_name)
                elif current_equity is None:
                     current_equity = 0.0

                if self.trader.exchange.can_trade and current_equity < 1.0:
                     return

                # Execute
                await self.execute_entry(signal_data, current_equity)
        except Exception as e:
            self.logger.error(f"Error in TradingBot.run_step ({self.symbol} {self.timeframe}): {e}")
            import traceback
            self.logger.error(traceback.format_exc())


import time
from analyzer import run_global_optimization
from notification import send_telegram_message, send_telegram_chunked

async def send_periodic_status_report(trader, data_manager):
    """Aggregates all active and pending positions and sends a summary to Telegram."""
    positions = trader.active_positions
    if not positions:
        return

    active_lines = []
    pending_lines = []
    total_pnl_usd = 0
    
    from notification import format_symbol, format_price, format_pnl
    
    for key, pos in positions.items():
        # Filter out closed/cancelled
        status = str(pos.get('status', '')).lower()
        if status in ['closed', 'cancelled'] or pos.get('qty', 0) == 0:
            continue
            
        parts = key.split('_')
        raw_symbol = pos.get('symbol', parts[1] if len(parts) >= 3 else parts[0])
        symbol = format_symbol(raw_symbol)
        tf = pos.get('timeframe', parts[-1] if len(parts) >= 3 else '1h')
        side = pos.get('side', 'N/A').upper()
        entry = float(pos.get('entry_price') or pos.get('price') or 0)
        qty = float(pos.get('qty') or 0)
        sl = float(pos.get('sl') or 0)
        tp = float(pos.get('tp') or 0)
        
        if status == 'filled':
            # Get current price from data store
            df = data_manager.get_data(raw_symbol, tf, exchange=trader.exchange_name)
            current_price = df.iloc[-1]['close'] if df is not None and not df.empty else entry
            
            # Calculate PnL
            if side == 'BUY' or side == 'LONG':
                pnl_pct = ((current_price - entry) / entry) * 100 * leverage if entry > 0 else 0
            else:
                pnl_pct = ((entry - current_price) / entry) * 100 * leverage if entry > 0 else 0
            
            pnl_usd = (pnl_pct / 100) * qty * entry
            total_pnl_usd += pnl_usd
            pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
            
            active_lines.append(
                f"{pnl_icon} **{symbol}** ({side})\n"
                f"   Entry: {format_price(entry)} | Now: {format_price(current_price)}\n"
                f"   PnL: {pnl_pct:+.2f}% | ${pnl_usd:+.2f}\n"
                f"   SL: {format_price(sl) if sl else 'N/A'} | TP: {format_price(tp) if tp else 'N/A'}"
            )
        else:
            # Pending Entries
            # Try to fetch current price for reference
            df = data_manager.get_data(raw_symbol, tf, exchange=trader.exchange_name)
            cur_price_str = f" | Now: {format_price(df.iloc[-1]['close'])}" if df is not None and not df.empty else ""
            
            pending_lines.append(
                f"⏳ **{symbol}** ({side}) @ `{format_price(entry)}`{cur_price_str} | Qty: {qty}\n"
                f"   🎯 TP: {format_price(tp) if tp else 'N/A'} | 🛡 SL: {format_price(sl) if sl else 'N/A'}"
            )

    if not active_lines and not pending_lines:
        return

    msg_sections = []
    now = time.strftime('%d/%m %H:%M')
    msg_sections.append(f"📊 **PORTFOLIO UPDATE** - {now}")
    
    if active_lines:
        msg_sections.append("🟢 **ACTIVE POSITIONS**")
        msg_sections.append("-" * 15)
        msg_sections.append("\n\n".join(active_lines))
        
        total_icon = "🟢" if total_pnl_usd >= 0 else "🔴"
        msg_sections.append(f"\n{total_icon} **Total PnL: ${total_pnl_usd:+.2f}**")
    
    if pending_lines:
        msg_sections.append("\n🟡 **PENDING ENTRIES**")
        msg_sections.append("-" * 15)
        msg_sections.append("\n\n".join(pending_lines))
    
    final_msg = "\n".join(msg_sections)
    await send_telegram_chunked(final_msg, exchange_name=trader.exchange_name)

async def main():
    import argparse
    parser = argparse.ArgumentParser(description='Trading Bot')
    parser.add_argument('--dry-run', action='store_true', help='Run in simulation mode')
    parser.add_argument('--live', action='store_true', help='Run in live trading mode')
    args, unknown = parser.parse_known_args()

    # DRY_RUN is strictly sourced from config (.env)
    import config
    print(f"🚩 Mode: {'Simulation (Dry Run)' if config.DRY_RUN else 'Live Trading'}")

    # Initialize Global Manager and Balance Tracker

    # Initialize Global Manager and Balance Tracker
    from exchange_factory import get_active_exchanges_map
    ex_adapters = get_active_exchanges_map()
    manager = MarketDataManager(adapters=ex_adapters)
    balance_tracker = BalanceTracker()
    
    # Initialize Traders for each Active Exchange
    traders = {name: Trader(adapter, dry_run=config.DRY_RUN, data_manager=manager) for name, adapter in ex_adapters.items()}
    
    print(f"🚀 Initializing parallel bots for {len(traders)} exchanges: {list(traders.keys())}")
    
    # Sync server time and LOAD MARKETS for each exchange
    for name, trader in traders.items():
        print(f"⏰ Synchronizing with {name} server time & markets...")
        try:
            await trader.exchange.sync_time()
            # CRITICAL: Load markets to populate precision/limits for amount_to_precision
            await trader.exchange.load_markets()
        except Exception as e:
            print(f"⚠️ [{name}] Initialization failed: {e}")

    # Initialize Bot instances per exchange/symbol/timeframe
    bots = []
    for ex_name, trader in traders.items():
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {
            'BINANCE': BINANCE_SYMBOLS,
            'BYBIT': BYBIT_SYMBOLS
        }
        # Determine strict symbols for this exchange
        active_symbols = exchange_symbol_map.get(ex_name, TRADING_SYMBOLS)
        # Filter to ensure we only use symbols present in the global TRADING_SYMBOLS if disjoint
        active_symbols = [s for s in active_symbols if s in TRADING_SYMBOLS]

        # Setup margin modes and leverage for LIVE (skip if unauthenticated)
        if not trader.dry_run and trader.exchange.is_authenticated:
            print(f"🔧 [{ex_name}] Setting up margin modes and leverage...")
            # Capture failed symbols (e.g. invalid permissions)
            failed_symbols = await manager.set_isolated_margin_mode(active_symbols, exchange=ex_name)
            
            if failed_symbols:
                print(f"⚠️ [{ex_name}] Removing {len(failed_symbols)} failed symbols from active list.")
                active_symbols = [s for s in active_symbols if s not in failed_symbols]

            if not active_symbols:
                print(f"❌ [{ex_name}] No valid symbols remaining after initialization checks.")
                continue

            await trader.enforce_isolated_on_startup(active_symbols)
            print(f"🔄 [{ex_name}] Synchronizing positions...")
            await trader.reconcile_positions()
            
            # Start monitoring tasks for any existing pending orders
            await trader.resume_pending_monitors()

        for symbol in active_symbols:
            for tf in TRADING_TIMEFRAMES:
                # Pass shared balance_tracker to each bot
                bot = TradingBot(symbol, tf, manager, trader, balance_tracker=balance_tracker)
                bots.append(bot)
                
    print(f"✅ Total {len(bots)} bot tasks initialized.")

    # ========== ADAPTIVE LEARNING SETUP ==========
    async def on_losses_detected(symbols_to_check):
        print(f"🚨 [ADAPTIVE] Evaluating performance for symbols: {symbols_to_check}")
        for symbol in symbols_to_check:
            skip, reason = signal_tracker.should_skip_symbol(symbol, min_wr=0.3, min_trades=3)
            if skip:
                print(f"📉 [ADAPTIVE] Stopping {symbol} due to poor performance: {reason}")
                for bot in bots:
                    if bot.symbol == symbol:
                        bot.running = False

    async def on_adjust_positions():
        print("🎯 [ADAPTIVE] Checking if any open positions need risk adjustment (Multi-TF v4.0)...")
        
        # TF Mapping for Trailing SL (Layer 2)
        # Entry TF -> Trail TF
        TRAIL_TF_MAP = {
            '15m': '1h',
            '30m': '2h',
            '1h':  '4h',
            '2h':  '8h',
            '4h':  '1d',
            '8h':  '1d',
            '1d':  '1d'
        }

        for ex_name, trader in traders.items():
            for pos_key, pos in trader.active_positions.items():
                if pos.get('status') != 'filled': continue
                
                symbol = pos.get('symbol')
                entry_tf = pos.get('timeframe', '1h')
                trail_tf = TRAIL_TF_MAP.get(entry_tf, '4h')
                
                # Fetch Data for Guard (Entry TF) and Trail (Higher TF)
                df_guard = manager.get_data_with_features(symbol, entry_tf, exchange=ex_name)
                df_trail = manager.get_data_with_features(symbol, trail_tf, exchange=ex_name)
                
                if df_guard is None or df_guard.empty or df_trail is None or df_trail.empty:
                    continue
                
                # Call new dynamic SL/TP (v4.0)
                # This function handles both Trailing Stop (Trail TF) and Emergency Guards (Entry TF)
                await trader.update_dynamic_sltp(
                    pos_key, 
                    df_trail=df_trail, 
                    df_guard=df_guard
                )

    def sync_adjust_positions():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Avoid creating task if loop is closing
                pass # asyncio.create_task(on_adjust_positions()) # Disabled to prevent interference
            else:
                pass # loop.run_until_complete(on_adjust_positions())
        except Exception as e:
            print(f"Error in position adjustment: {e}")

    signal_tracker.set_analysis_callback(on_losses_detected)
    signal_tracker.set_position_adjust_callback(sync_adjust_positions)
    print("✅ Adaptive Learning v2.0 callbacks registered")
    # ========== END ADAPTIVE LEARNING SETUP ==========

    # Per-Exchange RiskManager for circuit breaker
    risk_managers = {name: RiskManager(exchange_name=name, risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE) for name in traders}

    def get_current_balance(ex_name=None):
        """
        Calculates meaningful equity for the bot.
        If live: returns actual exchange equity (fetched earlier).
        If dry: returns simulation balance plus unrealized PNL.
        """
        from config import SIMULATION_BALANCE
        
        # Default behavior (legacy/global):
        if not ex_name:
            return SIMULATION_BALANCE # Fallback for absolute safety
            
        # 1. Start with the "base" liquidity
        if config.DRY_RUN:
            current_bal = SIMULATION_BALANCE
        else:
            # For live trading, we strictly use what was just updated in the tracker
            current_bal = balance_tracker.get_available(ex_name)
            
        # 2. Factor in unrealized PNL for active positions
        trader = traders.get(ex_name)
        if trader:
            for pos_key, pos in trader.active_positions.items():
                if pos.get('status') == 'filled':
                    symbol = pos.get('symbol')
                    tf = pos.get('timeframe', '1h')
                    df = manager.get_data(symbol, tf, exchange=ex_name)
                    if df is not None and not df.empty:
                        cur_price = df.iloc[-1]['close']
                        entry = pos.get('entry_price', cur_price)
                        qty = pos.get('qty', 0)
                        side = pos.get('side')
                        unrealized = (cur_price - entry) * qty if side == 'BUY' else (entry - cur_price) * qty
                        current_bal += unrealized
        return current_bal

    # Trackers for periodic tasks
    last_time_sync = time.time()
    last_auto_opt = time.time()
    last_status_update = time.time()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
    last_config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
    
    status_interval = 2 * 3600 
    opt_interval = 12 * 3600

    print("🚀 Starting Main Loop...")
    try:
        while True:
            curr_time = time.time()
            
            # -1. Reset Reservations (Self-Healing)
            # Since main loop waits for all bots to finish each cycle, any leftover reservations are leaks.
            balance_tracker.reset_reservations()
            
            # -0.5. Fast Deep Sync (Before bot logic runs)
            for ex_name, trader in traders.items():
                try: await trader.reconcile_positions()
                except: pass
            
            # 0. Sync Server Time
            if curr_time - last_time_sync >= 3600:
                print("⏲ Syncing server time for all exchanges...")
                for ex_name, trader in traders.items():
                    try: await trader.exchange.sync_time()
                    except: pass
                last_time_sync = curr_time
            
            # 0. Check for Auto-Optimization
            if curr_time - last_auto_opt >= opt_interval:
                print("⏰ Scheduled Auto-Optimization triggered...")
                try:
                    await run_global_optimization()
                    last_auto_opt = curr_time
                    for bot in bots: bot.strategy.reload_config()
                    if traders:
                        await send_telegram_message("🔄 Auto-Optimization Complete.", exchange_name=list(traders.keys())[0])
                except: pass

            # 0.1 Periodic Status Update
            if curr_time - last_status_update >= status_interval:
                print("📊 Sending periodic status updates...")
                for ex_name, trader in traders.items():
                    try: await send_periodic_status_report(trader, manager)
                    except: pass
                last_status_update = curr_time

            # 0.2 Deep Sync (Self-Healing)
            if not hasattr(main, 'last_deep_sync'): main.last_deep_sync = 0
            if curr_time - main.last_deep_sync >= 600:
                print("🔄 [SELF-HEALING] Running Deep Sync...")
                for ex_name, trader in traders.items():
                    # Only sync if authenticated or dry_run
                    if trader.dry_run or trader.exchange.is_authenticated:
                        try: await trader.reconcile_positions(auto_fix=True)
                        except: pass
                main.last_deep_sync = curr_time

            # 1. Update Market Data
            await manager.update_tickers(TRADING_SYMBOLS)
            data_updated = await manager.update_data(TRADING_SYMBOLS, TRADING_TIMEFRAMES)
            
            # 1.5. Reload config
            # Check strategy_config.json
            current_config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
            
            # Check src/config.py
            import config
            import importlib
            src_config_path = os.path.abspath(config.__file__)
            current_src_config_mtime = os.path.getmtime(src_config_path) if os.path.exists(src_config_path) else 0
            
            if current_config_mtime != last_config_mtime:
                print("🔄 Strategy Config changed, reloading...")
                for bot in bots: bot.strategy.reload_config()
                last_config_mtime = current_config_mtime
                
            if current_src_config_mtime != getattr(main, 'last_src_config_mtime', 0):
                if hasattr(main, 'last_src_config_mtime'): # Skip first run
                     print("🔄 Main Config (config.py) changed, reloading...")
                     importlib.reload(config)
                     # Re-apply global settings if needed, though strategy reads from config module directly now
                main.last_src_config_mtime = current_src_config_mtime
            
            # 2. Update and Check Balances
            # balances = {} <--- Removed local dict
            for ex_name, trader in traders.items():
                current_bal = 0.0
                if trader.dry_run:
                    current_bal = get_current_balance(ex_name)
                elif not trader.exchange.is_authenticated:
                    print(f"⚠️ [{ex_name}] Not Authenticated - Defaulting balance to 0.0")
                    current_bal = 0.0
                else:
                    try:
                        # Fetch real balance from exchange
                        bal_data = await trader.exchange.fetch_balance()
                        # Get total USDT equity
                        current_bal = float(bal_data.get('total', {}).get('USDT', 0))
                        
                        # If 0, try 'free' as a fallback
                        if current_bal == 0:
                             current_bal = float(bal_data.get('free', {}).get('USDT', 0))
                        
                        # LOG THE TOTAL EQUITY (DEBUG)
                        print(f"💰 [{ex_name}] Balance Fetch: {current_bal:.2f} USDT")

                    except Exception as e:
                        print(f"⚠️ [{ex_name}] Failed to fetch real balance details: {e}")
                        # FORCE 0 for live trades if fetch fails
                        current_bal = 0.0 

                # Update shared tracker
                balance_tracker.update_balance(ex_name, current_bal)
                
                # Check Circuit Breaker (Per Exchange)
                # Only check if authenticated/dry_run AND we have a valid balance
                stop_trading = False
                cb_reason = ""
                
                if (trader.exchange.is_authenticated or trader.dry_run) and current_bal > 0:
                    rm = risk_managers.get(ex_name)
                    if rm:
                        stop_trading, cb_reason = rm.check_circuit_breaker(current_bal)
                
                # Initialize CB throttle dict if not exists
                if not hasattr(main, 'last_cb_alert'):
                    main.last_cb_alert = {}
                
                if stop_trading:
                    print(f"🚨 [{ex_name}] CIRCUIT BREAKER: {cb_reason}")
                    
                    last_alert_time = main.last_cb_alert.get(ex_name, 0)
                    if curr_time - last_alert_time >= 7200:  # 7200 seconds = 2 hours
                        await send_telegram_message(f"🚨 CIRCUIT BREAKER: {cb_reason} (Next reminder in 2h)", exchange_name=ex_name)
                        main.last_cb_alert[ex_name] = curr_time

            # 3. COORDINATED LOGIC (Symbol-wise Competition)
            # Group bots by (trader, symbol) to coordinate across timeframes
            bot_groups = {}
            for bot in bots:
                if not bot.running: continue
                key = (bot.trader, bot.symbol)
                if key not in bot_groups:
                    bot_groups[key] = []
                bot_groups[key].append(bot)

            async def process_symbol_group(trader, symbol, group_bots):
                # 3.1 MONITORING PHASE (Parallel)
                await asyncio.gather(*[b.run_monitoring_cycle() for b in group_bots])
                
                # 3.2 ENTRY EVALUATION PHASE
                # Use trader's lock to prevent race conditions during evaluation
                async with trader._get_lock(symbol):
                    # Check if already in symbol (filled or pending)
                    existing_pending_key = None
                    existing_pending_pos = None
                    has_filled = False
                    
                    for key, pos in trader.active_positions.items():
                        if pos.get('symbol') == symbol:
                            if pos.get('status') == 'filled':
                                has_filled = True
                                break
                            elif pos.get('status') == 'pending':
                                existing_pending_key = key
                                existing_pending_pos = pos

                    if has_filled:
                        return

                    # Collect signals from all timeframes in parallel
                    signal_tasks = [b.get_new_entry_signal() for b in group_bots]
                    signals = await asyncio.gather(*signal_tasks)
                    
                    # Filter for valid signals
                    valid_signals = [] # list of (bot, signal_data)
                    for idx, sig in enumerate(signals):
                        if sig:
                            valid_signals.append((group_bots[idx], sig))
                    
                    if not valid_signals:
                        return
                        
                    # 3.3 CONFIDENCE COMPETITION (Highest Confidence Wins)
                    best_bot, best_signal = max(valid_signals, key=lambda x: x[1]['confidence'])
                    
                    # 3.4 PENDING COMPETITION & EXECUTION
                    if existing_pending_pos:
                        ex_conf = existing_pending_pos.get('entry_confidence', 0)
                        ex_tf = existing_pending_pos.get('timeframe')
                        new_conf = best_signal['confidence']
                        
                        # Replace if new signal is significantly better (+5% threshold to avoid noise)
                        if new_conf > (ex_conf + 0.05):
                            print(f"🔄 [{symbol}] COMPETITION: {best_bot.timeframe} ({new_conf:.2f}) > {ex_tf} ({ex_conf:.2f}). Replacing order...")
                            await trader.cancel_pending_order(existing_pending_key, reason=f"Replaced by better signal on {best_bot.timeframe}")
                        else:
                            # Current pending is good enough
                            return

                    # Final execution
                    ex_name = best_bot.trader.exchange_name
                    bot_stop_trading, _ = risk_managers.get(ex_name).check_circuit_breaker(balance_tracker.get_available(ex_name)) if risk_managers.get(ex_name) and balance_tracker.get_available(ex_name) > 0 else (False, "")

                    if not bot_stop_trading:
                        await best_bot.execute_entry(best_signal, balance_tracker.get_available(ex_name))

            # Run all symbol groups in parallel
            group_tasks = [process_symbol_group(t, s, g) for (t, s), g in bot_groups.items()]
            if group_tasks:
                await asyncio.gather(*group_tasks)
            
            
            from config import HEARTBEAT_INTERVAL, FAST_HEARTBEAT_INTERVAL
            # Dynamic Sleep: Fast if no data update, Slow if data updated
            sleep_time = HEARTBEAT_INTERVAL if data_updated else FAST_HEARTBEAT_INTERVAL
            await asyncio.sleep(sleep_time)
            
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await manager.close()
        for trader in traders.values():
            await trader.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
