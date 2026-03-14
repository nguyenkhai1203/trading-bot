import asyncio
import logging
import time
import os
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
                try:
                    await asyncio.wait_for(self.container.sync_service.sync_all(), timeout=60)
                except asyncio.TimeoutError:
                    self.logger.warning("🕒 Sync Timeout: Account sync took >60s. Skipping this cycle.")
                    await asyncio.sleep(5)
                    continue

                # Check for stop signal file (Graceful shutdown on Windows)
                if os.path.exists("stop_bot.txt"):
                    self.logger.info("🛑 Stop signal file detected. Shutting down gracefully...")
                    self.running = False
                    break
                
                # [PHASE 9] Daily Loss Circuit Breaker
                if await self._check_daily_circuit_breaker():
                    self.logger.warning("🚫 Circuit Breaker Active: Daily loss limit reached. Skipping entries.")
                    # Still monitor/manage existing positions, but skip entry loop
                    skip_entries = True
                else:
                    skip_entries = False

                # 2. Update Market Data
                if self.container.data_manager:
                    try:
                        await asyncio.wait_for(self.container.data_manager.update_tickers(config.DATA_SYMBOLS), timeout=30)
                        await asyncio.wait_for(self.container.data_manager.update_data(config.DATA_SYMBOLS, config.TRADING_TIMEFRAMES), timeout=120)
                        # Periodic prune (every ~10 mins)
                        if int(time.time()) % 600 < config.HEARTBEAT_INTERVAL:
                            self.container.data_manager.prune_caches(config.DATA_SYMBOLS)
                    except asyncio.TimeoutError:
                        self.logger.warning("🕒 Data Timeout: Market data refresh timed out. Skipping.")
                
                # 3. Monitor Positions (Reconcile & Sync)
                await self.container.monitor_positions_use_case.execute()
                
                # 4. Manage Active Positions (Profit Lock / Trailing SL)
                await self._manage_active_positions()
                
                # 5. Atomic Entry Opportunity Check (New BMS v2.1 Logic)
                if not skip_entries:
                    await self._process_all_entries()
                
                # 6. Heartbeat
                print(f"[ORCHESTRATOR] Cycle complete at {time.strftime('%H:%M:%S')}")
                await asyncio.sleep(config.HEARTBEAT_INTERVAL)
                
        except Exception as e:
            import traceback
            self.logger.error(f"Critical error in Orchestrator loop: {e}\n{traceback.format_exc()}")
        finally:
            await self.stop()

    async def _manage_active_positions(self):
        """Iterates through all active trades and checks for profit lock adjustments with TA data."""
        for profile in self.container.sync_service.profiles:
            active_trades = await self.container.trade_repo.get_active_positions(profile['id'])
            for trade in active_trades:
                try:
                    # 1. Get current price (leveraging the new ticker cache in DataManager)
                    ticker = await self.container.data_manager.fetch_ticker(trade.symbol, exchange=profile['exchange'])
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

    async def _process_all_entries(self):
        """
        NEW ATOMIC ORCHESTRATION:
        1. Collect signals from ALL profiles.
        2. Deduplicate by (Exchange, Symbol) - highest confidence wins.
        3. Execute winning signals sequentially with symbol locking.
        """
        all_signals = [] # List of (profile, signal)
        
        # A. Collect all signals concurrently
        for profile in self.container.sync_service.profiles:
            symbols = config.BINANCE_SYMBOLS if profile['exchange'].upper() == 'BINANCE' else config.BYBIT_SYMBOLS
            tasks = [self._get_best_signal_guarded(profile, s) for s in symbols]
            signals = await asyncio.gather(*tasks)
            for sig in signals:
                if sig and sig.get('side') != 'SKIP':
                    all_signals.append((profile, sig))

        if not all_signals:
            return

        # B. DEDUPLICATE by (Account, Symbol)
        # We allow same symbol on DIFFERENT physical accounts (multi-account isolation).
        winners = {} # {(account_key, symbol): (profile, signal)}
        for profile, sig in all_signals:
            # Use account_key for granular deduplication
            acc_key = self.container.sync_service._get_account_key(profile) if self.container.sync_service else profile['exchange'].upper()
            sym = sig['symbol']
            key = (acc_key, sym)
            
            if key not in winners or sig['confidence'] > winners[key][1]['confidence']:
                winners[key] = (profile, sig)

        # C. Sequential Execution of winners
        sorted_winners = sorted(winners.values(), key=lambda x: x[1].get('confidence', 0), reverse=True)
        
        for profile, signal in sorted_winners:
            symbol = signal['symbol']
            lock = self.container.get_symbol_lock(symbol)
            
            async with lock:
                try:
                    await self.container.execute_trade_use_case.execute(profile, signal)
                except Exception as e:
                    self.logger.error(f"Error executing signal for {symbol}: {e}")

    async def _get_best_signal_guarded(self, profile: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """Evaluate all timeframes for a symbol. No locking needed here - evaluation is read-only."""
        return await self._get_best_signal(profile, symbol)

    async def _get_best_signal(self, profile: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """Evaluate all timeframes for a symbol and return the best one."""
        signals = []
        for tf in config.TRADING_TIMEFRAMES:
            sig = await self.container.evaluate_strategy_use_case.execute(symbol, tf, profile['exchange'], profile['id'])
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
        
        is_better = (new_conf > weak_conf + 0.15) or (new_conf >= weak_conf and new_rr > weak_rr + 1.0)
        
        if is_better:
            self.logger.info(f"⚖️ REBALANCING: Cancelling {weakest.symbol} (Conf {weak_conf:.2f}, R/R {weak_rr:.1f}) to enter {new_signal['symbol']} (Conf {new_conf:.2f}, R/R {new_rr:.1f})")
            
            ex_name = profile['exchange'].upper()
            # Resolve adapter via account key (standardized logic)
            acc_key = self.container.sync_service._get_account_key(profile)
            adapter = self.container.adapters.get(acc_key)
            if not adapter:
                adapter = self.container.adapters.get(ex_name)
            
            if weakest.exchange_order_id and adapter:
                try:
                    await adapter.cancel_order(weakest.exchange_order_id, weakest.symbol)
                except Exception as e:
                    self.logger.error(f"Failed to cancel {weakest.symbol} during rebalance: {e}")
                    return False
            
            await self.container.trade_repo.update_status(weakest.id, 'CANCELLED', exit_reason='REBALANCED')
            
            await self.container.notification_service.notify_order_cancelled(
                weakest.symbol, weakest.timeframe, weakest.side, weakest.entry_price, 
                "Rebalanced for better signal", dry_run=not adapter.can_trade, exchange=ex_name
            )
            return True
        
        return False

    async def _check_daily_circuit_breaker(self) -> bool:
        """
        Checks if today's realized loss exceeds the safety threshold (5%) 
        across the TOTAL portfolio (all accounts combined).
        """
        total_pnl = 0.0
        today_start = int(time.time() // 86400 * 86400 * 1000)
        
        # 1. Sum PnL across all profiles
        for profile in self.container.sync_service.profiles:
            try:
                history = await self.container.trade_repo.get_trade_history(profile['id'], limit=200)
                today_trades = [t for t in history if (getattr(t, 'exit_time', None) or 0) >= today_start]
                
                for t in today_trades:
                    pnl = t.pnl if hasattr(t, 'pnl') else 0.0
                    total_pnl += pnl if pnl is not None else 0.0
            except Exception as e:
                self.logger.error(f"Error calculating PnL for profile {profile.get('label')}: {e}")

        # 2. Sum Balance across all UNIQUE physical accounts (Adapters)
        if total_pnl < 0:
            total_balance = 0.0
            unique_acc_keys = set()
            
            for profile in self.container.sync_service.profiles:
                acc_key = self.container.sync_service._get_account_key(profile)
                if acc_key in unique_acc_keys:
                    continue
                
                unique_acc_keys.add(acc_key)
                adapter = self.container.adapters.get(acc_key)
                if not adapter:
                    # Fallback to exchange name
                    adapter = self.container.adapters.get(profile['exchange'].upper())
                
                if adapter:
                    try:
                        balance_data = await adapter.fetch_balance()
                        total_balance += float(balance_data.get('total', {}).get('USDT', 0.0))
                    except Exception as e:
                        self.logger.warning(f"Could not fetch balance for circuit breaker check ({acc_key}): {e}")
            
            if total_balance > 0:
                loss_pct = abs(total_pnl) / total_balance
                if loss_pct >= 0.05: # Total Portfolio Daily Loss Limit: 5%
                    self.logger.critical(f"🛑 [CIRCUIT BREAKER] Total Daily Loss exceeded: {loss_pct*100:.2f}% (${total_pnl:.2f} / ${total_balance:.2f})")
                    return True
                    
        return False

    async def stop(self):
        self.running = False
        if self.container:
            await self.container.close()
        self.logger.info("TradeOrchestrator stopped.")
