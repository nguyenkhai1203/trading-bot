import asyncio
import logging
import numpy as np
import os
import sys
import time
import json

# Add src to path if running directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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
from notification import send_telegram_message
from notification_helper import format_order_cancelled, format_position_filled, format_position_closed
from signal_tracker import tracker as signal_tracker

# ...

class TradingBot:
    def __init__(self, symbol, timeframe, data_manager, trader):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_manager = data_manager 
        # Features are now computed and cached in data_manager (shared across all bots)
        self.strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe) 
        # Weights are loaded automatically in __init__ now
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
        self.trader = trader
        self.logger = logging.getLogger(__name__)
        self.running = True
        
    def get_tier_config(self, score):
        """Get sizing tier based on confidence score."""
        return self.strategy.get_sizing_tier(score)

    async def run_step(self, current_equity, circuit_breaker_triggered=False):
        # Circuit breaker is checked ONCE in main(), passed here as flag
        try:
            if circuit_breaker_triggered:
                self.running = False
                return

            df = self.data_manager.get_data(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
            if df is None or df.empty:
                return

            # Ensure current_price is available for all checks
            current_price = df.iloc[-1]['close']
            
            # PRICE VALIDATION: Ensure current_price is valid number to avoid NoneType comparison errors
            if current_price is None or (isinstance(current_price, (int, float)) and np.isnan(current_price)):
                self.logger.error(f"Invalid current_price ({current_price}) for {self.symbol}. Skipping cycle.")
                return
            
            current_price = float(current_price)

            # 0. GLOBAL CONFLICT CHECK (Single Position Rule)
            # Check if this symbol already has an active position/order on exchange or locally
            # Use namespaced key
            exchange_name = getattr(self.trader.exchange, 'name', 'BINANCE')
            pos_key = f"{exchange_name}_{self.symbol}_{self.timeframe}"
            already_in_symbol = await self.trader.has_any_symbol_position(self.symbol)
            if already_in_symbol:
                # If we have a position/order for THIS timeframe, we proceed to regular logic
                # If we DON'T have a record for this timeframe, but has_any_symbol_position is True, 
                # then it belongs to another timeframe. Block it.
                if pos_key not in self.trader.active_positions and pos_key not in self.trader.pending_orders:
                    return

            # Check if we already have a position for this symbol/timeframe
            pos_key = f"{exchange_name}_{self.symbol}_{self.timeframe}"
            
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
                        dry_run=self.trader.dry_run
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange.name)
                    return
                
                # For LIVE mode: exchange handles fill, just monitor and return
                if pending_from_live:
                    print(f"⏳ [{self.symbol} {self.timeframe}] Pending {pending_side} order @ {pending_order['price']:.3f} | Current: {current_price:.3f}")
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
                        
                        print(f"⏳ [{self.symbol} {self.timeframe}] PENDING {side} @ {limit_price:.3f} | Now: {current_price:.3f}")
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
                        dry_run=self.trader.dry_run
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange.name)
                
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
                        e_sl, e_tp = round(e_sl, 5), round(e_tp, 5)
                        curr_sl, curr_tp = round(float(sl or 0), 5), round(float(tp or 0), 5)
                        
                        if abs(curr_sl - e_sl) > 1e-6 or abs(curr_tp - e_tp) > 1e-6:
                            print(f"🛠️ [{self.symbol} {self.timeframe}] Updating SL/TP: {curr_sl}/{curr_tp} → {e_sl}/{e_tp}")
                            success = await self.trader.modify_sl_tp(
                                self.symbol,
                                timeframe=self.timeframe,
                                new_sl=e_sl,
                                new_tp=e_tp
                            )
                            if success:
                                # Update local cache immediately
                                existing_pos = self.trader.active_positions.get(pos_key, existing_pos)
                                existing_pos['sl'] = e_sl
                                existing_pos['tp'] = e_tp
                                self.trader.active_positions[pos_key] = existing_pos
                                sl, tp = e_sl, e_tp

                # Calculate unrealized PnL for monitoring (with leverage)
                if side == 'BUY':
                    unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * 100 * leverage
                else:  # SELL
                    unrealized_pnl_pct = ((entry_price - current_price) / entry_price) * 100 * leverage
                
                pnl_color = "🟢" if unrealized_pnl_pct > 0 else "🔴"
                status_icon = "📍" if pos_status == 'filled' else "⏳"
                print(f"{status_icon} [{self.symbol}] {side} x{leverage} | Entry: {entry_price:.3f} → {current_price:.3f} | {pnl_color} {unrealized_pnl_pct:+.2f}% | SL: {sl:.3f} TP: {tp:.3f}")
                
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
                        dry_run=self.trader.dry_run
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg, exchange_name=self.trader.exchange.name)
                    
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
                                dry_run=self.trader.dry_run
                            )
                            print(terminal_msg)
                            await send_telegram_message(telegram_msg)
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
                        print(f"⚠️ [{self.symbol} {self.timeframe}] Signal {side} but no Fibo/S/R confirmation - SKIP")
                        return
                    
                    self.logger.info(f"[{self.symbol} {self.timeframe}] Signal: {side} ({conf})")
                    
                    # === CRITICAL PRE-TRADE CHECK ===
                    # Verify with exchange before calculating or placing order
                    # This prevents 'Dual Orders' if local state is stale
                    if not self.trader.dry_run:
                        state = await self.trader.verify_symbol_state(self.symbol)
                        if state and (state['active_exists'] or state['order_exists']):
                            print(f"🛑 [{self.symbol}] STOP: Found existing position/order on exchange! skipping new order.")
                            self.logger.warning(f"[{self.symbol}] Pre-trade check failed: {state}")
                            # Trigger sync to adopt this state
                            await self.trader.reconcile_positions()
                            return

                    # === SAFE REVERSAL ENTRY ===
                    # If this signal flips the previous trade direction for this symbol, take it cautiously.
                    last_trade_side = signal_tracker.get_last_trade_side(self.symbol)
                    is_reversal = last_trade_side and last_trade_side != side
                    
                    # Dynamic Tier Sizing - Get leverage early for display
                    score = conf * 10
                    tier = self.get_tier_config(score)
                    target_lev = tier.get('leverage', LEVERAGE)
                    
                    if is_reversal:
                        print(f"⚠️ [{self.symbol} {self.timeframe}] REVERSAL DETECTED ({last_trade_side} -> {side}). Reduction: x{target_lev} -> x{max(3, int(target_lev*0.6))}")
                        target_lev = max(3, int(target_lev * 0.6)) # Reduce leverage for early reversal protection
                    
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
                            actual_cost *= 0.5 # 50% size for early reversal entries
                        
                        qty = self.risk_manager.calculate_size_by_cost(current_price, actual_cost, target_lev)
                        risk_info = f"${actual_cost:.1f} (REVERSAL SAFE)" if is_reversal else f"${target_cost}"
                    else:
                        # Fallback to Risk %
                        use_risk = target_risk if target_risk else 0.01
                        if is_reversal: use_risk *= 0.5
                        
                        qty = self.risk_manager.calculate_position_size(
                            current_equity, current_price, sl, 
                            leverage=target_lev, risk_pct=use_risk
                        )
                        risk_info = f"{use_risk*100}% (REVERSAL SAFE)" if is_reversal else f"{use_risk*100}%"
                    
                    if qty > 0:
                        exec_side = side.lower()
                        
                        # === DUPLICATE PREVENTION ===
                        # Check if we already have a pending order for this symbol/timeframe
                        # This prevents placing multiple orders for the same signal
                        exchange_name = getattr(self.trader.exchange, 'name', 'BINANCE')
                        pos_key_check = f"{exchange_name}_{self.symbol}_{self.timeframe}"
                        
                        # Check in pending_orders (live mode)
                        if pos_key_check in self.trader.pending_orders:
                            existing_pending = self.trader.pending_orders[pos_key_check]
                            self.logger.warning(f"[DUPLICATE SKIP] {pos_key_check} already has pending order (ID: {existing_pending.get('order_id', 'N/A')})")
                            print(f"⚠️ [{self.symbol} {self.timeframe}] Already have pending order - SKIP to prevent duplicate")
                            return
                        
                        # Check in active_positions (both live and dry_run)
                        if pos_key_check in self.trader.active_positions:
                            existing_status = self.trader.active_positions[pos_key_check].get('status', 'unknown')
                            if existing_status == 'pending':
                                existing_order_id = self.trader.active_positions[pos_key_check].get('order_id', 'N/A')
                                self.logger.warning(f"[DUPLICATE SKIP] {pos_key_check} already has pending position (ID: {existing_order_id})")
                                print(f"⚠️ [{self.symbol} {self.timeframe}] Already have pending position - SKIP to prevent duplicate")
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
                        
                        if res:
                            mode_label = "🟢 LIVE" if not self.trader.dry_run else "🧪 TEST"
                            # Status: PENDING for limit, FILLED for market
                            if order_type == 'limit':
                                status_label = "📌 PENDING"
                            else:
                                status_label = "✅ FILLED"
                            # Escape symbol for Telegram (replace / with -)
                            safe_symbol = self.symbol.replace('/', '-')
                            msg = (
                                f"{mode_label} | {self.trader.exchange_name} | {status_label}\n"
                                f"{safe_symbol} | {self.timeframe} | {side} x{target_lev}\n"
                                f"Entry: {entry_price:.3f}\n"
                                f"SL: {sl:.3f} | TP: {tp:.3f}\n"
                                f"PnL: 0.00%"
                            )
                            print(msg)
                            await send_telegram_message(msg, exchange_name=self.trader.exchange.name)
                        
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
    """Aggregates all active positions and sends a summary to Telegram."""
    positions = trader.active_positions
    if not positions:
        return

    msg = "📊 **Active Positions** 📊\n\n"
    total_pnl_usd = 0
    
    for key, pos in positions.items():
        symbol = pos.get('symbol', key.split('_')[0])
        tf = key.split('_')[-1] if '_' in key else '1h'
        side = pos.get('side', 'N/A').upper()
        entry = pos.get('entry_price', 0)
        qty = pos.get('qty', 0)
        sl = pos.get('sl', 0)
        tp = pos.get('tp', 0)
        
        # Get current price from data store
        df = data_manager.get_data(symbol, tf, exchange=trader.exchange_name)
        current_price = df.iloc[-1]['close'] if df is not None and not df.empty else entry
        
        # Calculate PnL
        if side == 'BUY':
            pnl_pct = ((current_price - entry) / entry) * 100 if entry > 0 else 0
        else:
            pnl_pct = ((entry - current_price) / entry) * 100 if entry > 0 else 0
        
        pnl_usd = (pnl_pct / 100) * qty * entry
        total_pnl_usd += pnl_usd
        pnl_icon = "🟢" if pnl_pct > 0 else "🔴"
        
        msg += (
            f"{pnl_icon} **{symbol}** ({side})\n"
            f"Entry: {entry:.4f} | Now: {current_price:.4f}\n"
            f"PnL: {pnl_pct:+.2f}% | ${pnl_usd:+.2f}\n"
            f"SL: {sl:.4f} | TP: {tp:.4f}\n"
            f"---\n"
        )
    
    total_icon = "🟢" if total_pnl_usd > 0 else "🔴"
    msg += f"\n{total_icon} **Total PnL: ${total_pnl_usd:+.2f}**"
    
    await send_telegram_chunked(msg, exchange_name=trader.exchange.name)

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

    # Initialize Global Manager
    manager = MarketDataManager()
    
    # Initialize Traders for each Active Exchange
    from exchange_factory import get_active_exchanges_map
    ex_adapters = get_active_exchanges_map()
    traders = {name: Trader(adapter, dry_run=DRY_RUN, data_manager=manager) for name, adapter in ex_adapters.items()}
    
    print(f"🚀 Initializing parallel bots for {len(traders)} exchanges: {list(traders.keys())}")
    
    # Sync server time for each exchange
    for name, trader in traders.items():
        print(f"⏰ Synchronizing with {name} server time...")
        try:
            await trader.exchange.sync_time()
        except Exception as e:
            print(f"⚠️ [{name}] Time sync failed: {e}")

    # Initialize Bot instances per exchange/symbol/timeframe
    bots = []
    for ex_name, trader in traders.items():
        # Setup margin modes and leverage for LIVE
        if not trader.dry_run:
            print(f"🔧 [{ex_name}] Setting up margin modes and leverage...")
            await manager.set_isolated_margin_mode(TRADING_SYMBOLS, exchange=ex_name)
            await trader.enforce_isolated_on_startup(TRADING_SYMBOLS)
            print(f"🔄 [{ex_name}] Synchronizing positions...")
            await trader.reconcile_positions()
            
        from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
        exchange_symbol_map = {
            'BINANCE': BINANCE_SYMBOLS,
            'BYBIT': BYBIT_SYMBOLS
        }
        active_symbols = exchange_symbol_map.get(ex_name, TRADING_SYMBOLS)

        for symbol in active_symbols:
            for tf in TRADING_TIMEFRAMES:
                bot = TradingBot(symbol, tf, manager, trader)
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
        print("🎯 [ADAPTIVE] Checking if any open positions need risk adjustment...")
        for ex_name, trader in traders.items():
            for pos_key, pos in trader.active_positions.items():
                if pos.get('status') != 'filled': continue
                
                symbol = pos.get('symbol')
                side = pos.get('side')
                entry_conf = pos.get('entry_confidence', 0.5)
                
                df = manager.get_data(symbol, '1h', exchange=ex_name)
                if df is None or df.empty: continue
                
                last_row = df.iloc[-1]
                # We need to find the matching bot to use its strategy (or just use a generic one)
                # For now, we'll look for any bot that matches symbol/timeframe
                matching_bot = next((b for b in bots if b.symbol == symbol and b.trader == trader), None)
                if not matching_bot: continue
                
                current_signal = matching_bot.strategy.get_signal(last_row)
                current_side = current_signal.get('side')
                current_conf = current_signal.get('confidence', 0)
                
                # Check for None values before comparison
                if not side or not current_side:
                    continue
                
                if (side == 'BUY' and current_side == 'SELL') or (side == 'SELL' and current_side == 'BUY'):
                    print(f"🔄 [{ex_name}] Signal reversed for {symbol}. Closing.")
                    await trader.force_close_position(pos_key, reason=f"Signal reversed: {side} → {current_side}")
                    continue
                
                if current_conf < entry_conf * 0.5:
                    print(f"📉 [{ex_name}] Confidence drop for {symbol}. Tightening SL.")
                    await trader.tighten_sl(pos_key, factor=0.5)

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

    # Shared RiskManager for circuit breaker
    risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
    initial_balance = 1000

    def get_current_balance():
        current_bal = initial_balance
        history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r') as f:
                    history = json.load(f)
                current_bal += sum(t.get('pnl_usdt', 0) for t in history)
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
                    try: await trader.reconcile_positions(auto_fix=True)
                    except: pass
                main.last_deep_sync = curr_time

            # 1. Update Market Data
            await manager.update_tickers(TRADING_SYMBOLS)
            await manager.update_data(TRADING_SYMBOLS, TRADING_TIMEFRAMES)
            
            # 1.5. Reload config
            current_config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
            if current_config_mtime != last_config_mtime:
                print("🔄 Config changed, reloading...")
                for bot in bots: bot.strategy.reload_config()
                last_config_mtime = current_config_mtime
            
            # 2. Check Circuit Breaker
            balances = {}
            for ex_name, trader in traders.items():
                balances[ex_name] = get_current_balance() # Simplified: uses same total bal
                stop_trading, cb_reason = risk_manager.check_circuit_breaker(balances[ex_name])
                if stop_trading:
                    print(f"🚨 [{ex_name}] CIRCUIT BREAKER: {cb_reason}")
                    await send_telegram_message(f"🚨 [{ex_name}] CIRCUIT BREAKER: {cb_reason}", exchange_name=ex_name)

            # 3. Run Logic for all bots
            tasks = [bot.run_step(balances[bot.trader.exchange.name]) for bot in bots if bot.running]
            if tasks: await asyncio.gather(*tasks)
            
            # 4. Fast Deep Sync
            for ex_name, trader in traders.items():
                try: await trader.reconcile_positions()
                except: pass
            
            from config import HEARTBEAT_INTERVAL
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            
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
