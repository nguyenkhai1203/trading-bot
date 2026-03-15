import logging
from typing import Dict, Any, Optional
from src.domain.repository import ITradeRepository
from src.infrastructure.adapters.base_adapter import BaseAdapter
from src.domain.services.notification_service import NotificationService
from src import config

class ManagePositionUseCase:
    """
    Use Case: Manage an active position (Profit Lock, Trailing SL, TP extensions).
    Restores the v3.0 logic from execution.py.
    """
    def __init__(
        self, 
        trade_repo: ITradeRepository, 
        adapters: Dict[str, BaseAdapter],
        notification_service: NotificationService
    ):
        self.trade_repo = trade_repo
        self.adapters = adapters
        self.notification_service = notification_service
        self.logger = logging.getLogger("ManagePositionUseCase")

    async def execute(self, profile: Dict[str, Any], trade: Any, current_price: float, ta_data: Dict[str, Any] = None):
        """
        Calculates and applies Emergency Close, ATR Trailing, and Profit Lock.
        """
        profile_id = profile.get('id')
        ex_name = profile.get('exchange', 'UNKNOWN').upper()
        
        # 0. Get Adapter (Priority: Profile-ID, Fallback: Exchange Name)
        adapter = self.adapters.get(profile_id) or self.adapters.get(ex_name)
        
        if not adapter:
            self.logger.error(f"Adapter not found for profile {profile_id} or exchange {ex_name}")
            return False

        # 0. Emergency SLTP Guardian (8% Hard-Close)
        entry = trade.entry_price
        side = trade.side
        emergency_pct = getattr(config, 'SLTP_GUARDIAN_EMERGENCY_PCT', 0.08)
        
        is_emergency = False
        if side == 'BUY' and current_price <= entry * (1 - emergency_pct): is_emergency = True
        elif side == 'SELL' and current_price >= entry * (1 + emergency_pct): is_emergency = True
        
        if is_emergency:
            self.logger.warning(f"🚨 [EMERGENCY] {trade.symbol} hit {emergency_pct*100}% threshold. Hard-closing!")
            try:
                await adapter.close_position(trade.symbol, side, trade.qty)
                
                # Calculate PnL for the closure
                pnl = 0.0
                if side == 'BUY':
                    pnl = (current_price - entry) * trade.qty
                else:
                    pnl = (entry - current_price) * trade.qty
                
                await self.trade_repo.update_status(trade.id, 'CLOSED', exit_reason='EMERGENCY_SL', exit_price=current_price, pnl=pnl)
                await self.notification_service.notify_generic(f"🚨 **EMERGENCY CLOSE** | {trade.symbol} | Price: {current_price} | PnL: {pnl:+.2f}")
                return True
            except Exception as e:
                self.logger.error(f"Failed emergency close for {trade.symbol}: {e}")

        # 1. Continuous ATR Trailing SL
        changes = False
        final_sl = trade.sl_price
        final_tp = trade.tp_price
        
        if ta_data and getattr(config, 'ENABLE_DYNAMIC_SLTP', True):
            atr = ta_data.get('ATR_14')
            if atr:
                mult = getattr(config, 'ATR_TRAIL_MULTIPLIER', 1.5)
                trail_dist = atr * mult
                
                # Minimum price move (0.1%) to avoid spamming the exchange
                min_move = current_price * getattr(config, 'ATR_TRAIL_MIN_MOVE_PCT', 0.001)
                
                if side == 'BUY':
                    atr_sl = current_price - trail_dist
                    if final_sl is None or (atr_sl > final_sl + min_move):
                        final_sl = atr_sl
                        changes = True
                else: # SELL
                    atr_sl = current_price + trail_dist
                    if final_sl is None or (atr_sl < final_sl - min_move):
                        final_sl = atr_sl
                        changes = True

        # 2. Profit Lock / TP Extension Logic (Only if ATR trail didn't already trigger changes)
        if not changes:
            tp = trade.tp_price
            sl = trade.sl_price
            
            if all([entry, tp, sl]):
                total_dist = abs(tp - entry)
                if total_dist > 0:
                    current_profit_dist = abs(current_price - entry)
                    # Use a basic profit check
                    is_in_profit = (side == 'BUY' and current_price > entry) or (side == 'SELL' and current_price < entry)
                    
                    if is_in_profit:
                        progress = current_profit_dist / total_dist
                        threshold = getattr(config, 'PROFIT_LOCK_THRESHOLD', 0.8)
                        
                        if progress >= threshold:
                            lock_level = getattr(config, 'PROFIT_LOCK_LEVEL', 0.1)
                            lock_amount = total_dist * lock_level
                            
                            if side == 'BUY':
                                lock_sl = entry + lock_amount
                                if sl is None or lock_sl > sl:
                                    final_sl = lock_sl
                                    changes = True
                            else:
                                lock_sl = entry - lock_amount
                                if sl is None or lock_sl < sl:
                                    final_sl = lock_sl
                                    changes = True

        # 3. TA-Based TP Extension (Enhanced)
        extension_count = trade.meta.get('tp_extensions') or 0
        if ta_data and extension_count < getattr(config, 'MAX_TP_EXTENSIONS', 3):
            # Look for higher resistance if BUY, or lower support if SELL
            # (Check multiple levels for better extension)
            new_target = None
            if side == 'BUY':
                levels = [ta_data.get('Resistance_3'), ta_data.get('Resistance_2'), ta_data.get('Resistance_1')]
                for lvl in filter(None, levels):
                    if lvl > (final_tp or 0):
                        new_target = lvl
                        break
            else: # SELL
                levels = [ta_data.get('Support_3'), ta_data.get('Support_2'), ta_data.get('Support_1')]
                for lvl in filter(None, levels):
                    if lvl < (final_tp or 999999):
                        new_target = lvl
                        break
            
            if new_target:
                # Limit the extension distance to avoid extreme targets
                max_ext_dist = entry * 0.10 # Max 10% move extension
                is_safe_ext = abs(new_target - entry) < max_ext_dist
                
                if is_safe_ext and ((side == 'BUY' and new_target > (final_tp or 0)) or (side == 'SELL' and new_target < (final_tp or 999999))):
                    final_tp = float(adapter.price_to_precision(trade.symbol, new_target))
                    trade.meta['tp_extensions'] = extension_count + 1
                    changes = True
                    self.logger.info(f"✨ [TA-EXTENSION] {trade.symbol} found new target: {final_tp}")

        # 4. FINAL CROSS-VALIDATION (The "RIVER" Fix)
        # Ensure SL never crosses TP. Maintain a minimum gap (e.g., 0.2%).
        min_gap_pct = 0.002
        if final_sl and final_tp:
            if side == 'BUY':
                if final_sl >= final_tp * (1 - min_gap_pct):
                    # SL is pushing TP! Push TP ahead or cap SL.
                    # Since user wants TA-based TP, we try to push TP first.
                    self.logger.warning(f"⚠️ [GAP-SAFETY] {trade.symbol} SL({final_sl}) approaching TP({final_tp}). Adjusting...")
                    final_tp = final_sl * (1 + min_gap_pct)
                    changes = True
            else: # SELL
                if final_sl <= final_tp * (1 + min_gap_pct):
                    self.logger.warning(f"⚠️ [GAP-SAFETY] {trade.symbol} SL({final_sl}) approaching TP({final_tp}). Adjusting...")
                    final_tp = final_sl * (1 - min_gap_pct)
                    changes = True

        # 5. CURRENT PRICE CAPPING (Bybit Safety)
        # Ensure SL stays on the "Loss" side of current price.
        # v4.4: Using 0.2% buffer instead of 0.05% to avoid immediate stop-outs.
        safe_buffer = 0.002 # 0.2% safety
        if final_sl:
            if side == 'BUY':
                # For BUY, SL must be < Current. If we try to set SL >= current, Bybit rejects.
                if final_sl >= current_price * (1 - 0.0005):
                    self.logger.warning(f"⚠️ [SL-CAPPING] {trade.symbol} SL({final_sl}) too close to Current({current_price}). Capping to safe level (-0.2%).")
                    final_sl = current_price * (1 - safe_buffer)
                    changes = True
            else: # SELL
                # For SELL, SL must be > Current.
                if final_sl <= current_price * (1 + 0.0005):
                    self.logger.warning(f"⚠️ [SL-CAPPING] {trade.symbol} SL({final_sl}) too close to Current({current_price}). Capping to safe level (+0.2%).")
                    final_sl = current_price * (1 + safe_buffer)
                    changes = True

        # 6. MARKET EXIT FALLBACK (Rescue Logic)
        # If the desired SL has been hit, but exchange doesn't have it -> Hard close.
        # Note: We use the SL value BEFORE capping for this check to see if it SHOULD have triggered.
        sl_is_hit = False
        if final_sl:
            if side == 'BUY' and current_price <= final_sl: sl_is_hit = True
            elif side == 'SELL' and current_price >= final_sl: sl_is_hit = True
            
        if sl_is_hit:
            actual_sl = 0
            if adapter.is_tpsl_attached_supported():
                # We need to know if the exchange position actually has SL.
                # Since we don't have the position dict here, we might need to rely on trade.sl_order_id fallback
                # or assume if we are in this step, we should just close if we were REPLACING it anyway.
                pass
            
            # If our DB thinks SL exists but exchange rejected it (or it's missing), and price HIT it -> CLOSE.
            if not trade.sl_order_id or trade.sl_order_id in ('None', None, ''):
                self.logger.error(f"🚨 [MARKET-RESCUE] {trade.symbol} SL({final_sl}) hit, but exchange order ID is {trade.sl_order_id}. Market closing now!")
                try:
                    await adapter.close_position(trade.symbol, side, trade.qty)
                    
                    # Calculate PnL for the closure
                    pnl = 0.0
                    if side == 'BUY':
                        pnl = (current_price - entry) * trade.qty
                    else:
                        pnl = (entry - current_price) * trade.qty
                        
                    await self.trade_repo.update_status(trade.id, 'CLOSED', exit_reason='RESCUE_SL', exit_price=current_price, pnl=pnl)
                    await self.notification_service.notify_generic(f"🚨 **MARKET RESCUE** | {trade.symbol} | SL {final_sl} hit. Closed at {current_price} | PnL: {pnl:+.2f}")
                    return True
                except Exception as e:
                    self.logger.error(f"Rescue close failed for {trade.symbol}: {e}")

        if changes:
            if final_sl is not None:
                final_sl = float(adapter.price_to_precision(trade.symbol, final_sl))
            if final_tp is not None:
                final_tp = float(adapter.price_to_precision(trade.symbol, final_tp))
                
            self.logger.info(f"🛡️ [MANAGEMENT] {trade.symbol}: SL={final_sl}, TP={final_tp}")
            
            # Update Exchange
            if adapter.can_trade:
                try:
                    # Prefer specialized methods
                    if adapter.is_tpsl_attached_supported():
                        await adapter.set_position_sl_tp(trade.symbol, side, final_sl, final_tp)
                        # Sync IDs to DB
                        trade.sl_order_id = 'attached'
                        trade.tp_order_id = 'attached'
                    else:
                        # For Binance (separate), we must cancel old ones first or the exchange might reject
                        await adapter.cancel_stop_orders(trade.symbol, trade.sl_order_id, trade.tp_order_id)
                        
                        ids = await adapter.place_stop_orders(
                            trade.symbol, side, trade.qty, 
                            sl=final_sl, tp=final_tp, 
                            is_pending=False # Position is active
                        )
                        # Sync IDs to DB so Monitor doesn't think they are missing!
                        if ids.get('sl_id'): trade.sl_order_id = ids['sl_id']
                        if ids.get('tp_id'): trade.tp_order_id = ids['tp_id']

                except Exception as e:
                    self.logger.error(f"Failed to update exchange SL/TP for {trade.symbol}: {e}")

            # Update DB prices
            trade.sl_price = final_sl
            trade.tp_price = final_tp
            await self.trade_repo.save_trade(trade)
            
            # Notify on significant changes (Profit Lock or TP Ext)
            # (ATR trail can be noisy, maybe only notify if lock/ext?)
            if trade.meta.get('tp_extensions') == extension_count + 1:
                await self.notification_service.notify_generic(f"💰 **TP EXTENDED** | {trade.symbol} ➔ {final_tp}")
            
            return True
            
        return False
