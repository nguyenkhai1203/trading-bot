import asyncio
import logging
import os
import sys

# Add src to path if running directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    TRADING_SYMBOLS, 
    TRADING_TIMEFRAMES, 
    LEVERAGE,
    RISK_PER_TRADE,
    STOP_LOSS_PCT, 
    TAKE_PROFIT_PCT,
    USE_LIMIT_ORDERS,
    PATIENCE_ENTRY_PCT,
    DRY_RUN
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

            df = self.data_manager.get_data(self.symbol, self.timeframe)
            if df is None or df.empty:
                return

            # Ensure current_price is available for all checks
            current_price = df.iloc[-1]['close']

            # Check if we already have a position for this symbol/timeframe
            pos_key = f"{self.symbol}_{self.timeframe}"
            
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
                df_check = self.data_manager.get_data_with_features(self.symbol, self.timeframe)
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
                    await send_telegram_message(telegram_msg)
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
                        dry_run=self.trader.dry_run
                    )
                    print(terminal_msg)
                    await send_telegram_message(telegram_msg)
                
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
                    # Calculate expected SL/TP
                    expected_sl, expected_tp = self.risk_manager.calculate_sl_tp(
                        entry_price, side,
                        sl_pct=self.strategy.sl_pct,
                        tp_pct=self.strategy.tp_pct
                    )
                    # If SL/TP missing or different, update
                    if (not sl or abs(sl - expected_sl) > 1e-6) or (not tp or abs(tp - expected_tp) > 1e-6):
                        print(f"🛠️ [{self.symbol} {self.timeframe}] Updating SL/TP: SL {sl}→{expected_sl}, TP {tp}→{expected_tp}")
                        await self.trader.modify_sl_tp(
                            self.symbol,
                            timeframe=self.timeframe,
                            new_sl=expected_sl,
                            new_tp=expected_tp
                        )
                        # Update local position
                        existing_pos['sl'] = expected_sl
                        existing_pos['tp'] = expected_tp
                        self.trader.active_positions[pos_key] = existing_pos

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
                    await send_telegram_message(telegram_msg)
                    
                    # Set cooldown after STOP LOSS to prevent immediate re-entry
                    if "STOP LOSS" in exit_reason:
                        self.trader.set_sl_cooldown(self.symbol)
                    
                    # ADAPTIVE LEARNING: Record trade outcome
                    signals_used = existing_pos.get('signals_used', [])
                    result = 'WIN' if 'TAKE PROFIT' in exit_reason else 'LOSS'
                    
                    # Get BTC 1h change for market condition check
                    btc_change = None
                    try:
                        btc_df = self.data_manager.get_data('BTC/USDT', '1h')
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
                        btc_change=btc_change
                    )
                    
                    await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason=exit_reason)
                    return 

                # 2. In Live mode, sync with exchange
                if not self.trader.dry_run:
                    # Double check if still open on exchange 
                    still_open = await self.trader.get_open_position(self.symbol, timeframe=self.timeframe)
                    if not still_open:
                        await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason="EXTERNAL_EXIT")
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
                        print(f"⏸️ [{self.symbol}] In cooldown after SL ({remaining:.0f} min remaining)")
                    return

                # ADAPTIVE LEARNING: Check if symbol has poor recent performance
                skip, reason = signal_tracker.should_skip_symbol(self.symbol, min_wr=0.3, min_trades=3)
                if skip:
                    import random
                    if random.random() < 0.1:  # Print 10% of time to avoid spam
                        print(f"📉 [{self.symbol}] Skipping due to recent losses: {reason}")
                    return

                # Use cached features from data_manager (computed once per cycle, shared across bots)
                df = self.data_manager.get_data_with_features(self.symbol, self.timeframe)
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
                    
                    # Dynamic Tier Sizing - Get leverage early for display
                    score = conf * 10
                    tier = self.get_tier_config(score)
                    target_lev = tier.get('leverage', LEVERAGE)
                    
                    tech_info = f" ({', '.join(confirm_signals)})" if confirm_signals else ""
                    print(f"🎯 [{self.symbol} {self.timeframe}] SIGNAL FOUND: {side} x{target_lev}{tech_info} | Conf: {conf:.2f} | Price: {current_price:.3f}")
                    
                    # Use dynamic SL/TP from strategy (optimized by analyzer)
                    sl, tp = self.risk_manager.calculate_sl_tp(
                        current_price, side, 
                        sl_pct=self.strategy.sl_pct, 
                        tp_pct=self.strategy.tp_pct
                    )
                    
                    # Dynamic Tier Sizing (target_lev already calculated above)
                    # Score and tier already calculated
                    # Check for cost_usdt (fixed margin) or risk_pct (account %)
                    target_cost = tier.get('cost_usdt', None)
                    target_risk = tier.get('risk_pct', None)
                    
                    qty = 0
                    if target_cost is not None:
                        # Fixed Margin Mode (User Preferred)
                        qty = self.risk_manager.calculate_size_by_cost(current_price, target_cost, target_lev)
                        risk_info = f"${target_cost}"
                    else:
                        # Fallback to Risk %
                        use_risk = target_risk if target_risk else 0.01
                        qty = self.risk_manager.calculate_position_size(
                            current_equity, current_price, sl, 
                            leverage=target_lev, risk_pct=use_risk
                        )
                        risk_info = f"{use_risk*100}%"
                    
                    if qty > 0:
                        exec_side = side.lower()
                        
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
                            print(f"📋 Using LIMIT order: {entry_price:.3f} (patience: {PATIENCE_ENTRY_PCT*100:.1f}% from {current_price:.3f}){tech_label}")
                        
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
                        
                        res = await self.trader.place_order(
                            self.symbol, exec_side, qty, 
                            timeframe=self.timeframe, 
                            order_type=order_type,
                            price=entry_price, 
                            sl=sl, tp=tp,
                            timeout=LIMIT_ORDER_TIMEOUT if order_type == 'limit' else None,
                            leverage=target_lev,
                            signals_used=signals_used,
                            entry_confidence=conf  # For adaptive position adjustment
                        )
                        
                        if res:
                            mode_label = "✅ REAL" if not self.trader.dry_run else "🧪 TEST"
                            # Status: PENDING for limit, FILLED for market
                            if order_type == 'limit':
                                status_label = "📌 PENDING"
                            else:
                                status_label = "✅ FILLED"
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
                            await send_telegram_message(msg)
                        
                        # NOTE: SL/TP setup is already handled in execution.py:420
                        # No need to call setup_sl_tp_for_pending() here to avoid duplicates


        except Exception as e:
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
        df = data_manager.get_data(symbol, tf)
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
    
    await send_telegram_chunked(msg)

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

    manager = MarketDataManager()
    
    # Sync server time to fix timestamp offset issues
    print("⏰ Synchronizing with Binance server time...")
    await manager.sync_server_time()
    
    bots = []
    
    print("Initializing Bots...")
    # 0. Shared Trader (One instance for all bots to sync positions)
    trader = Trader(manager.exchange, dry_run=DRY_RUN) 

    # 0.5 Set Isolated Margin Mode (one-time setup)
    if not trader.dry_run:
        print("🔧 [LIVE] Setting up margin modes and leverage...")
        await manager.set_isolated_margin_mode(TRADING_SYMBOLS)
        # Also ensure trader enforces isolated/leverage for configured symbols
        await trader.enforce_isolated_on_startup(TRADING_SYMBOLS)
    else:
        print("🧪 [SIMULATION] Dry Run Mode active. Private API calls skipped.")

    # Initialize one bot per pair/tf
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            bot = TradingBot(symbol, tf, manager, trader)
            bots.append(bot)
            
    # Initial Notif
    await send_telegram_message(f"🤖 Bot Started! Monitoring {len(TRADING_SYMBOLS)} symbols.")
    
    # ========== ADAPTIVE LEARNING v2.0 SETUP ==========
    # Callback for mini-analyzer after consecutive losses
    def on_losses_detected(symbols_to_check):
        """Called by signal_tracker when loss threshold reached."""
        from analyzer import StrategyAnalyzer
        analyzer = StrategyAnalyzer()
        updates = analyzer.run_mini_optimization(symbols_to_check)
        
        if updates:
            # Reload config for affected bots
            for bot in bots:
                key = f"{bot.symbol}_{bot.timeframe}"
                if key in updates:
                    bot.strategy.reload_config()
                    print(f"🔄 [{bot.symbol} {bot.timeframe}] Config reloaded after mini-optimization")
    
    # Callback for position adjustment
    async def on_adjust_positions():
        """Called by signal_tracker to check and adjust open positions."""
        positions = trader.get_all_filled_positions()
        if not positions:
            return
        
        print(f"⚙️ [ADJUST] Checking {len(positions)} open positions...")
        
        for pos_key, pos in positions.items():
            symbol = pos.get('symbol')
            tf = pos.get('timeframe')
            side = pos.get('side')
            entry_conf = pos.get('entry_confidence', 0.7)
            
            # Find the bot for this position
            matching_bot = None
            for bot in bots:
                if bot.symbol == symbol and bot.timeframe == tf:
                    matching_bot = bot
                    break
            
            if not matching_bot:
                continue
            
            # Re-evaluate current signal
            df = manager.get_data_with_features(symbol, tf)
            if df is None or df.empty:
                continue
            
            last_row = df.iloc[-1]
            current_signal = matching_bot.strategy.get_signal(last_row)
            current_side = current_signal.get('side')
            current_conf = current_signal.get('confidence', 0)
            
            # Check for signal reversal
            if (side == 'BUY' and current_side == 'SELL') or (side == 'SELL' and current_side == 'BUY'):
                # Signal reversed! Force close
                await trader.force_close_position(pos_key, reason=f"Signal reversed: {side} → {current_side}")
                continue
            
            # Check for confidence drop
            if current_conf < entry_conf * 0.5:  # Confidence dropped below 50% of entry
                # Tighten SL by 50%
                trader.tighten_sl(pos_key, factor=0.5)
    
    # Wrap async callback for sync caller
    def sync_adjust_positions():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(on_adjust_positions())
            else:
                loop.run_until_complete(on_adjust_positions())
        except Exception as e:
            print(f"Error in position adjustment: {e}")
    
    # Register callbacks
    signal_tracker.set_analysis_callback(on_losses_detected)
    signal_tracker.set_position_adjust_callback(sync_adjust_positions)
    print("✅ Adaptive Learning v2.0 callbacks registered")
    # ========== END ADAPTIVE LEARNING SETUP ==========
    
    # Track optimization time (set to 0 to trigger first run if needed)
    last_auto_opt = time.time()
    opt_interval = 12 * 3600 # 12 hours

    # Track periodic status update
    last_status_update = time.time()
    status_interval = 2 * 3600 # 2 hours
    
    # Track config file modification time (single check instead of 125)
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_config.json')
    last_config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
            
    print("Starting Loop...")
    try:
        initial_balance = 1000  # Starting capital
        
        # Shared RiskManager for circuit breaker (checked ONCE per cycle)
        risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
        
        # Calculate cumulative PnL from trade history and unrealized PnL from active positions
        def get_current_balance():
            current_bal = initial_balance
            
            # 1. Add Closed PnL
            history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
            if os.path.exists(history_file):
                try:
                    with open(history_file, 'r') as f:
                        history = json.load(f)
                    current_bal += sum(t.get('pnl_usdt', 0) for t in history)
                except Exception:
                    pass
            
            # 2. Add Unrealized PnL from active positions
            for pos_key, pos in trader.active_positions.items():
                if pos.get('status') == 'filled':
                    symbol = pos.get('symbol')
                    # Get latest price from manager data store
                    # Timeframe is stored in pos, but we can check all to be safe
                    tf = pos.get('timeframe', '1h')
                    df = manager.get_data(symbol, tf)
                    if df is not None and not df.empty:
                        cur_price = df.iloc[-1]['close']
                        entry = pos.get('entry_price')
                        qty = pos.get('qty', 0)
                        side = pos.get('side')
                        
                        if side == 'BUY':
                            unrealized = (cur_price - entry) * qty
                        else:  # SELL
                            unrealized = (entry - cur_price) * qty
                        current_bal += unrealized
                        
            return current_bal
        
        # Track time sync (every 1 hour = 3600 seconds)
        last_time_sync = 0
        time_sync_interval = 3600  # 1 hour
        
        while True:
            curr_time = time.time()
            
            # 0. Periodic Time Sync (every 1 hour)
            if curr_time - last_time_sync >= time_sync_interval:
                print("⏰ Periodic time sync with Binance server...")
                try:
                    await manager.sync_server_time()
                    last_time_sync = curr_time
                except Exception as sync_err:
                    print(f"Error during time sync: {sync_err}")
            
            # 0. Check for Auto-Optimization (Twice a day)
            if curr_time - last_auto_opt >= opt_interval:
                print("⏰ Scheduled Auto-Optimization triggered...")
                try:
                    await run_global_optimization()
                    last_auto_opt = curr_time
                    # Reload config for all bots
                    for bot in bots:
                        bot.strategy.reload_config()
                    await send_telegram_message("🔄 Auto-Optimization Complete and Bot Configs Reloaded.")
                except Exception as opt_err:
                    print(f"Error during auto-optimization: {opt_err}")

            # 0.1 Check for Periodic Status Update (Every 2 hours)
            if curr_time - last_status_update >= status_interval:
                print("📊 Sending periodic status update...")
                try:
                    await send_periodic_status_report(trader, manager)
                    last_status_update = curr_time
                except Exception as status_err:
                    print(f"Error sending status update: {status_err}")

            # 1. Centralized Data Fetch (ALWAYS fetch real-time data)
            print(f"🔄 Heartbeat: Updating data for {len(TRADING_SYMBOLS)} symbols...")
            await manager.update_data(TRADING_SYMBOLS, TRADING_TIMEFRAMES)
            
            # 1.5. Reload config if changed (SINGLE CHECK instead of 125)
            current_config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
            if current_config_mtime != last_config_mtime:
                print(f"🔄 Config changed, reloading for all {len(bots)} bots...")
                for bot in bots:
                    bot.strategy.reload_config()
                last_config_mtime = current_config_mtime
            
            # 2. Check Circuit Breaker ONCE (not 125 times)
            current_balance = get_current_balance()
            circuit_triggered = False
            stop_trading, cb_reason = risk_manager.check_circuit_breaker(current_balance)
            if stop_trading:
                if any(b.running for b in bots):  # Only notify once
                    await send_telegram_message(f"🚨 CIRCUIT BREAKER: {cb_reason}")
                    print(f"🚨 CIRCUIT BREAKER TRIGGERED: {cb_reason}")
                circuit_triggered = True
            
            # 3. Run Logic for all bots
            tasks = []
            for bot in bots:
                if bot.running:
                    try:
                        tasks.append(bot.run_step(current_balance, circuit_triggered))
                    except Exception as e:
                        print(f"Error starting bot task {bot.symbol}: {e}")
            if tasks:
                await asyncio.gather(*tasks)
            
            await asyncio.sleep(60) # 60s interval - SL/TP is set on entry, no need for frequent polling
            
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await manager.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
