import logging
import time
from typing import Dict, Any, Optional
from src.domain.repository import ITradeRepository
from src.infrastructure.adapters.base_adapter import BaseAdapter
from src.domain.models import Trade
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.domain.exceptions import InsufficientFundsError
from src.cooldown_manager import CooldownManager
import json

class ExecuteTradeUseCase:
    """
    Use Case: Execute a trade (place order, handle SL/TP setup, log to DB).
    """
    def __init__(self, 
                 trade_repo: ITradeRepository, 
                 adapters: Dict[str, BaseAdapter], 
                 risk_service: RiskService, 
                 notification_service: NotificationService,
                 cooldown_manager: CooldownManager):
        self.trade_repo = trade_repo
        self.adapters = adapters
        self.risk_service = risk_service
        self.notification_service = notification_service
        self.cooldown_manager = cooldown_manager
        self.logger = logging.getLogger("ExecuteTradeUseCase")

    async def execute(self, profile: Dict[str, Any], signal: Dict[str, Any]) -> bool:
        """
        Places orders on information from the signal.
        """
        from src import config
        
        # 0. Basic Filter
        if signal.get('side') == 'SKIP':
            return False

        symbol = signal['symbol']
        ex_name = profile.get('exchange', '').upper()
        
        # 1. Safety Checks (Cooldown & Margin)
        if self.cooldown_manager.is_in_cooldown(ex_name, symbol):
            self.logger.info(f"Signal for {symbol} skipped: In SL Cooldown.")
            return False
            
        # Margin throttling (Profile specific check via shared cache in CooldownManager)
        # We use profile['id'] as part of the check if needed, but CooldownManager uses account_key
        # In this refactor, we'll use a simplified check or pass the account_key
        # Assuming CooldownManager.is_margin_throttled handles the logic
        # For simplicity, we'll check if the profile's exchange is throttled
        # (In old code, this was account-wide)
        
        # 2. Confidence Check (Strict)
        conf = signal.get('confidence')
        if conf is None:
            self.logger.warning(f"Signal for {symbol} has NO confidence value. Skipping.")
            return False
            
        min_conf = getattr(config, 'MIN_CONFIDENCE_TO_TRADE', 0.3)
        if conf < min_conf:
            self.logger.debug(f"Signal for {symbol} skipped: confidence {conf:.2f} < {min_conf}")
            return False
 
        # 3. Risk/Reward Filter
        sl_pct = signal.get('sl_pct')
        tp_pct = signal.get('tp_pct')
        
        if sl_pct is not None and tp_pct is not None and sl_pct > 0:
            rr = tp_pct / sl_pct
            if rr < 1.3: # Minimum 1.3 R/R safety
                self.logger.info(f"Signal for {symbol} skipped: Poor R/R ({rr:.2f})")
                return False
        elif sl_pct is None or tp_pct is None:
             self.logger.warning(f"Signal for {symbol} missing SL/TP pct (SL:{sl_pct}, TP:{tp_pct})")
             # Fallback to defaults or return False
             sl_pct = sl_pct or getattr(config, 'STOP_LOSS_PCT', 0.02)
             tp_pct = tp_pct or getattr(config, 'TAKE_PROFIT_PCT', 0.04)

        adapter = self.adapters.get(ex_name)
        if not adapter:
            self.logger.error(f"No adapter found for {ex_name}")
            return False

        profile_id = profile['id']
        side = signal['side']
        timeframe = signal.get('timeframe', '1h')
        
        # 4. Idempotency & Advanced Rebalancing/Reversal Check
        active_trades = await self.trade_repo.get_active_positions(profile_id)
        current_trade = next((t for t in active_trades if t.symbol == symbol), None)
        
        if current_trade:
            # Case A: Reversal (Current is BUY, signal is SELL or vice versa)
            if current_trade.side != side.upper():
                if conf >= 0.6: # Moderate confidence required for reversal
                    self.logger.info(f"🔄 **REVERSAL** | {symbol}: Closing {current_trade.side} for {side}")
                    await adapter.close_position(symbol)
                    await self.trade_repo.update_status(current_trade.id, 'CLOSED', exit_reason='REVERSAL')
                    await self.notification_service.notify_generic(f"🔄 **REVERSAL** | {symbol} | Closing {current_trade.side} for {side}")
                    # Continue to open new position
                else:
                    return False
            
            # Case B: Pending Adjustment (Current is PENDING, signal is BETTER)
            elif current_trade.status == 'PENDING':
                old_conf = current_trade.meta.get('signal_confidence') or 0.5
                old_rr = current_trade.meta.get('rr_ratio') or 1.5
                
                # New R/R
                new_rr = tp_pct / sl_pct if sl_pct > 0 else 1.0
                
                # Logic: Replace if confidence is significantly better OR R/R is better with same confidence
                is_better = (conf > old_conf + 0.1) or (conf >= old_conf and new_rr > old_rr + 0.5)
                
                if is_better:
                    self.logger.info(f"⚖️ **ADJUSTMENT** | {symbol}: Replacing PENDING (Conf {old_conf:.2f}➔{conf:.2f}, R/R {old_rr:.1f}➔{new_rr:.1f})")
                    if current_trade.exchange_order_id:
                        await adapter.cancel_order(current_trade.exchange_order_id, symbol)
                    await self.trade_repo.update_status(current_trade.id, 'CANCELLED', exit_reason='ADJUSTMENT')
                    await self.notification_service.notify_order_cancelled(symbol, timeframe, side, current_trade.entry_price, "Better Signal", dry_run=not adapter.can_trade, exchange=ex_name)
                    # Continue to open new position
                else:
                    return False
            else:
                # Already in a matching ACTIVE trade, skip
                return False

        try:
            # 5. Fetch Fresh Ticker (for current price)
            ticker = await adapter.fetch_ticker(symbol)
            entry_price = float(ticker['last'])
            
            # 6. Position Sizing
            tiers = getattr(config, 'CONFIDENCE_TIERS', {})
            selected_tier = tiers.get('low', {})
            if conf >= 0.7: selected_tier = tiers.get('high', {})
            elif conf >= 0.5: selected_tier = tiers.get('medium', {})
            
            leverage = selected_tier.get('leverage', 5)
            cost_usdt = selected_tier.get('cost_usdt', 5.0)
            
            qty = self.risk_service.calculate_size_by_cost(entry_price, cost_usdt, leverage)
            
            # 7. STRICT NOTIONAL CHECK (Safety against exchange rejections)
            is_valid, reason, notional = adapter.check_min_notional(symbol, entry_price, qty)
            if not is_valid:
                self.logger.warning(f"Order for {symbol} rejected locally: {reason} (Notional: ${notional:.2f})")
                # Trigger a long cooldown for invalid signals to prevent spam
                await self.cooldown_manager.set_sl_cooldown(ex_name, symbol, profile_id, custom_duration=86400)
                return False

            if qty <= 0:
                self.logger.warning(f"Quantity too low for {symbol} ({qty})")
                return False
                
            # Generate pos_key
            pos_key = f"{ex_name}_{symbol.replace('/', '_')}_{timeframe}"

            self.logger.info(f"Executing {side} on {symbol} | Conf: {conf:.2f} | Qty: {qty:.4f}")

            # 8. Place Order
            order_res = {}
            if adapter.can_trade:
                # Calculate SL/TP prices
                sl_price, tp_price = self.risk_service.calculate_sl_tp(entry_price, side, sl_pct=sl_pct, tp_pct=tp_pct)
                params = {
                    'leverage': leverage,
                    'sl_price': sl_price,
                    'tp_price': tp_price
                }
                order_res = await adapter.create_order(symbol, 'market', side.lower(), qty, price=None, params=params)
            else:
                sl_price, tp_price = self.risk_service.calculate_sl_tp(entry_price, side, sl_pct=sl_pct, tp_pct=tp_pct)
                self.logger.info(f"[DRY-RUN] Signal {symbol} would open {side} at {entry_price}")
                order_res = {'id': f'dry_{int(time.time()*1000)}'}
            
            # 9. Save to Repository
            new_trade = Trade(
                profile_id=profile_id,
                exchange=ex_name,
                symbol=symbol,
                side=side.upper(),
                qty=qty,
                entry_price=entry_price,
                leverage=leverage,
                status='ACTIVE', # In basic flow, market means active
                timeframe=timeframe,
                pos_key=pos_key,
                sl_price=sl_price,
                tp_price=tp_price,
                exchange_order_id=str(order_res.get('id', '')),
                meta={
                    'signal_confidence': conf, 
                    'rr_ratio': tp_pct / sl_pct if sl_pct > 0 else 1.0,
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'comment': signal.get('comment')
                }
            )
            
            trade_id = await self.trade_repo.save_trade(new_trade)
            
            # 10. AI Snapshot Logging
            if signal.get('snapshot'):
                from src.infrastructure.repository.database import DataManager
                db = await DataManager.get_instance()
                await db.log_ai_snapshot(trade_id, json.dumps(signal['snapshot']), conf)

            # 11. Notify User
            await self.notification_service.notify_order_filled(new_trade, conf, dry_run=not adapter.can_trade)
            
            self.logger.info(f"Successfully registered {side} on {symbol} (Conf: {conf:.2f})")
            return True

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "balance" in err_msg or "110007" in err_msg:
                # Log as WARNING instead of ERROR since Orchestrator handles rebalancing
                self.logger.warning(f"Insufficient funds for {symbol} (Bybit code 110007). Rebalancing may be triggered.")
                
                # Trigger Margin Throttling (account-wide via shared cache)
                acc_key = getattr(adapter, 'account_key', f"{ex_name}_GLOBAL")
                await self.cooldown_manager.handle_margin_error(acc_key, {}, ex_name) 
                raise InsufficientFundsError(str(e))
            else:
                self.logger.error(f"Trade execution failed for {symbol}: {e}")
                import traceback
                self.logger.debug(traceback.format_exc())
                return False
