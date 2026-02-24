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
    ACTIVE_EXCHANGE
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

    async def run_step(self, current_equity=None, circuit_breaker_triggered=False):
        # Circuit breaker is checked ONCE in main(), passed here as flag
        try:
            if circuit_breaker_triggered:
                self.running = False
                return

            df = self.data_manager.get_data(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
            if df is None or df.empty:
                return

            # Determine Available Equity from Tracker if possible
            if self.balance_tracker:
                 current_equity = self.balance_tracker.get_available(self.trader.exchange_name)
            elif current_equity is None:
                 current_equity = 0.0

            # Low Funds Guard: Skip if less than $1 total equity (ONLY if trading is enabled)
            if self.trader.exchange.can_trade and current_equity < 1.0 and not circuit_breaker_triggered:
                 print(f"⏹️ [{self.trader.exchange_name}] [{self.symbol}] Insufficient funds for trading (${current_equity:.2f}). Skipping.")
                 return

            # Ensure current_price is available for all checks
            last_row = df.iloc[-1]
            current_price = last_row['close']
            
            # PRICE VALIDATION: Ensure current_price is valid number to avoid NoneType comparison errors
            if current_price is None or (isinstance(current_price, (int, float)) and np.isnan(current_price)):
                self.logger.error(f"Invalid current_price ({current_price}) for {self.symbol}. Skipping cycle.")
                return
            
            current_price = float(current_price)

            # 0. GLOBAL CONFLICT CHECK (Single Position Rule)
            # Check if this symbol already has an active position/order on exchange or locally
            # Use namespaced key
            # Determine namespaced key using unified helper
            pos_key = self.trader._get_pos_key(self.symbol, self.timeframe)
            already_in_symbol = await self.trader.has_any_symbol_position(self.symbol)
            if already_in_symbol:
                # If we have a position/order for THIS timeframe, we proceed to regular logic
                # If we DON'T have a record for this timeframe, but has_any_symbol_position is True, 
                # then it belongs to another timeframe. Block it.
                if pos_key not in self.trader.active_positions and pos_key not in self.trader.pending_orders:
                    return

            # pos_key already defined above
            
            # 1. CHECK PENDING ORDERS FIRST - Cancel if technical invalidation
            # Live mode: pending orders are in pending_orders dict
            # Dry run mode: pending orders are in active_positions with status='pending'
            pending_from_live = self.trader.pending_orders.get(pos_key)
            pending_from_dryrun = None
            existing_pos = self.trader.active_positions.get(pos_key)
            
            # Check if dry_run pending order exists
            if not pending_from_live and existing_pos and existing_pos.get('status') == 'pending':
                pending_from_dryrun = {
                    'side': existing_pos.get('side'),
                    'price': existing_pos.get('entry_price'),
                    'symbol': existing_pos.get('symbol')
                }
            
            pending_order = pending_from_live or pending_from_dryrun
            
            if pending_order:
                # Analyze current signal to check if still valid
                # Use cached features from data_manager
                df_check = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df_check is None or df_check.empty:
                    return
                last_row_check = df_check.iloc[-1]
                signal_data = self.strategy.get_signal(last_row_check)
                current_signal_side = signal_data['side']
                current_conf = signal_data['confidence']
                
                pending_side = pending_order['side']
                
                # Cancel if signal reversed or confidence dropped below threshold
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
                    cancel_reason = "No signal detected"
                elif current_conf < 0.2:  # Below minimum entry threshold
                    should_cancel = True
                    cancel_reason = f"Confidence dropped to {current_conf:.2f}"
                
                if should_cancel:
                    await self.trader.cancel_pending_order(pos_key, reason=cancel_reason)
                    # Unified notification
                    pending_entry = pending_order.get('price', 0)
                    terminal_msg, telegram_msg = format_order_cancelled(
                        symbol=self.symbol,
                        timeframe=self.timeframe,
                        side=pending_side,
                        entry_price=pending_entry,
                        reason=cancel_reason,
                        dry_run=self.trader.dry_run,
                        exchange_name=self.trader.exchange_name
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                    return
                
                # For LIVE mode: exchange handles fill, just monitor and return
                if pending_from_live:
                    print(f"⏳ [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] Pending {pending_side} order @ {pending_order['price']:.3f} | Current: {current_price:.3f}")
                    return
                # For DRY RUN mode: fall through to fill check below
            
            # 2. CHECK EXISTING POSITIONS (both pending and filled)
            existing_pos = self.trader.active_positions.get(pos_key)
            if existing_pos:
                pos_status = existing_pos.get('status', 'filled')  # backwards compat
                order_type = existing_pos.get('order_type', 'market')
                
                # For pending limit orders in dry_run mode, check if should be filled
                if pos_status == 'pending' and self.trader.dry_run:
                    filled = self.trader.check_pending_limit_fills(self.symbol, self.timeframe, current_price)
                    if not filled:
                        # Still pending, just display status and return
                        limit_price = existing_pos.get('entry_price')
                        side = existing_pos.get('side')
                        
                        print(f"⏳ [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] PENDING {side} @ {limit_price:.3f} | Now: {current_price:.3f}")
                        return
                    # If filled, reload position data and notify
                    existing_pos = self.trader.active_positions.get(pos_key)
                    pos_status = existing_pos.get('status', 'filled')
                    # Unified notification for filled order
                    side = existing_pos.get('side')
                    limit_price = existing_pos.get('entry_price')
                    sl = existing_pos.get('sl')
                    tp = existing_pos.get('tp')
                    size = existing_pos.get('quantity', 0)
                    notional = existing_pos.get('notional', 0)
                    terminal_msg, telegram_msg = format_position_filled(
                        symbol=self.symbol,
                        timeframe=self.timeframe,
                        side=side,
                        entry_price=limit_price,
                        size=size,
                        notional=notional,
                        sl_price=sl,
                        tp_price=tp,
                        score=existing_pos.get('entry_confidence'),
                        leverage=existing_pos.get('leverage'),
                        dry_run=self.trader.dry_run,
                        exchange_name=self.trader.exchange_name
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                
                # Skip SL/TP monitoring for pending orders
                if pos_status == 'pending':
                    return
                
                sl = existing_pos.get('sl')
                tp = existing_pos.get('tp')
                side = existing_pos.get('side')
                leverage = existing_pos.get('leverage', 10)
                entry_price = existing_pos.get('entry_price', current_price)

                # --- AUTO CHECK & UPDATE SL/TP FOR FILLED POSITIONS (LIVE) ---
                if pos_status == 'filled' and not self.trader.dry_run:
                    # Sync with Symbol Lock to prevent multi-timeframe collisions
                    async with self.trader._get_lock(self.symbol):
                        # Calculate expected SL/TP
                        e_sl, e_tp = self.risk_manager.calculate_sl_tp(
                            entry_price, side,
                            sl_pct=self.strategy.sl_pct,
                            tp_pct=self.strategy.tp_pct
                        )
                        # Round for comparison to avoid float drift (0.0001% tolerance)
                        if e_sl is not None and e_tp is not None:
                            e_sl, e_tp = round(e_sl, 5), round(e_tp, 5)
                            curr_sl, curr_tp = round(float(sl or 0), 5), round(float(tp or 0), 5)
                        else:
                            # Skip this sync cycle if we can't calculate SL/TP
                            return
                        
                        if abs(curr_sl - e_sl) > 1e-6 or abs(curr_tp - e_tp) > 1e-6:
                            print(f"🛠️ [{self.symbol} {self.timeframe}] Updating SL/TP: {curr_sl}/{curr_tp} → {e_sl}/{e_tp}")
                            success = await self.trader.modify_sl_tp(
                                self.symbol,
                                timeframe=self.timeframe,
                                new_sl=e_sl,
                                new_tp=e_tp
                            )
                            if success:
                                # Update local variables for the rest of this loop iteration
                                # modify_sl_tp now returns the updated position dict
                                if isinstance(success, dict):
                                    existing_pos = success
                                    sl = existing_pos.get('sl')
                                    tp = existing_pos.get('tp')
                                else:
                                    # Fallback if it returns boolean
                                    existing_pos = self.trader.active_positions.get(pos_key, existing_pos)
                                    sl, tp = e_sl, e_tp

                # Calculate unrealized PnL for monitoring (with leverage)
                if side == 'BUY':
                    unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * 100 * leverage
                else:  # SELL
                    unrealized_pnl_pct = ((entry_price - current_price) / entry_price) * 100 * leverage
                
                pnl_color = "🟢" if unrealized_pnl_pct > 0 else "🔴"
                status_icon = "📍" if pos_status == 'filled' else "⏳"
                print(f"{status_icon} [{self.trader.exchange_name}] [{self.symbol}] {side} x{leverage} | Entry: {entry_price:.3f} → {current_price:.3f} | {pnl_color} {unrealized_pnl_pct:+.2f}% | SL: {sl:.3f} TP: {tp:.3f}")
                
                # 1. Check for Exit Conditions (SL/TP)
                exit_hit = False
                exit_reason = ""
                
                # Ensure current_price is valid
                if current_price is None:
                    return

                if side == 'BUY':
                    if sl and current_price <= sl:
                        exit_hit = True
                        exit_reason = f"STOP LOSS hit at {current_price}"
                    elif tp and current_price >= tp:
                        exit_hit = True
                        exit_reason = f"TAKE PROFIT hit at {current_price}"
                elif side == 'SELL':
                    if sl and current_price >= sl:
                        exit_hit = True
                        exit_reason = f"STOP LOSS hit at {current_price}"
                    elif tp and current_price <= tp:
                        exit_hit = True
                        exit_reason = f"TAKE PROFIT hit at {current_price}"

                if exit_hit:
                    if self.trader.dry_run:
                        # Calculate PnL (with leverage)
                        entry_price = existing_pos.get('entry_price', current_price)
                        qty = existing_pos.get('qty', 0)
                        leverage = existing_pos.get('leverage', 3)
                        
                        if side == 'BUY':
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100 * leverage
                        else:
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100 * leverage
                        
                        # USD P&L based on actual notional change (not multiplied by leverage again)
                        notional = qty * entry_price
                        pnl_usd = (pnl_pct / 100) * notional / leverage
                        
                        # Determine exit reason for notification
                        exit_reason_label = "TP" if "TAKE PROFIT" in exit_reason else "SL" if "STOP LOSS" in exit_reason else exit_reason
                        
                        # Unified notification
                        terminal_msg, telegram_msg = format_position_closed(
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            side=side,
                            entry_price=entry_price,
                            exit_price=current_price,
                            pnl=pnl_usd,
                            pnl_pct=pnl_pct,
                            reason=exit_reason_label,
                            dry_run=self.trader.dry_run,
                            exchange_name=self.trader.exchange_name
                        )
                        print(terminal_msg)
                        await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                        
                        # Set cooldown after STOP LOSS to prevent immediate re-entry
                        if "STOP LOSS" in exit_reason:
                            self.trader.set_sl_cooldown(self.symbol)
                        
                        # ADAPTIVE LEARNING: Record trade outcome
                        signals_used = existing_pos.get('signals_used', [])
                        snapshot = existing_pos.get('snapshot')
                        result = 'WIN' if 'TAKE PROFIT' in exit_reason else 'LOSS'
                        
                        # Get BTC 1h change for market condition check
                        btc_change = None
                        try:
                            btc_df = self.data_manager.get_data('BTC/USDT', '1h', exchange=self.trader.exchange_name)
                            if btc_df is not None and len(btc_df) >= 2:
                                btc_change = (btc_df.iloc[-1]['close'] - btc_df.iloc[-2]['close']) / btc_df.iloc[-2]['close']
                        except Exception:
                            pass
                        
                        signal_tracker.record_trade(
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            side=side,
                            signals_used=signals_used,
                            result=result,
                            pnl_pct=pnl_pct,
                            btc_change=btc_change,
                            snapshot=snapshot
                        )
                        
                        await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason=exit_reason)
                        return
                    else:
                        # LIVE MODE: Rely on exchange state!
                        # The exchange handles SL/TP via real orders. Wait for reconcile_positions to detect the closure.
                        print(f"⏳ [{self.trader.exchange_name}] [{self.symbol}] Price crossed SL/TP internally. Waiting for exchange sync to confirm closure.")
                        # Return to prevent further logic (like trailing SL updates) from running on a potentially closed position.
                        return 

                # 2.5 Check for Profit Lock-in & Dynamic TP Extension (v3.0)
                # Proactively adjust SL/TP if profit threshold reached
                res_val = last_row.get('resistance_level')
                sup_val = last_row.get('support_level')
                atr_val = last_row.get('ATR_14')
                
                await self.trader.adjust_sl_tp_for_profit_lock(
                    pos_key, 
                    current_price, 
                    resistance=res_val, 
                    support=sup_val, 
                    atr=atr_val
                )

                # 3. Check for Signal Reversal (Early Exit)
                # Use cached features
                df_rev = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df_rev is not None and not df_rev.empty:
                    last_row_rev = df_rev.iloc[-1]
                    rev_signal_data = self.strategy.get_signal(last_row_rev)
                    rev_side = rev_signal_data['side']
                    rev_score = rev_signal_data['confidence'] * 10
                    
                    # Exit threshold from config (default 2.5)
                    # We use a slightly lower threshold for EXITING than ENTERING
                    exit_thresh = self.strategy.config_data.get('thresholds', {}).get('exit_score', 2.5)
                    
                    opp_side = 'SELL' if side == 'BUY' else 'BUY'
                    if rev_side == opp_side and rev_score >= exit_thresh:
                        print(f"🔄 [{self.symbol} {self.timeframe}] SIGNAL REVERSED to {rev_side} (Score {rev_score:.1f}). Force closing...")
                        success = await self.trader.force_close_position(pos_key, reason=f"Signal Flip to {rev_side}")
                        if success:
                            # Approximate PnL for notification
                            qty = existing_pos.get('qty', 0)
                            pnl_usd = (unrealized_pnl_pct / 100) * (qty * entry_price) / leverage
                            
                            terminal_msg, telegram_msg = format_position_closed(
                                symbol=self.symbol,
                                timeframe=self.timeframe,
                                side=side,
                                entry_price=entry_price,
                                exit_price=current_price,
                                pnl=pnl_usd,
                                pnl_pct=unrealized_pnl_pct,
                                reason=f"Signal Flip ({rev_side})",
                                dry_run=self.trader.dry_run,
                                exchange_name=self.trader.exchange_name
                            )
                            print(terminal_msg)
                            await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange_name)
                            return

                # If position is still open and no exit hit, skip entry analysis
                return

            # GLOBAL GUARD: Protected by per-symbol lock to prevent race conditions
            async with self.trader._get_lock(self.symbol):
                already_in_symbol = await self.trader.has_any_symbol_position(self.symbol)
                if already_in_symbol:
                    # Silently skip entry to avoid overlapping positions on same coin
                    return

                # COOLDOWN CHECK: Prevent re-entry after recent SL
                if self.trader.is_in_cooldown(self.symbol):
                    remaining = self.trader.get_cooldown_remaining(self.symbol)
                    # Only print occasionally to avoid spam
                    if int(remaining) % 30 == 0:  # Print every 30 minutes
                        print(f"⏸️ [{self.trader.exchange_name}] [{self.symbol}] In cooldown after SL ({remaining:.0f} min remaining)")
                    return

                # ADAPTIVE LEARNING: Check if symbol has poor recent performance
                skip, reason = signal_tracker.should_skip_symbol(self.symbol, min_wr=0.3, min_trades=3)
                if skip:
                    import random
                    if random.random() < 0.1:  # Print 10% of time to avoid spam
                        print(f"📉 [{self.trader.exchange_name}] [{self.symbol}] Skipping due to recent losses: {reason}")
                    return

                # Use cached features from data_manager (computed once per cycle, shared across bots)
                df = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df is None or df.empty:
                    return
                last_row = df.iloc[-1]
                # current_price already defined at top

                signal_data = self.strategy.get_signal(last_row)
                side = signal_data['side']
                conf = signal_data['confidence']

                if side in ['BUY', 'SELL']:
                    # === OPTION 3: Check if config is enabled before opening NEW positions ===
                    # Existing positions can still close, but NEW entries blocked if disabled
                    if hasattr(self.strategy, 'is_enabled') and not self.strategy.is_enabled():
                        self.logger.warning(f"[{self.symbol} {self.timeframe}] Config DISABLED - Skipping new entry")
                        print(f"⛔ [{self.symbol} {self.timeframe}] Config disabled, blocking new position (existing can close)")
                        return
                    
                    # Check technical confirmation (Fibonacci + S/R)
                    technical_confirm = False
                    confirm_signals = []
                    
                    if hasattr(last_row, 'signal_at_fibo_key_level') and last_row.get('signal_at_fibo_key_level', False):
                        technical_confirm = True
                        confirm_signals.append("Fibo")
                    
                    if hasattr(last_row, 'signal_bounce_from_support') and last_row.get('signal_bounce_from_support', False):
                        technical_confirm = True
                        confirm_signals.append("Support")
                    
                    if hasattr(last_row, 'signal_bounce_from_resistance') and last_row.get('signal_bounce_from_resistance', False):
                        technical_confirm = True
                        confirm_signals.append("Resistance")
                    
                    if hasattr(last_row, 'signal_price_at_support') and last_row.get('signal_price_at_support', False):
                        technical_confirm = True
                        confirm_signals.append("At Support")
                    
                    if hasattr(last_row, 'signal_price_at_resistance') and last_row.get('signal_price_at_resistance', False):
                        technical_confirm = True
                        confirm_signals.append("At Resistance")
                    
                    # If technical confirmation required but not found, skip entry
                    from config import REQUIRE_TECHNICAL_CONFIRMATION
                    if REQUIRE_TECHNICAL_CONFIRMATION and not technical_confirm:
                        self.logger.info(f"[{self.symbol} {self.timeframe}] Signal {side} found but no technical confirmation - SKIP")
                        print(f"⚠️ [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] Signal {side} but no Fibo/S/R confirmation - SKIP")
                        return
                    
                    self.logger.info(f"[{self.symbol} {self.timeframe}] Signal: {side} ({conf})")
                    
                    # === CRITICAL PRE-TRADE CHECK ===
                    # Verify with exchange before calculating or placing order
                    # This prevents 'Dual Orders' if local state is stale
                    if not self.trader.dry_run:
                        state = await self.trader.verify_symbol_state(self.symbol)
                        if state and (state['active_exists'] or state['order_exists']):
                            msg = "position" if state['active_exists'] else "open order"
                            print(f"🛑 [{self.trader.exchange_name}] [{self.symbol}] STOP: Found existing {msg} on exchange! skipping new order.")
                            self.logger.warning(f"[{self.symbol}] Pre-trade check failed ({msg} exists): {state}")
                            # Trigger sync to adopt this state
                            await self.trader.reconcile_positions()
                            return

                    # === SAFE REVERSAL ENTRY ===
                    # If this signal flips the previous trade direction for this symbol, take it cautiously.
                    last_trade_side = signal_tracker.get_last_trade_side(self.symbol)
                    is_reversal = last_trade_side and last_trade_side != side
                    
                    # Get exchange min cost early to support floor-aware reduction
                    try:
                        market = self.trader.exchange.market(self.symbol)
                        exchange_min_notional = float(market.get('limits', {}).get('cost', {}).get('min', 5.0) or 5.0)
                    except:
                        exchange_min_notional = 5.0

                    # Dynamic Tier Sizing - Get leverage early for display
                    score = conf * 10
                    tier = self.get_tier_config(score)
                    target_lev = tier.get('leverage', LEVERAGE)
                    
                    if is_reversal:
                        new_lev = max(3, int(target_lev * 0.6))
                        print(f"⚠️ [{self.symbol} {self.timeframe}] REVERSAL DETECTED ({last_trade_side} -> {side}). Reduction: x{target_lev} -> x{new_lev}")
                        target_lev = new_lev
                    
                    tech_info = f" ({', '.join(confirm_signals)})" if confirm_signals else ""
                    print(f"🎯 [{self.symbol} {self.timeframe}] SIGNAL FOUND: {side} x{target_lev}{tech_info} | Conf: {conf:.2f} | Price: {current_price:.3f}")
                    
                    # Use dynamic SL/TP from strategy
                    # UPDATED: Tighter SL for reversals to be 'Safe' as requested
                    current_sl_pct = self.strategy.sl_pct
                    if is_reversal:
                        current_sl_pct *= 0.6 # 40% tighter stop
                        
                    sl, tp = self.risk_manager.calculate_sl_tp(
                        current_price, side, 
                        sl_pct=current_sl_pct, 
                        tp_pct=self.strategy.tp_pct
                    )
                    
                    # Dynamic Tier Sizing (target_lev already adjusted above if reversal)
                    target_cost = tier.get('cost_usdt', None)
                    target_risk = tier.get('risk_pct', None)
                    
                    qty = 0
                    if target_cost is not None:
                        # Fixed Margin Mode (User Preferred)
                        actual_cost = target_cost
                        if is_reversal:
                            # Safely reduce cost while respecting exchange floor
                            unreduced_notional = target_cost * target_lev
                            reduced_cost = target_cost * 0.5
                            reduced_notional = reduced_cost * target_lev
                            
                            if reduced_notional < exchange_min_notional and unreduced_notional >= exchange_min_notional:
                                # Compensate to exactly meet the floor + small buffer
                                actual_cost = (exchange_min_notional * 1.05) / target_lev
                                print(f"⛽ [{self.trader.exchange_name}] [{self.symbol}] Floor Protection: Adjusting reversal cost to ${actual_cost:.2f} to meet ${exchange_min_notional} min.")
                            else:
                                actual_cost = reduced_cost # Normal 50% reduction
                        
                        # Ensure we don't exceed available balance if it's very low
                        # but NEVER exceed the user's hard cap of $5
                        if actual_cost > current_equity:
                             safe_cost = max(0, current_equity * 0.98) # Reduced buffer to 2% for fees (was 5%)
                             if self.balance_tracker:
                                 b = self.balance_tracker.balances.get(self.trader.exchange_name, {})
                                 total = b.get('total', 0)
                                 reserved = b.get('reserved', 0)
                                 print(f"⚠️ [{self.trader.exchange_name}] [{self.symbol}] Scaling down Margin: ${actual_cost:.2f} -> ${safe_cost:.2f} (Total Equity: ${total:.2f} | Reserved: ${reserved:.2f} | Available: ${current_equity:.2f})")
                             else:
                                 print(f"⚠️ [{self.trader.exchange_name}] [{self.symbol}] Scaling down Margin: ${actual_cost:.2f} -> ${safe_cost:.2f} (Available: ${current_equity:.2f})")
                             actual_cost = safe_cost
                        
                        # Minimum Notional Check: If after scale-down it's too small, skip
                        if actual_cost < 1.0:
                             print(f"⏹️ [{self.trader.exchange_name}] [{self.symbol}] Scale-down resulted in margin too low (${actual_cost:.2f}). Skipping trade.")
                             return

                        qty = self.risk_manager.calculate_size_by_cost(current_price, actual_cost, target_lev)
                        risk_info = f"${actual_cost:.1f} Margin" if is_reversal else f"${target_cost} Margin"
                    else:
                        # Fallback to Risk %
                        use_risk = target_risk if target_risk else 0.01
                        if is_reversal: use_risk *= 0.5
                        
                        qty = self.risk_manager.calculate_position_size(
                            current_equity, current_price, sl, 
                            leverage=target_lev, risk_pct=use_risk
                        )
                        risk_info = f"{use_risk*100}% Risk"
                    
                    # === MINIMUM NOTIONAL FLOOR ===
                    strict_min_notional = exchange_min_notional
                    exchange_min_cost = exchange_min_notional

                    actual_notional = qty * current_price
                    
                    # Allow a tiny floating point tolerance (e.g. 9.99 vs 10.0)
                    if qty > 0 and actual_notional < (strict_min_notional * 0.99):
                        msg = f"[{self.trader.exchange_name}] [{self.symbol}] Calculated Size ${actual_notional:.2f} < Strict Min ${strict_min_notional:.2f} (Exch: ${exchange_min_cost}). Skipping (Strict Rule)."
                        self.logger.warning(msg)
                        print(f"⏹️ {msg}")
                        qty = 0  # STRICT SKIP

                    
                    if qty > 0:
                        # Calculate estimated margin cost for reservation
                        estimated_margin_cost = (qty * current_price) / target_lev
                        print(f"💰 [{self.trader.exchange_name}] [{self.symbol}] Trade Plan: Margin ${estimated_margin_cost:.2f} | Notional ${actual_notional:.2f} | Lev x{target_lev}")
                        
                        exec_side = side.lower()
                        
                        # === DUPLICATE PREVENTION ===
                        # Check if we already have a pending order for this symbol/timeframe
                        # This prevents placing multiple orders for the same signal
                        # Determine namespaced key using unified helper
                        pos_key_check = self.trader._get_pos_key(self.symbol, self.timeframe)
                        
                        # Check in pending_orders (live mode)
                        if pos_key_check in self.trader.pending_orders:
                            existing_pending = self.trader.pending_orders[pos_key_check]
                            self.logger.warning(f"[DUPLICATE SKIP] {pos_key_check} already has pending order (ID: {existing_pending.get('order_id', 'N/A')})")
                            print(f"⚠️ [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] Already have pending order - SKIP to prevent duplicate")
                            return
                        
                        # Check in active_positions (both live and dry_run)
                        if pos_key_check in self.trader.active_positions:
                            existing_status = self.trader.active_positions[pos_key_check].get('status', 'unknown')
                            if existing_status == 'pending':
                                existing_order_id = self.trader.active_positions[pos_key_check].get('order_id', 'N/A')
                                self.logger.warning(f"[DUPLICATE SKIP] {pos_key_check} already has pending position (ID: {existing_order_id})")
                                print(f"⚠️ [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] Already have pending position - SKIP to prevent duplicate")
                                return
                        
                        # Determine order type and price
                        from config import USE_LIMIT_ORDERS, PATIENCE_ENTRY_PCT, LIMIT_ORDER_TIMEOUT
                        order_type = 'market'
                        entry_price = current_price
                        
                        if USE_LIMIT_ORDERS:
                            # Use limit order with patience for better entry
                            order_type = 'limit'
                            if side == 'BUY':
                                # Buy at lower price (more patience)
                                entry_price = current_price * (1 - PATIENCE_ENTRY_PCT)
                            else:
                                # Sell at higher price (more patience)
                                entry_price = current_price * (1 + PATIENCE_ENTRY_PCT)
                            
                            # Recalculate SL/TP based on LIMIT entry price (not current price)
                            sl, tp = self.risk_manager.calculate_sl_tp(
                                entry_price, side, 
                                sl_pct=self.strategy.sl_pct, 
                                tp_pct=self.strategy.tp_pct
                            )
                            
                            tech_label = " (Fibo/SR)" if technical_confirm else ""
                            print(f"📋 [{self.trader.exchange_name}] [{self.symbol}] Using LIMIT order: {entry_price:.3f} (patience: {PATIENCE_ENTRY_PCT*100:.1f}% from {current_price:.3f}){tech_label}")
                        
                        # Extract signals from comment for adaptive learning
                        # Comment format: "Long Score 6.5 (RSI_oversold,EMA9_EMA21_cross_up,...)"
                        signals_used = []
                        comment = signal_data.get('comment', '')
                        if '(' in comment and ')' in comment:
                            try:
                                signals_str = comment.split('(')[1].split(')')[0]
                                signals_used = [s.strip() for s in signals_str.split(',') if s.strip()]
                            except:
                                pass
                        
                        # Extract snapshot (features) for training
                        snapshot = signal_data.get('snapshot')
                        
                        # === BALANCE RESERVATION ===
                        # Prevent race conditions by locking estimated funds
                        if self.trader.exchange.is_authenticated or self.trader.dry_run:
                            if self.balance_tracker:
                                if not self.balance_tracker.reserve(self.trader.exchange_name, estimated_margin_cost):
                                    print(f"⚠️ [{self.symbol}] Reservation failed. Insufficient shared funds for ${estimated_margin_cost:.2f}. Skipping.")
                                    return

                        # === PUBLIC MODE GUARD (Simulation Fallback) ===
                        is_public_sim = not self.trader.dry_run and not self.trader.exchange.is_authenticated
                        if is_public_sim:
                            print(f"📢 [{self.trader.exchange_name}] [{self.symbol} {self.timeframe}] SIGNAL FOUND (Public Mode): {side} @ {entry_price:.3f} | Simulation Active.")
                            # We let it fall through to place_order, which will handle the simulation
                            # But we should mark it as such for notifications
                            pass

                        try:
                            res = await self.trader.place_order(
                                self.symbol, exec_side, qty, 
                                timeframe=self.timeframe, 
                                order_type=order_type,
                                price=entry_price, 
                                sl=sl, tp=tp,
                                timeout=LIMIT_ORDER_TIMEOUT if order_type == 'limit' else None,
                                leverage=target_lev,
                                signals_used=signals_used,
                                entry_confidence=conf,  # For adaptive position adjustment
                                snapshot=snapshot       # Save features for RL training
                            )
                        except Exception as e:
                            # Re-raise to be caught by outer loop, but ensure finally runs
                            raise e
                        finally:
                            # ALWAYS release reservation.
                            # If order succeeded, exchange balance will decrease, and next update will be correct.
                            # If failed, we must unlock funds.
                            if self.balance_tracker:
                                self.balance_tracker.release(self.trader.exchange_name, estimated_margin_cost)

                        if res:
                            mode_label = "🟢 LIVE" if not self.trader.dry_run else "🧪 TEST"
                            # Status Mapping: Map CCXT variations to user-friendly labels
                            raw_status = (res.get('status') or '').lower()
                            if raw_status in ['open', 'pending']:
                                status = 'PENDING'
                            elif raw_status in ['closed', 'filled']:
                                status = 'FILLED'
                            elif order_type == 'limit':
                                status = 'PENDING' # Default for limit
                            else:
                                status = 'FILLED' # Default for market
                                
                            status_label = f"📌 {status}" if status == 'PENDING' else f"✅ {status}"
                            
                            # Escape symbol for Telegram (replace / with -)
                            safe_symbol = self.symbol.replace('/', '-')
                            msg = (
                                f"{mode_label} | {status_label}\n"
                                f"{safe_symbol} | {self.timeframe} | {side} x{target_lev}\n"
                                f"Entry: {entry_price:.3f}\n"
                                f"SL: {sl:.3f} | TP: {tp:.3f}\n"
                                f"PnL: 0.00%"
                            )
                            print(msg)
                            await send_telegram_message(msg, exchange_name=self.trader.exchange.name)
                        else:
                            print(f"❌ [{self.symbol}] Order placement failed (See warning logs).")
                        
                        # NOTE: SL/TP setup is already handled in execution.py:420
                        # No need to call setup_sl_tp_for_pending() here to avoid duplicates


        except Exception as e:
            import traceback
            traceback.print_exc()
            self.logger.error(f"Error in bot step {self.symbol}: {e}")

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
                pnl_pct = ((current_price - entry) / entry) * 100 if entry > 0 else 0
            else:
                pnl_pct = ((entry - current_price) / entry) * 100 if entry > 0 else 0
            
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

    # Override config DRY_RUN if argument provided
    global DRY_RUN
    import config
    if args.dry_run:
        config.DRY_RUN = True
        print("🚩 Command-line override: Simulation Mode (--dry-run)")
    elif args.live:
        config.DRY_RUN = False
        print("🚩 Command-line override: Live Mode (--live)")

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
    initial_balance = 1000

    def get_current_balance():
        current_bal = initial_balance
        # Fix 10 (Unified Store): Read from signal_performance.json instead of deprecated trade_history.json
        perf_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signal_performance.json')
        if os.path.exists(perf_file):
            try:
                with open(perf_file, 'r') as f:
                    data = json.load(f)
                trades = data.get('trades', [])
                current_bal += sum(float(t.get('pnl_usdt', 0) or 0) for t in trades)
            except: pass
            
        for ex_name, trader in traders.items():
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
                    current_bal = get_current_balance()
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

            # 3. Run Logic for all bots
            # SKIP unauthenticated exchanges to prevent pointless signal analysis/errors
            active_tasks = []
            for bot in bots:
                if not bot.running: continue
                
                ex_name = bot.trader.exchange_name
                
                # Check if this exchange tripped the circuit breaker
                bot_stop_trading, _ = risk_managers.get(ex_name).check_circuit_breaker(balance_tracker.get_available(ex_name)) if risk_managers.get(ex_name) and balance_tracker.get_available(ex_name) > 0 else (False, "")

                # Only run if it's paper trading OR authenticated live trading OR PUBLIC MODE (Task requirement)
                # Public mode allowed: we just skip ordering logic inside run_step
                active_tasks.append(bot.run_step(circuit_breaker_triggered=bot_stop_trading))
                
            if active_tasks: 
                await asyncio.gather(*active_tasks)
            
            
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
