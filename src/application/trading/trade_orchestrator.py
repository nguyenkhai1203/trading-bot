import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from src.infrastructure.container import Container
from src import config
from src.domain.exceptions import InsufficientFundsError

class TradeOrchestrator:
    """
    Application service that orchestrates the entire trading system.
    Replaces the main loop in bot.py.
    """
    def __init__(self, container: Container):
        self.container = container
        self.logger = logging.getLogger("TradeOrchestrator")
        self.running = False
        
    async def initialize(self):
        """Initialize all required components and services."""
        await self.container.initialize()
        self.logger.info(f"Initialized TradeOrchestrator.")

    async def start(self):
        """Main Orchestration Loop."""
        self.running = True
        self.logger.info("TradeOrchestrator started.")
        
        try:
            while self.running:
                # 1. Global State Sync
                await self.container.sync_service.sync_all()
                
                # [PHASE 9] Daily Loss Circuit Breaker
                if await self._check_daily_circuit_breaker():
                    self.logger.warning("🚫 Circuit Breaker Active: Daily loss limit reached. Skipping entries.")
                    # Still monitor/manage existing positions, but skip entry loop
                    skip_entries = True
                else:
                    skip_entries = False

                # 2. Update Market Data
                if self.container.data_manager:
                    await self.container.data_manager.update_tickers(config.TRADING_SYMBOLS)
                    await self.container.data_manager.update_data(config.TRADING_SYMBOLS, config.TRADING_TIMEFRAMES)
                
                # 3. Monitor Positions (Reconcile & Sync)
                await self.container.monitor_positions_use_case.execute()
                
                # 4. Manage Active Positions (Profit Lock / Trailing SL)
                await self._manage_active_positions()
                
                # 5. Entry Opportunity Check (Optimized)
                if not skip_entries:
                    for profile in self.container.sync_service.profiles:
                        await self._process_profile_entry(profile)
                
                # 6. Heartbeat
                print(f"[ORCHESTRATOR] Cycle complete at {time.strftime('%H:%M:%S')}")
                await asyncio.sleep(config.HEARTBEAT_INTERVAL)
                
        except Exception as e:
            self.logger.error(f"Critical error in Orchestrator loop: {e}")
        finally:
            await self.stop()

    async def _manage_active_positions(self):
        """Iterates through all active trades and checks for profit lock adjustments with TA data."""
        for profile in self.container.sync_service.profiles:
            active_trades = await self.container.trade_repo.get_active_positions(profile['id'])
            for trade in active_trades:
                try:
                    # 1. Get current price
                    ticker = await self.container.data_manager.fetch_ticker(trade.symbol, profile['exchange'])
                    if not ticker or 'last' not in ticker:
                        continue
                        
                    # 2. Get latest TA features for context (e.g. for TP extension)
                    # We use the timeframe the trade was opened on
                    ta_row = None
                    df = self.container.data_manager.get_data_with_features(trade.symbol, trade.timeframe, exchange=profile['exchange'])
                    if df is not None and not df.empty:
                        ta_row = df.iloc[-1].to_dict()
                    
                    # 3. Execute management logic
                    await self.container.manage_position_use_case.execute(profile, trade, ticker['last'], ta_data=ta_row)
                except Exception as e:
                    self.logger.error(f"Error managing position {trade.symbol}: {e}")

    async def _process_profile_entry(self, profile: Dict[str, Any]):
        """Collects all signals for a profile and executes them by priority with symbol locking."""
        symbols = config.BINANCE_SYMBOLS if profile['exchange'].upper() == 'BINANCE' else config.BYBIT_SYMBOLS
        
        # A. Parallel Evaluate all symbols
        tasks = [self._get_best_signal_guarded(profile, s) for s in symbols]
        signals = await asyncio.gather(*tasks)
        valid_signals = [s for s in signals if s and s.get('side') != 'SKIP']
        
        if not valid_signals:
            return

        # B. Sort by confidence descending
        valid_signals.sort(key=lambda x: x.get('confidence', 0), reverse=True)

        # C. Sequential Execution with Rebalancing
        for signal in valid_signals:
            symbol = signal['symbol']
            lock = self.container.get_symbol_lock(symbol)
            
            async with lock:
                try:
                    success = await self.container.execute_trade_use_case.execute(profile, signal)
                except InsufficientFundsError:
                    # REBALANCING LOGIC: If no funds, try to cancel a weaker PENDING order
                    self.logger.info(f"Insufficient funds for {symbol}. Checking for rebalancing...")
                    if await self._try_rebalance_for_better_signal(profile, signal):
                        # Retry once after rebalancing
                        try:
                            await self.container.execute_trade_use_case.execute(profile, signal)
                        except Exception:
                            pass 
                    else:
                        # No better signals or no pending to cancel, stop trying for this profile this cycle
                        break
                except Exception as e:
                    self.logger.error(f"Error executing signal for {symbol}: {e}")

    async def _get_best_signal_guarded(self, profile: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """Evaluate all timeframes for a symbol with locking for safety."""
        # Using a lock here ensures that if multiple heartbeats/tasks run, we don't evaluate the same symbol twice simultaneously
        lock = self.container.get_symbol_lock(symbol)
        async with lock:
            return await self._get_best_signal(profile, symbol)

    async def _get_best_signal(self, profile: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """Evaluate all timeframes for a symbol and return the best one."""
        signals = []
        for tf in config.TRADING_TIMEFRAMES:
            sig = await self.container.evaluate_strategy_use_case.execute(symbol, tf, profile['exchange'])
            if sig and sig.get('side') != 'SKIP':
                if 'confidence' not in sig: sig['confidence'] = 0.5
                sig['symbol'] = symbol # Ensure symbol is present
                sig['timeframe'] = tf
                signals.append(sig)
        
        if not signals: return None
        signals.sort(key=lambda x: x.get('confidence') or 0, reverse=True)
        return signals[0]

    async def _try_rebalance_for_better_signal(self, profile: Dict[str, Any], new_signal: Dict[str, Any]) -> bool:
        """Finds a pending order to cancel if the new signal is significantly better (Confidence or R/R)."""
        active_trades = await self.container.trade_repo.get_active_positions(profile['id'])
        pending_trades = [t for t in active_trades if t.status == 'PENDING']
        
        if not pending_trades:
            return False

        # Sort pending by a combined metric: Confidence + (R/R / 10)
        # This helps pick the objectively 'weakest' pending order
        def _get_score(t):
            c = t.meta.get('signal_confidence') or 0.5
            r = t.meta.get('rr_ratio') or 1.5
            return c + (r / 10.0)

        pending_trades.sort(key=_get_score)
        weakest = pending_trades[0]
        
        weak_conf = weakest.meta.get('signal_confidence') or 0.5
        weak_rr = weakest.meta.get('rr_ratio') or 1.5
        
        new_conf = new_signal.get('confidence') or 0.0
        new_sl = new_signal.get('sl_pct') or 0.02
        new_tp = new_signal.get('tp_pct') or 0.04
        new_rr = new_tp / new_sl if new_sl > 0 else 1.0
        
        # Threshold: New signal must be notably better either in confidence or risk profile
        is_better = (new_conf > weak_conf + 0.15) or (new_conf >= weak_conf and new_rr > weak_rr + 1.0)
        
        if is_better:
            self.logger.info(f"⚖️ REBALANCING: Cancelling {weakest.symbol} (Conf {weak_conf:.2f}, R/R {weak_rr:.1f}) to enter {new_signal['symbol']} (Conf {new_conf:.2f}, R/R {new_rr:.1f})")
            
            # 1. Cancel weakest on exchange
            ex_name = profile['exchange'].upper()
            adapter = self.container.get_adapter(ex_name)
            if weakest.exchange_order_id and adapter:
                try:
                    await adapter.cancel_order(weakest.exchange_order_id, weakest.symbol)
                except Exception as e:
                    self.logger.error(f"Failed to cancel {weakest.symbol} during rebalance: {e}")
                    return False
            
            # 2. Mark as CANCELLED in DB
            await self.container.trade_repo.update_status(weakest.id, 'CANCELLED', exit_reason='REBALANCED')
            
            # 3. Notify
            await self.container.notification_service.notify_order_cancelled(
                weakest.symbol, weakest.timeframe, weakest.side, weakest.entry_price, 
                "Rebalanced for better signal", dry_run=not adapter.can_trade, exchange=ex_name
            )
            return True
        
        return False

    async def _check_daily_circuit_breaker(self) -> bool:
        """Checks if today's realized loss exceeds the safety threshold (5%)."""
        total_loss = 0.0
        for profile in self.container.sync_service.profiles:
            try:
                # Get closed trades from today
                history = await self.container.trade_repo.get_trade_history(profile['id'], limit=50)
                today_start = int(time.time() // 86400 * 86400 * 1000)
                today_trades = [t for t in history if (getattr(t, 'exit_time', None) or 0) >= today_start]
                
                for t in today_trades:
                    total_loss += getattr(t, 'pnl', 0.0)
            except Exception as e:
                self.logger.error(f"Error calculating PnL for circuit breaker: {e}")

        if total_loss < 0:
            # Estimate balance (use first profile's exchange)
            try:
                ex_name = self.container.sync_service.profiles[0]['exchange'].upper()
                adapter = self.container.get_adapter(ex_name)
                balance_data = await adapter.fetch_balance()
                balance = float(balance_data.get('total', {}).get('USDT', 100.0))
            except:
                balance = 100.0
                
            loss_pct = abs(total_loss) / balance
            if loss_pct >= 0.05: # 5% Daily Loss Limit
                return True
        return False

    async def stop(self):
        self.running = False
        if self.container:
            await self.container.close()
        self.logger.info("TradeOrchestrator stopped.")
