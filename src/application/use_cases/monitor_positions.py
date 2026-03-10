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
        cooldown_manager: CooldownManager
    ):
        self.sync_service = sync_service
        self.trade_repo = trade_repo
        self.risk_service = risk_service
        self.notification_service = notification_service
        self.cooldown_manager = cooldown_manager
        self.logger = logging.getLogger("MonitorPositionsUseCase")

    async def execute(self):
        """
        Runs the monitoring logic for all profiles.
        """
        for profile in self.sync_service.profiles:
            await self._monitor_profile(profile)

    async def _monitor_profile(self, profile: Dict[str, Any]):
        # 1. Get cached state for this profile's account
        state = self.sync_service.get_account_state(profile)
        if not state:
            return

        exchange_positions = state.get('positions', [])
        
        # 2. Get active trades from DB for this profile
        db_trades = await self.trade_repo.get_active_positions(profile['id'])
        
        # 3. Reconcile
        for trade in db_trades:
            # A. Handle Virtual Trades (No exchange presence)
            if trade.meta.get('is_virtual'):
                await self._monitor_virtual_trade(profile, trade)
                continue

            # B. Real Trades: Find matching position on exchange
            matched = next(
                (p for p in exchange_positions if p.get('symbol') == trade.symbol), 
                None
            )
            
            if not matched:
                # Ghost trade: closed on exchange but OPEN in DB
                self.logger.info(f"Trade {trade.id} ({trade.symbol}) closed on exchange. Syncing history...")
                
                # Try to fetch real exit data
                await self._resolve_ghost_trade(profile, trade)
            else:
                # Trade still active.
                pass
                
        # 4. Check for Orphans (Exchange positions NOT in DB)
        for ep in exchange_positions:
            sym = ep.get('symbol')
            # Check if this exchange position is already tracked in DB
            db_match = next((t for t in db_trades if t.symbol == sym), None)
            
            if not db_match:
                # Orphan detected!
                self.logger.info(f"🕵️ [ORPHAN] Found untracked {sym} on exchange. Adopting...")
                try:
                    from src.domain.models import Trade
                    import time
                    
                    # Create a new Trade model for the orphan
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
                        pos_key=f"{profile['exchange'].upper()}_{sym.replace('/', '_')}_adopted",
                        sl_price=float(ep.get('stopLoss') or 0) or None,
                        tp_price=float(ep.get('takeProfit') or 0) or None,
                        exchange_order_id=f"adopted_{int(time.time())}",
                        entry_time=int(time.time() * 1000), # FIX: Include entry_time for adopted orphans
                        meta={'is_orphan': True, 'adopted_at': int(time.time() * 1000)}
                    )
                    
                    await self.trade_repo.save_trade(orphan)
                    await self.notification_service.notify_generic(
                        f"🕵️ **ORPHAN ADOPTED** | {sym} | {orphan.side} | Entry: {orphan.entry_price}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to adopt orphan {sym}: {e}")

    async def _resolve_ghost_trade(self, profile: Dict[str, Any], trade: Any):
        """
        Fetches trade history to find the exit price/reason and updates DB.
        """
        ex_name = profile['exchange'].upper()
        adapter = self.sync_service.adapters.get(ex_name)
        if not adapter: return
        
        symbol = trade.symbol
        try:
            # Fetch recent trades (lookback)
            trades = await adapter.fetch_my_trades(symbol, limit=20)
            
            # Find the most recent closing trade for this side
            target_side = 'sell' if trade.side == 'BUY' else 'buy'
            close_trade = None
            
            # Anchor time: Use entry_time (fallback to 0)
            anchor_time = (trade.entry_time or 0)
            
            for t in reversed(trades or []):
                # Simple check: trade must be roughly newer than entry
                # FIX: Handle missing entry_time gracefully
                if t.get('timestamp', 0) >= (anchor_time - 5000) and t.get('side', '').lower() == target_side:
                    close_trade = t
                    break
            
            exit_price = trade.entry_price # Default / Fallback
            reason = 'SYNC'
            pnl = 0.0
            
            if close_trade:
                exit_price = float(close_trade.get('price', exit_price))
                # Infer reason (simplified logic: if exit < entry for BUY -> SL)
                if trade.side == 'BUY':
                    reason = 'SL' if exit_price < trade.entry_price else 'TP'
                else:
                    reason = 'SL' if exit_price > trade.entry_price else 'TP'
                
                # Trigger SL Cooldown
                if reason == 'SL':
                    self.logger.info(f"[{symbol}] Sync detected SL closure. Applying SL Cooldown.")
                    await self.cooldown_manager.set_sl_cooldown(ex_name, symbol, profile['id'])

                # Calculate PnL (Simplified for notifications)
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
