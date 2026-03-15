import logging
from typing import List, Dict, Any
from src.application.trading.account_sync_service import AccountSyncService
from src.domain.repository import ITradeRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.cooldown_manager import CooldownManager
from src.infrastructure.adapters.base_adapter import BaseAdapter

class MonitorPositionsUseCase:
    """
    Use Case: Monitor all open positions across multiple profiles.
    Reconciles exchange state with DB and handles SL/TP or reversal closures.
    """
    def __init__(
        self, 
        sync_service: AccountSyncService, 
        trade_repo: ITradeRepository,
        risk_service: RiskService,
        notification_service: NotificationService,
        cooldown_manager: CooldownManager,
        evaluate_strategy_use_case: Any = None
    ):
        self.sync_service = sync_service
        self.trade_repo = trade_repo
        self.risk_service = risk_service
        self.notification_service = notification_service
        self.cooldown_manager = cooldown_manager
        self.evaluate_strategy_use_case = evaluate_strategy_use_case
        self.logger = logging.getLogger("MonitorPositionsUseCase")

    async def execute(self):
        """
        Runs the monitoring logic for all profiles.
        """
        for profile in self.sync_service.profiles:
            # OPTIMIZATION: Fetch active trades once per profile
            db_trades = await self.trade_repo.get_active_positions(profile['id'])
            await self._monitor_profile(profile, db_trades)
            await self._manage_pending_trades(profile, db_trades)

    async def _monitor_profile(self, profile: Dict[str, Any], db_trades: List[Any]):
        # 1. Get cached state for this profile's account
        state = self.sync_service.get_account_state(profile)
        if not state:
            return

        exchange_positions = state.get('positions', [])
        
        # 3. Reconcile
        for trade in db_trades:
            # A. Handle Virtual Trades (No exchange presence)
            if trade.meta.get('is_virtual'):
                await self._monitor_virtual_trade(profile, trade)
                continue

            # Skip PENDING trades for position ghost detection since they live in orders
            if trade.status == 'PENDING':
                continue

            # FIX D3: Match by (symbol, side, and price) for precision.
            # Stale trades in DB (e.g. ATOM at 1.8$) should NOT match a current position (ATOM at 6.5$).
            def _is_price_match(db_price, pos_dict):
                ex_price = float(pos_dict.get('entryPrice') or pos_dict.get('avgPrice') or 0)
                if not db_price or not ex_price: 
                    # If we can't compare prices, we only match if it's the ONLY trade for this symbol
                    return True 
                return abs(db_price - ex_price) / ex_price < 0.15 # 15% tolerance
                
            matched = next(
                (p for p in exchange_positions 
                 if p.get('symbol') == trade.symbol 
                 and p.get('side', '').upper() == trade.side.upper()
                 and _is_price_match(trade.entry_price, p)), 
                None
            )
            
            if not matched:
                import time
                current_time = int(time.time() * 1000)
                entry_time = trade.entry_time or 0
                
                # Grace period of 60s for newly opened positions
                if current_time - entry_time < 60000:
                    self.logger.debug(f"Trade {trade.id} ({trade.symbol}) not found on exchange, but within 60s grace period. Skipping ghost check.")
                    continue
                    
                # Ghost trade: closed on exchange but OPEN in DB
                self.logger.info(f"Trade {trade.id} ({trade.symbol}) closed on exchange. Syncing history...")
                
                # Try to fetch real exit data
                await self._resolve_ghost_trade(profile, trade)
            else:
                # 4. SELF-HEALING: Check if SL/TP are missing on exchange
                await self._heal_missing_sltp(profile, trade, matched, state)
                
        # 4. Check for Orphans and Status Transitions (Exchange positions vs DB)
        for ep in exchange_positions:
            sym = ep.get('symbol')
            # Check if this exchange position is already tracked in DB
            def _is_price_match(db_price, ex_price):
                if not db_price or not ex_price: return True
                return abs(db_price - ex_price) / ex_price < 0.10

            db_match = next(
                (t for t in db_trades 
                 if t.symbol == sym 
                 and t.side.upper() == ep.get('side', '').upper()
                 and _is_price_match(t.entry_price, float(ep.get('entryPrice', 0)))),
                None
            )
            
            if db_match:
                # BUG 18 FIX: If DB thinks it's PENDING but it's now an ACTIVE position, update status!
                if db_match.status == 'PENDING':
                    self.logger.info(f"📈 [FILL] Trade {db_match.id} ({sym}) filled! Updating PENDING -> ACTIVE.")
                    db_match.status = 'ACTIVE'
                    # Sync info from exchange
                    db_match.entry_price = float(ep.get('entryPrice') or db_match.entry_price)
                    db_match.qty = float(ep.get('contracts') or db_match.qty)
                    # We also must ensure it has an entry time for ghost checks
                    if not db_match.entry_time:
                        import time
                        db_match.entry_time = int(time.time() * 1000)
                    await self.trade_repo.save_trade(db_match)
                    
                    # Notify order filled
                    conf = db_match.meta.get('signal_confidence', 0.5) if db_match.meta else 0.5
                    is_live = str(profile.get('environment', 'LIVE')).upper() == 'LIVE'
                    await self.notification_service.notify_order_filled(db_match, conf, dry_run=not is_live)
                # Already tracked - no action needed
            else:
                # Orphan detected! Found on exchange but not in DB.
                self.logger.info(f"🕵️ [ORPHAN] Found untracked {sym} on exchange. Adopting...")
                try:
                    from src.domain.models import Trade
                    import time
                    
                    # Create a new Trade model for the orphan
                    orphan_id = f"adopted_{int(time.time())}"
                    orphan = Trade(
                        profile_id=profile['id'],
                        exchange=profile['exchange'].upper(),
                        symbol=sym,
                        side=ep['side'].upper(),
                        qty=float(ep['contracts']),
                        entry_price=float(ep['entryPrice']),
                        leverage=float(ep['leverage'] or 10),
                        status='ACTIVE',
                        timeframe='1h', # Placeholder since exchange doesn't store TF
                        pos_key=f"{profile['exchange'].upper()}_{sym.replace('/', '_')}_{orphan_id}",
                        sl_price=float(ep.get('stopLoss') or 0) or None,
                        tp_price=float(ep.get('takeProfit') or 0) or None,
                        exchange_order_id=orphan_id,
                        entry_time=int(time.time() * 1000),
                        meta={'is_orphan': True, 'adopted_at': int(time.time() * 1000)}
                    )
                    
                    await self.trade_repo.save_trade(orphan)
                    await self.notification_service.notify_generic(
                        f"🕵️ **ORPHAN ADOPTED** | {sym} | {orphan.side} | Entry: {orphan.entry_price}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to adopt orphan {sym}: {e}")

    async def _heal_missing_sltp(self, profile: Dict[str, Any], trade: Any, matched: Dict[str, Any], state: Dict[str, Any]):
        """
        Reconciles expected SL/TP with actual exchange state. 
        Adopts exchange targets if DB is invalid or missing.
        """
        ex_name = profile['exchange'].upper()
        adapter = self.sync_service.adapters.get(ex_name)
        if not adapter: return

        # Helper to validate if SL/TP are logical (Not Inverted)
        def _is_logical(side, sl, tp):
            if not sl or not tp: return True # One-sided is always OK
            if side.upper() == 'BUY':
                return sl < tp  # Inverted if SL >= TP
            else: # SELL
                return sl > tp  # Inverted if SL <= TP

        # 1. ATTACHED MODE (Bybit)
        if adapter.is_tpsl_attached_supported():
            actual_sl = matched.get('stopLoss', 0)
            actual_tp = matched.get('takeProfit', 0)
            
            # Check for inversion in DB
            db_invalid = not _is_logical(trade.side, trade.sl_price, trade.tp_price)
            
            # Scenario A: DB is invalid, but exchange HAS it -> Adopt exchange as truth
            if db_invalid and (actual_sl or actual_tp):
                self.logger.warning(f"⚠️ [HEAL] DB SL/TP for {trade.symbol} is INVERTED but exchange has values. Adopting exchange truth.")
                trade.sl_price = actual_sl if actual_sl > 0 else None
                trade.tp_price = actual_tp if actual_tp > 0 else None
                trade.sl_order_id = 'attached'
                trade.tp_order_id = 'attached'
                await self.trade_repo.save_trade(trade)
                return
            
            # Scenario B: DB is invalid and exchange is EMPTY -> Proactively clear DB to allow recalculation
            if db_invalid:
                self.logger.error(f"❌ [HEAL] DB targets for {trade.symbol} are INVERTED and exchange is empty. Clearing DB targets to rescue.")
                trade.sl_price = None
                trade.tp_price = None
                await self.trade_repo.save_trade(trade)
                return

            # Normal Repair Logic
            expected_sl = trade.sl_price
            expected_tp = trade.tp_price
            if not expected_sl and not expected_tp: return

            needs_repair = False
            if expected_sl and (not actual_sl or abs(actual_sl - expected_sl) / expected_sl > 0.001):
                needs_repair = True
            if expected_tp and (not actual_tp or abs(actual_tp - expected_tp) / expected_tp > 0.001):
                needs_repair = True
                
            if needs_repair:
                # FINAL SAFETY: Ensure we don't send something that will definitely be rejected
                # (ManagePosition handles "passed" prices, here we just check for basic logic)
                self.logger.info(f"🔧 [SELF-HEALING] {trade.symbol} repairing missing/mismatched SL/TP on {ex_name} (Expected SL:{expected_sl}, TP:{expected_tp})")
                try:
                    res = await adapter.set_position_sl_tp(trade.symbol, trade.side, sl=expected_sl, tp=expected_tp)
                    
                    if isinstance(res, dict) and res.get('info') == 'already_passed':
                        # This means Bybit rejected the SL because price already passed it.
                        # We leave sl_order_id as None so ManagePosition can trigger Rescue Close.
                        self.logger.warning(f"⚠️ [HEAL] {trade.symbol} SL/TP already passed current price. Leaving for Rescue Logic.")
                        trade.sl_order_id = None
                        trade.tp_order_id = None
                    else:
                        trade.sl_order_id = 'attached'
                        trade.tp_order_id = 'attached'
                        
                    await self.trade_repo.save_trade(trade)
                except Exception as e:
                    self.logger.error(f"Failed to repair attached SL/TP for {trade.symbol}: {e}")
        
        # 2. SEPARATE ORDERS MODE (Binance)
        else:
            orders = state.get('orders', [])
            # For Binance, if we have orphaned stop orders that match our side/qty, we should adopt them
            actual_sl_id = next((str(o['id']) for o in orders if o.get('stopPrice') and o.get('side').upper() != trade.side.upper() and 'LOSS' in str(o.get('type','')).upper()), None)
            actual_tp_id = next((str(o['id']) for o in orders if o.get('stopPrice') and o.get('side').upper() != trade.side.upper() and 'PROFIT' in str(o.get('type','')).upper()), None)
            
            if (not trade.sl_order_id and actual_sl_id) or (not trade.tp_order_id and actual_tp_id):
                self.logger.info(f"🕵️ [HEAL] Adopting orphaned stop orders for {trade.symbol} from exchange.")
                if actual_sl_id: trade.sl_order_id = actual_sl_id
                if actual_tp_id: trade.tp_order_id = actual_tp_id
                await self.trade_repo.save_trade(trade)

            # Normal Repair
            expected_sl = trade.sl_price
            expected_tp = trade.tp_price
            if not expected_sl and not expected_tp: return

            sl_found = any(str(o.get('id')) == str(trade.sl_order_id) for o in orders) if trade.sl_order_id else False
            tp_found = any(str(o.get('id')) == str(trade.tp_order_id) for o in orders) if trade.tp_order_id else False
            
            if not sl_found or not tp_found:
                # FINAL SAFETY
                if not _is_logical(trade.side, trade.entry_price, expected_sl, expected_tp):
                    self.logger.error(f"❌ [HEAL] Cannot repair {trade.symbol}: DB Targets are invalid (Inverted).")
                    return

                self.logger.info(f"🔧 [SELF-HEALING] {trade.symbol} missing stop orders on {ex_name}. Re-placing...")
                try:
                    repair_sl = expected_sl if not sl_found else None
                    repair_tp = expected_tp if not tp_found else None
                    ids = await adapter.place_stop_orders(trade.symbol, trade.side, trade.qty, sl=repair_sl, tp=repair_tp)
                    if ids.get('sl_id'): trade.sl_order_id = ids['sl_id']
                    if ids.get('tp_id'): trade.tp_order_id = ids['tp_id']
                    await self.trade_repo.save_trade(trade)
                except Exception as e:
                    self.logger.error(f"Failed to repair separate SL/TP for {trade.symbol}: {e}")

    async def _resolve_ghost_trade(self, profile: Dict[str, Any], trade: Any):
        """
        Fetches trade history to find the exit price/reason and updates DB.
        """
        ex_name = profile['exchange'].upper()
        adapter = self.sync_service.adapters.get(ex_name)
        if not adapter: return
        
        symbol = trade.symbol
        try:
            # 1. Try to fetch precise closing trade from history using orderID or symbol
            close_price = None
            reason = 'SYNC'
            
            # Anchor time: Use entry_time (fallback to 0)
            anchor_time = (trade.entry_time or 0)
            
            # Prefer using fetch_order if we have an exchange_order_id
            order_info = None
            if trade.exchange_order_id and trade.exchange_order_id != 'None':
                try:
                    order_info = await adapter.fetch_order(trade.exchange_order_id, symbol)
                except Exception:
                    pass
            
            if order_info and order_info.get('status') in ['closed', 'filled']:
                close_price = float(order_info.get('average') or order_info.get('price') or 0)
                # If average is missing, we still need to check trades
            
            if not close_price:
                # Fallback: Fetch recent trades
                trades = await adapter.fetch_my_trades(symbol, limit=20)
                target_side = 'sell' if trade.side == 'BUY' else 'buy'
                
                for t in reversed(trades or []):
                    # Match by time and side (allowing 5s leeway for entry time)
                    if t.get('timestamp', 0) >= (anchor_time - 5000) and t.get('side', '').lower() == target_side:
                        close_price = float(t.get('price', 0))
                        break
            
            exit_price = trade.entry_price # Default / Fallback
            if close_price:
                exit_price = close_price
                # Infer reason based on price movement
                if trade.side == 'BUY':
                    reason = 'SL' if exit_price < trade.entry_price else 'TP'
                else:
                    reason = 'SL' if exit_price > trade.entry_price else 'TP'
            else:
                # 2. Fallback: If exchange history is unreachable or too old, use current ticker
                try:
                    ticker = await adapter.fetch_ticker(symbol)
                    exit_price = float(ticker['last'])
                    if trade.side == 'BUY':
                        reason = 'SL' if exit_price < trade.entry_price else 'TP'
                    else:
                        reason = 'SL' if exit_price > trade.entry_price else 'TP'
                except Exception:
                    exit_price = trade.entry_price
                    reason = 'SYNC'
                self.logger.warning(f"⚠️ [GHOST] Could not find history for {symbol} (Order {trade.exchange_order_id}). Using ticker price.")

            # Trigger SL Cooldown if applicable
            if reason == 'SL':
                self.logger.info(f"[{symbol}] Sync detected SL closure. Applying SL Cooldown.")
                await self.cooldown_manager.set_sl_cooldown(ex_name, symbol, profile['id'])

            # Calculate PnL (Source of truth: entry_price, exit_price, qty)
            qty = float(trade.qty)
            leverage = float(trade.leverage or 10)
            if trade.side == 'BUY':
                pnl = (exit_price - trade.entry_price) * qty
            else:
                pnl = (trade.entry_price - exit_price) * qty
            
            margin = (trade.entry_price * qty) / leverage if (trade.entry_price and qty and leverage) else 1.0
            pnl_pct = (pnl / margin * 100) if margin > 0 else 0.0
            
            # Update DB
            await self.trade_repo.update_status(
                trade.id, 
                status='CLOSED',
                exit_reason=reason,
                exit_price=exit_price,
                pnl=pnl
            )
            
            # Notify - Using structured notification to include PnL and fixed TP/SL
            is_live = str(profile.get('environment', 'LIVE')).upper() == 'LIVE'
            await self.notification_service.notify_position_closed(
                trade=trade,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                reason=reason,
                dry_run=not is_live
            )
            
            self.logger.info(f"Resolved ghost {symbol} as {reason} at {exit_price} | PnL: ${pnl:.2f}")

        except Exception as e:
            self.logger.error(f"Failed to resolve ghost {symbol}: {e}")
            # Fallback update to prevent infinite loop
            await self.trade_repo.update_status(trade.id, status='CANCELLED', exit_reason='SYNC_ERR')

    async def _monitor_virtual_trade(self, profile: Dict[str, Any], trade: Any):
        """Monitors a virtual trade by checking ticker prices manually."""
        ex_name = profile['exchange'].upper()
        adapter = self.sync_service.adapters.get(ex_name)
        if not adapter: return

        try:
            ticker = await adapter.fetch_ticker(trade.symbol)
            current_price = float(ticker['last'])
            
            side = trade.side.upper()
            sl = trade.sl_price
            tp = trade.tp_price
            
            hit_reason = None
            if side == 'BUY':
                if sl and current_price <= sl: hit_reason = 'SL'
                elif tp and current_price >= tp: hit_reason = 'TP'
            else: # SELL
                if sl and current_price >= sl: hit_reason = 'SL'
                elif tp and current_price <= tp: hit_reason = 'TP'
                
            if hit_reason:
                self.logger.info(f"🚀 [VIRTUAL] {trade.symbol} hit {hit_reason} at {current_price}")
                
                # Calculate PnL
                qty = float(trade.qty)
                leverage = float(trade.leverage or 10)
                if side == 'BUY':
                    pnl = (current_price - trade.entry_price) * qty
                else:
                    pnl = (trade.entry_price - current_price) * qty
                
                margin = (trade.entry_price * qty) / leverage
                pnl_pct = (pnl / margin * 100) if margin > 0 else 0.0

                # Update DB
                await self.trade_repo.update_status(
                    trade.id, 
                    status='CLOSED',
                    exit_reason=f'VIRTUAL_{hit_reason}',
                    exit_price=current_price,
                    pnl=pnl
                )
                
                # Notify
                # Ensure profile environment is checked for notification label
                is_live = str(profile.get('environment', 'LIVE')).upper() == 'LIVE'
                await self.notification_service.notify_position_closed(
                    trade=trade,
                    exit_price=current_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=f'VIRTUAL_{hit_reason}',
                    dry_run=True # Virtual is always reported as simulated
                )
        except Exception as e:
            self.logger.error(f"Error monitoring virtual trade {trade.symbol}: {e}")

    async def _manage_pending_trades(self, profile: Dict[str, Any], db_trades: List[Any]):
        """
        Manages trades with 'PENDING' status in DB:
        1. Expiration: Cancel if older than 2 hours.
        2. Reversal: Cancel if signal side changed.
        """
        import time
        from src import config
        
        pending_trades = [t for t in db_trades if t.status == 'PENDING']
        
        if not pending_trades:
            return

        ex_name = profile['exchange'].upper()
        adapter = self.sync_service.adapters.get(ex_name)
        if not adapter: return

        # Get current state to see if orders still exist
        state = self.sync_service.get_account_state(profile)
        exchange_orders = state.get('orders', []) if state else []

        for trade in pending_trades:
            # 1. Existence check (robust ID + Symbol matching)
            def _order_matches(o, trade):
                sym = o.get('symbol', '')
                if sym and '/' not in sym:  # Normalize just in case
                    sym = f"{sym[:-4]}/USDT:USDT" if sym.endswith('USDT') else sym
                id_match = str(o.get('id')) == str(trade.exchange_order_id)
                # Fallback: Same symbol + same side = same pending order (for Bybit ID quirks)
                sym_match = sym == trade.symbol and o.get('side', '').upper() == trade.side.upper()
                return id_match or sym_match
                
            match = next((o for o in exchange_orders if _order_matches(o, trade)), None)
            
            # If filled or cancelled on exchange, we'll let ghost check handle status updates
            # But if it's still there, we check for expiration or reversal
            if match:
                current_time = int(time.time() * 1000)
                entry_time = trade.entry_time or 0
                
                # A. Expiration (2 hours)
                if entry_time > 0 and (current_time - entry_time) > 7200000:
                    self.logger.info(f"[{trade.symbol}] Pending order expired (2h). Cancelling...")
                    await self._cancel_pending_trade(profile, adapter, trade, "EXPIRED")
                    continue

                # B. Reversal Check
                if self.evaluate_strategy_use_case:
                    # Evaluate current signal
                    sig = await self.evaluate_strategy_use_case.execute(trade.symbol, trade.timeframe, ex_name, profile['id'])
                    if sig and sig.get('side') != 'SKIP' and sig.get('side').upper() != trade.side:
                        self.logger.info(f"[{trade.symbol}] Signal reversal detected ({trade.side} -> {sig.get('side')}). Cancelling pending...")
                        await self._cancel_pending_trade(profile, adapter, trade, "REVERSED")
                        # Apply cooldown to prevent immediate re-entry in same/next heartbeat
                        await self.cooldown_manager.set_sl_cooldown(ex_name, trade.symbol, profile['id'], custom_duration=60)
                        continue
            else:
                # GHOST PENDING: Missing from open orders. 
                # Check if it was filled (which should have been updated by _monitor_profile)
                # We re-fetch state to be absolutely sure we don't cancel a trade that just filled/transitioned
                # But since _monitor_profile runs first, if it's still PENDING in our snapshot, we check if it's in positions
                is_in_positions = any(p.get('symbol') == trade.symbol for p in (state or {}).get('positions', []))
                
                if not is_in_positions:
                    import time
                    # Small grace period for newly created pending orders (60s) 
                    # so we don't murder them if sync is slightly out of phase
                    if (int(time.time() * 1000) - (trade.entry_time or 0)) > 60000:
                        self.logger.warning(f"👻 [GHOST-PENDING] {trade.symbol} missing from exchange orders/positions. Cancelling in DB.")
                        await self.trade_repo.update_status(trade.id, 'CANCELLED', exit_reason='GHOST_PENDING')
                        await self.notification_service.notify_generic(f"👻 **GHOST PENDING CLEANUP** | {trade.symbol} | Moved to CANCELLED")

        # 3. New: Aggressive Duplicate Cleanup ("Best Order Only")
        await self._cleanup_duplicate_exchange_orders(profile, exchange_orders, db_trades)

    async def _cleanup_duplicate_exchange_orders(self, profile: Dict[str, Any], exchange_orders: List[Dict], db_trades: List[Any]):
        """
        Scans exchange orders for duplicates (same symbol + side).
        Keeps only the BEST one based on Confidence/RR, cancels others.
        """
        from collections import defaultdict
        orders_by_key = defaultdict(list)
        
        for o in exchange_orders:
            # Skip orders that are clearly Stop Loss or Take Profit
            o_type = str(o.get('type', '')).upper()
            if 'STOP' in o_type or 'TAKE_PROFIT' in o_type or o.get('stopPrice'):
                continue
                
            sym = o.get('symbol')
            side = o.get('side', '').upper()
            if not sym or not side: continue
            
            # Group by normalized symbol + side
            from src.utils.symbol_helper import to_raw_format
            key = (to_raw_format(sym), side)
            orders_by_key[key].append(o)
            
        for key, dupes in orders_by_key.items():
            if len(dupes) <= 1:
                continue
            
            # Multiple orders found for same symbol/side.
            # 1. Identify which ones correlate to DB trades and what their 'quality' is.
            scored_orders = []
            for o in dupes:
                o_id = str(o.get('id'))
                # Find matching trade in DB to get score
                db_match = next((t for t in db_trades if str(t.exchange_order_id) == o_id), None)
                
                score = 0.0
                if db_match and db_match.meta:
                    conf = db_match.meta.get('signal_confidence') or 0.3
                    rr = db_match.meta.get('rr_ratio') or 1.3
                    score = conf + (rr / 10.0)
                
                scored_orders.append({'order': o, 'score': score, 'db_match': db_match})
            
            # 2. Sort: Highest score first. If scores equal, keep oldest (first ID).
            scored_orders.sort(key=lambda x: (x['score'], -int(x['order'].get('id', 0)) if str(x['order'].get('id', '')).isdigit() else 0), reverse=True)
            
            winner = scored_orders[0]
            loosers = scored_orders[1:]
            
            self.logger.warning(f"⚔️ [DUPE-CLEANUP] Found {len(dupes)} {key[1]} orders for {key[0]}. Winner: {winner['order']['id']} (Score: {winner['score']:.2f})")
            
            adapter = self.sync_service.adapters.get(profile['exchange'].upper())
            if not adapter: continue
            
            for loose in loosers:
                l_o = loose['order']
                l_id = str(l_o.get('id'))
                l_sym = l_o.get('symbol')
                
                self.logger.info(f"   ↳ Cancelling redundant order {l_id} for {l_sym}")
                try:
                    await adapter.cancel_order(l_id, l_sym)
                    if loose['db_match']:
                        await self.trade_repo.update_status(loose['db_match'].id, 'CANCELLED', exit_reason='DUPLICATE_CLEANUP')
                except Exception as e:
                    self.logger.error(f"Failed to cancel duplicate {l_id}: {e}")

    async def _cancel_pending_trade(self, profile: Dict[str, Any], adapter: BaseAdapter, trade: Any, reason: str):
        """Helper to cancel a pending order on exchange and DB."""
        try:
            await adapter.cancel_order(trade.exchange_order_id, trade.symbol)
            await self.trade_repo.update_status(trade.id, status='CANCELLED', exit_reason=reason)
            
            # Notify
            await self.notification_service.notify_order_cancelled(
                symbol=trade.symbol,
                timeframe=trade.timeframe,
                side=trade.side,
                price=trade.entry_price,
                reason=reason,
                dry_run=not adapter.can_trade,
                exchange=profile['exchange'].upper()
            )
            self.logger.info(f"Successfully cancelled pending {trade.symbol} due to {reason}")
        except Exception as e:
            self.logger.error(f"Failed to cancel pending {trade.symbol}: {e}")
            # If not found, it might have just filled. Let ghost sync fix it later.

