import ccxt.async_support as ccxt
import logging
import json
import os
import sys
import tempfile
import asyncio
import numpy as np
from datetime import datetime

from src import config
from src.infrastructure.notifications.notification import (
    send_telegram_message,
    format_pending_order,
    format_position_filled,
    format_position_closed,
    format_order_cancelled
)
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from src.utils.symbol_helper import to_api_format, to_display_format


# Logger Adapter for Exchange Prefix
class BotJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for non-serializable types like Timestamp and numpy types."""
    def default(self, obj):
        try:
            # Handle pandas Timestamp
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            # Handle numpy integers and floats
            if isinstance(obj, (np.integer, np.floating)):
                return obj.item()
            # Handle numpy arrays
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            # Handle other types that might have to_dict
            if hasattr(obj, 'to_dict'):
                return obj.to_dict()
            return super().default(obj)
        except Exception:
            return str(obj)

class ExchangeLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return '[%s] %s' % (self.extra['exchange_name'], msg), kwargs

# Cooldown after SL (in seconds)
SL_COOLDOWN_SECONDS = 2 * 3600  # 2 hours cooldown after stop loss

class Trader:
    """
    Principal Orchestrator for trade execution and state synchronization.

    The Trader class acts as the bridge between signal-generating strategies and 
    the low-level exchange adapters. It manages internal state, enforces risk controls, 
    and delegates complex operations to specialized sub-managers:
    
    Sub-Managers:
    - OrderExecutor: Handles the lifecycle of individual orders and timeout recovery.
    - CooldownManager: Manages SL-based re-entry blocks and margin-based rejection throttling.
    - DataManager (external): Handles persistence of trade history and market data.

    Design Patterns:
    - Delegation: Most 'Place' and 'Cancel' logic is delegated to OrderExecutor.
    - Shared State: Uses class-level `_shared_account_cache` to coordinate multiple profiles.
    - Idempotency: Uses Client Order IDs for robust order recovery.
    - Reconciliation: Authoritative periodic sync with exchange API to resolve 'ghost' positions.
    """
    # Class-level shared cache for account-wide status to prevent multi-profile order duplication
    # Format: {account_key: {'pos_symbols': set(), 'open_order_symbols': set(), 'timestamp': 0}}
    _shared_account_cache = {}

    def __init__(self, exchange, db, profile_id, profile_name="Unknown", signal_tracker=None, dry_run=True, data_manager=None, env="LIVE"):
        self.exchange = exchange
        self.db = db
        self.profile_id = profile_id
        self.profile_name = profile_name
        self.signal_tracker = signal_tracker
        self.dry_run = dry_run
        self.env = env.upper()
        
        # Configure Logger with Exchange Prefix
        # exchange.name is set by BaseAdapter (e.g., 'BINANCE', 'BYBIT')
        ex_name = getattr(exchange, 'name', 'UNKNOWN')
        # Standardize: binanceusdm -> BINANCE
        if 'BINANCE' in ex_name.upper():
            ex_name = 'BINANCE'
        elif 'BYBIT' in ex_name.upper():
            ex_name = 'BYBIT'
            
        self.exchange_name = ex_name
        self.logger = ExchangeLoggerAdapter(logging.getLogger(__name__), {'exchange_name': ex_name})
        
        # Use shared MarketDataManager for time synchronization
        if data_manager is None:
            from src.data_manager import MarketDataManager
            self.data_manager = MarketDataManager(db=self.db)
        else:
            self.data_manager = data_manager
            
        # Hard isolation for Live/Test environment is handled by DataManager/DB Path
        
        # Cross-Exchange Data Buckets are no longer needed as each Trader instance 
        # is tied to exactly one DB and Profile.
        
        self.active_positions = {} # Tracked via DB, synced in initial loop or via specific call
        self.pending_orders = {}  # Track pending limit orders: {pos_key: {'order_id': id, 'symbol': symbol, 'side': side, 'price': price}}
        self._symbol_locks = {}  # Per-symbol locks to prevent entry race conditions
        self._position_locks = {}  # Per-position locks for SL/TP recreation
        
        # Performance Cache for account-level state (populated by sync_with_exchange)
        self._last_ex_pos_map = {}            # {norm_symbol: position_data}
        self._last_ex_open_order_ids = set()      # {order_id_string}
        self._last_ex_open_order_symbols = set()  # {norm_symbol}
        
        # Unique identifier for this exchange account (shared across profiles)
        self.account_key = self._get_account_key()
        
        # Initialize shared account state if not present
        if self.account_key not in self.__class__._shared_account_cache:
            self.__class__._shared_account_cache[self.account_key] = {
                'last_balance_check': 0,
                'last_balance': None,
                'margin_cooldown_until': 0,
                'leverage_cache': {}, # Issue 8: {symbol: (leverage, timestamp)}
                'pos_symbols': set(),
                'open_order_symbols': set(),
                'timestamp': 0
            }


        self.cooldown_manager = CooldownManager(db, self.logger, env)
        self.order_executor = OrderExecutor(self)
        
        self.default_leverage = config.LEVERAGE
        self._missing_order_counts = {} # { order_id: missing_cycle_count }
        self._pos_action_timestamps = {} # { pos_key_action: timestamp_ms } to prevent rapid spam
        self._last_sync_time = 0       # Throttling for sync_with_exchange (60s)
        self._last_reconcile_time = 0  # Throttling for reconcile_positions (10m)
        self._last_history_sync_time = 0 # Throttling for history_sync (1h)
        
    def _get_account_key(self):
        """Uniquely identify the account to share state across multiple profiles."""
        api_key = "DRY"
        if not self.dry_run:
            try:
                # adapter.exchange.exchange is the underlying CCXT instance
                api_key = getattr(self.exchange.exchange, 'apiKey', str(self.profile_id))
            except:
                api_key = str(self.profile_id)
        return f"{self.exchange_name}_{api_key}"


    def _get_lock(self, symbol):
        """Get or create a lock for a given symbol (for entry operations)."""
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = asyncio.Lock()
        return self._symbol_locks[symbol]
    
    def _get_position_lock(self, pos_key):
        """Get or create a lock for a given position key (for SL/TP operations)."""
        if pos_key not in self._position_locks:
            self._position_locks[pos_key] = asyncio.Lock()
        return self._position_locks[pos_key]
    
    def _safe_int(self, value, default=0):
        """Safely convert to int with fallback for None values."""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    
    def _safe_float(self, value, default=0.0):
        """Safely convert to float with fallback for None values."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    async def _execute_with_timestamp_retry(self, api_call, *args, **kwargs):
        """Execute exchange API call with timestamp error retry using this specific exchange's adapter."""
        # Use BaseExchangeClient's method directly to avoid __getattr__ proxy to raw CCXT
        from src.infrastructure.adapters.base_exchange_client import BaseExchangeClient
        res = await BaseExchangeClient._execute_with_timestamp_retry(self.exchange, api_call, *args, **kwargs)
        # Double safety check: if we somehow got a coroutine back (due to nested calls), await it.
        if asyncio.iscoroutine(res):
            return await res
        return res

    async def fetch_balance_throttled(self, force=False):
        """
        Fetches balance from exchange with throttling (default 5 minutes).
        Returns the cached balance if within the interval.
        """
        if self.dry_run:
            from src import config
            return { 'total': { 'USDT': config.SIMULATION_BALANCE } }

        now = time.time()
        shared = self.__class__._shared_account_cache.get(self.account_key)
        
        # Check if we should fetch (every 300s / 5m)
        if force or (now - shared.get('last_balance_check', 0) > 300):
            try:
                bal = await self.exchange.fetch_balance()
                shared['last_balance'] = bal
                shared['last_balance_check'] = now
                self.logger.info(f"[{self.exchange_name}] Balance updated from exchange.")
                return bal
            except Exception as e:
                self.logger.error(f"Error fetching balance: {e}")
                return shared.get('last_balance') or {}
        
        return shared.get('last_balance') or {}

    async def check_margin_error(self, error_msg, new_confidence=None):
        """
        Check if error is 'Insufficient Margin' and set a cooldown if so.
        Smart eviction: cancels only pending orders with LOWER confidence than the new signal.
        Active filled positions are logged but never force-closed (SL/TP protects them).
        Falls back to legacy single-worst cancel when new_confidence is not provided.
        """
        msg = str(error_msg).lower()
        if "insufficient margin" in msg or "-2019" in msg or "insufficient balance" in msg:
            await self.cooldown_manager.handle_margin_error(self.account_key, self.__class__._shared_account_cache, self.exchange_name)

            try:
                # All cancellable pending orders (status=pending, have an order_id)
                pending_candidates = sorted(
                    [(k, v) for k, v in self.pending_orders.items()
                     if v.get('status') == 'pending' and v.get('order_id')],
                    key=lambda x: x[1].get('entry_confidence') or 0.5
                )

                if new_confidence is not None:
                    # --- SMART EVICTION: only cancel orders WORSE than the new signal ---
                    cancelled = 0
                    for p_key, p_val in pending_candidates:
                        existing_conf = p_val.get('entry_confidence') or 0.5
                        if existing_conf < new_confidence:
                            self.logger.info(
                                f"🛡️ [EVICT] Cancelling pending {p_val.get('order_id')} "
                                f"({p_val.get('symbol')} conf={existing_conf:.2f}) "
                                f"< new signal conf={new_confidence:.2f}"
                            )
                            await self.cancel_pending_order(
                                p_key, reason="Insufficient margin (lower-quality eviction)"
                            )
                            cancelled += 1

                    # Warn about filled positions blocking capital (do NOT force-close)
                    active_worse = [
                        (k, v) for k, v in self.active_positions.items()
                        if v.get('status') == 'filled'
                        and (v.get('entry_confidence') or 0.5) < new_confidence
                    ]
                    if active_worse:
                        self.logger.warning(
                            f"⚠️ [MARGIN] {len(active_worse)} active position(s) have lower "
                            f"confidence than new signal ({new_confidence:.2f}) but won't be "
                            f"force-closed — SL/TP will guard capital."
                        )

                    if cancelled == 0:
                        self.logger.info(
                            f"[MARGIN] No pending orders worse than new signal "
                            f"(conf={new_confidence:.2f}). Cooldown active, no evictions."
                        )
                else:
                    # --- LEGACY SAFE MODE: cancel single worst order only if conf < 0.6 ---
                    if pending_candidates:
                        l_key, l_val = pending_candidates[0]  # already sorted worst-first
                        if (l_val.get('entry_confidence') or 0.5) < 0.6:
                            self.logger.info(
                                f"🛡️ [MARGIN RECOVERY] Cancelling low-confidence order "
                                f"{l_val.get('order_id')} for {l_val.get('symbol')} to free margin."
                            )
                            await self.cancel_pending_order(
                                l_key, reason="Insufficient margin (Low confidence eviction)"
                            )

            except Exception as cancel_err:
                self.logger.error(f"Error during margin-recovery eviction: {cancel_err}")

            return True
        return False

    def is_margin_throttled(self):
        """Check if we are currently in a margin-rejection cooldown."""
        return self.cooldown_manager.is_margin_throttled(self.account_key, self.__class__._shared_account_cache)

    async def sync_from_db(self):
        """Authoritative sync: Load active positions and pending orders from the database."""
        try:
            # 1. Load active positions using TradeSyncHelper
            from trade_sync_helper import TradeSyncHelper
            active_list = await self.db.get_active_positions(self.profile_id)
            self.active_positions = {}
            for row in active_list:
                pos = TradeSyncHelper.map_db_to_execution(row, default_leverage=self.default_leverage)
                pos_key = pos['pos_key']
                if not pos_key:
                     pos_key = self._get_pos_key(pos['symbol'], pos['timeframe'])
                self.active_positions[pos_key] = pos

            # 2. Update pending orders subset
            self.pending_orders = {k: v for k, v in self.active_positions.items() if v.get('status') == 'pending'}
            
            # 3. SL Cooldowns
            await self.cooldown_manager.sync_from_db(self.profile_id)
            
            self.logger.info(f"Synced {len(self.active_positions)} positions and cooldowns from DB for Profile {self.profile_id}")
        except Exception as e:
            self.logger.error(f"Failed to sync from DB: {e}")

    async def _cancel_stale_position_in_db(self, pos_key: str, reason: str = "expired"):
        """Mark a position as CANCELLED in DB when it no longer exists on the exchange.
        Must be called BEFORE deleting from active_positions so we have the trade_id."""
        pos = self.active_positions.get(pos_key)
        trade_id = pos.get('id') if pos else None
        if trade_id:
            try:
                await self.db.update_position_status(trade_id, 'CANCELLED', exit_reason=reason)
                self.logger.info(f"[DB] Marked {pos_key} (id={trade_id}) as CANCELLED ({reason})")
            except Exception as e:
                self.logger.error(f"[DB] Failed to cancel {pos_key} in DB: {e}")

    async def _update_db_position(self, pos_key):
        """Persist a single position change to the database."""
        pos = self.active_positions.get(pos_key)
        if not pos:
            return

        # Map internal status to DB status
        internal_status = pos.get('status', 'filled').lower()
        db_status = 'ACTIVE'
        if internal_status == 'pending':
            db_status = 'OPENED'
        elif internal_status in ['filled', 'active']:
            db_status = 'ACTIVE'

        try:
            from trade_sync_helper import TradeSyncHelper
            trade_data = TradeSyncHelper.map_execution_to_db(pos_key, pos, self.profile_id, self.exchange_name)
            trade_id = await self.db.save_position(trade_data)
            pos['id'] = trade_id
            
            # [AI TRAINING] Log snapshot for future training
            snapshot = pos.get('snapshot')
            if snapshot and trade_id:
                # Use confidence if available
                conf = pos.get('entry_confidence', 0.5)
                await self.db.log_ai_snapshot(trade_id, json.dumps(snapshot, cls=BotJSONEncoder), conf)
                
        except Exception as e:
            import traceback
            err_trace = traceback.format_exc()
            self.logger.error(f"Failed to update DB for {pos_key}: {e}\n{err_trace}")

    async def _clear_db_position(self, pos_key, exit_price=None, exit_reason=None):
        """Mark a position as CLOSED or CANCELLED in the database."""
        # Use active_positions or pending_orders
        # Check memory FIRST, if not found, it might have been deleted already (caller error)
        pos = self.active_positions.get(pos_key) or self.pending_orders.get(pos_key)
        
        status = 'CLOSED'
        if pos and pos.get('status') == 'pending':
            status = 'CANCELLED'
        
        # Fallback: if caller passed an exit_reason containing "Cancelled" or "Evicted", count as CANCELLED even if pos is gone
        if not pos and exit_reason and ("Cancelled" in exit_reason or "Eviction" in exit_reason):
            status = 'CANCELLED'
            
        try:
            pnl = 0
            if pos and exit_price and pos.get('entry_price'):
                fee_est = (pos['entry_price'] + exit_price) * pos['qty'] * 0.0006
                if pos['side'] == 'BUY':
                    pnl = (exit_price - pos['entry_price']) * pos['qty'] - fee_est
                else:
                    pnl = (pos['entry_price'] - exit_price) * pos['qty'] - fee_est

            # Look up internal trade ID if possible
            # We use pos_key to identify which row to close
            await self.db._execute_write("""
                UPDATE trades SET 
                    status=?, exit_price=?, pnl=?, exit_reason=?, exit_time=?
                WHERE profile_id=? AND pos_key=? AND status IN ('ACTIVE', 'OPENED')
            """, (status, exit_price, pnl, exit_reason, int(time.time() * 1000) if status == 'CLOSED' else None, 
                  self.profile_id, pos_key))
            
        except Exception as e:
            self.logger.error(f"Failed to clear DB position {pos_key}: {e}")

    async def _save_positions(self):
        """Deprecated: Use _update_db_position() for granular updates."""
        # For compatibility during migration, we can implement a bulk sync if absolutely needed,
        # but the goal is to use granular updates.
        pass

    async def _verify_or_clear_id(self, pos_key, order_id, symbol, orders_in_snap):
        """Authoritative check for order existence. Returns (verified_id, order_data)."""
        if self.dry_run:
            # In Dry Run, we cannot verify IDs against exchange.
            # We return the order_id as "verified" to allow local state to remain.
            return order_id, None
            
        if not order_id: return None, None
        
        # 1. Fast path: Check global snapshot
        for o in orders_in_snap or []:
            # Check ALL possible ID fields: id, orderId, algoId, clientAlgoId
            chk_id = str(o.get('id') or o.get('orderId') or o.get('algoId') or o.get('clientAlgoId'))
            if chk_id == str(order_id):
                self._missing_order_counts[order_id] = 0
                return order_id, o
            
        # 2. Authoritative path: Targeted fetch
        try:
            o_info = await self._execute_with_timestamp_retry(self.exchange.fetch_order, order_id, symbol)
            status = o_info.get('status', '').lower()
            if status in ['open', 'untouched', 'new', 'partially_filled']:
                self._missing_order_counts[order_id] = 0
                return order_id, o_info
            else:
                self.logger.info(f"[SYNC] Order {order_id} is {status}. Clearing from local state.")
                if order_id in self._missing_order_counts: del self._missing_order_counts[order_id]
                return None, None
        except Exception as e:
            err_str = str(e).lower()
            
            # BYBIT 500-ORDER LIMIT FIX
            if "last 500 orders" in err_str or "acknowledged" in err_str:
                try:
                    self.logger.info(f"[SYNC] {symbol} order {order_id} not in last 500. Falling back to open order scan.")
                    open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                    found_o = next((o for o in open_orders if str(o.get('id')) == str(order_id) or str(o.get('clientOrderId')) == str(order_id)), None)
                    
                    if found_o:
                        self._missing_order_counts[order_id] = 0
                        return order_id, found_o
                    else:
                        self.logger.info(f"[SYNC] Order {order_id} not in open orders and too old for Bybit Fetch/History. Clearing.")
                        if order_id in self._missing_order_counts: del self._missing_order_counts[order_id]
                        return None, None
                        
                except Exception as fb_e:
                    self.logger.warning(f"[SYNC] Fallback search failed for {order_id}: {fb_e}")

            # BUG-01 FIX: Definite missing = đã qua hết fallback của adapter
            if 'not found' in err_str or 'order does not exist' in err_str or '2013' in err_str:
                # Immediate wipe, không cần grace period
                self.logger.warning(f"[SYNC] {symbol}/{order_id}: Definite missing. Wiping ID.")
                if order_id in self._missing_order_counts: del self._missing_order_counts[order_id]
                return None, None

            # Transient error (network, rate limit, etc): keep alive
            self.logger.warning(f"[SYNC] Transient error verifying {order_id} for {symbol}: {e}")
            return order_id, None 

    def _normalize_symbol(self, symbol):
        """Standardize symbol format for reliable comparison (ARBUSDT style)."""
        return to_api_format(symbol)

    def _is_spot(self, symbol):
        """Check if a symbol is a spot market symbol on the current exchange."""
        if not symbol: return False
        try:
            if hasattr(self.exchange, 'markets') and symbol in self.exchange.markets:
                return self.exchange.markets[symbol].get('spot') is True
            # Fallback: if not loaded or not found, assume Bybit spot if no ':'
            if 'BYBIT' in self.exchange_name.upper():
                return ':' not in symbol
            return False
        except:
            return False

    def _get_unified_symbol(self, symbol):
        """Map a native or partial symbol back to its unified format (e.g. BTC/USDT:USDT)."""
        if not symbol: return ""
        try:
            # Check if already unified
            if '/' in symbol and ':' in symbol:
                return symbol
            
            # Use market data from exchange if available
            if hasattr(self.exchange, 'markets') and self.exchange.markets:
                return self.exchange.get_unified_symbol(symbol)
        except Exception:
            return symbol

    def _get_pos_key(self, symbol, timeframe=None):
        """Standardized position key: P{ID}_{EXCHANGE}_{SYMBOL}_{TF}"""
        exchange_name = self.exchange_name
        # Extract base/quote and replace slashes with underscores (e.g., BTC/USDT -> BTC_USDT)
        # Handle settlement info if present (e.g. BTC/USDT:USDT -> discard the :USDT part)
        clean_symbol = to_display_format(symbol).replace('/', '_').replace(':', '_')
        base_key = f"P{self.profile_id}_{exchange_name}_{clean_symbol}"
        if timeframe:
            return f"{base_key}_{timeframe}"
        return base_key

    def _check_min_notional(self, symbol, price, qty):
        """
        Verify if the order meets the exchange's minimum notional value requirements.
        Prevents API errors for tiny orders.
        """
        notional = price * qty
        # Use CCXT markets if available to get exact min notional (cost limit)
        min_notional = 5.0 # default fallback
        if hasattr(self.exchange, 'exchange') and hasattr(self.exchange.exchange, 'markets') and symbol in self.exchange.exchange.markets:
             market = self.exchange.exchange.markets[symbol]
             min_notional = market.get('limits', {}).get('cost', {}).get('min', 5.0)
        
        if notional < min_notional:
            return False, f"Notional {notional:.2f} < min {min_notional}", notional
        return True, "OK", notional

    def _parse_pos_key(self, pos_key):
        """
        Parses the pos_key back into its components: exchange, symbol (unified), and timeframe.
        Handles both old format (EXCHANGE_BASE_QUOTE_TF) and new format (P{id}_EXCHANGE_BASE_QUOTE_TF).
        Returns: (exchange, symbol, timeframe)
        """
        if not pos_key:
            return None, None, None
            
        parts = pos_key.split('_')
        # New format: P{profile_id}_EXCHANGE_BASE_QUOTE_TF  (e.g. P1_BINANCE_BTC_USDT_1h)
        # Old format: EXCHANGE_BASE_QUOTE_TF
        # Skip the P{id} prefix if present
        start = 1 if parts[0].startswith('P') and parts[0][1:].isdigit() else 0
        
        if len(parts) >= start + 3:
            exchange = parts[start]
            base = parts[start + 1]
            quote = parts[start + 2]
            # Reconstitute symbol (unified format BASE/QUOTE)
            symbol = f"{base}/{quote}"
            timeframe = parts[start + 3] if len(parts) > start + 3 else None
            return exchange, symbol, timeframe
        
        # Fallback for simpler or legacy keys
        return None, None, None

    def _clamp_leverage(self, lev):
        """Clamp config.LEVERAGE to allowed range (default 5-20)."""
        # Import dynamically to ensure we get latest config value
        from src import config
        
        lv = self._safe_int(lev, default=None)
        if lv is None:
            lv = self._safe_int(self.default_leverage, default=5)
            
        # 1. Clamp to global max setting (User safety preference)
        if lv > config.LEVERAGE:
            lv = config.LEVERAGE
            
        # 2. Hard limits: ensure between 1 and 20 (Exchange limits)
        return max(1, min(20, lv))

    def _debug_log(self, *parts):
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'execution_debug.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f"{self.exchange.id if hasattr(self.exchange,'id') else 'exchange'} | {str(parts)}\n")
        except Exception:
            pass
    
    async def _async_save_cooldowns(self):
        """Persist current SL cooldowns to SQLite."""
        try:
            # Filter expired before saving
            now = time.time()
            self._sl_cooldowns = {k: v for k, v in self._sl_cooldowns.items() if v > now}
            await self.db.set_risk_metric(self.profile_id, 'sl_cooldowns_json', json.dumps(self._sl_cooldowns), self.env)
        except Exception as e:
            self.logger.warning(f"Failed to save cooldowns to DB: {e}")

    async def set_sl_cooldown(self, symbol, custom_duration: Optional[int] = None):
        """Set cooldown after a stop loss hit or other rejection for this symbol."""
        await self.cooldown_manager.set_sl_cooldown(self.exchange_name, symbol, self.profile_id, custom_duration)
        
        # Immediately cancel all open orders on exchange for this symbol

        # Immediately cancel all open orders on exchange for this symbol
        # to prevent any already-submitted pending entry from filling.
        try:
            await self.cancel_all_orders(symbol)
        except Exception as e:
            self.logger.warning(f"[COOLDOWN] cancel_all_orders failed for {symbol}: {e}")

        # Purge local pending entries for this symbol (any timeframe)
        pending_keys = [
            k for k, p in list(self.active_positions.items())
            if p.get('symbol') == symbol and p.get('status') == 'pending'
        ]
        for pk in pending_keys:
            self.logger.info(f"[COOLDOWN] Removing pending entry {pk} due to cooldown")
            try:
                await self._clear_db_position(pk, exit_reason='cooldown_cancelled')
            except Exception as e:
                self.logger.warning(f"[COOLDOWN] Failed to clear DB for {pk}: {e}")
            self.active_positions.pop(pk, None)

    async def is_in_cooldown(self, symbol):
        """Check if symbol is in cooldown period after SL."""
        return self.cooldown_manager.is_in_cooldown(self.exchange_name, symbol)

    def get_cooldown_remaining(self, symbol):
        """Get remaining cooldown time in minutes."""
        return self.cooldown_manager.get_remaining_minutes(self.exchange_name, symbol)

    async def check_pending_limit_fills(self, symbol, timeframe, current_price):
        """
        [DRY RUN] Check if a pending limit order should be filled based on current price.
        Returns True if the order got filled, False otherwise.
        """
        pos_key = self._get_pos_key(symbol, timeframe)
        
        if pos_key not in self.active_positions:
            return False
        
        pos = self.active_positions[pos_key]
        
        # Only check pending limit orders
        if pos.get('status') != 'pending' or pos.get('order_type') != 'limit':
            return False
        
        side = pos['side']
        limit_price = pos['entry_price']
        
        # Check if price reached limit
        # Ensure prices are not None to avoid "<= not supported between instances of 'NoneType' and 'NoneType'"
        if current_price is None or limit_price is None:
            return False

        filled = False
        if side == 'BUY' and current_price <= limit_price:
            # Buy limit: fill when price goes down to limit or below
            filled = True
        elif side == 'SELL' and current_price >= limit_price:
            # Sell limit: fill when price goes up to limit or above
            filled = True
        
        if filled:
            pos['status'] = 'filled'
            pos['filled_at'] = current_price
            await self._update_db_position(pos_key)
            print(f"✅ [{self.exchange_name}] [DRY RUN] Limit order FILLED: {symbol} {side} @ {limit_price:.3f} (current: {current_price:.3f})")
            return True
        
        return False

    def get_pending_positions(self):
        """Get all pending limit orders."""
        return {k: v for k, v in self.active_positions.items() if v.get('status') == 'pending'}

    def get_filled_positions(self):
        """Get all filled positions."""
        return {k: v for k, v in self.active_positions.items() if v.get('status') == 'filled'}

    async def has_any_symbol_position(self, symbol):
        """
        Checks if ANY position or order for the symbol exists (across any timeframe).
        This is a 'Readiness Check' used to prevent dual-orders and timeframe conflicts.
        Now optimized to use cached exchange state from sync_with_exchange.
        """
        norm_target = self._normalize_symbol(symbol)
        
        # 1. Check local memory storage (Most authoritative for bot's own trades)
        for p in self.active_positions.values():
            if self._normalize_symbol(p.get('symbol', '')) == norm_target:
                return True
        
        for o in self.pending_orders.values():
            if self._normalize_symbol(o.get('symbol', '')) == norm_target:
                return True
        
        # 2. Check cached Exchange State (populated every ~60s by sync_with_exchange)
        # This catches manual trades or orphaned orders without spamming the API for every symbol.
        if not self.dry_run:
            # Check profile-local cache (fastest)
            if norm_target in self._last_ex_pos_map:
                return True
            if norm_target in self._last_ex_open_order_symbols:
                return True
                
            # Check SHARED Class-Level Cache (covers other profiles on same account)
            shared = self.__class__._shared_account_cache.get(self.account_key)
            if shared:
                if norm_target in shared.get('pos_symbols', set()):
                    return True
                if norm_target in shared.get('open_order_symbols', set()):
                    return True

        return False

    async def get_open_position(self, symbol, timeframe=None):
        """Checks if there's an open position for the symbol/timeframe."""
        key = self._get_pos_key(symbol, timeframe)
        # 2. Check if we have it on exchange (if live & authenticated)
        
        if self.dry_run or not self.exchange.can_trade:
            return key in self.active_positions

        try:
            # LIVE Mode: Always check exchange 
            positions = await self._execute_with_timestamp_retry(
                self.exchange.fetch_positions
            )
            found_on_exchange = False
            for pos in positions:
                if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                    found_on_exchange = True
                    # Sync details if missing
                    # Note: Exchange doesn't tell us the TF, but we check if we have a match
                    if key not in self.active_positions:
                        self.active_positions[key] = {
                            "side": pos['side'].upper(),
                            "qty": float(pos['contracts']),
                            "entry_price": float(pos['entryPrice']),
                            "timestamp": pos['timestamp'],
                            "timeframe": timeframe
                        }
                        await self._update_db_position(key)
                    break
            
            if not found_on_exchange and key in self.active_positions:
                return False
                
            return found_on_exchange
        except Exception as e:
            self.logger.error(f"Error fetching position from exchange for {symbol}: {e}")
            return key in self.active_positions

        return self.exchange.check_min_notional(symbol, price, qty)



    async def place_order(self, symbol, side, qty, timeframe=None, order_type='market', price=None, sl=None, tp=None, timeout=None, leverage=10, signals_used=None, entry_confidence=None, snapshot=None, reduce_only=False, params=None):
        """Places an order and updates persistent storage. For limit orders, monitors fill in background."""
        try:
            # Determine if TP/SL are attached (for scope safety)
            tpsl_attached = (
                getattr(self, 'exchange_name', '').upper() == 'BYBIT'
                and bool(sl) and bool(tp)
            )
            
            # Validate qty - reject invalid orders
            if qty is None or qty <= 0:
                self.logger.warning(f"Rejected order: invalid qty={qty} for {symbol}")
                return None
        
            # Dynamic decimal places based on exchange metadata
            qty = float(qty)  # Ensure native float (not np.float64) for API compatibility
            try:
                # Delegate to adapter's precision handling (handles Bybit Swap vs Spot key discrepancy)
                qty_rounded = self.exchange.round_qty(symbol, qty)
                qty_str = str(qty_rounded)
                
                # [DEBUG] Check if rounding changed it significantly
                if abs(qty - qty_rounded) > (qty * 0.001):
                    self.logger.info(f"Qty rounded: {qty} -> {qty_rounded} via adapter.round_qty")
                    
            except Exception as e:
                self.logger.warning(f"round_qty failed for {symbol}: {e}. Fallback to naive rounding.")
                # Fallback
                qty_decimals = 6 if price and price > 1000 else 3
                qty_rounded = round(qty, qty_decimals)
                qty_str = str(qty_rounded)
                
            # Validate minimum qty after rounding
            if qty_rounded <= 0:
                self.logger.warning(f"Rejected order: qty too small after rounding ({qty:.8f} -> {qty_rounded}) for {symbol}")
                print(f"⏹️ [{self.exchange_name}] [{symbol}] Qty {qty:.8f} below min step after rounding. Skipping.")
                return None

            # UPDATE QTY TO ROUNDED VALUE FOR API CALLS
            qty = qty_rounded 
            
            # STRICT NOTIONAL CHECK (Safety against exchange rejections/spam)
            price_to_check = price
            if not price_to_check or price_to_check <= 0:
                # Try to estimate price if missing (market order)
                 try:
                     ticker = await self.exchange.fetch_ticker(symbol)
                     price_to_check = ticker['last']
                 except: pass
                
            if price_to_check and price_to_check > 0:
                is_valid, reason, notional = self._check_min_notional(symbol, price_to_check, qty)
                if not is_valid:
                    msg = f"Rejected order {symbol}: {reason}"
                    self.logger.warning(msg)
                    print(f"⚠️ [{self.exchange_name}] [{symbol}] Order rejected: {reason} (Notional: ${notional:.2f})")
                    
                    # Apply SL cooldown to prevent continuous loop rejections for the same signal
                    asyncio.create_task(self.set_sl_cooldown(symbol, custom_duration=86400))
                    
                    return None
            
            exchange_name = getattr(self.exchange, 'name', self.exchange_name)
            pos_key = self._get_pos_key(symbol, timeframe)
            signals = signals_used or []
            confidence = entry_confidence or 0.5

            # Safety Enhancement: Max Daily Loss Protection
            if not reduce_only and hasattr(config, 'MAX_DAILY_LOSS_USD'):
                try:
                    # Get today's realized PnL from DataManager (DB)
                    daily_pnl = await self.db.get_daily_realized_pnl(self.profile_id)
                    if daily_pnl <= -config.MAX_DAILY_LOSS_USD:
                        self.logger.warning(f"❌ [SAFETY] Blocked {symbol} order: Daily loss threshold reached (${daily_pnl:.2f} <= -${config.MAX_DAILY_LOSS_USD:.2f})")
                        print(f"🛑 [{self.exchange_name}] Max Daily Loss hit (${daily_pnl:.2f}). Skipping.")
                        return None
                except Exception as pnl_err:
                    self.logger.warning(f"Could not verify daily PnL: {pnl_err}")

            # Symbol-level guard: reject if any position for this symbol is already active or pending.
            # pos_key is per-symbol+timeframe, but we must prevent duplicate symbol exposure
            # across ALL timeframes (e.g. NEAR 1h active + NEAR 15m trying to enter).
            if not reduce_only:
                existing = [
                    (k, p) for k, p in self.active_positions.items()
                    if p.get('symbol') == symbol and p.get('status') in ('filled', 'pending')
                ]
                if existing:
                    ek, ep = existing[0]
                    self.logger.info(
                        f"[GUARD] Blocked {symbol} new order: already have "
                        f"{ep['status'].upper()} position on {ek}"
                    )
                    return None

            # Use string quantity for API (if available) to ensure exact precision.
            # Falls back to float `qty` if string generation failed.
            api_qty = qty_str if 'qty_str' in locals() and qty_str else qty
            print(f"🔧 [{exchange_name}] Sending Order: {side} {symbol} Qty={api_qty} (Type={api_qty.__class__.__name__}) @ {price or 'MARKET'}")

            if self.dry_run or not self.exchange.can_trade:
                # For simulation notifications, we need a price even if it's a market order
                sim_price = price
                if not sim_price or sim_price <= 0:
                    sim_price = price_to_check or 0
                
                if not self.exchange.can_trade:
                    print(f"🛡️ [PUBLIC MODE] Simulating {side} {symbol} ({timeframe}) - Simulation Active.")
                
                self.logger.info(f"[SIMULATION] {side} {symbol} ({timeframe}): Qty={qty}, SL={sl}, TP={tp}")
                
                # Determine status based on order type
                is_limit = order_type == 'limit'
                status = 'pending' if is_limit else 'filled'
                
                if self.dry_run:
                    print(f"[{self.exchange_name}] [DRY RUN] Placed {side} {symbol} {timeframe} {qty} [{status.upper()}]")
                
                self.active_positions[pos_key] = {
                    "symbol": symbol,
                    "side": side.upper(),
                    "qty": qty_rounded,
                    "entry_price": round(sim_price, 3) if isinstance(sim_price, (int, float)) else sim_price,
                    "sl": round(sl, 3) if sl else None,
                    "tp": round(tp, 3) if tp else None,
                    "timeframe": timeframe,
                    "order_type": order_type,
                    "status": status,
                    "config.LEVERAGE": config.LEVERAGE,
                    "signals_used": signals,
                    "entry_confidence": confidence,
                    "snapshot": snapshot,
                    "timestamp": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
                }
                await self._update_db_position(pos_key)
                
                # Send notification
                _, tg_msg = format_pending_order(
                    symbol, timeframe, side, sim_price, sl, tp, confidence, leverage, self.dry_run,
                    exchange_name=self.exchange_name, profile_label=self.profile_name
                ) if is_limit else format_position_filled(
                    symbol, timeframe, side, sim_price, qty_rounded, sim_price * qty_rounded, sl, tp, confidence, leverage, self.dry_run,
                    exchange_name=self.exchange_name, profile_label=self.profile_name
                )
                await send_telegram_message(tg_msg)
                
                return {'id': 'dry_run_id', 'status': 'closed' if not is_limit else 'open', 'filled': qty if not is_limit else 0}

            # LIVE LOGIC
            # Prepare params
            params = params or {}
            tpsl_attached = self.exchange.is_tpsl_attached_supported() and (sl or tp)

            if sl: params['stopLoss'] = sl
            if tp: params['takeProfit'] = tp
            if reduce_only: 
                params['reduceOnly'] = True

            # Determine and clamp config.LEVERAGE to allowed range
            use_leverage = self._clamp_leverage(config.LEVERAGE)
            
            # Issue 10: Strict Spot Filtering Safeguard
            if self.exchange.is_spot(symbol):
                self.logger.error(f"❌ [ABORT] Detected Spot symbol {symbol} during Futures trade attempt on {self.exchange_name}! Aborting.")
                return None

            # Margin Throttling Check (v4.0)
            if not self.dry_run and self.is_margin_throttled():
                self.logger.warning(f"❌ [THROTTLED] Skip order for {symbol} due to recent insufficient margin.")
                return None

            # Ensure margin mode & leverage set on exchange for LIVE orders
            if not self.dry_run and self.exchange.can_trade:
                await self._ensure_isolated_and_leverage(symbol, use_leverage)
                
                # Delegate LIVE execution to OrderExecutor
                return await self.order_executor.place_order(
                    symbol, timeframe, side, qty, price, order_type, sl, tp, 
                    confidence, signals, snapshot, params, reduce_only, use_leverage, qty_rounded
                )

            self.logger.info(f"Order placed: simulation_id")
            return {'id': 'simulation'}
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            return None






    async def cancel_pending_order(self, pos_key, reason="Technical invalidation"):
        """Delegates pending order cancellation to OrderExecutor."""
        return await self.order_executor.cancel_pending_order(pos_key, reason)

    async def setup_sl_tp_for_pending(self, symbol, timeframe):
        """Delegates SL/TP setup for pending orders to OrderExecutor."""
        return await self.order_executor.setup_sl_tp_for_pending(symbol, timeframe)


    async def cancel_order(self, order_id, symbol, params={}):
        if not self.exchange.can_trade:
             self.logger.info(f"📢 [PUBLIC MODE] Simulation: Cancel Order {order_id} on {symbol}")
             return {'status': 'canceled', 'id': order_id}

        try:
            self._debug_log('cancel_order', {'id': order_id, 'symbol': symbol, 'params': params})
            res = await self._execute_with_timestamp_retry(self.exchange.cancel_order, order_id, symbol, params)
            self._debug_log('cancel_order:response', res)
            return res
        except Exception as e:
            self._debug_log('cancel_order:error', str(e))
            self.logger.error(f"❌ Cancel failed for {symbol}: {e}")
            raise e # Raise to allow _execute_with_timestamp_retry to function

    async def log_trade(self, pos_key, exit_price, exit_reason):
        """Logs a closed trade to the history file."""
        pos = self.active_positions.get(pos_key)
        if not pos:
            return

        exchange, raw_symbol, _ = self._parse_pos_key(pos_key)
        if not raw_symbol:
            raw_symbol = pos.get('symbol', 'UNKNOWN')
        
        # Format for signal_performance.json (e.g., BYBIT_NEAR_USDT instead of NEAR/USDT)
        base_quote = raw_symbol.split(':')[0].replace('/', '_').upper()
        if exchange:
            symbol = f"{exchange}_{base_quote}"
        else:
            symbol = base_quote
        entry_price = pos.get('entry_price')
        side = pos.get('side')
        qty = pos.get('qty')
        
        # Fix 2: Use actual exchange fees when available, fallback to 0.06% estimate
        actual_fees = pos.get('_exit_fees')
        if actual_fees is not None and actual_fees > 0:
            fee_est = actual_fees
        else:
            fee_est = (entry_price + exit_price) * qty * 0.0006  # 0.06% taker fee fallback
        pnl = 0
        pnl_pct = 0
        config.LEVERAGE = int(pos.get('config.LEVERAGE', 1))
        
        if isinstance(entry_price, (int, float)) and entry_price > 0:
            if side == 'BUY':
                pnl = (exit_price - entry_price) * qty - fee_est
            else:
                pnl = (entry_price - exit_price) * qty - fee_est
            
            # Use ROE (leveraged percentage) for reporting
            pnl_pct = (pnl / (entry_price * qty)) * 100 * config.LEVERAGE

        trade_record = {
            "symbol": symbol,
            "side": side,
            "entry_price": round(entry_price, 5) if isinstance(entry_price, (int, float)) else entry_price,
            "exit_price": round(exit_price, 5) if isinstance(exit_price, (int, float)) else exit_price,
            "qty": round(qty, 5),
            "pnl_usdt": round(pnl, 3),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": exit_reason,
            "entry_time": pos.get('timestamp'),
            "exit_time": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
        }

        # Fix 9 (Unified Store): Write all data to signal_performance.json via tracker.
        # 1. Fetch fees from exchange if not already stored during sync
        fees = pos.get('_exit_fees', 0)
        
        # 2. Record to database via signal_tracker
        try:
            if self.signal_tracker:
                await self.signal_tracker.record_trade(
                    symbol=symbol,
                    timeframe=pos.get('timeframe', '1h'),
                    side=side,
                    signals_used=pos.get('signals_used', []),
                    result='WIN' if pnl > 0 else 'LOSS',
                    pnl_pct=pnl_pct / 100,  # record_trade expects decimal
                    pnl_usdt=round(pnl, 3), # NEW
                    exit_price=exit_price,  # NEW
                    btc_change=0, # Optional or fetch
                    snapshot=pos.get('snapshot'),
                    pos_key=pos_key,
                    leverage=config.LEVERAGE
                )

            # Notify Telegram for ALL closed positions
            if exit_reason:
                from notification import send_telegram_message, format_position_closed
                
                # Use format_position_closed for consistent formatting
                terminal_msg, telegram_msg = format_position_closed(
                    symbol=symbol,
                    timeframe=pos.get('timeframe', '1h'),
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=exit_reason,
                    entry_time=pos.get('timestamp'),
                    profile_label=self.profile_name
                )
                print(terminal_msg)
                asyncio.create_task(send_telegram_message(telegram_msg))
            
            self.logger.info(f"Trade logged: {symbol} {side} | PnL: {pnl:.2f} ({pnl_pct:.2f}%) | Reason: {exit_reason}")

        except Exception as e:
            self.logger.warning(f"Failed to record trade in signal_tracker: {e}")

    async def remove_position(self, symbol, timeframe=None, exit_price=None, exit_reason=None):
        """Removes a position and optionally logs it to history."""
        key = self._get_pos_key(symbol, timeframe)
        if key in self.active_positions:
            if exit_price is not None:
                await self.log_trade(key, exit_price, exit_reason)
            
            # Exchange-side cleanup: Cancel all pending orders for this symbol
            self.logger.info(f"[CLEANUP] Removing {key}. Purging all exchange orders for {symbol}...")
            await self.cancel_all_orders(symbol)
            
            del self.active_positions[key]
            if key in self.pending_orders:
                del self.pending_orders[key]
            # No need to call _save_positions, status is already updated in log_trade or force_close
            self.logger.info(f"Position for {key} removed.")
            return True
        return False

    # ========== ADAPTIVE POSITION ADJUSTMENT (v2.0) ==========
    
    async def tighten_sl(self, pos_key, factor=0.5):
        """
        Tighten stop loss by moving it closer to entry price.
        
        Args:
            pos_key: Position key (e.g., 'BTC/USDT_1h')
            factor: How much to tighten (0.5 = move 50% closer to entry)
        
        Returns:
            New SL price if updated, None otherwise
        """
        if pos_key not in self.active_positions:
            return None
        
        pos = self.active_positions[pos_key]
        entry = pos.get('entry_price')
        old_sl = pos.get('sl')
        side = pos.get('side')
        symbol = pos.get('symbol')
        
        if not all([entry, old_sl, side, symbol]):
            return None
        
        # Calculate new SL
        sl_distance = abs(entry - old_sl)
        new_distance = sl_distance * (1 - factor)  # Reduce distance by factor
        
        if side == 'BUY':
            new_sl = entry - new_distance  # For long, SL below entry
        else:
            new_sl = entry + new_distance  # For short, SL above entry
        
        # Clamp to avoid moving SL past entry or in the wrong direction
        if side == 'BUY':
            new_sl = max(old_sl, min(new_sl, entry))
        else:
            new_sl = min(old_sl, max(new_sl, entry))

        if new_sl == old_sl:
            return None

        self.logger.info(f"🔄 [ADAPTIVE] Tightening SL for {pos_key}: {old_sl} -> {new_sl}")
        
        # Fix 1: Parse timeframe from pos_key so modify_sl_tp can look up the right position
        _, _, timeframe = self._parse_pos_key(pos_key)
        success = await self.modify_sl_tp(symbol, timeframe=timeframe, new_sl=new_sl)

        if success:
            # modify_sl_tp already persists sl; just mark flag and save
            pos['sl_tightened'] = True
            await self._update_db_position(pos_key)
            return round(new_sl, 4)
        return None

    async def update_dynamic_sltp(self, pos_key, df_trail=None, df_guard=None):
        """
        Multi-Timeframe Dynamic SL/TP (v4.0)
        
        Logic:
        - Layer 2: 4H (or higher) ATR Trailing Stop
        - Layer 3: Entry Timeframe RSI/EMA Guard (Early Exit)
        """
        if self.dry_run or self.exchange.is_public_only:
            return False
            
        if not config.ENABLE_DYNAMIC_SLTP:
            # Fallback to legacy profit lock if enabled
            return await self.adjust_sl_tp_for_profit_lock(pos_key, df_guard.iloc[-1]['close'] if df_guard is not None else None)

        if pos_key not in self.active_positions:
            return False
            
        pos = self.active_positions[pos_key]
        if pos.get('status') != 'filled':
            return False
            
        symbol = pos.get('symbol')
        side = pos.get('side')
        entry_price = pos.get('entry_price')
        current_sl = pos.get('sl')
        current_tp = pos.get('tp')
        
        if not all([symbol, side, entry_price, current_sl, current_tp]):
            return False

        last_row_guard = df_guard.iloc[-1] if df_guard is not None and not df_guard.empty else None
        last_row_trail = df_trail.iloc[-1] if df_trail is not None and not df_trail.empty else None
        
        if last_row_guard is None or last_row_trail is None:
            return False
            
        current_price = last_row_guard['close']
        changes = False
        
        # --- PHASE 6: Track Max PnL ---
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if side == 'BUY' else ((entry_price - current_price) / entry_price * 100)
        pos['max_pnl_pct'] = max(pos.get('max_pnl_pct', 0), pnl_pct)

        # 1. LAYER 2: ATR Trailing Stop (Higher TF)
        # We look back at last 10 candles on the trail timeframe to find recent extreme
        trail_lookback = 10
        atr_trail = last_row_trail.get('ATR_14')
        
        if atr_trail:
            # For Long: SL = max(high) - 1.5 * ATR
            # For Short: SL = min(low) + 1.5 * ATR
            if side == 'BUY':
                recent_extreme = df_trail['high'].iloc[-trail_lookback:].max()
                # Apply x2 multiplier for 1d timeframe trailing as per conversation
                multiplier = config.ATR_TRAIL_MULTIPLIER * 2 if pos.get('timeframe') == '1d' else config.ATR_TRAIL_MULTIPLIER
                new_sl = recent_extreme - (multiplier * atr_trail)
                
                # Only move SL UP
                if new_sl > current_sl:
                    # Min move threshold to avoid API spam
                    if (new_sl - current_sl) / current_sl >= config.ATR_TRAIL_MIN_MOVE_PCT:
                        pos['sl'] = round(new_sl, 5)
                        pos['sl_move_count'] = pos.get('sl_move_count', 0) + 1
                        pos['sl_original'] = pos.get('sl_original') or current_sl
                        changes = True
            else:
                recent_extreme = df_trail['low'].iloc[-trail_lookback:].min()
                multiplier = config.ATR_TRAIL_MULTIPLIER * 2 if pos.get('timeframe') == '1d' else config.ATR_TRAIL_MULTIPLIER
                new_sl = recent_extreme + (multiplier * atr_trail)
                
                # Only move SL DOWN
                if new_sl < current_sl:
                    if (current_sl - new_sl) / current_sl >= config.ATR_TRAIL_MIN_MOVE_PCT:
                        pos['sl'] = round(new_sl, 5)
                        pos['sl_move_count'] = pos.get('sl_move_count', 0) + 1
                        pos['sl_original'] = pos.get('sl_original') or current_sl
                        changes = True

        # 2. LAYER 3: Guard (Entry TF - RSI/EMA)
        rsi_guard = last_row_guard.get('RSI_14', 50)
        ema21_guard = last_row_guard.get('EMA_21')
        
        # Guard A: RSI Overbought/Oversold (Profit Protection)
        # Pull TP closer to current price to lock in 50% of remaining potential
        if side == 'BUY':
            if rsi_guard > config.RSI_OVERBOUGHT_EXIT and pnl_pct > 0.5:
                if current_tp > current_price:
                    new_tp = current_price + (current_tp - current_price) * 0.5
                    pos['tp'] = round(new_tp, 5)
                    pos['tp_tightened'] = True
                    changes = True
        else:
            if rsi_guard < (100 - config.RSI_OVERBOUGHT_EXIT) and pnl_pct > 0.5:
                if current_tp < current_price:
                    new_tp = current_price - (current_price - current_tp) * 0.5
                    pos['tp'] = round(new_tp, 5)
                    pos['tp_tightened'] = True
                    changes = True

        # Guard B: EMA21 Violation (Emergency Exit)
        if ema21_guard and pnl_pct > 0.3: # Only exit if slightly in profit
            if side == 'BUY' and current_price < ema21_guard * config.EMA_BREAK_CLOSE_THRESHOLD:
                self.logger.info(f"🚨 [GUARD] {symbol} broke EMA21. Emergency exit.")
                await self.force_close_position(pos_key, reason="EMA21 breakage guard")
                return True
            elif side == 'SELL' and current_price > ema21_guard * (2 - config.EMA_BREAK_CLOSE_THRESHOLD):
                self.logger.info(f"🚨 [GUARD] {symbol} broke EMA21. Emergency exit.")
                await self.force_close_position(pos_key, reason="EMA21 breakage guard")
                return True

        if changes:
            pos['sl_tightened'] = True
            pos['last_dynamic_update'] = self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
            await self._update_db_position(pos_key)
            self.logger.info(f"🔄 [DYNAMIC SL/TP] {symbol}: SL={pos['sl']} TP={pos['tp']}")
            
            # Recreate orders on exchange
            await self.recreate_missing_sl_tp(pos_key, recreate_sl_force=True, recreate_tp_force=True)
            return True
            
        return False
    
    async def adjust_sl_tp_for_profit_lock(self, pos_key, current_price, resistance=None, support=None, atr=None):
        """
        Check and adjust SL/TP for profit lock-in and dynamic extension (v3.0).
        
        Logic:
        - If price reaches 80% of TP, move SL to lock in 10% profit.
        - Attempt to extend TP based on Support/Resistance or ATR.
        """
        if self.dry_run or self.exchange.is_public_only:
            return False
            
        # EXCHANGE-AWARE CHECK: Skip if position belongs to a different exchange
        if self.exchange_name and self.exchange_name not in pos_key:
            return False
            
        if not config.ENABLE_PROFIT_LOCK:
            return False
            
        if pos_key not in self.active_positions:
            return False
            
        pos = self.active_positions[pos_key]
        if pos.get('status') != 'filled':
            return False
            
        entry = pos.get('entry_price')
        tp = pos.get('tp')
        sl = pos.get('sl')
        side = pos.get('side')
        
        if not all([entry, tp, sl, side]):
            return False
            
        # 1. Avoid continuous price shifting unless significant improvement
        # (We allow re-locking if we can move SL significantly better)
        # However, for now, let's just make it easier to reach by removing the hard 'return False'
        # if we have a better SL later in the code.

        # 2. Calculate Progress
        total_dist = abs(tp - entry)
        if total_dist == 0: return False
        
        current_profit_dist = abs(current_price - entry)
        # Ensure we are actually in profit before calculating progress
        if side == 'BUY' and current_price < entry: return False
        if side == 'SELL' and current_price > entry: return False
        
        progress = current_profit_dist / total_dist
        
        # Only proceed if we reached 80% threshold
        if progress < config.PROFIT_LOCK_THRESHOLD:
            return False
            
        # 3. Calculate Positive SL (Lock in config.PROFIT_LOCK_LEVEL of target profit)
        lock_amount = total_dist * config.PROFIT_LOCK_LEVEL
        if side == 'BUY':
            new_sl = entry + lock_amount
        else:
            new_sl = entry - lock_amount
            
        # 4. TA-Based TP Extension
        new_tp = tp
        extension_count = pos.get('tp_extensions', 0)
        
        if extension_count < config.MAX_TP_EXTENSIONS:
            if side == 'BUY':
                # Use resistance if available and above current TP
                if resistance and resistance > tp:
                    # Cap extension to 1.5x original total distance from entry
                    max_tp = entry + (total_dist * 1.5)
                    new_tp = min(resistance, max_tp)
                elif atr:
                    # Fallback to ATR-based extension
                    new_tp = tp + (atr * config.ATR_EXT_MULTIPLIER)
            else:
                # Use support if available and below current TP
                if support and support < tp:
                    # Cap extension to 1.5x original total distance from entry
                    max_tp = entry - (total_dist * 1.5)
                    new_tp = max(support, max_tp)
                elif atr:
                    # Fallback to ATR-based extension
                    new_tp = tp - (atr * config.ATR_EXT_MULTIPLIER)
        
        # 5. Apply Changes
        changes = False
        # Move SL into profit only if it's better than current SL
        if (side == 'BUY' and new_sl > sl) or (side == 'SELL' and new_sl < sl):
            pos['sl'] = round(new_sl, 4)
            changes = True
            
        if new_tp != tp:
            # Only update if new TP is actually farther
            if (side == 'BUY' and new_tp > tp) or (side == 'SELL' and new_tp < tp):
                pos['tp'] = round(new_tp, 4)
                pos['tp_extensions'] = extension_count + 1
                changes = True
            
        if changes:
            pos['profit_locked'] = True
            await self._update_db_position(pos_key)
            msg = f"💰 [PROFIT LOCK] {pos_key}: New SL={pos['sl']}, New TP={pos['tp']} (Prog: {progress*100:.1f}%)"
            print(msg)
            self.logger.info(msg)
            
            # Force recreate orders on exchange
            await self.recreate_missing_sl_tp(pos_key, recreate_sl_force=True, recreate_tp_force=True)
            return True
            
        return False
    
    async def force_close_position(self, pos_key, reason="Signal reversal"):
        """
        Force close a position at market price.
        
        Args:
            pos_key: Position key (e.g., 'BTC/USDT_1h') 
            reason: Reason for force close
        
        Returns:
            True if closed, False otherwise
        """
        if pos_key not in self.active_positions:
            return False
        
        pos = self.active_positions[pos_key]
        symbol = pos.get('symbol')
        side = pos.get('side')
        qty = pos.get('qty', 0)
        
        print(f"🚨 [FORCE CLOSE] {pos_key} | Reason: {reason}")
        
        if self.dry_run:
            # Dry run: just remove position
            await self.log_trade(pos_key, pos.get('entry_price', 0), reason)
            # Clear from DB while still in memory
            await self._clear_db_position(pos_key, exit_price=pos.get('entry_price', 0), exit_reason=reason)
            del self.active_positions[pos_key]
            self.logger.info(f"[DRY RUN] Force closed {pos_key}: {reason}")
            return True
        
        # Live: Close at market — delegate to adapter (handles category:linear, etc.)
        try:
            order = await self.exchange.close_position(symbol, side, qty)
            exit_price = order.get('price') or order.get('average') or pos.get('entry_price') # fallback for DB
            
            await self.log_trade(pos_key, exit_price, reason)
            
            # Clear from DB while still in memory
            await self._clear_db_position(pos_key, exit_price=exit_price, exit_reason=reason)

            del self.active_positions[pos_key]
            if pos_key in self.pending_orders:
                del self.pending_orders[pos_key]

            self.logger.info(f"[FORCE CLOSE] Closed {pos_key}: {reason}")
            return True


        except Exception as e:
            err_str = str(e).lower()
            # Handle "already closed" or "reduce-only same side" (Bybit 110017)
            is_already_closed = any(msg in err_str for msg in [
                "110017", "position is not open", "order does not exist", 
                "reduce-only", "insufficiant margin", "33004"
            ])
            
            if is_already_closed:
                self.logger.info(f"🚨 [FORCE CLOSE] IDEMPOTENT: {pos_key} already closed on exchange. Resolving actual exit details.")
                # FIX: Fetch actual exit price from history instead of using entry_price
                resolved = await self._resolve_ghost_position(pos_key, hit_likely=True)
                if resolved:
                    self.logger.info(f"✅ [FORCE CLOSE] Ghost resolved for {pos_key}. State cleared.")
                    return True
                
                # Fallback if resolution failed
                self.logger.warning(f"⚠️ [FORCE CLOSE] Could not resolve ghost {pos_key}. Clearing with entry_price fallback.")
                await self.log_trade(pos_key, pos.get('entry_price', 0), reason)
                await self._clear_db_position(pos_key, exit_price=pos.get('entry_price', 0), exit_reason=reason)
                del self.active_positions[pos_key]
                if pos_key in self.pending_orders:
                    del self.pending_orders[pos_key]
                return True

            self.logger.error(f"Failed to force close {pos_key}: {e}")
            return False
    
    def get_position_entry_confidence(self, pos_key):
        """
        Get the confidence/score when position was entered.
        Stored in position data during entry.
        """
        if pos_key not in self.active_positions:
            return None
        
        pos = self.active_positions[pos_key]
        # Confidence was stored as part of entry, extract from score
        # Entry score is typically confidence * 10
        return pos.get('entry_confidence', 0.5)
    

    async def set_mode(self, symbol, leverage):
        """Sets config.LEVERAGE and margin mode via adapter."""
        if self.dry_run: return
        await self.exchange.ensure_isolated_and_leverage(symbol, leverage)

    async def cancel_all_orders(self, symbol):
        """Cancels all active orders for a symbol."""
        if self.dry_run:
            self.logger.info(f"[DRY RUN] Cancelling all orders for {symbol}")
            return
        try:
            await self._execute_with_timestamp_retry(self.exchange.cancel_all_orders, symbol)
        except Exception as e:
            self.logger.error(f"Failed to cancel orders: {e}")

    def get_active_positions(self):
        """Trả về dict của active positions."""
        return self.active_positions

    def get_pending_orders(self):
        """Trả về dict của pending orders."""
        return self.pending_orders

    async def setup_sl_tp_for_pending(self, symbol, timeframe=None):
        """
        For all pending limit orders, setup SL/TP conditional orders.
        """
        if self.dry_run:
            self.logger.info(f"[SIMULATION] Skipping SL/TP setup for {symbol}")
            return True
        pos_key = self._get_pos_key(symbol, timeframe)
        pending = self.pending_orders.get(pos_key)
        # Fallback to persisted pending in active_positions if in-memory pending not present
        if not pending:
            active = self.active_positions.get(pos_key)
            if active and active.get('status') == 'pending':
                pending = {
                    'order_id': active.get('order_id'),
                    'symbol': active.get('symbol'),
                    'side': active.get('side'),
                    'qty': active.get('qty'),
                    'price': active.get('entry_price'),
                    'sl': active.get('sl'),
                    'tp': active.get('tp'),
                    'timeframe': active.get('timeframe')
                }
            else:
                print(f"[WARN] No pending order found for {pos_key}")
                return False
        
        qty = pending['qty']
        side = pending['side']
        sl = pending['sl']
        tp = pending['tp']
        price = pending['price']
        # Only setup if both SL/TP exist
        if not sl and not tp:
            print(f"[WARN] No SL/TP to setup for {pos_key}")
            return False
        
        # Determine close side
        close_side = 'sell' if side == 'BUY' else 'buy'
        results = {}
        try:
            # Check if SL/TP already exist (idempotency - prevent duplicates)
            existing_sl_id = pending.get('sl_order_id') or self.active_positions.get(pos_key, {}).get('sl_order_id')
            existing_tp_id = pending.get('tp_order_id') or self.active_positions.get(pos_key, {}).get('tp_order_id')
            
            # Determine how much qty to protect (cap at stored position size)
            try:
                stored_qty = float(self.active_positions.get(pos_key, {}).get('qty') or qty)
            except Exception:
                stored_qty = qty
            qty_to_use = min(float(qty), stored_qty)

            # Place SL / TP through adapter (handles all exchange-specific params internally)
            sl_to_place = sl if sl and not existing_sl_id else None
            tp_to_place = tp if tp and not existing_tp_id else None

            if existing_sl_id:
                print(f"[SKIP] SL already exists for {pos_key} (id={existing_sl_id})")
            if existing_tp_id:
                print(f"[SKIP] TP already exists for {pos_key} (id={existing_tp_id})")

            if sl_to_place or tp_to_place:
                ids = await self.exchange.place_stop_orders(
                    symbol, side, qty_to_use, sl=sl_to_place, tp=tp_to_place
                )
                sl_order_id = ids.get('sl_id')
                tp_order_id = ids.get('tp_id')

                if sl_order_id:
                    pending['sl_order_id'] = sl_order_id
                    if pos_key in self.pending_orders:
                        self.pending_orders[pos_key]['sl_order_id'] = sl_order_id
                    if pos_key in self.active_positions:
                        self.active_positions[pos_key]['sl_order_id'] = sl_order_id
                        await self._update_db_position(pos_key)
                    print(f"[SETUP] SL placed for {symbol} @ {sl_to_place} (id={sl_order_id})")
                elif sl_to_place:
                    self.logger.error(f"[SL FAILED] No order ID returned for {symbol}")
                    print(f"❌ [SL FAILED] Exchange did not return order ID")

                if tp_order_id:
                    pending['tp_order_id'] = tp_order_id
                    if pos_key in self.pending_orders:
                        self.pending_orders[pos_key]['tp_order_id'] = tp_order_id
                    if pos_key in self.active_positions:
                        self.active_positions[pos_key]['tp_order_id'] = tp_order_id
                        await self._update_db_position(pos_key)
                    print(f"[SETUP] TP placed for {symbol} @ {tp_to_place} (id={tp_order_id})")
                elif tp_to_place:
                    self.logger.error(f"[TP FAILED] No order ID returned for {symbol}")
                    print(f"❌ [TP FAILED] Exchange did not return order ID")
                
            return results
        except Exception as e:
            print(f"[ERROR] Failed to setup SL/TP for {pos_key}: {e}")
            return False

    async def _ensure_isolated_and_leverage(self, symbol, leverage):
        """Delegates to adapter's ensure_isolated_and_leverage with 1h caching (Issue 8)."""
        if self.dry_run or not self.exchange.can_trade:
            return
            
        shared = self.__class__._shared_account_cache.get(self.account_key, {})
        cache = shared.setdefault('leverage_cache', {})
        
        cached_val = cache.get(symbol)
        now = time.time()
        
        # If cached and matches and less than 1 hour old, skip API call
        if cached_val and cached_val[0] == leverage and (now - cached_val[1] < 3600):
            return
            
        await self.exchange.ensure_isolated_and_leverage(symbol, leverage)
        
        # Update cache
        cache[symbol] = (leverage, now)
        shared['leverage_cache'] = cache
        self.__class__._shared_account_cache[self.account_key] = shared
        self.logger.debug(f"[CACHE] Updated leverage cache for {symbol}: {leverage}x")

    async def _binance_batch_create(self, symbol, orders):
        """Redirected to adapter's batch_create_orders."""
        return await self.exchange.batch_create_orders(orders)

    async def _create_sl_tp_orders_for_position(self, pos_key):
        """Create SL/TP reduce-only orders for a filled position and persist the order ids."""
        if self.dry_run or not self.exchange.can_trade:
            return {'skipped': True}
            
        # Respect the global flag: if auto-create disabled, skip creating orders here.
        if not config.AUTO_CREATE_SL_TP:
            self.logger.info(f"config.AUTO_CREATE_SL_TP disabled; skipping auto SL/TP for {pos_key}")
            return {'skipped': True}

        position = self.active_positions.get(pos_key)
        if not position:
            return None

        symbol = position.get('symbol')
        qty = position.get('qty')
        side = position.get('side')
        sl = position.get('sl')
        tp = position.get('tp')

        # Check if SL/TP already attached (Bybit V5 native check)
        if self.exchange_name == 'BYBIT' and symbol:
            ex_pos = self._last_ex_pos_map.get(self._normalize_symbol(symbol))
            if ex_pos:
                has_sl = float(ex_pos.get('stopLoss') or 0) > 0
                has_tp = float(ex_pos.get('takeProfit') or 0) > 0
                if has_sl or has_tp:
                    self.logger.info(f"✅ [SKIP] {symbol} already has attached SL/TP on Bybit. Skipping separate order creation.")
                    # Update local state so we don't keep checking
                    if has_sl: position['sl_order_id'] = 'attached'
                    if has_tp: position['tp_order_id'] = 'attached'
                    return {'skipped': True, 'reason': 'attached'}

        close_side = 'sell' if side == 'BUY' else 'buy'
        results = {}
        try:
            try:
                stored_qty = float(position.get('qty') or qty)
            except Exception:
                stored_qty = qty
            qty_to_use = min(float(qty), stored_qty)

            # Place SL and/or TP via adapter (all exchange-specific logic encapsulated there)
            ids = await self.exchange.place_stop_orders(
                symbol, side, qty_to_use,
                sl=sl if sl else None,
                tp=tp if tp else None
            )

            if ids.get('sl_id'):
                position['sl_order_id'] = ids['sl_id']       # key matches _update_db_position
                results['sl_order'] = {'id': ids['sl_id']}
                print(f"[AUTO-SL] Placed SL for {pos_key} id={ids['sl_id']} @ {sl}")
            if ids.get('tp_id'):
                position['tp_order_id'] = ids['tp_id']       # key matches _update_db_position
                results['tp_order'] = {'id': ids['tp_id']}
                print(f"[AUTO-TP] Placed TP for {pos_key} id={ids['tp_id']} @ {tp}")

            # Persist order ids to positions
            self.active_positions[pos_key] = position
            await self._update_db_position(pos_key)
            return results
        except Exception as e:
            print(f"DEBUG: OrderExecutor.place_order Exception: {e}")
            import traceback
            traceback.print_exc()
            self.logger.error(f"Failed to create SL/TP for {pos_key}: {e}")
            return None

    async def verify_symbol_state(self, symbol):
        """
        Verify if there are any active positions or open orders for the symbol on exchange.
        This acts as a 'Pre-Trade Check' to prevent Dual Orders.
        """
        # In Dry Run or Public Mode, we trust our local state
        if self.dry_run or not self.exchange.can_trade:
            # Assume clean state in dry/public mode if not in local storage
            return {
                'active_exists': False,
                'order_exists': False,
                'position': None,
                'orders': []
            }
        try:
            # Check Open Orders
            open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
            
            # Check Active Positions
            positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions)
            active_pos = None
            for p in positions:
                if p['symbol'] == symbol and float(p.get('contracts', 0)) > 0:
                    active_pos = p
                    break
            
            return {
                'active_exists': bool(active_pos),
                'order_exists': bool(open_orders),
                'position': active_pos,
                'orders': open_orders
            }
        except Exception as e:
            self.logger.error(f"Failed to verify state for {symbol}: {e}")
            # If verify fails, assume something exists to be safe (fail-safe)
            return None

    # ------------------------------------------------------------------------
    # STATE SYNCHRONIZATION (RECONCILIATION)
    # ------------------------------------------------------------------------
    async def sync_with_exchange(self):
        """
        Reconcile local state with exchange. 
        Focuses on closing positions that were closed externally (TP/SL).
        Integrated into the main bot loop for persistent accuracy.
        """
        if self.dry_run: return
    
        # 0. Throttling: only sync once every 60 seconds to avoid rate limits
        now = time.time()
        if now - self._last_sync_time < 60:
            return
        self._last_sync_time = now
        
        self.logger.info(f"📡 [SYNC] Starting authoritative sync with {self.exchange_name}...")
        
        # 1. Fetch current positions and open orders from exchange
        try:
            exchange_positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions)
            # Normalize: only non-zero positions
            ex_pos_map = {self._normalize_symbol(p['symbol']): p 
                          for p in exchange_positions 
                          if self._safe_float(p.get('contracts', 0) or p.get('qty', 0)) != 0}
            
            # Fetch all open orders once to check for TP/SL presence
            open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders)
            open_order_ids = {str(o.get('id') or o.get('orderId')) for o in open_orders}
            
            # Update caches for has_any_symbol_position logic
            self._last_ex_pos_map = ex_pos_map
            self._last_ex_open_order_ids = open_order_ids
            self._last_ex_open_order_symbols = {self._normalize_symbol(o.get('symbol', '')) for o in open_orders}
            
            # Update SHARED Class-Level Cache for multi-profile safety
            self.__class__._shared_account_cache[self.account_key] = {
                'pos_symbols': set(ex_pos_map.keys()),
                'open_order_symbols': self._last_ex_open_order_symbols,
                'timestamp': time.time()
            }
        except Exception as e:
            self.logger.error(f"Sync failed to fetch exchange data: {e}")
            return

        # 2. Iterate through local active positions
        for pos_key, pos in list(self.active_positions.items()):
            # Corrected Prefix Check: pos_key format "P1_BINANCE_BTC_USDT_1h"
            # We must verify this key belongs to OUR profile and exchange
            prefix = f"P{self.profile_id}_{self.exchange_name}_"
            if not pos_key.startswith(prefix):
                continue
                
            symbol = pos.get('symbol')
            norm_symbol = self._normalize_symbol(symbol)
            status = pos.get('status') # 'filled' or 'pending'
            timeframe = pos.get('timeframe', '1h')
            
            # Heuristic: Get current price for crossing check from CACHE (no API call)
            current_price = 0.0
            try:
                # Try to get from MarketDataManager data_store (updated by bot loop)
                # data_store keys are like "BINANCE_BTC/USDT_1h"
                cache_key = f"{self.exchange_name}_{symbol}_{timeframe}"
                df = self.data_manager.data_store.get(cache_key)
                if df is not None and not df.empty:
                    current_price = float(df.iloc[-1]['close'])
                else:
                    # Fallback: if not in cache, we just don't have price for crossing check (ignore check)
                    pass
            except: pass

            # CASE 1: ACTIVE POSITION
            if status == 'filled':
                if norm_symbol not in ex_pos_map:
                    # GHOST DETECTED
                    sl_id = pos.get('sl_order_id')
                    tp_id = pos.get('tp_order_id')
                    tp_price = pos.get('tp')
                    sl_price = pos.get('sl')
                    side = pos.get('side')
                    
                    # Heuristic: Did price cross TP/SL?
                    hit_likely = False
                    if current_price > 0:
                        if side == 'BUY':
                            if tp_price and current_price >= tp_price: hit_likely = True
                            elif sl_price and current_price <= sl_price: hit_likely = True
                        else: # SELL
                            if tp_price and current_price <= tp_price: hit_likely = True
                            elif sl_price and current_price >= sl_price: hit_likely = True

                    # Check if SL/TP orders are still in open_orders
                    sl_still_open = str(sl_id) in open_order_ids if sl_id else False
                    tp_still_open = str(tp_id) in open_order_ids if tp_id else False
                    
                    if not sl_still_open and not tp_still_open:
                         self.logger.info(f"👻 [SYNC] {symbol} position gone and TP/SL orders missing. Resolving...")
                    elif hit_likely:
                         self.logger.info(f"👻 [SYNC] {symbol} position gone and price crossed target ({current_price}). Resolving...")
                    
                    # Resolve via history (Definitive verification)
                    # DEDUP GUARD: only resolve if still in active_positions (not already resolved by concurrent task)
                    if pos_key in self.active_positions:
                        await self._resolve_ghost_position(pos_key, hit_likely)

            # CASE 2: PENDING ORDER
            elif status == 'pending':
                order_id = pos.get('order_id')
                if order_id and str(order_id) not in open_order_ids:
                    # Pending order gone. Check if filled or cancelled.
                    if norm_symbol in ex_pos_map:
                         # Filled!
                         self.logger.info(f"📈 [SYNC] Pending order {order_id} for {symbol} filled externally.")
                         pos['status'] = 'filled'
                         ex_p = ex_pos_map[norm_symbol]
                         pos['entry_price'] = self._safe_float(ex_p.get('entryPrice'), pos.get('entry_price'))
                         pos['qty'] = self._safe_float(ex_p.get('contracts'), pos.get('qty'))
                         await self._update_db_position(pos_key)
                    else:
                         # Resolve via history
                         await self._resolve_ghost_position(pos_key, hit_likely=False, is_pending=True)


    def _infer_exit_reason(self, close_trade: dict, pos: dict) -> str:
        """Delegates exit reason inference to adapter."""
        return self.exchange.infer_exit_reason(close_trade, pos)

    async def _resolve_ghost_position(self, pos_key, hit_likely=False, is_pending=False):
        """Helper to find closing trade in history and update DB/memory."""
        pos = self.active_positions.get(pos_key)
        if not pos: return
        
        symbol = pos['symbol']
        side = pos['side']
        entry_time = pos.get('timestamp') or 0
        
        try:
            # Ghost Resolution (Issue 2): Using 24h lookback and pagination
            since = (entry_time - 86400000) if entry_time > 0 else (int(time.time() * 1000) - 86400000)
            target_side = 'sell' if side == 'BUY' else 'buy'
            close_trade = None
            
            all_trades = []
            current_since = since
            
            # Pagination loop for deep history search
            max_pages = 5
            for page in range(max_pages):
                self.logger.debug(f"[SYNC] Fetching trades for {symbol} (Page {page+1}, since={current_since})")
                trades = await self._execute_with_timestamp_retry(self.exchange.fetch_my_trades, symbol, since=current_since)
                if not trades:
                    break
                
                all_trades.extend(trades)
                # If we got a full page (assuming 1000), fetch next starting from last trade's timestamp + 1ms
                if len(trades) >= 1000:
                    current_since = trades[-1]['timestamp'] + 1
                else:
                    break
                # Short sleep to prevent rate limits during intense pagination
                await asyncio.sleep(0.5)

            # Find the most recent closing trade for this side
            for t in reversed(all_trades):
                # Trade must be newer than entry (with small buffer for time drift)
                if t['timestamp'] >= (entry_time - 1000) and t['side'].lower() == target_side:
                    close_trade = t
                    break
            
            if close_trade:
                exit_price = self._safe_float(close_trade['price'])
                exit_time = close_trade['timestamp']
                
                # Update DB and Memory
                reason = self._infer_exit_reason(close_trade, pos)
                if is_pending: reason = "SYNC(Fast Fill+Exit)"
                
                # Apply cooldown if SL detected
                if reason == 'SL' and not is_pending:
                    self.logger.info(f"[{symbol}] Sync detected SL closure. Applying SL Cooldown.")
                    await self.set_sl_cooldown(symbol)

                # 1. Update DB 'trades' table status
                await self._clear_db_position(pos_key, exit_price=exit_price, exit_reason=reason)
                # 2. Log to signal_tracker, notify Telegram, and remove from memory/cleanup orders
                await self.remove_position(symbol, pos.get('timeframe'), exit_price=exit_price, exit_reason=reason)
                
                self.logger.info(f"✅ [SYNC] Resolved {symbol} at {exit_price} ({reason})")
            else:
                if is_pending:
                    self.logger.info(f"🚫 [SYNC] Pending order {pos.get('order_id')} for {symbol} was cancelled externally.")
                    await self._cancel_stale_position_in_db(pos_key, reason="SYNC(External Cancel)")
                    if pos_key in self.active_positions: del self.active_positions[pos_key]
                else:
                    self.logger.warning(f"⚠️ [SYNC] Could not find closing trade for ghost {symbol} in history. Preserving.")
        except Exception as e:
            self.logger.error(f"Error resolving ghost {pos_key}: {e}")

    async def deep_history_sync(self, lookback_hours=24):
        """
        Exhaustive sync scanning history for all active positions to catch missed closures.
        Runs periodically (e.g., every 1 hour) with Issue 4 rate limiting.
        """
        now = time.time()
        # Issue 4: Throttling deep sync to once per hour
        if now - self._last_history_sync_time < 3600:
            self.logger.debug("[SYNC] Skipping deep sync (last run < 1h ago)")
            return
            
        self._last_history_sync_time = now
        self.logger.info(f"🔄 [SYNC] Starting deep history sync (lookback {lookback_hours}h)...")
        
        # 1. Fetch all active positions in DB
        db_active = await self.db.get_active_positions(self.profile_id)
        if not db_active:
            self.logger.debug("[SYNC] No active positions in DB for deep sync.")
            return
            
        active_map = {f"{trade['symbol']}_{trade['side']}": trade for trade in db_active}
        since = int((time.time() - (lookback_hours * 3600)) * 1000)
        
        total_fixed = 0
        symbols_to_check = {t['symbol'] for t in db_active}
        
        # 2. Check history for each active symbol
        for symbol in symbols_to_check:
            try:
                # Use retry wrapper for stability
                trades = await self._execute_with_timestamp_retry(self.exchange.fetch_my_trades, symbol, since=since)
                if not trades: 
                    await asyncio.sleep(0.5)
                    continue
                
                trades.sort(key=lambda x: x['timestamp'])
                
                for t in trades:
                    # ... (logic remains same)
                    side = t['side'].upper() 
                    price = self._safe_float(t['price'])
                    qty = self._safe_float(t['amount'])
                    ts = t['timestamp']
                    
                    opp_side = 'SELL' if side == 'BUY' else 'BUY'
                    key = f"{symbol}_{opp_side}"
                    
                    if key in active_map:
                        active_trade = active_map[key]
                        if ts > (active_trade.get('entry_time') or 0):
                            self.logger.info(f"[SYNC] Found deep EXIT for {symbol} {opp_side}: Price {price}")
                            p_key = active_trade.get('pos_key')
                            
                            # DEDUP GUARD: ONLY resolve if NOT in active_positions (i.e. truly an orphan in DB)
                            # Regular sync (every minute) handles memory-resident positions.
                            if p_key and p_key in self.active_positions:
                                self.logger.debug(f"[SYNC] Deep sync: {p_key} is currently live in memory, skipping deep resolution.")
                                del active_map[key]
                                continue
                            
                            actual_reason = self._infer_exit_reason(t, active_trade)
                            if actual_reason == 'SL':
                                self.logger.info(f"[{symbol}] Deep sync detected SL closure. Applying SL Cooldown.")
                                await self.set_sl_cooldown(symbol)
                                
                            await self._clear_db_position(p_key, exit_price=price, exit_reason=actual_reason)
                            await self.remove_position(symbol, active_trade.get('timeframe', 'sync'), exit_price=price, exit_reason=actual_reason)
                            total_fixed += 1
                            del active_map[key]
                
                # Issue 4: Random delay after each symbol fetch to prevent rate limit spikes (429)
                await asyncio.sleep(1 + (time.time() % 2))
            except Exception as e:
                self.logger.error(f"Error in deep sync for {symbol}: {e}")
                await asyncio.sleep(2)
                
        if total_fixed > 0:
            self.logger.info(f"✨ [SYNC] Deep sync completed. Fixed {total_fixed} positions.")
        else:
            self.logger.debug("[SYNC] Deep sync completed. No issues found.")

    async def reconcile_positions(self, auto_fix=True, force_verify=False):
        """
        Synchronizes local state with exchange reality.
        force_verify: If True, bypasses 3-cycle sync wait for missing positions.
        
        Args:
            auto_fix: If True, attempt to adopt orphan positions and orders.
            force_verify: If True, bypass the missing_cycles wait and check history immediately.
        """
        self._debug_log('reconcile_positions:start')
        if self.dry_run or self.exchange.is_public_only:
            return
        """
        - Recover missing order_ids for pending entries
        - Ensure filled positions have SL/TP orders on-exchange
        - AUTO-ADOPT: If position exists on exchange but not locally, add it.
        - AUTO-FIX: If SL/TP missing on exchange, recreate them.
        """
        summary = {
            'recovered_order_ids': 0, 
            'created_tp_sl': 0, 
            'adopted_positions': 0, 
            'adopted_orders': 0,
            'orphans_cancelled': 0,
            'errors': []
        }
        
        # 1. FETCH GLOBAL EXCHANGE STATE (Atomic snapshot)
        fetch_success = False
        ex_positions = []
        active_ex_pos = {}
        all_exchange_orders = [] # Shared global order book
        
        # SKIP SYNC IF DRY RUN AND NO API KEY
        if self.dry_run and not self.exchange.apiKey:
            self.logger.info("[SYNC] Dry run without API key: Skipping exchange fetch (simulating clean state)")
            return summary

        try:
            # Fix 12: Let each Adapter (Bybit/Binance) handle its own params.
            # Do NOT pass exchange-specific params here (e.g. {'type': 'future'} is Binance-only and breaks Bybit).
            ex_positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions)
            # Normalize keys for reliable lookups
            # Robust filter: check contracts, amount, and size in info (include absolute value for SHORTs)
            active_ex_pos = {}
            for p in ex_positions:
                qty = abs(self._safe_float(p.get('contracts') or p.get('amount') or p.get('info', {}).get('positionAmt'), 0))
                if qty > 0:
                    active_ex_pos[p['symbol']] = p
            
            # 1. Fetch TOTAL Open Orders (Standard + Algo via Adapter)
            try:
                # Adapter handles both Std + Algo merging internally
                all_exchange_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders)
                
                if all_exchange_orders:
                    self.logger.info(f"[SYNC] Total visibility: {len(all_exchange_orders)} orders (Std + Algo)")
            except Exception as e:
                self.logger.warning(f"[SYNC] Total visibility fetch failed: {e}. Falling back to per-symbol fetch later.")
                all_exchange_orders = None

            # Populate local and shared caches for high-performance readiness checks
            self._last_ex_pos_map = {self._normalize_symbol(s): p for s, p in active_ex_pos.items()}
            self._last_ex_open_order_ids = {str(o.get('id') or o.get('orderId')) for o in all_exchange_orders} if all_exchange_orders else set()
            self._last_ex_open_order_symbols = {self._normalize_symbol(o.get('symbol', '')) for o in all_exchange_orders} if all_exchange_orders else set()
            
            # Shared cache update
            self.__class__._shared_account_cache[self.account_key] = {
                'pos_symbols': set(self._last_ex_pos_map.keys()),
                'open_order_symbols': self._last_ex_open_order_symbols,
                'timestamp': time.time()
            }

            fetch_success = True
            # Fix 4: Removed duplicate adoption block here.
            # The comprehensive adoption logic below (section 2) handles this correctly
            # with config.LEVERAGE fetching, SL/TP discovery, and side normalisation.
        except Exception as e:

            self.logger.error(f"[SYNC] Critical fetch failed: {e}")
            summary['errors'].append(f"Sync failed (fetch error): {e}")

        # 2. ADOPT POSITIONS (Ex -> Local) - Only if fetch succeeded
        if fetch_success:
            for original_sym, p in active_ex_pos.items():
                # Check if we have this symbol in any timeframe
                norm_sym = self._normalize_symbol(original_sym)
                unified_sym = self._get_unified_symbol(original_sym)
                found = False
                for k, local_p in self.active_positions.items():
                    # Prefix check happened at load time, here we check normalized symbol
                    if self._normalize_symbol(local_p.get('symbol')) == norm_sym:
                        found = True
                        break
                
                if not found:
                    # Bot found a position it didn't know about - Adopt it!
                    # Initialize prefix for adopted key with standardized generator
                    pos_key = self._get_pos_key(original_sym, "adopted")
                    self.logger.info(f"[ADOPT] Found unknown position for {original_sym} ({unified_sym}) on exchange. Adopting as {pos_key}")
                    
                    # Fetch ACTUAL config.LEVERAGE setting from exchange
                    try:
                        lev_info = await self.exchange.fetch_leverage(original_sym)
                        if lev_info:
                            actual_leverage = int(lev_info.get('config.LEVERAGE', leverage))
                            self.logger.info(f"[ADOPT] Fetched actual config.LEVERAGE for {original_sym}: {actual_leverage}x")
                        else:
                            # Use fallback from config instead of hardcoded 8
                            actual_leverage = config.LEVERAGE
                            self.logger.warning(f"[ADOPT] fetch_leverage returned None for {original_sym}. Using fallback: {actual_leverage}x")
                    except Exception as lev_err:
                        raw_leverage = p.get('config.LEVERAGE')
                        actual_leverage = int(raw_leverage) if raw_leverage is not None else config.LEVERAGE
                        self.logger.warning(f"[ADOPT] Could not fetch config.LEVERAGE for {original_sym}, using fallback: {actual_leverage}x | Error: {lev_err}")
                    
                    # Calculate entry price
                    entry_price = self._safe_float(p.get('entryPrice') or p.get('avgPrice') or p.get('info', {}).get('entryPrice') or p.get('info', {}).get('avgEntryPrice'), default=0)
                    
                    # Issue 5: Adoption Validation (Contracts > 0 and Side check)
                    pos_amt = self._safe_float(p.get('contracts') or p.get('amount') or p.get('info', {}).get('positionAmt', 0))
                    if abs(pos_amt) <= 0:
                        self.logger.debug(f"[ADOPT] Skipping {original_sym} - zero size.")
                        continue

                    raw_side = p.get('side', '')
                    if not raw_side:
                        raw_side = p.get('info', {}).get('side', '')
                    if not raw_side:
                        raw_side = 'LONG' if pos_amt > 0 else 'SHORT' if pos_amt < 0 else ''
                        
                    raw_side = raw_side.upper()
                    # Normalize side to BUY/SELL
                    if raw_side == 'LONG' or (not raw_side and pos_amt > 0): side = 'BUY'
                    elif raw_side == 'SHORT' or (not raw_side and pos_amt < 0): side = 'SELL'
                    elif raw_side: side = raw_side
                    else: 
                        self.logger.warning(f"[ADOPT] Could not determine side for {original_sym}. Skipping.")
                        continue
                    
                    # Initialize SL/TP placeholders
                    auto_sl = None
                    auto_tp = None
                    sl_order_id = None
                    tp_order_id = None
                    
                    # CHECK EXISTING OPEN ORDERS FIRST (Filter from global list)
                    try:
                        symbol_orders = []
                        if all_exchange_orders is not None:
                            symbol_orders = [o for o in all_exchange_orders if o.get('symbol') == original_sym]
                        else:
                            # Fallback if global failed
                            symbol_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, original_sym)
                        
                        for o in symbol_orders:
                            o_type = str(o.get('type') or '').upper()
                            o_side = o.get('side').upper()
                            # Use stopPrice for conditional, price for basic if stopPrice missing
                            o_price = float(o.get('stopPrice') or o.get('price') or 0)
                            o_id = str(o.get('id'))
                            
                            expected_close_side = 'SELL' if side == 'BUY' else 'BUY'
                            
                            if o_side == expected_close_side:
                                # Logic from Gemini: STOP_MARKET or TAKE_PROFIT_MARKET
                                if 'STOP' in o_type:
                                    auto_sl = o_price
                                    sl_order_id = o_id
                                    self.logger.info(f"[ADOPT] Found existing SL for {original_sym}: {o_id} @ {o_price}")
                                elif 'TAKE' in o_type:
                                    auto_tp = o_price
                                    tp_order_id = o_id
                                    self.logger.info(f"[ADOPT] Found existing TP for {original_sym}: {o_id} @ {o_price}")
                    except Exception as e:
                        self.logger.warning(f"[ADOPT] Failed to check existing open orders for {original_sym}: {e}")

                    # Auto-calculate SL/TP only if NOT found
                    if not auto_sl:
                        if side == 'BUY':
                            auto_sl = round(entry_price * 0.97, 5)
                        elif side == 'SELL':
                            auto_sl = round(entry_price * 1.03, 5)
                    
                    if not auto_tp:
                        if side == 'BUY':
                            auto_tp = round(entry_price * 1.03, 5)
                        elif side == 'SELL':
                            auto_tp = round(entry_price * 0.97, 5)
                    
                    self.active_positions[pos_key] = {
                        "symbol": unified_sym,
                        "side": side,
                        "qty": self._safe_float(p.get('contracts') or p.get('amount') or p.get('info', {}).get('positionAmt') or 0),
                        "entry_price": entry_price,
                        "status": "filled",
                        "timeframe": "sync",
                        "config.LEVERAGE": actual_leverage,
                        "timestamp": p.get('timestamp') or self.exchange.milliseconds(),
                        "sl": auto_sl,
                        "tp": auto_tp,
                        "order_id": None, 
                        "sl_order_id": sl_order_id,
                        "tp_order_id": tp_order_id
                    }
                    await self._update_db_position(pos_key)
                    print(f"[ADOPT] {pos_key} adopted with config.LEVERAGE {actual_leverage}x, auto SL={auto_sl} TP={auto_tp}")

        # 2.5 ADOPT ORDERS (Ex -> Local)
        # If an order exists on exchange but bot doesn't know about it, adopt it.
        if fetch_success and all_exchange_orders:
            for o in all_exchange_orders:
                try:
                    o_id = str(o.get('id') or o.get('orderId'))
                    sym = o.get('symbol')
                    norm_sym = self._normalize_symbol(sym)
                    unified_sym = self._get_unified_symbol(sym)
                    
                    # Filter out SL/TP/Reduction orders (we only adopt standard entry orders)
                    o_type = str(o.get('type') or '').upper()
                    is_reduce = o.get('reduceOnly') or o.get('info', {}).get('reduceOnly') == 'true'
                    if 'STOP' in o_type or 'TAKE' in o_type or is_reduce:
                        continue
                        
                    # Check if we already know about this order
                    known = False
                    # Check pending_orders map
                    for p_key, p_val in self.pending_orders.items():
                        if str(p_val.get('order_id')) == o_id:
                            known = True
                            break
                    if known: continue
                    
                    # Check active_positions (status=pending)
                    for p_key, p_val in self.active_positions.items():
                        if str(p_val.get('order_id')) == o_id:
                            known = True
                            break
                    if known: continue
                    
                    # Stray entry order found! Adopt it. Standardize key to avoid slashes.
                    pos_key = self._get_pos_key(unified_sym, 'order_adopted')
                    
                    # Avoid collision if we already 'own' this symbol path
                    if pos_key in self.pending_orders or pos_key in self.active_positions:
                        continue
                        
                    self.logger.info(f"[ADOPT-ORDER] Found unidentified entry order {o_id} for {sym}. Adopting as {pos_key}")
                    
                    side = o.get('side').upper()
                    qty = self._safe_float(o.get('amount') or o.get('info', {}).get('origQty'))
                    # Robust price extraction: check price, stopPrice (trigger), or avgPrice
                    price = self._safe_float(o.get('stopPrice') or o.get('price') or o.get('avgPrice') or o.get('info', {}).get('price', 0))
                    
                    # Auto-calculate default SL/TP based on entry price
                    auto_sl = 0
                    auto_tp = 0
                    if price > 0:
                        if side == 'BUY':
                            auto_sl = round(price * 0.97, 5)
                            auto_tp = round(price * 1.03, 5)
                        else:
                            auto_sl = round(price * 1.03, 5)
                            auto_tp = round(price * 0.97, 5)

                    # Add to both trackers
                    order_data = {
                        'order_id': o_id,
                        'symbol': unified_sym,
                        'side': side,
                        'price': price,
                        'qty': qty,
                        'timeframe': 'sync',
                        'status': 'pending',
                        'timestamp': o.get('timestamp') or self.exchange.milliseconds(),
                        'sl': auto_sl,
                        'tp': auto_tp
                    }
                    self.pending_orders[pos_key] = order_data
                    self.active_positions[pos_key] = order_data
                    await self._update_db_position(pos_key)
                    print(f"📦 [ADOPT] Adopted stray order {o_id} for {sym} as {pos_key}")
                    
                except Exception as e:
                    self.logger.error(f"Error during order adoption for {o.get('id')}: {e}")

        # 3. SYNC & REPAIR (Local <-> Ex)
        for pos_key, pos in list(self.active_positions.items()):
            # EXTRA GUARD: Skip if key does not match current exchange
            if self.exchange_name and self.exchange_name not in pos_key:
                continue

            try:
                symbol = pos.get('symbol')
                status = pos.get('status')
                qty = pos.get('qty')

                norm_symbol = self._normalize_symbol(symbol)
                ex_match = None
                # Find if symbol exists on exchange (use normalized comparison for robust matching)
                for ex_sym, ex_p in active_ex_pos.items():
                    if self._normalize_symbol(ex_sym) == norm_symbol:
                        ex_match = ex_p
                        break

                # 3.1 Handle PENDING -> FILLED synchronization
                if status == 'pending' and ex_match:
                    self.logger.info(f"📈 [SYNC] Pending order {pos_key} found as active position. Transitioning to FILLED.")
                    pos['status'] = 'filled'
                    
                    # Robust key extraction for different exchanges
                    new_entry = self._safe_float(ex_match.get('entry_price') or ex_match.get('entryPrice') or ex_match.get('avgPrice'))
                    if new_entry > 0:
                        pos['entry_price'] = new_entry
                        
                    new_qty = self._safe_float(ex_match.get('contracts') or ex_match.get('amount') or ex_match.get('qty'))
                    if new_qty > 0:
                        pos['qty'] = new_qty
                        
                    pos['timestamp'] = ex_match.get('timestamp') or pos.get('timestamp')
                    pos['missing_cycles'] = 0
                    self.active_positions[pos_key] = pos
                    await self._update_db_position(pos_key)
                    
                    # After marking filled, ensure SL/TP orders are placed (if not already there)
                    try:
                        if not self.is_margin_throttled():
                            # Check if SL/TP already attached (Bybit V5)
                            tpsl_attached_sync = False
                            p_symbol = pos.get('symbol')
                            if self.exchange_name == 'BYBIT' and p_symbol:
                                ex_pos = self._last_ex_pos_map.get(self._normalize_symbol(p_symbol))
                                if ex_pos and (float(ex_pos.get('stopLoss') or 0) > 0 or float(ex_pos.get('takeProfit') or 0) > 0):
                                    tpsl_attached_sync = True
                                    self.logger.info(f"✅ [SYNC] {p_symbol} already has attached SL/TP on Bybit. Skipping.")
                            
                            if not tpsl_attached_sync:
                                await self._create_sl_tp_orders_for_position(pos_key)
                            else:
                                # Ensure local state reflects attachment
                                self.active_positions[pos_key]['sl_order_id'] = 'attached'
                                self.active_positions[pos_key]['tp_order_id'] = 'attached'
                                await self._update_db_position(pos_key)
                        else:
                            self.logger.warning(f"[SYNC] Skipping SL/TP creation for {pos_key} due to margin throttling.")
                    except Exception as e:
                        self.logger.debug(f"[SYNC] SL/TP setup error: {e}")
                    
                    status = 'filled'

                # Self-healing: Update missing price if it was previously 0
                if pos.get('status') == 'pending' and pos.get('price', 0) == 0:
                    matching_order = None
                    if all_exchange_orders:
                        matching_order = next((o for o in all_exchange_orders if str(o.get('id')) == str(pos.get('order_id'))), None)
                    
                    if matching_order:
                        new_price = self._safe_float(matching_order.get('stopPrice') or matching_order.get('price') or matching_order.get('avgPrice') or 0)
                        if new_price > 0:
                            self.logger.info(f"[SYNC] Healing price for {pos_key}: {pos.get('price')} -> {new_price}")
                            pos['price'] = new_price
                            self.active_positions[pos_key] = pos

                # Self-healing: if SL/TP missing locally, add placeholders/defaults
                if (not pos.get('sl') or not pos.get('tp')) and (pos.get('entry_price', 0) > 0 or pos.get('price', 0) > 0):
                    ref_price = pos.get('entry_price') or pos.get('price')
                    side = pos.get('side', 'BUY').upper()
                    if not pos.get('sl'):
                        pos['sl'] = round(ref_price * 0.97, 5) if side == 'BUY' else round(ref_price * 1.03, 5)
                    if not pos.get('tp'):
                        pos['tp'] = round(ref_price * 1.03, 5) if side == 'BUY' else round(ref_price * 0.97, 5)
                    self.active_positions[pos_key] = pos
                    self.logger.info(f"[SYNC] Healing SL/TP for {pos_key}: SL={pos['sl']} TP={pos['tp']}")

                # Handle newly recovered status from unverified
                if status == 'unverified' and ex_match:
                    self.logger.info(f"[SYNC] Position {pos_key} recovered from unverified state.")
                    pos['status'] = 'filled'
                    pos['missing_cycles'] = 0
                    if 'unverified_since' in pos:
                        del pos['unverified_since']
                    self.active_positions[pos_key] = pos
                    await self._update_db_position(pos_key)
                    status = 'filled'

                # Skip removal if fetch failed!
                if status in ['filled', 'unverified'] and not ex_match:
                    if fetch_success:
                        # NEW AIRTIGHT LOGIC: Wait 3 cycles before checking history (unless forced)
                        missing_cycles = pos.get('missing_cycles', 0) + 1
                        pos['missing_cycles'] = missing_cycles
                        
                        # Skip wait if force_verify is True (Immediate results for tests/proactive sync)
                        if not force_verify and missing_cycles <= 3:
                            self.logger.info(f"[SYNC WAIT] Position {pos_key} missing from exchange (cycle {missing_cycles}/3). Waiting for history sync...")
                            pos['status'] = 'unverified'
                            self.active_positions[pos_key] = pos
                            await self._update_db_position(pos_key)
                            continue
                            
                        self.logger.info(f"[SYNC] Position {pos_key} missing for {missing_cycles} cycles. Checking trade history...")
                        
                        # Determine exit price: prioritize actual fill from exchange (Issue 3)
                        exit_price = 0
                        side = pos.get('side')
                        try:
                            await asyncio.sleep(0.5)
                            
                            # Only look for trades since the position's entry timestamp (reduced window)
                            # Or if missing (None/0), fallback to 24 hours ago to ensure we catch fast adopted trades.
                            since = pos.get('timestamp')
                            if not since:
                                since = int((time.time() - 86400) * 1000)
                            recent_trades = await self._execute_with_timestamp_retry(self.exchange.fetch_my_trades, symbol, since=since)
                            
                            if recent_trades:
                                target_side = 'sell' if side == 'BUY' else 'buy'
                                close_trades = [t for t in recent_trades if t.get('side', '').lower() == target_side]
                                
                                if close_trades:
                                    total_qty = sum(self._safe_float(t.get('amount')) for t in close_trades)
                                    if total_qty > 0:
                                        weighted_sum = sum(self._safe_float(t.get('price')) * self._safe_float(t.get('amount')) for t in close_trades)
                                        exit_price = weighted_sum / total_qty
                                        # Fix 3: Extract actual exchange fees for accurate P&L
                                        actual_fees = sum(
                                            float((t.get('fee') or {}).get('cost') or 0)
                                            for t in close_trades
                                        )
                                        if actual_fees > 0:
                                            pos['_exit_fees'] = actual_fees
                                        self.logger.info(f"[SYNC] Actual fill {symbol}: {exit_price} (fees: ${actual_fees:.4f})")

                            # 2. NO FALLBACK TO MARKET PRICE. Mark unverified if trade history not found.
                            if exit_price == 0:
                                unverified_since = pos.get('unverified_since', time.time())
                                pos['unverified_since'] = unverified_since
                                
                                if time.time() - unverified_since > 72 * 3600:
                                    self.logger.warning(f"[ORPHAN CHECK] Position {pos_key} unverified for > 72h. Logging as orphaned.")
                                    await self.log_trade(pos_key, pos.get('entry_price', 0), exit_reason="Closed - Orphaned")
                                    del self.active_positions[pos_key]
                                else:
                                    self.logger.warning(f"[SYNC] No recent trades found for missing pos {symbol}. Keeping as unverified.")
                                    pos['status'] = 'unverified'
                                    self.active_positions[pos_key] = pos
                                await self._update_db_position(pos_key)
                                continue
                                
                        except Exception as e:
                            self.logger.warning(f"[SYNC] Failed to fetch actual fill for {symbol}: {e}")
                            pos['status'] = 'unverified'
                            self.active_positions[pos_key] = pos
                            self._save_positions()
                            continue
                        
                        # Determine exit reason accurately using exchange data where possible
                        if recent_trades:
                            # Use the most recent closing trade for classification
                            target_side = 'sell' if side == 'BUY' else 'buy'
                            close_trades_only = [t for t in recent_trades if t.get('side', '').lower() == target_side]
                            if close_trades_only:
                                latest_trade = max(close_trades_only, key=lambda t: t['timestamp'])
                                actual_reason = self._infer_exit_reason(latest_trade, pos)
                            else:
                                actual_reason = 'SYNC(Unknown)'
                        else:
                            # Heuristic fallback if no trades found (though exit_price > 0 implies trades usually exist)
                            actual_reason = 'SYNC(External)'

                        if actual_reason == 'SL':
                            self.logger.info(f"[{symbol}] Sync detected SL closure. Applying SL Cooldown.")
                            await self.set_sl_cooldown(symbol)
                        
                        # 1. Update DB status FIRST
                        await self._clear_db_position(pos_key, exit_price=exit_price, exit_reason=actual_reason)
                        # 2. Log trade and remove from memory (idempotent notification)
                        await self.remove_position(symbol, pos.get('timeframe'), exit_price=exit_price, exit_reason=actual_reason)
                        continue
                    else:
                        self.logger.warning(f"[SYNC] Fetch failed, preserving local position {pos_key} (Assume alive)")
                        continue

                # A) Recover missing order_id for pending
                if status == 'pending':
                    if not pos.get('order_id'):
                        try:
                            # Use global snapshot for recovery speed
                            symbol_orders = []
                            if all_exchange_orders is not None:
                                norm_symbol = self._normalize_symbol(symbol)
                                symbol_orders = [o for o in all_exchange_orders if self._normalize_symbol(o.get('symbol')) == norm_symbol]
                            else:
                                symbol_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                            
                            for o in symbol_orders or []:
                                o_id = str(o.get('id') or o.get('orderId'))
                                o_amt = float(o.get('amount') or o.get('info', {}).get('origQty') or 0)
                                if abs(o_amt - float(qty)) < max(1e-6, 0.01 * float(qty)):
                                    pos['order_id'] = o_id
                                    self.active_positions[pos_key] = pos
                                    await self._update_db_position(pos_key)
                                    summary['recovered_order_ids'] += 1
                                    break
                        except Exception as e:
                            self.logger.warning(f"[SYNC] Recovery failed for {pos_key}: {e}")
                    
                    # B) Verify if pending order still exists on exchange
                    order_id = pos.get('order_id')
                    if order_id:
                        try:
                            # Use a focused list for verification
                            symbol_orders = []
                            if all_exchange_orders is not None:
                                norm_symbol = self._normalize_symbol(symbol)
                                symbol_orders = [o for o in all_exchange_orders if self._normalize_symbol(o.get('symbol')) == norm_symbol]
                            else:
                                symbol_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                            
                            # Check both id and orderId
                            found_in_open = False
                            try:
                                found_in_open = any(str(o.get('id') or o.get('orderId')) == str(order_id) for o in symbol_orders)
                            except Exception as e:
                                self.logger.debug(f"[SYNC] Error scanning open orders for {order_id}: {e}")
                            
                            if not found_in_open:
                                # Order is not in open orders. Check if it was filled, cancelled, or just missing (expired)
                                try:
                                    # Proactive check before we assume it's filled
                                    # If fetch_order fails with "Order does not exist", it's likely cancelled/expired and too old for some histories
                                    order_info = await self.exchange.fetch_order(order_id, symbol)
                                    status_on_ex = str(order_info.get('status', '')).lower()
                                    if status_on_ex in ['closed', 'filled']:
                                        found_in_open = False # proceed to fill logic
                                    elif status_on_ex in ['canceled', 'cancelled', 'expired', 'rejected']:
                                        self.logger.warning(f"[SYNC] Pending order {order_id} was {status_on_ex.upper()} on exchange. Clearing.")
                                        if pos_key in self.pending_orders: del self.pending_orders[pos_key]
                                        del self.active_positions[pos_key]
                                        await self._update_db_position(pos_key)
                                        continue
                                except Exception as e:
                                    # Catch "Order does not exist" (-2013 on Binance, etc.)
                                    err_str = str(e).lower()
                                    if "order does not exist" in err_str or "-2013" in err_str or "30001" in err_str:
                                        self.logger.warning(f"[SYNC] Pending order {order_id} not found on exchange (expired/deleted). Clearing.")
                                        if pos_key in self.pending_orders: del self.pending_orders[pos_key]
                                        await self._cancel_stale_position_in_db(pos_key, reason="order_not_found_on_exchange")
                                        del self.active_positions[pos_key]
                                        await self._update_db_position(pos_key)
                                        continue
                                    self.logger.debug(f"[SYNC] fetch_order check failed for {order_id}: {e}")

                                # Not open check if filled
                                if fetch_success and symbol in active_ex_pos:
                                    self.logger.info(f"[SYNC] Pending order {order_id} filled. Updating status.")
                                    pos['status'] = 'filled'
                                    ex_p = active_ex_pos[symbol]
                                    pos['entry_price'] = self._safe_float(ex_p.get('entryPrice'), pos['entry_price'])
                                    pos['qty'] = self._safe_float(ex_p.get('contracts'), pos['qty'])
                                    if 'config.LEVERAGE' in ex_p:
                                        pos['config.LEVERAGE'] = int(ex_p['config.LEVERAGE'])
                                    self.active_positions[pos_key] = pos
                                    await self._update_db_position(pos_key)
                                else:
                                    # Order is gone from open, but not in current active_ex_pos.
                                    # Check if it was FILLED and then IMMEDIATELY CLOSED or SL'd
                                    self.logger.info(f"[SYNC] Pending order {order_id} gone from open. Checking trade history for fill...")
                                    
                                    side = pos.get('side', '').upper()
                                    found_fill = False
                                    try:
                                        # Use a small grace period and fetch trades
                                        await asyncio.sleep(0.5)
                                        since = pos.get('timestamp')
                                        if not since:
                                            since = int((time.time() - 86400) * 1000)
                                        recent_trades = await self._execute_with_timestamp_retry(self.exchange.fetch_my_trades, symbol, since=since)
                                        
                                        if recent_trades:
                                            # Look for trades matching this order_id
                                            order_trades = [t for t in recent_trades if str(t.get('order') or t.get('orderId')) == str(order_id)]
                                            
                                            if order_trades:
                                                self.logger.info(f"[SYNC] Detected fill in trade history for 'gone' pending order {order_id}.")
                                                found_fill = True
                                                
                                                # Calculate fill details
                                                fill_qty = sum(self._safe_float(t.get('amount')) for t in order_trades)
                                                fill_weighted_price = sum(self._safe_float(t.get('price')) * self._safe_float(t.get('amount')) for t in order_trades)
                                                entry_price = fill_weighted_price / fill_qty if fill_qty > 0 else pos.get('entry_price', 0)
                                                
                                                # Temporarily mark as filled to use log_trade logic
                                                pos['status'] = 'filled'
                                                pos['entry_price'] = entry_price
                                                pos['qty'] = fill_qty
                                                
                                                # Now check for CLOSING trades (SL or exit)
                                                target_side = 'sell' if side == 'BUY' else 'buy'
                                                close_trades = [t for t in recent_trades if t.get('side', '').lower() == target_side and str(t.get('order')) != str(order_id)]
                                                
                                                exit_price = 0
                                                if close_trades:
                                                    total_close_qty = sum(self._safe_float(t.get('amount')) for t in close_trades)
                                                    if total_close_qty > 0:
                                                        weighted_close_sum = sum(self._safe_float(t.get('price')) * self._safe_float(t.get('amount')) for t in close_trades)
                                                        exit_price = weighted_close_sum / total_close_qty
                                                        
                                                        # Extract actual fees
                                                        actual_fees = sum(
                                                            float((t.get('fee') or {}).get('cost') or 0)
                                                            for t in order_trades + close_trades
                                                        )
                                                        if actual_fees > 0:
                                                            pos['_exit_fees'] = actual_fees
                                                
                                                # If no closing trades found but position is gone, use current price as fallback for closure logging
                                                if exit_price == 0:
                                                    df = self.data_manager.get_data(symbol, pos.get('timeframe', '1h'), exchange=self.exchange_name)
                                                    exit_price = df.iloc[-1]['close'] if df is not None and not df.empty else entry_price
                                                
                                                # Apply Cooldown if loss
                                                is_loss = (side == 'BUY' and exit_price < entry_price) or (side == 'SELL' and exit_price > entry_price)
                                                if is_loss:
                                                    self.logger.info(f"[{symbol}] Sync detected loss for fast pending-to-SL trade. Applying Cooldown.")
                                                    print(f"[SAFE] [{self.exchange_name}] [{symbol}] Applying SL Cooldown after fast SL hit.")
                                                    await self.set_sl_cooldown(symbol)
                                                
                                                await self.log_trade(pos_key, exit_price, exit_reason="Exchange Sync (Fast Fill + SL)")
                                    except Exception as e:
                                        self.logger.warning(f"[SYNC] Trade history check failed for {pos_key}: {e}")
                                    
                                    if not found_fill:
                                        # ACTUAL CANCELLED
                                        self.logger.info(f"[SYNC] Pending order {order_id} gone. No fill trades found. Removing.")
                                        
                                    if pos_key in self.pending_orders:
                                        del self.pending_orders[pos_key]
                                    del self.active_positions[pos_key]
                                    await self._update_db_position(pos_key)
                                    continue
                        except Exception as e:
                            self.logger.warning(f"[SYNC] Failed to verify pending order {order_id}: {e}")

                # B) Fill Missing SL/TP IDs & Cleanup Orphans
                if status == 'filled':
                    try:
                        symbol_orders = []
                        if all_exchange_orders is not None:
                            norm_symbol = self._normalize_symbol(symbol)
                            symbol_orders = [o for o in all_exchange_orders if self._normalize_symbol(o.get('symbol')) == norm_symbol]
                        else:
                            symbol_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                        
                        found_sl = pos.get('sl_order_id')
                        found_tp = pos.get('tp_order_id')
                    
                        # 0. COOLDOWN CHECK: If we just recreated SL/TP for this position, SKIP verification
                        # This prevents "Verification Lag" where we create verify fail create again instantly
                        # 300s (5 min) is needed because Binance algo orders can take time to appear in fetch_order
                        last_creation = self._pos_action_timestamps.get(f"{pos_key}_recreation", 0)
                        if (self.exchange.milliseconds() - last_creation) < 300000: # 5 minutes trust period
                            # self.logger.info(f"[SYNC] Skipping verification for {pos_key} (In Grace Period)")
                            continue

                        prices_changed = False
                        
                        # 0.5 BYBIT SPECIAL: Check for attached SL/TP on positions
                        # FIX: Use _last_ex_pos_map (normalized keys) instead of active_ex_pos
                        # (raw Bybit IDs like UNIUSDT) to avoid symbol format mismatch.
                        if "BYBIT" in self.exchange_name.upper():
                            norm_sym = self._normalize_symbol(symbol)
                            ex_p = self._last_ex_pos_map.get(norm_sym) or {}
                            attached_sl = self._safe_float(ex_p.get('stopLoss'))
                            attached_tp = self._safe_float(ex_p.get('takeProfit'))
                            
                            if attached_sl > 0:
                                if pos.get('sl') != attached_sl:
                                    self.logger.info(f"[SYNC] Updating Bybit SL for {pos_key}: {pos.get('sl')} -> {attached_sl}")
                                    pos['sl'] = attached_sl
                                    prices_changed = True
                                found_sl = "attached"
                                pos['sl_order_id'] = 'attached'
                            if attached_tp > 0:
                                if pos.get('tp') != attached_tp:
                                    self.logger.info(f"[SYNC] Updating Bybit TP for {pos_key}: {pos.get('tp')} -> {attached_tp}")
                                    pos['tp'] = attached_tp
                                    prices_changed = True
                                found_tp = "attached"
                                pos['tp_order_id'] = 'attached'


                        # 1. VERIFY SL (With Grace Period)
                        if found_sl != "attached":
                            found_sl, sl_order = await self._verify_or_clear_id(pos_key, found_sl, symbol, symbol_orders)
                        else:
                            sl_order = None
                        
                        # 2. VERIFY TP (With Grace Period)
                        if found_tp != "attached":
                            found_tp, tp_order = await self._verify_or_clear_id(pos_key, found_tp, symbol, symbol_orders)
                        else:
                            tp_order = None

                        # SYNC PRICES FROM EXCHANGE (If verified)
                        if sl_order:
                            new_sl = self._safe_float(sl_order.get('stopPrice') or sl_order.get('triggerPrice') or 0)
                            if new_sl > 0 and new_sl != pos.get('sl'):
                                self.logger.info(f"[SYNC] Updating SL price for {pos_key}: {pos.get('sl')} -> {new_sl}")
                                pos['sl'] = new_sl
                                prices_changed = True

                        if tp_order:
                            new_tp = self._safe_float(tp_order.get('stopPrice') or tp_order.get('triggerPrice') or tp_order.get('price') or 0)
                            if new_tp > 0 and new_tp != pos.get('tp'):
                                self.logger.info(f"[SYNC] Updating TP price for {pos_key}: {pos.get('tp')} -> {new_tp}")
                                pos['tp'] = new_tp
                                prices_changed = True

                        # 3. DISCOVERY (If local missing, check sàn using normalized symbols)
                        if not found_sl or not found_tp:
                            for o in symbol_orders or []:
                                # ROBUST Algo ID Matching
                                o_id = str(o.get('algoId') or o.get('orderId') or o.get('id') or o.get('info', {}).get('orderId', ''))
                                o_type = str(o.get('type') or o.get('info', {}).get('type', '') or o.get('algoType', '')).upper()
                                
                                # BROADER CHECK for STOP orders
                                is_stop = ('STOP' in o_type or o.get('algoType') == 'STOP_LOSS')
                                
                                # BROADER CHECK for TP orders (Include LIMIT for manual TP)
                                is_tp = ('TAKE' in o_type or 'LIMIT' in o_type or o.get('algoType') == 'TAKE_PROFIT')
                                
                                if not found_sl and is_stop:
                                    found_sl = o_id
                                    self.logger.info(f"[SYNC] Discovered existing SL for {pos_key}: {o_id}")
                                elif not found_tp and is_tp:
                                    found_tp = o_id
                                    self.logger.info(f"[SYNC] Discovered existing TP for {pos_key}: {o_id}")

                        # Update persistence if changed
                        if found_sl != pos.get('sl_order_id') or found_tp != pos.get('tp_order_id') or prices_changed:
                            pos['sl_order_id'] = found_sl
                            pos['tp_order_id'] = found_tp
                            self.active_positions[pos_key] = pos
                            await self._update_db_position(pos_key)

                        # C) AUTO-RECREATE IF TRULY MISSING
                        # Fix: ONLY repair if the position actually exists on the exchange!
                        if auto_fix and ex_match and (not found_sl or not found_tp):
                            if self.is_margin_throttled():
                                self.logger.warning(f"[REPAIR] Skipping recreation for {pos_key} due to margin throttling.")
                                continue
                                
                            print(f"[REPAIR] {pos_key} is missing SL or TP on exchange. Recreating...")
                            
                            # MARK TIMESTAMP BEFORE ACTION to prevent immediate re-entry
                            self._pos_action_timestamps[f"{pos_key}_recreation"] = self.exchange.milliseconds()
                            
                            await self.recreate_missing_sl_tp(
                                pos_key, 
                                recreate_sl=not found_sl, 
                                recreate_tp=not found_tp,
                                recreate_sl_force=True, 
                                recreate_tp_force=True
                            )
                            summary['created_tp_sl'] += 1
                    except Exception as e:
                        self.logger.error(f"[SYNC] Error during SL/TP sync for {pos_key}: {e}")

            except Exception as e:
                summary['errors'].append(str(e))
                self.logger.error(f"Reconcile error for {pos_key}: {e}")

        # 3.5 PENDING ORDER ADOPTION PHASE
        if all_exchange_orders is not None:
            for o in all_exchange_orders:
                o_id = str(o.get('id') or o.get('orderId'))
                o_symbol = o.get('symbol')
                o_symbol_norm = self._normalize_symbol(o_symbol)
                o_type = str(o.get('type') or o.get('info', {}).get('type', '')).upper()
                
                # Only adopt entry orders (LIMIT/MARKET), not SL/TP (which Reaper handles)
                is_conditional = 'STOP' in o_type or 'TAKE' in o_type or o.get('algoType') in ('STOP_LOSS', 'TAKE_PROFIT')
                if is_conditional:
                    continue
                
                # Check if this order ID is known locally
                known = False
                for pk, p in self.active_positions.items():
                    if str(p.get('order_id')) == o_id:
                        known = True
                        break
                if not known:
                    for pk, p in self.pending_orders.items():
                        if str(p.get('order_id')) == o_id:
                            known = True
                            break
                            
                if not known:
                    # Issue 10: Strict Spot Filtering
                    if self._is_spot(o_symbol):
                        # self.logger.debug(f"[SYNC] Ignoring spot order: {o_symbol}")
                        continue

                    unified_symbol = self._get_unified_symbol(o_symbol)
                    new_pk = f"{self.exchange_name}_{unified_symbol}_adopted"
                    self.logger.info(f"[SYNC] Adopting ghost order on exchange: {o_id} for {o_symbol}")
                    self.pending_orders[new_pk] = {
                        'order_id': o_id,
                        'symbol': unified_symbol,
                        'side': o['side'].upper(),
                        'price': self._safe_float(o.get('price')),
                        'qty': self._safe_float(o.get('amount')),
                        'timeframe': 'sync',
                        'status': 'pending',
                        'timestamp': self.exchange.milliseconds(),
                        'adopted': True
                    }
                    # Also put in active_positions for persistence
                    self.active_positions[new_pk] = self.pending_orders[new_pk]
                    summary['adopted_orders'] = summary.get('adopted_orders', 0) + 1
                    await self._update_db_position(new_pk)

        # 4. GLOBAL ORPHAN REAPER (Conditional Only)
        # This scans ALL account orders and cancels SL/TP that aren't in active_positions
        if all_exchange_orders is not None:
            managed_ids = set()
            for pk, p in self.active_positions.items():
                if self.exchange_name and not pk.startswith(self.exchange_name):
                    continue
                if p.get('sl_order_id'): managed_ids.add(str(p.get('sl_order_id')))
                if p.get('tp_order_id'): managed_ids.add(str(p.get('tp_order_id')))
            
            # Also protect orders in Grace Period
            for oid, count in self._missing_order_counts.items():
                if count > 0: managed_ids.add(str(oid))

            # UNIVERSAL REAPER (Run only every 5 minutes)
            # This cleans up orphaned orders from previous sessions or manual interventions
            current_ts = self.exchange.milliseconds()
            if not hasattr(self, '_last_reaper_run'): self._last_reaper_run = 0
            
            # Run 1st time immediately, then every 5 mins
            if current_ts - self._last_reaper_run > 300000: 
                self.logger.info("🧹 [REAPER] Starting periodic orphan scan...")
                self._debug_log('reaper:start', {'managed_ids_count': len(managed_ids)})
                self._last_reaper_run = current_ts

                # Randomize order to avoid getting stuck on the same failing orders if we hit limits
                import random
                shuffled_orders = list(all_exchange_orders)
                random.shuffle(shuffled_orders)
                
                orphans_deleted = 0
                MAX_ORPHANS_PER_CYCLE = 20  # Increased batch limit since we run less often

                for o in shuffled_orders:
                    if orphans_deleted >= MAX_ORPHANS_PER_CYCLE:
                        self.logger.info(f"🧹 [REAPER] Hit max orphans limit ({MAX_ORPHANS_PER_CYCLE}). pausing until next cycle.")
                        break

                    o_id = str(o.get('id') or o.get('orderId'))
                    o_type = str(o.get('type') or o.get('info', {}).get('type', '')).upper()
                    o_symbol = o.get('symbol')
                    
                    # Issue 10: Strict Spot Filtering (Reaper)
                    if self._is_spot(o_symbol):
                        continue

                    norm_symbol = self._normalize_symbol(o_symbol)
                    
                    # Check if this symbol belongs to our TRADING_SYMBOLS (normalized)
                    # UPDATED: Reaper now scans ALL account symbols to clear ghosts from previous runs
                    
                    # We only reaper STOP/TAKE orders (Conditional)
                    is_conditional = 'STOP' in o_type or 'TAKE' in o_type or o.get('algoType') in ('STOP_LOSS', 'TAKE_PROFIT')
                    
                    if is_conditional and o_id not in managed_ids:
                        # Throttle terminal output to avoid spam
                        current_sec = time.time()
                        if not hasattr(self, '_last_reaper_log') or current_sec - getattr(self, '_last_reaper_log', 0) > 60:
                            print(f"🧹 [{self.exchange_name}] [REAPER] Cleaning orphaned conditional orders...")
                            self._last_reaper_log = current_sec
                            
                        self.logger.info(f"🧹 [REAPER] Cancelling orphaned {o_type} order {o_id} for {o_symbol}")
                        self._debug_log('reaper:orphan_found', {'id': o_id, 'type': o_type, 'symbol': o_symbol})
                        try:
                            # Use adapter's cancel_order which handles Standard/Algo fallback
                            await self.cancel_order(o_id, o_symbol, params={'is_algo': o.get('is_algo', False) or o.get('algoType') is not None})
                            
                            summary['orphans_cancelled'] += 1
                            orphans_deleted += 1
                            # Rate limit protection: Increased to 0.5s + Batch limit
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            self.logger.info(f"[REAPER] Failed to cancel {o_id}: {e}")

        return summary

    async def check_missing_sl_tp(self, pos_key, orders_in_snap=None):
        """Check whether SL/TP ids exist on-exchange for a given position.
        Returns a dict: {'sl_exists': bool, 'tp_exists': bool, 'errors': []}
        This method is non-destructive (does not recreate orders).
        """
        res = {'sl_exists': False, 'tp_exists': False, 'errors': []}
        pos = self.active_positions.get(pos_key)
        if not pos:
            res['errors'].append('position_not_found')
            return res

        symbol = pos.get('symbol')
        sl_id = pos.get('sl_order_id')
        tp_id = pos.get('tp_order_id')

        # Use helper for authoritative verification
        if sl_id:
            verified_sl, _ = await self._verify_or_clear_id(pos_key, sl_id, symbol, orders_in_snap)
            if verified_sl:
                res['sl_exists'] = True
            else:
                pos['sl_order_id'] = None # Clear if definitively gone

        if tp_id:
            verified_tp, _ = await self._verify_or_clear_id(pos_key, tp_id, symbol, orders_in_snap)
            if verified_tp:
                res['tp_exists'] = True
            else:
                pos['tp_order_id'] = None # Clear if definitively gone
        
        if not res['sl_exists'] or not res['tp_exists']:
            await self._update_db_position(pos_key)

        return res

    async def scan_sltp_liveness(self):
        """
        SL/TP Guardian: Scan every filled position to ensure SL/TP orders are active.
        - Uses _last_ex_pos_map cache for Bybit attached SL/TP — no extra API calls.
        - Throttled: each position is checked at most once per minute.
        - If SL/TP is missing: recreates immediately via recreate_missing_sl_tp.
        - Emergency close: if BOTH SL and TP are absent AND |PnL%| >= config.SLTP_GUARDIAN_EMERGENCY_PCT.
        """
        if self.dry_run or self.exchange.is_public_only:
            return

        prefix = f"P{self.profile_id}_{self.exchange_name}_"
        for pos_key, pos in list(self.active_positions.items()):
            if pos.get('status') != 'filled':
                continue
            if not pos_key.startswith(prefix):
                continue

            # Throttle: check each position at most once per minute
            last_check = self._pos_action_timestamps.get(f"{pos_key}_sltp_guardian", 0)
            now_ms = time.time() * 1000
            if now_ms - last_check < 60_000:
                continue
            self._pos_action_timestamps[f"{pos_key}_sltp_guardian"] = now_ms

            symbol = pos.get('symbol')
            side = pos.get('side')
            entry_price = self._safe_float(pos.get('entry_price'))
            norm_sym = self._normalize_symbol(symbol)

            # 1. Check SL/TP presence via stored order IDs
            has_sl = bool(pos.get('sl_order_id'))
            has_tp = bool(pos.get('tp_order_id'))

            # 2. Bybit: check attached SL/TP from exchange position cache (no API call needed)
            if 'BYBIT' in self.exchange_name.upper():
                ex_p = self._last_ex_pos_map.get(norm_sym) or {}
                attached_sl = self._safe_float(ex_p.get('stopLoss'))
                attached_tp = self._safe_float(ex_p.get('takeProfit'))
                if attached_sl > 0:
                    has_sl = True
                if attached_tp > 0:
                    has_tp = True

            # 3. Both present — nothing to do
            if has_sl and has_tp:
                continue

            # 4. Emergency close: no protection at all AND price move exceeds threshold
            if not has_sl and not has_tp and entry_price > 0:
                current_price = 0.0
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    current_price = self._safe_float(
                        ticker.get('last') or ticker.get('close') or 0
                    )
                except Exception as e:
                    self.logger.warning(f"[GUARDIAN] Ticker fetch failed for {pos_key}: {e}")

                if current_price > 0:
                    raw_pnl_pct = abs(current_price - entry_price) / entry_price
                    if raw_pnl_pct >= config.SLTP_GUARDIAN_EMERGENCY_PCT:
                        direction = "PROFIT" if (
                            (side == 'BUY' and current_price > entry_price) or
                            (side == 'SELL' and current_price < entry_price)
                        ) else "LOSS"
                        close_reason = f"Guardian: No SL/TP, PnL {raw_pnl_pct*100:.1f}%"
                        self.logger.warning(
                            f"[GUARDIAN] {pos_key} has NO SL/TP, "
                            f"PnL={raw_pnl_pct*100:.1f}% ({direction}) - Emergency close!"
                        )
                        # Force close FIRST — print is non-critical and may fail on some terminals
                        await self.force_close_position(pos_key, reason=close_reason)
                        try:
                            print(
                                f"[GUARDIAN] [{self.exchange_name}] {symbol} no SL/TP, "
                                f"PnL={raw_pnl_pct*100:.1f}% ({direction}). Emergency close!"
                            )
                        except Exception:
                            pass
                        continue

            # 5. Recreate whichever of SL / TP is missing
            missing = []
            if not has_sl:
                missing.append('SL')
            if not has_tp:
                missing.append('TP')
            self.logger.warning(
                f"[GUARDIAN] {pos_key} missing {'/'.join(missing)} on exchange. Recreating..."
            )
            print(f"[GUARDIAN] [{self.exchange_name}] {symbol} missing {'/'.join(missing)}. Recreating...")

            # Mark recreation timestamp so reconcile grace period is aware
            self._pos_action_timestamps[f"{pos_key}_recreation"] = self.exchange.milliseconds()
            try:
                await self.recreate_missing_sl_tp(
                    pos_key,
                    recreate_sl=not has_sl,
                    recreate_tp=not has_tp,
                    recreate_sl_force=True,
                    recreate_tp_force=True
                )
            except Exception as e:
                self.logger.error(f"[GUARDIAN] recreate_missing_sl_tp failed for {pos_key}: {e}")


    async def _calculate_dynamic_sl_tp(self, symbol, side, entry_price):
        """
        Calculate SL/TP using ATR (Volatility) instead of fixed %.
        Uses 1h timeframe, ATR(14) with multiplier 2.0 for SL, and 1.5R for TP.
        """
        try:
            # Fetch last 20 candles (1h)
            ohlcv = await self._execute_with_timestamp_retry(self.exchange.fetch_ohlcv, symbol, '1h', limit=20)
            if not ohlcv or len(ohlcv) < 15:
                return None, None
            
            # Manual ATR Calculation (Lightweight, no pandas)
            trs = []
            for i in range(1, len(ohlcv)):
                curr = ohlcv[i]
                prev = ohlcv[i-1]
                high, low, close = curr[2], curr[3], curr[4]
                prev_close = prev[4]
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            
            if not trs: return None, None
            
            # Simple Smoothing for ATR
            atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / len(trs)
            
            multiplier = 2.0  # Standard defensive SL (2x ATR)
            sl_dist = atr * multiplier
            tp_dist = atr * multiplier * 1.5  # 1.5R default reward
            
            if side == 'BUY':
                sl = entry_price - sl_dist
                tp = entry_price + tp_dist
            else:
                sl = entry_price + sl_dist
                tp = entry_price - tp_dist
                
            return sl, tp
        except Exception as e:
            self.logger.warning(f"ATR Calc failed for {symbol}: {e}")
            return None, None

    async def resume_pending_monitors(self):
        """Iterates through all pending positions and restarts background monitors."""
        monitored_count = 0
        for pos_key, pos in list(self.active_positions.items()):
            # EXTRA GUARD: Skip if key does not match current exchange
            if self.exchange_name and self.exchange_name not in pos_key:
                continue
                
            if pos.get('status') == 'pending' and pos.get('order_id'):
                order_id = pos['order_id']
                symbol = pos.get('symbol')
                if symbol:
                    self.logger.info(f"🚀 [STARTUP] Resuming monitor for pending order {order_id} ({symbol})")
                    asyncio.create_task(self._monitor_limit_order_fill(pos_key, order_id, symbol))
                    monitored_count += 1
        return monitored_count

    async def recreate_missing_sl_tp(self, pos_key, recreate_sl=True, recreate_tp=True, recreate_sl_force=False, recreate_tp_force=False):
        """Recreate missing SL/TP orders for a given position."""
        if self.dry_run:
            self.logger.info(f"[SIMULATION] Skipping SL/TP recreation for {pos_key}")
            return {'sl_recreated': False, 'tp_recreated': False}
        # Acquire per-position lock to prevent race conditions
        async with self._get_position_lock(pos_key):
            result = {'sl_recreated': False, 'tp_recreated': False, 'errors': []}
            pos = self.active_positions.get(pos_key)
            if not pos:
                result['errors'].append('position_not_found')
                return result

            # 1. Exchange Mismatch check
            if self.exchange_name and self.exchange_name not in pos_key:
                result['status'] = 'exchange_mismatch'
                return result
                
            symbol = pos.get('symbol')
            side = pos.get('side')
            sl = pos.get('sl')
            tp = pos.get('tp')
            qty = pos.get('qty')
            
            # 2. Urgent Protection Check (Price Fetch here)
            current_price = 0.0
            try:
                ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)
                if self.exchange_name.upper() == 'BYBIT':
                    # Robust mark price extraction for Bybit V5
                    current_price = float(ticker.get('mark') or ticker.get('info', {}).get('markPrice') or ticker.get('last') or ticker.get('close') or 0)
                else:
                    current_price = float(ticker.get('last') or ticker.get('close') or 0)
            except Exception as e:
                self.logger.warning(f"Could not fetch current price for {symbol} validation: {e}")

            if current_price > 0:
                is_hit = False
                reason = ""
                
                # Issue 3: Dynamic Buffer based on ATR (Volatility)
                # Fetch ATR from DataManager/Analyzer if available
                buffer_val = 0.001 # Default 0.1% fallback
                try:
                    # Attempt to fetch ATR(14) for 1h or 15m timeframe
                    # DataManager store keys: {exchange}_{symbol}_{tf}
                    tf_to_check = pos.get('timeframe') or '1h'
                    cache_key = f"{self.exchange_name}_{symbol}_{tf_to_check}"
                    df = self.data_manager.data_store.get(cache_key)
                    if df is not None and len(df) >= 14:
                        # Simple ATR calculation if 'atr' column missing
                        if 'atr' in df.columns:
                            latest_atr = float(df['atr'].iloc[-1])
                        else:
                            high_low = df['high'] - df['low']
                            high_close = (df['high'] - df['close'].shift()).abs()
                            low_close = (df['low'] - df['close'].shift()).abs()
                            ranges = np.max([high_low, high_close, low_close], axis=0)
                            latest_atr = np.mean(ranges[-14:])
                        
                        # Use ATR * 0.5 as buffer (clamped between 0.05% and 0.5% for safety)
                        buffer_pct = (latest_atr * 0.5) / current_price
                        buffer_val = max(0.0005, min(0.005, buffer_pct))
                        self.logger.debug(f"[SYNC] Using dynamic ATR buffer for {symbol}: {buffer_val:.4f} (ATR: {latest_atr:.2f})")
                except Exception as atr_err:
                    self.logger.debug(f"[SYNC] Could not calculate dynamic buffer for {symbol}: {atr_err}. Using fallback 0.1%")
                
                if side == 'BUY':
                    if tp and current_price >= tp * (1 - buffer_val):
                        is_hit, reason = True, f"TP passed or within {buffer_val*100:.2f}% buffer ({current_price} >= {tp * (1 - buffer_val):.5f})"
                    elif sl and current_price <= sl * (1 + buffer_val):
                        is_hit, reason = True, f"SL passed or within {buffer_val*100:.2f}% buffer ({current_price} <= {sl * (1 + buffer_val):.5f})"
                else: # SELL
                    if tp and current_price <= tp * (1 + buffer_val):
                        is_hit, reason = True, f"TP passed or within {buffer_val*100:.2f}% buffer ({current_price} <= {tp * (1 + buffer_val):.5f})"
                    elif sl and current_price >= sl * (1 - buffer_val):
                        is_hit, reason = True, f"SL passed or within {buffer_val*100:.2f}% buffer ({current_price} >= {sl * (1 - buffer_val):.5f})"
                
                if is_hit:
                    self.logger.warning(f"🚀 [URGENT] {pos_key} target hit locally ({reason}). Closing at Market.")
                    # Use a STABLE reason for Telegram to avoid bypassing deduper with price changes
                    stable_reason = f"Target Hit: {'TP' if 'TP' in reason else 'SL'}"
                    await self.force_close_position(pos_key, reason=stable_reason)
                    return {'sl_recreated': False, 'tp_recreated': False, 'status': 'closed'}

            # 3. Guards for Order Recreation (Actual Exchange actions)
            if self.dry_run or self.exchange.is_public_only:
                return {'sl_recreated': False, 'tp_recreated': False, 'status': 'skipped'}
                
            if not self.exchange.can_trade:
                self.logger.warning(f"[REPAIR] No trading permissions for {self.exchange_name}. Skipping repair for {pos_key}")
                result['status'] = 'permission_denied'
                return result

            if not config.AUTO_CREATE_SL_TP:
                result['errors'].append('auto_create_disabled')
                return result
            close_side = 'sell' if side == 'BUY' else 'buy'

            # ─── AUTO-CALCULATE MISSING SL/TP (TECHNICAL / ATR) ───
            # If SL/TP are missing (e.g. manual open or state lost), calc from entry price
            if (not sl or not tp) and pos.get('entry_price'):
                try:
                    entry_price = float(pos['entry_price'])
                    params_updated = False
                    
                    # Try Technical Calculation first (ATR)
                    tech_sl, tech_tp = await self._calculate_dynamic_sl_tp(symbol, side, entry_price)
                    
                    if not sl:
                        if tech_sl:
                            calc_sl = tech_sl
                            method = "ATR"
                        else:
                            # Fallback to Fixed %
                            if side == 'BUY':
                                calc_sl = entry_price * (1 - config.STOP_LOSS_PCT)
                            else:
                                calc_sl = entry_price * (1 + config.STOP_LOSS_PCT)
                            method = "PCT"
                        
                        try:
                            # Use CCXT precision handling via adapter's exchange instance
                            calc_sl = float(self.exchange.exchange.price_to_precision(symbol, calc_sl))
                        except Exception:
                            calc_sl = round(calc_sl, 4)
                            
                        pos['sl'] = calc_sl
                        sl = calc_sl
                        params_updated = True
                        self.logger.info(f"[REPAIR] Auto-calculated missing SL for {pos_key} using {method}: {sl}")

                    if not tp:
                        if tech_tp:
                            calc_tp = tech_tp
                            method = "ATR"
                        else:
                            # Fallback to Fixed %
                            if side == 'BUY':
                                calc_tp = entry_price * (1 + config.TAKE_PROFIT_PCT)
                            else:
                                calc_tp = entry_price * (1 - config.TAKE_PROFIT_PCT)
                            method = "PCT"
                            
                        try:
                            calc_tp = float(self.exchange.exchange.price_to_precision(symbol, calc_tp))
                        except Exception:
                            calc_tp = round(calc_tp, 4)
                            
                        pos['tp'] = calc_tp
                        tp = calc_tp
                        params_updated = True
                        self.logger.info(f"[REPAIR] Auto-calculated missing TP for {pos_key} using {method}: {tp}")
                    
                    if params_updated:
                        self.active_positions[pos_key] = pos
                        await self._update_db_position(pos_key)

                except Exception as e:
                    self.logger.error(f"[REPAIR] Auto-calc SL/TP failed for {pos_key}: {e}")
            # ─── END AUTO-CALC ──────────────────


            # ─── BYBIT: Use position-level SL/TP (trading-stop) ───────────────────────
            if self.exchange_name.upper() == 'BYBIT' and hasattr(self.exchange, 'set_position_sl_tp'):
                try:
                    set_sl = sl if (recreate_sl and sl) else None
                    set_tp = tp if (recreate_tp and tp) else None
                    
                    if set_sl or set_tp:
                        # Price validation (API errors 10001 protection)
                        # CRITICAL: Do NOT fall back to entry_price for validation if ticker failed.
                        # If price is unknown, we cannot safely set SL/TP.
                        val_price = current_price
                        
                        if val_price > 0:
                            # Use 0.2% buffer for Bybit to prevent "Immediate Trigger" rejections but limit deadzone
                            buffer_val = 0.002 if self.exchange_name.upper() == 'BYBIT' else 0.0
                            
                            if side == 'BUY':
                                # TP must be above price, SL must be below
                                if set_tp and set_tp <= val_price * (1 + buffer_val): 
                                    self.logger.warning(f"[Bybit Safety] Blocking TP {set_tp} for BUY (Price {val_price} + Buffer)")
                                    set_tp = None
                                if set_sl and set_sl >= val_price * (1 - buffer_val): 
                                    self.logger.warning(f"[Bybit Safety] Blocking SL {set_sl} for BUY (Price {val_price} - Buffer)")
                                    set_sl = None
                            else: # SELL
                                # TP must be below price, SL must be above
                                if set_tp and set_tp >= val_price * (1 - buffer_val): 
                                    self.logger.warning(f"[Bybit Safety] Blocking TP {set_tp} for SELL (Price {val_price} - Buffer)")
                                    set_tp = None
                                if set_sl and set_sl <= val_price * (1 + buffer_val): 
                                    self.logger.warning(f"[Bybit Safety] Blocking SL {set_sl} for SELL (Price {val_price} + Buffer)")
                                    set_sl = None

                        if set_sl or set_tp:
                            r = await self.exchange.set_position_sl_tp(symbol, side, sl=set_sl, tp=set_tp)
                            if r.get('sl_set'):
                                pos['sl_order_id'] = 'attached'
                                result['sl_recreated'] = True
                            if r.get('tp_set'):
                                pos['tp_order_id'] = 'attached'
                                result['tp_recreated'] = True
                            self.active_positions[pos_key] = pos
                            await self._update_db_position(pos_key)
                except Exception as e:
                    result['errors'].append(f'bybit_set_tpsl:{e}')
                return result
            # ─── END BYBIT ──────────────────────────────────────────────────────────────


            try:
                if recreate_sl and sl and (not pos.get('sl_order_id') or recreate_sl_force):
                    # SAFETY: Try to cancel old SL order "blindly" if ID exists in local state
                    old_sl = pos.get('sl_order_id')
                    if old_sl:
                        try:
                            await self.exchange.cancel_stop_orders(symbol, sl_id=old_sl)
                            self.logger.info(f"[RECREATE] Cancelled old SL {old_sl} before creating new one")
                        except Exception:
                            pass  # Ignore if already gone

                    # SL Validation (Already covered by Urgent Protection, but keeping as double-check)
                    is_valid_sl = False
                    if current_price > 0:
                        if side == 'BUY': 
                            is_valid_sl = sl < current_price
                        else:
                            is_valid_sl = sl > current_price
                    else:
                        is_valid_sl = True # Fallback
                    
                    if not is_valid_sl:
                        self.logger.warning(f"[SL SAFETY] Skipping SL for {pos_key}: SL {sl} vs Current {current_price} (Immediate Trigger Risk)")
                    else:
                        qty_to_use = min(float(qty), float(pos.get('qty') or qty))

                        # Cancel old first (via adapter), then place new
                        if old_sl:
                            try:
                                await self.exchange.cancel_stop_orders(symbol, sl_id=old_sl)
                            except Exception:
                                pass

                        ids = await self.exchange.place_stop_orders(
                            symbol, side, qty_to_use, sl=sl
                        )
                        if ids.get('sl_id'):
                            pos['sl_order_id'] = ids['sl_id']
                            self.active_positions[pos_key] = pos
                            await self._update_db_position(pos_key)
                            result['sl_recreated'] = True
                            self._debug_log('recreated_sl', pos_key, pos['sl_order_id'])

            except Exception as e:
                result['errors'].append(f'sl_recreate:{e}')

            # TP Recreation with Safety Check
            try:
                if recreate_tp and tp and (not pos.get('tp_order_id') or recreate_tp_force):
                    # SAFETY: Try to cancel old TP order "blindly" if ID exists in local state
                    old_tp = pos.get('tp_order_id')
                    if old_tp:
                        try:
                            await self.exchange.cancel_stop_orders(symbol, tp_id=old_tp)
                            self.logger.info(f"[RECREATE] Cancelled old TP {old_tp} before creating new one")
                        except Exception:
                            pass  # Ignore if already gone

                    # Safety check: (Already covered by Urgent Protection)
                    try:
                        if current_price > 0:
                            if side == 'BUY':
                                if tp <= current_price * 1.001:
                                    raise Exception("TP_SAFETY_ABORT")
                            else:
                                if tp >= current_price * 0.999:
                                    raise Exception("TP_SAFETY_ABORT")
                    except Exception as safety_error:
                        if "TP_SAFETY_ABORT" in str(safety_error):
                            raise  # Re-raise to skip TP creation
                        # If ticker fetch fails, log but continue with TP creation
                        print(f"[TP SAFETY] Could not verify TP safety for {pos_key}: {safety_error}")
                    
                    # Proceed with TP creation via adapter
                    try:
                        stored_qty = float(pos.get('qty') or qty)
                    except Exception:
                        stored_qty = qty
                    qty_to_use = min(float(qty), stored_qty)

                    # Cancel old first (via adapter), then place new
                    if old_tp:
                        try:
                            await self.exchange.cancel_stop_orders(symbol, tp_id=old_tp)
                        except Exception:
                            pass

                    ids = await self.exchange.place_stop_orders(
                        symbol, side, qty_to_use, tp=tp
                    )
                    if ids.get('tp_id'):
                        pos['tp_order_id'] = ids['tp_id']
                        self.active_positions[pos_key] = pos
                        await self._update_db_position(pos_key)
                        result['tp_recreated'] = True
                        self._debug_log('recreated_tp', pos_key, pos['tp_order_id'])
            except Exception as e:
                if "TP_SAFETY_ABORT" not in str(e):
                    result['errors'].append(f'tp_recreate:{e}')

            # persist any changes
            self.active_positions[pos_key] = pos
            await self._update_db_position(pos_key)
            return result

    async def enforce_isolated_on_startup(self, symbols=None):
        """Call at startup to enforce isolated mode + config.LEVERAGE for configured symbols (LIVE only).

        Args:
            symbols: iterable of symbol strings to enforce (if None, use symbols from `active_positions`).
        """
        if self.dry_run:
            return
        # GUARD: Public Mode (No keys) -> Skip
        if not getattr(self.exchange, 'can_trade', False):
            return

        if symbols is None:
            symbols = {v.get('symbol') for v in self.active_positions.values() if v.get('symbol')}
        else:
            symbols = set(symbols)

        for sym in symbols:
            await asyncio.sleep(0.5) # Reduction API pressure & jitter
            lev = self._safe_int(self.default_leverage, default=8)
            await self._ensure_isolated_and_leverage(sym, lev)

    async def modify_sl_tp(self, symbol, timeframe=None, new_sl=None, new_tp=None):
        """
        Modify SL/TP for an existing position by canceling old orders and creating new ones.
        """
        pos_key = self._get_pos_key(symbol, timeframe)
        position = self.active_positions.get(pos_key)
        if not position:
            print(f"[WARN] No active position found for {pos_key} to modify SL/TP")
            return False

        try:
            # 1. Targeted Cancel: Only cancel OUR specific SL/TP orders for this position
            old_sl_id = position.get('sl_order_id')
            old_tp_id = position.get('tp_order_id')
            
            # Encode cancel into adapter (handles BYBIT category, BINANCE algo, etc.)
            old_sl_id = position.get('sl_order_id')
            old_tp_id = position.get('tp_order_id')

            if old_sl_id or old_tp_id:
                try:
                    await self.exchange.cancel_stop_orders(symbol, sl_id=old_sl_id, tp_id=old_tp_id)
                    print(f"[MODIFY] Cancelled old SL/TP for {pos_key}")
                except Exception:
                    pass  # Already gone — safe to ignore
            
            # Ensure price precision matching exchange (round to 4 or 5 decimals)
            if new_sl: new_sl = round(float(new_sl), 5)
            if new_tp: new_tp = round(float(new_tp), 5)

            # Get position details
            qty = position['qty']
            side = position['side']
            close_side = 'sell' if side == 'BUY' else 'buy'

            # Place new orders via adapter (handles all exchange-specific params)
            ids = await self.exchange.place_stop_orders(
                symbol, side, float(qty),
                sl=new_sl if new_sl else None,
                tp=new_tp if new_tp else None
            )
            if ids.get('sl_id'):
                position['sl_order_id'] = ids['sl_id']
                print(f"[MODIFY] New SL placed for {symbol} @ {new_sl}")
            if ids.get('tp_id'):
                position['tp_order_id'] = ids['tp_id']
                print(f"[MODIFY] New TP placed for {symbol} @ {new_tp}")

            # Update position data
            if new_sl:
                position['sl'] = new_sl
            if new_tp:
                position['tp'] = new_tp
            self.active_positions[pos_key] = position
            await self._update_db_position(pos_key)

            return True

        except Exception as e:
            self.logger.error(f"Failed to modify SL/TP for {pos_key}: {e}")
            return False

    async def close(self):
        """Close exchange connection to release resources."""
        try:
            if hasattr(self.exchange, 'close'):
                await self.exchange.close()
        except Exception:
            pass

if __name__ == "__main__":
    import asyncio
    class MockExchange:
        async def create_order(self, *args, **kwargs):
            return {'id': '123', 'status': 'open', 'average': 50000, 'timestamp': 1234567}
        def milliseconds(self): return 1234567
    async def main():
        trader = Trader(MockExchange(), dry_run=True)
        await trader.place_order('BTC/USDT', 'buy', 0.001, sl=49000, tp=55000)
        print(f"Active: {trader.active_positions}")
        await trader.remove_position('BTC/USDT')
        print(f"After removal: {trader.active_positions}")
    asyncio.run(main())

# Bottom of file to prevent circular imports
from src.cooldown_manager import CooldownManager
from src.order_executor import OrderExecutor
