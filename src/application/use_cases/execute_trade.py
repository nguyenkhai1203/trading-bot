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
from src.utils.symbol_helper import to_raw_format
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
                 cooldown_manager: CooldownManager,
                 sync_service: Any = None,
                 container: Any = None): # Inject container for profile adapters
        self.trade_repo = trade_repo
        self.adapters = adapters
        self.risk_service = risk_service
        self.notification_service = notification_service
        self.cooldown_manager = cooldown_manager
        self.sync_service = sync_service
        self.container = container
        self.logger = logging.getLogger("ExecuteTradeUseCase")

    async def execute(self, profile: Dict[str, Any], signal: Dict[str, Any]) -> bool:
        """
        Places orders on information from the signal.
        """
        from src import config
        is_upgrade = False
        
        # 0. Basic Filter
        if signal.get('side') == 'SKIP':
            return False

        symbol = signal['symbol']
        ex_name = profile.get('exchange', '').upper()
        
        # 0.5. Get Adapter
        adapter = None
        if self.container and hasattr(self.container, 'adapters_by_profile'):
            adapter = self.container.adapters_by_profile.get(profile.get('id'))
        
        if not adapter:
            adapter = self.adapters.get(ex_name) # Fallback

        if not adapter:
            self.logger.error(f"No adapter found for {ex_name} (Profile {profile.get('id')})")
            return False

        # Use profile ID to ensure marginal throttling is account-specific
        # Format: BYBIT_3 (for profile ID 3)
        acc_key = f"{ex_name}_{profile['id']}"

        # Extract core identifiers early — needed by cooldown checks
        profile_id = profile['id']
        side = signal['side']
        timeframe = signal.get('timeframe', '1h')

        # 1. Safety Checks (Cooldown & Margin)
        if self.cooldown_manager.is_in_cooldown(ex_name, symbol, profile_id):
            self.logger.info(f"Signal for {symbol} skipped: In SL Cooldown.")
            return False
            
        # 1b. Margin Throttling Check
        if self.cooldown_manager.is_margin_throttled(acc_key):
            if self.cooldown_manager.should_log_margin_throttle(acc_key):
                self.logger.warning(f"Signal for {symbol} skipped: Account {acc_key} is margin throttled (Log throttled).")
            else:
                self.logger.debug(f"Signal for {symbol} skipped: Account {acc_key} is margin throttled.")
            return False

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
        
        # 4. PROFILE SYMBOL ISOLATION (Strict: 1 position per symbol per profile)
        # Check Database for any active/pending trade for THIS profile and symbol
        active_in_profile = await self.trade_repo.get_active_positions(profile_id)
        
        raw_symbol = to_raw_format(symbol)
        
        # Match by normalized (raw) symbol to be robust against "AVAXUSDT" vs "AVAX/USDT:USDT"
        existing_in_profile = next((t for t in active_in_profile if to_raw_format(t.symbol) == raw_symbol), None)
        
        # Check Sync Cache for live exchange state (Safety net)
        if not existing_in_profile and self.sync_service:
            state = self.sync_service.get_account_state(profile)
            if state:
                # IMPORTANT: Since we are in a specific Profile, we ONLY care about trades
                # on this specific account that match our symbol.
                
                # 1. Check Positions
                for p in state.get('positions', []):
                    if to_raw_format(p.get('symbol')) == raw_symbol:
                        # If it exists on exchange, we treat it as blocking this symbol for this profile
                        self.logger.info(f"Signal for {symbol} skipped: Profile {profile_id} restricted to 1 active position per symbol.")
                        return False
                
                # 2. Check Open Orders
                for o in state.get('orders', []):
                    if to_raw_format(o.get('symbol')) == raw_symbol:
                        self.logger.info(f"Signal for {symbol} skipped: Profile {profile_id} has existing open order for {symbol}.")
                        return False

        if existing_in_profile:
            # GHOST PROTECTION: If DB thinks we have an ACTIVE trade, verify against exchange cache
            if existing_in_profile.status == 'ACTIVE' and self.sync_service:
                state = self.sync_service.get_account_state(profile)
                if state:
                    # Check if this specific symbol exists in exchange positions
                    ex_match = next((p for p in state.get('positions', []) if to_raw_format(p.get('symbol')) == raw_symbol), None)
                    if not ex_match:
                        self.logger.warning(f"👻 [REVERSAL-GUARD] {symbol} in DB as ACTIVE, but not found on exchange. Marking as CLOSED and proceeding.")
                        await self.trade_repo.update_status(existing_in_profile.id, 'CLOSED', exit_reason='GHOST_SYNC')
                        existing_in_profile = None # Allow execution to proceed as a fresh trade
            
        if existing_in_profile:
            # Case A: Reversal (Current is BUY, signal is SELL or vice versa)
            if existing_in_profile.side != side.upper():
                if conf >= 0.6: # Moderate confidence required for reversal
                    self.logger.info(f"🔄 **REVERSAL** | {symbol}: Closing {existing_in_profile.side} for {side}")
                    
                    # Use adapter to close
                    close_res = await adapter.close_position(symbol, existing_in_profile.side, existing_in_profile.qty)
                    # Handle already closed case
                    if isinstance(close_res, dict) and close_res.get('info') == 'already_closed':
                        self.logger.info(f"[{symbol}] Reversal bypass: Position already closed on exchange.")
                    
                    # If the trade was ACTIVE, it's a REAL loss/profit. Record it as CLOSED with PnL.
                    if existing_in_profile.status == 'ACTIVE':
                        # Fetch current price for accurate PnL recording
                        try:
                            ticker = await adapter.fetch_ticker(symbol)
                            exit_price = float(ticker['last'])
                        except:
                            exit_price = existing_in_profile.entry_price # Fallback
                        
                        # Calculate PnL
                        qty = float(existing_in_profile.qty)
                        if existing_in_profile.side == 'BUY':
                            pnl = (exit_price - existing_in_profile.entry_price) * qty
                        else:
                            pnl = (existing_in_profile.entry_price - exit_price) * qty
                        
                        # Update as CLOSED
                        await self.trade_repo.update_status(
                            existing_in_profile.id, 
                            'CLOSED', 
                            exit_reason='REVERSAL', 
                            exit_price=exit_price, 
                            pnl=pnl
                        )
                        
                        # Notify proper closure
                        # Leverage might be missing in some models, default to 10
                        leverage = getattr(existing_in_profile, 'leverage', 10)
                        margin = (existing_in_profile.entry_price * qty) / leverage if (existing_in_profile.entry_price and qty and leverage) else 1.0
                        pnl_pct = (pnl / margin * 100) if margin > 0 else 0.0
                        
                        is_live = str(profile.get('environment', 'LIVE')).upper() == 'LIVE'
                        await self.notification_service.notify_position_closed(
                            trade=existing_in_profile,
                            exit_price=exit_price,
                            pnl=pnl,
                            pnl_pct=pnl_pct,
                            reason='REVERSAL',
                            dry_run=not is_live
                        )
                        
                        # If it was a loss, apply SL Cooldown
                        if pnl < 0:
                            self.logger.info(f"[{symbol}] Reversal resulted in loss. Applying SL Cooldown.")
                            await self.cooldown_manager.set_sl_cooldown(ex_name, symbol, profile['id'])
                    else:
                        # For PENDING orders, it's just a cancellation
                        await self.trade_repo.update_status(existing_in_profile.id, 'CANCELLED', exit_reason='REVERSAL')
                        await self.notification_service.notify_generic(f"🔄 **REVERSAL** | {symbol} | Cancelling PENDING {existing_in_profile.side} for {side}")
                    
                    # Continue to open new position
                else:
                    return False
            
            # Case B: Signal Upgrade (Current is PENDING, signal is BETTER)
            elif existing_in_profile.status == 'PENDING':
                old_conf = existing_in_profile.meta.get('signal_confidence') or 0.5
                old_rr = existing_in_profile.meta.get('rr_ratio') or 1.5
                new_rr = tp_pct / sl_pct if sl_pct > 0 else 1.0
                
                # Logic: Replace if confidence is significantly better (+0.2) OR R/R is better
                is_better = (conf > old_conf + 0.2) or (conf >= old_conf and new_rr > old_rr + 1.0)
                
                if is_better:
                    self.logger.info(f"⚖️ **UPGRADE** | {symbol}: Replacing PENDING (Conf {old_conf:.2f}➔{conf:.2f})")
                    if existing_in_profile.exchange_order_id:
                        await adapter.cancel_order(existing_in_profile.exchange_order_id, symbol)
                    await self.trade_repo.update_status(existing_in_profile.id, 'CANCELLED', exit_reason='UPGRADE')
                    # Apply short cooldown to prevent re-entry loop in next heartbeat
                    await self.cooldown_manager.set_sl_cooldown(ex_name, symbol, profile_id, custom_duration=300)
                    existing_in_profile = None # Clear so later logic treats this as a fresh start if needed
                    is_upgrade = True # Flag for unique ID generation below
                    # Continue to open new position
                else:
                    return False
            
            # Case C: Passive Upgrade (Current is ACTIVE, signal is BETTER)
            elif existing_in_profile.status == 'ACTIVE':
                # We don't open a new trade, but we check if we should update SL/TP
                old_conf = existing_in_profile.meta.get('signal_confidence') or 0.5
                if conf > old_conf + 0.15:
                    self.logger.info(f"📈 **POSITION OPTIMIZATION** | {symbol}: Updating SL/TP from better signal (Conf {old_conf:.2f}➔{conf:.2f})")
                    sl_price, tp_price = self.risk_service.calculate_sl_tp(existing_in_profile.entry_price, side, sl_pct=sl_pct, tp_pct=tp_pct)
                    await adapter.set_position_sl_tp(symbol, side, sl=sl_price, tp=tp_price)
                    # Update DB
                    existing_in_profile.sl_price = sl_price
                    existing_in_profile.tp_price = tp_price
                    existing_in_profile.meta['signal_confidence'] = conf
                    await self.trade_repo.save_trade(existing_in_profile)
                return False
            else:
                return False

        # Initialise before try-block so the except handler can safely log them
        # even if an exception fires before they are calculated.
        sl_price: Optional[float] = None
        tp_price: Optional[float] = None
        qty: float = 0.0
        entry_price: float = 0.0
        pos_key: str = ""

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

            # --- NEW: Available Balance Guard ---
            try:
                balance = await adapter.fetch_balance()
                free_usdt = float(balance.get('USDT', {}).get('free', 0))
                
                # Check if we have enough USDT to cover the cost + 10% buffer
                required_margin = cost_usdt * 1.1
                if free_usdt < required_margin:
                    self.logger.warning(f"Account depleted! Free Margin ${free_usdt:.2f} < Required ${required_margin:.2f}. Skipping {symbol} (Proactive Guard).")
                    # Trigger Margin Throttling proactively
                    await self.cooldown_manager.handle_margin_error(acc_key, ex_name)
                    return False
            except Exception as e:
                self.logger.warning(f"Could not fetch balance before trade: {e}")
            
            # --- CRITICAL: Enforce ISOLATED Margin and Leverage before sizing/trading ---
            # This ensures BTCDOM isn't CROSS and TAO doesn't exceed capital targets
            await adapter.ensure_isolated_and_leverage(symbol, leverage)
            
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
            sl_oid = None
            tp_oid = None
            use_limit = getattr(config, 'USE_LIMIT_ORDERS', False)
            patience_pct = getattr(config, 'PATIENCE_ENTRY_PCT', 0.01)
            
            # Use provided entry_price from signal (or ticker) as base
            base_price = entry_price
            order_type = 'market'
            order_price = None
            
            if use_limit:
                order_type = 'limit'
                # --- STRICT TECHNICAL ENTRY LOGIC (v4.2) ---
                # REMOVED FALLBACK: We only enter if we find a valid technical level.
                tech_entry = None
                
                # Try to find technical levels from signal snapshot or TA row
                # Priority: Support/Resistance > Fibo > EMAs
                ta_data = signal.get('snapshot') or {}
                
                if side.upper() == 'BUY':
                    # For BUY, we want to buy at SUPPORT or FIBO below current price
                    candidates = []
                    # Check both snake_case and TitleCase
                    candidates.append(signal.get('support_level'))
                    candidates.append(signal.get('Support'))
                    candidates.append(signal.get('fibo_618'))
                    candidates.append(signal.get('Fibo 0.618'))
                    candidates.append(signal.get('fibo_50'))
                    candidates.append(signal.get('EMA_21'))
                    
                    # Filter out None and cast to float
                    candidates = [float(c) for c in candidates if c is not None]
                    
                    # Pick the highest candidate that is BELOW current price (Patience Entry)
                    valid_candidates = [c for c in candidates if c < entry_price * 0.998]
                    if valid_candidates:
                        tech_entry = max(valid_candidates)
                else:
                    # For SELL, we want to sell at RESISTANCE or FIBO above current price
                    candidates = []
                    candidates.append(signal.get('resistance_level'))
                    candidates.append(signal.get('Resistance'))
                    candidates.append(signal.get('fibo_618'))
                    candidates.append(signal.get('Fibo 0.618'))
                    candidates.append(signal.get('fibo_50'))
                    candidates.append(signal.get('EMA_21'))
                    
                    # Filter out None and cast to float
                    candidates = [float(c) for c in candidates if c is not None]
                    
                    # Pick the lowest candidate that is ABOVE current price
                    valid_candidates = [c for c in candidates if c > entry_price * 1.002]
                    if valid_candidates:
                        tech_entry = min(valid_candidates)

                if tech_entry is None:
                    self.logger.info(f"[{symbol}] No valid technical entry level found. Skipping signal (Strict Entry).")
                    return False

                order_price = tech_entry
                # Update entry_price to the limit price for DB/SLTP calculations
                entry_price = order_price
            
            self.logger.info(f"[{symbol}] Type: {order_type.upper()} | Target: {entry_price:.4f} | Qty: {qty:.4f}")
            if order_type == 'limit' and base_price:
                dist_pct = abs(entry_price - base_price) / base_price * 100
                self.logger.info(f"[{symbol}] Limit Distance: {dist_pct:.4f}% from signal price ({base_price:.4f})")

            if adapter.can_trade:
                # Calculate SL/TP prices based on the actual entry target
                sl_price, tp_price = self.risk_service.calculate_sl_tp(entry_price, side, sl_pct=sl_pct, tp_pct=tp_pct)
                
                # Generate deterministic Idempotency Key (clientOrderId)
                tf_secs = 3600 # Default 1h
                if timeframe.endswith('m'): tf_secs = int(timeframe[:-1]) * 60
                elif timeframe.endswith('h'): tf_secs = int(timeframe[:-1]) * 3600
                elif timeframe.endswith('d'): tf_secs = int(timeframe[:-1]) * 86400
                
                candle_start = int(time.time() / tf_secs) * tf_secs
                import re
                # Handle CCXT unified format (e.g. DOT/USDT:USDT -> DOTUSDT)
                base_sym = symbol.split(':')[0]
                clean_sym = re.sub(r'[^a-zA-Z0-9]', '', base_sym)
                
                # Format: BMS_DOTUSDT_B_15m_1710000000 (Bybit max length: 36)
                client_oid = f"BMS_{clean_sym}_{side.upper()[:1]}_{timeframe}_{candle_start}"
                if is_upgrade:
                    # Append 'U' for Upgrade to distinguish from the original pending order
                    # This avoids 'OrderLinkedID is duplicate' if we re-enter in the same candle
                    client_oid = f"{client_oid}_U"

                # LOCAL IDEMPOTENCY CHECK (v4.3)
                # Check if we already have this specific trade in DB (e.g. from previous tick or parallel instance)
                existing_oid = await self.trade_repo.get_trade_by_order_id(client_oid)
                if existing_oid:
                    # Only block if the existing trade is still "Live" (not Cancelled/Closed)
                    if existing_oid.get('status') in ('ACTIVE', 'PENDING'):
                        self.logger.warning(f"🛡️ [LOCAL-IDEMPOTENCY] {symbol} order {client_oid} already live in DB. Skipping duplicate.")
                        return True 
                    else:
                        # If it was cancelled/closed, we can try again with a salt to be extra safe
                        client_oid = f"{client_oid}_{int(time.time()) % 1000}"

                params = {
                    'leverage': leverage,
                    'stopLoss': sl_price,
                    'takeProfit': tp_price,
                    'clientOrderId': client_oid
                }
                
                try:
                    order_res = await adapter.create_order(symbol, order_type, side.lower(), qty, price=order_price, params=params)
                    
                    # --- NEW: SL/TP Attachment Check & Fallback ---
                    tpsl_attached = adapter.is_tpsl_attached_supported() and (sl_price or tp_price)
                    sl_oid = 'attached' if tpsl_attached else None
                    tp_oid = 'attached' if tpsl_attached else None
                    
                    if not tpsl_attached and (sl_price or tp_price):
                        self.logger.info(f"[{symbol}] Attachment not supported. Placing separate SL/TP orders...")
                        try:
                            # Note: place_stop_orders now supports is_pending which we set if order_type is limit
                            ids = await adapter.place_stop_orders(symbol, side, qty, sl=sl_price, tp=tp_price, is_pending=(order_type == 'limit'))
                            sl_oid = ids.get('sl_id')
                            tp_oid = ids.get('tp_id')
                        except Exception as pso_err:
                            self.logger.error(f"Fallback SL/TP placement failed: {pso_err}")
                except Exception as api_err:
                    err_str = str(api_err).lower()
                    # 10002: Order already exists, 110072: OrderLinkedID is duplicate
                    if "already exists" in err_str or "110072" in err_str or "10002" in err_str:
                        self.logger.warning(f"🛡️ [EXCHANGE-IDEMPOTENCY] {symbol} order {client_oid} already exists on Bybit. Reconstructing DB record.")
                        order_res = {'id': client_oid} # Use clientOrderId as the exchange_order_id for recovery
                        # Fall through to save record below
                    else:
                        raise api_err
            else:
                sl_price, tp_price = self.risk_service.calculate_sl_tp(entry_price, side, sl_pct=sl_pct, tp_pct=tp_pct)
                self.logger.info(f"[DRY-RUN] Signal {symbol} would open {side} ({order_type}) at {entry_price}")
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
                status='PENDING' if order_type == 'limit' else 'ACTIVE',
                timeframe=timeframe,
                pos_key=pos_key,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_order_id=sl_oid if 'sl_oid' in locals() else None,
                tp_order_id=tp_oid if 'tp_oid' in locals() else None,
                exchange_order_id=str(order_res.get('id', '')),
                entry_time=int(time.time() * 1000), 
                meta={
                    'signal_confidence': conf, 
                    'rr_ratio': tp_pct / sl_pct if sl_pct > 0 else 1.0,
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'comment': signal.get('comment'),
                    'order_type': order_type
                }
            )
            
            trade_id = await self.trade_repo.save_trade(new_trade)
            
            # CRITICAL: Immediately update the in-memory state cache to prevent
            # the same order being placed again in the same heartbeat cycle.
            # Without this, sync_service cache is stale until the next sync_all() (5s later).
            if self.sync_service and hasattr(self.sync_service, 'register_pending_order'):
                order_id = str(order_res.get('id', ''))
                self.sync_service.register_pending_order(profile, symbol, side, order_id)
            
            # 10. AI Snapshot Logging
            if signal.get('snapshot'):
                from src.infrastructure.repository.database import DataManager
                db = await DataManager.get_instance()
                await db.log_ai_snapshot(trade_id, json.dumps(signal['snapshot']), conf)

            # 11. Notify User
            if order_type == 'limit':
                await self.notification_service.notify_order_pending(
                    symbol=symbol, timeframe=timeframe, side=side, price=entry_price,
                    sl=sl_price, tp=tp_price, score=conf, leverage=leverage,
                    dry_run=not adapter.can_trade, exchange=ex_name
                )
            else:
                await self.notification_service.notify_order_filled(new_trade, conf, dry_run=not adapter.can_trade)
            
            self.logger.info(f"Successfully registered {order_type.upper()} on {symbol}")
            return True

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "balance" in err_msg or "110007" in err_msg:
                self.logger.warning(f"⚠️ [MARGIN-FAIL] {symbol} failed (Bybit 110007). Creating VIRTUAL trade for AI tracking.")
                
                # Trigger Margin Throttling
                await self.cooldown_manager.handle_margin_error(acc_key, ex_name) 

                # Fallback: Save as a VIRTUAL trade so we don't lose the AI snapshot and TP/SL tracking
                new_trade = Trade(
                    profile_id=profile_id,
                    exchange=ex_name,
                    symbol=symbol,
                    side=side.upper(),
                    qty=qty,
                    entry_price=entry_price,
                    leverage=leverage,
                    status='ACTIVE', # Keep as ACTIVE so MonitorPosition picks it up
                    timeframe=timeframe,
                    pos_key=pos_key,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    exchange_order_id=f"virtual_{int(time.time())}",
                    entry_time=int(time.time() * 1000),
                    meta={
                        'signal_confidence': conf, 
                        'rr_ratio': tp_pct / sl_pct if sl_pct > 0 else 1.0,
                        'is_virtual': True, # Flag as virtual due to margin
                        'original_error': str(e)
                    }
                )
                trade_id = await self.trade_repo.save_trade(new_trade)
                
                # Log AI Snapshot even for virtual trade
                if signal.get('snapshot'):
                    from src.infrastructure.repository.database import DataManager
                    db = await DataManager.get_instance()
                    await db.log_ai_snapshot(trade_id, json.dumps(signal['snapshot']), conf)
                
                
                await self.notification_service.notify_order_filled(new_trade, conf, dry_run=not adapter.can_trade, is_virtual=True)
                return True
            else:
                self.logger.error(f"Trade execution failed for {symbol}: {e}")
                import traceback
                self.logger.debug(traceback.format_exc())
                return False
