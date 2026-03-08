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
        # 0. Emergency SLTP Guardian (8% Hard-Close)
        entry = trade.entry_price
        side = trade.side
        emergency_pct = getattr(config, 'SLTP_GUARDIAN_EMERGENCY_PCT', 0.08)
        
        is_emergency = False
        if side == 'BUY' and current_price <= entry * (1 - emergency_pct): is_emergency = True
        elif side == 'SELL' and current_price >= entry * (1 + emergency_pct): is_emergency = True
        
        if is_emergency:
            self.logger.warning(f"🚨 [EMERGENCY] {trade.symbol} hit {emergency_pct*100}% threshold. Hard-closing!")
            ex_name = profile['exchange'].upper()
            adapter = self.adapters.get(ex_name)
            if adapter:
                try:
                    await adapter.close_position(trade.symbol, side, trade.qty)
                    await self.trade_repo.update_status(trade.id, 'CLOSED', exit_reason='EMERGENCY_SL', exit_price=current_price)
                    await self.notification_service.notify_generic(f"🚨 **EMERGENCY CLOSE** | {trade.symbol} | Price: {current_price}")
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
                        final_sl = round(atr_sl, 6)
                        changes = True
                else: # SELL
                    atr_sl = current_price + trail_dist
                    if final_sl is None or (atr_sl < final_sl - min_move):
                        final_sl = round(atr_sl, 6)
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
                                    final_sl = round(lock_sl, 6)
                                    changes = True
                            else:
                                lock_sl = entry - lock_amount
                                if sl is None or lock_sl < sl:
                                    final_sl = round(lock_sl, 6)
                                    changes = True

        # 3. TA-Based TP Extension (Restore v3.0 logic)
        extension_count = trade.meta.get('tp_extensions') or 0
        if ta_data and extension_count < getattr(config, 'MAX_TP_EXTENSIONS', 2):
            resistance = ta_data.get('Resistance_1')
            support = ta_data.get('Support_1')
            
            if side == 'BUY' and resistance and resistance > final_tp:
                max_tp = entry + (abs(trade.tp_price - entry) * 1.5) if trade.tp_price else 0
                ext_tp = min(resistance, max_tp) if max_tp > 0 else resistance
                if ext_tp > final_tp:
                    final_tp = round(ext_tp, 6)
                    trade.meta['tp_extensions'] = extension_count + 1
                    changes = True
            elif side == 'SELL' and support and support < final_tp:
                max_tp = entry - (abs(trade.tp_price - entry) * 1.5) if trade.tp_price else 0
                ext_tp = max(support, max_tp) if max_tp > 0 else support
                if ext_tp < final_tp:
                    final_tp = round(ext_tp, 6)
                    trade.meta['tp_extensions'] = extension_count + 1
                    changes = True

        if changes:
            self.logger.info(f"🛡️ [MANAGEMENT] {trade.symbol}: SL={final_sl}, TP={final_tp}")
            
            # Update Exchange
            ex_name = profile['exchange'].upper()
            adapter = self.adapters.get(ex_name)
            if adapter and adapter.can_trade:
                try:
                    # Bybit supports attached SL/TP, but we might need to recreate them
                    # We'll use the adapter's set_position_sl_tp if available
                    if hasattr(adapter, 'set_position_sl_tp'):
                        await adapter.set_position_sl_tp(trade.symbol, side, final_sl, final_tp)
                    else:
                        # Fallback to creating separate stop orders if not supported/available
                        await adapter.create_order(
                            trade.symbol, 'market', 'sell' if side == 'BUY' else 'buy', 
                            trade.qty, params={'stopPrice': final_sl, 'reduceOnly': True}
                        )
                except Exception as e:
                    self.logger.error(f"Failed to update exchange SL/TP for {trade.symbol}: {e}")

            # Update DB
            trade.sl_price = final_sl
            trade.tp_price = final_tp
            await self.trade_repo.save_trade(trade)
            
            # Notify on significant changes (Profit Lock or TP Ext)
            # (ATR trail can be noisy, maybe only notify if lock/ext?)
            if trade.meta.get('tp_extensions') == extension_count + 1:
                await self.notification_service.notify_generic(f"💰 **TP EXTENDED** | {trade.symbol} ➔ {final_tp}")
            
            return True
            
        return False
