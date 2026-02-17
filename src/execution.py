import ccxt.async_support as ccxt
import logging
import json
import os
import tempfile
import asyncio

from config import (
    LEVERAGE, BINANCE_API_KEY, BINANCE_API_SECRET, AUTO_CREATE_SL_TP,
    ENABLE_PROFIT_LOCK, PROFIT_LOCK_THRESHOLD, PROFIT_LOCK_LEVEL,
    MAX_TP_EXTENSIONS, ATR_EXT_MULTIPLIER
)
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

# Logger Adapter for Exchange Prefix
class ExchangeLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return '[%s] %s' % (self.extra['exchange_name'], msg), kwargs

# Cooldown after SL (in seconds)
SL_COOLDOWN_SECONDS = 2 * 3600  # 2 hours cooldown after stop loss

class Trader:
    def __init__(self, exchange, dry_run=True, data_manager=None):
        self.exchange = exchange
        self.dry_run = dry_run
        
        # Configure Logger with Exchange Prefix
        # exchange.name is set by BaseAdapter (e.g., 'BINANCE', 'BYBIT')
        ex_name = getattr(exchange, 'name', 'UNKNOWN')
        self.exchange_name = ex_name
        self.logger = ExchangeLoggerAdapter(logging.getLogger(__name__), {'exchange_name': ex_name})
        
        # Use shared MarketDataManager for time synchronization
        if data_manager is None:
            from data_manager import MarketDataManager
            self.data_manager = MarketDataManager()
        else:
            self.data_manager = data_manager
            
        # Persistent storage for positions
        self.positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'positions.json')
        self.history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
        
        # Cross-Exchange Data Buckets (to prevent data loss while filtering)
        self._other_exchange_positions = {}
        self._other_pending_orders = {}
        
        self.active_positions = self._load_positions() 
        self.pending_orders = {}  # Track pending limit orders: {pos_key: {'order_id': id, 'symbol': symbol, 'side': side, 'price': price}}
        self._symbol_locks = {}  # Per-symbol locks to prevent entry race conditions
        self._position_locks = {}  # Per-position locks for SL/TP recreation
        self._sl_cooldowns = self._load_cooldowns()  # Persist cooldowns across restarts
        self.default_leverage = LEVERAGE
        self._missing_order_counts = {} # { order_id: missing_cycle_count }
        self._pos_action_timestamps = {} # { pos_key_action: timestamp_ms } to prevent rapid spam
        
        
        # Public Mode detection now handled by adapter.permissions
        # self.is_public_binance = ... (Removed, use self.exchange.is_public_only)
        
        # ...
        legacy_keys = [k for k in self.active_positions.keys() if '_sync' in k or '_adopted' in k]
        if legacy_keys:
            self.logger.info(f"[CLEANUP] Found {len(legacy_keys)} legacy position keys. Migrating...")
            for old_key in legacy_keys:
                pos = self.active_positions.pop(old_key)
                symbol = pos.get('symbol')
                if symbol:
                    # Rename to standard _adopted for consistency
                    new_key = f"{symbol}_adopted"
                    self.active_positions[new_key] = pos
            self._save_positions()

        # MIGRATION: Prefix legacy keys with current exchange name if not present
        migrated = False
        keys_to_migrate = list(self.active_positions.keys())
        current_exchange = self.exchange_name if self.exchange_name else 'BINANCE'
        
        for k in keys_to_migrate:
            # Check if likely legacy (no prefix like BINANCE_ or BYBIT_)
            if not k.startswith(('BINANCE_', 'BYBIT_')):
                val = self.active_positions.pop(k)
                # Use current exchange for migration
                new_key = f"{current_exchange}_{k}"
                # Also ensure val has 'symbol'
                if 'symbol' not in val:
                   # Try to recover symbol from key (e.g. ETH/USDT_1h)
                   parts = k.split('_')
                   if len(parts) >= 1:
                       val['symbol'] = parts[0]
                
                self.active_positions[new_key] = val
                migrated = True
                self.logger.info(f"[MIGRATION] Key {k} -> {new_key} (Assumed {current_exchange})")
        if migrated:
            self._save_positions()

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
        # Directly use the adapter's implementation instead of delegating to DataManager's default adapter
        return await self.exchange._execute_with_timestamp_retry(api_call, *args, **kwargs)

    def _load_positions(self):
        """Loads positions and pending orders from the JSON file, filtering for current exchange."""
        if os.path.exists(self.positions_file):
            try:
                with open(self.positions_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        self.active_positions = {}
                        self.pending_orders = {}
                        self._save_positions()
                        return self.active_positions
                    
                    data = json.loads(content)
                    
                    raw_active = {}
                    raw_pending = {}

                    # Backward compatibility check
                    if 'active_positions' in data or 'pending_orders' in data:
                        raw_active = data.get('active_positions', {})
                        raw_pending = data.get('pending_orders', {})
                    else:
                        raw_active = data
                    
                    # Partition Data by Exchange Prefix
                    prefix = f"{self.exchange_name}_"
                    filtered_active = {}
                    filtered_pending = {}
                    
                    for k, v in raw_active.items():
                        if k.startswith(prefix):
                            filtered_active[k] = v
                        else:
                            self._other_exchange_positions[k] = v
                            
                    for k, v in raw_pending.items():
                        if k.startswith(prefix):
                            filtered_pending[k] = v
                        else:
                            self._other_pending_orders[k] = v
                    
                    self.pending_orders = filtered_pending
                    if self.pending_orders:
                        self.logger.info(f"Loaded {len(filtered_active)} active positions and {len(self.pending_orders)} pending orders for {self.exchange_name}.")
                    
                    return filtered_active
                        
            except json.JSONDecodeError as e:
                self.logger.error(f"Error loading positions file: {e}")
                self.active_positions = {}
                self.pending_orders = {}
                # Do not overwrite on decode error to prevent data loss
            except Exception as e:
                self.logger.error(f"Error loading positions file: {e}")
                self.active_positions = {}
                self.pending_orders = {}
        else:
            self.active_positions = {}
            self.pending_orders = {}
            self._save_positions()
        return self.active_positions

    def _save_positions(self):
        """Saves current active positions AND pending orders to the JSON file."""
        import time
        
        # Merge current exchange data with data from other exchanges before saving
        merged_active = self._other_exchange_positions.copy()
        merged_active.update(self.active_positions)
        
        merged_pending = self._other_pending_orders.copy()
        merged_pending.update(self.pending_orders)

        # Prepare data structure
        data = {
            'active_positions': merged_active,
            'pending_orders': merged_pending
        }
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                tmp = self.positions_file + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                
                # Windows file locking workaround: retry replace
                try:
                    os.replace(tmp, self.positions_file)
                    return  # Success
                except PermissionError:
                    if attempt < max_retries - 1:
                        time.sleep(0.1)  # Wait 100ms and retry
                        continue
                    else:
                        raise
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                    continue
                else:
                    self.logger.error(f"Error saving positions file after {max_retries} attempts: {e}")
                    # Try to clean up tmp file
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except:
                        pass

    async def _verify_or_clear_id(self, pos_key, order_id, symbol, orders_in_snap):
        """Authoritative check for order existence. Returns (verified_id, order_data)."""
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
            # Error: "can only access an order if it is in last 500 orders"
            if "last 500 orders" in err_str or "acknowledged" in err_str:
                try:
                    self.logger.info(f"[SYNC] {symbol} order {order_id} not in last 500. Falling back to open order scan.")
                    
                    # 1. Try Open Orders (If it's not open, and it's too old for Bybit to fetch recent history, 
                    # we can usually assume it's closed/filled/cancelled if we don't see it in open orders)
                    open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                    found_o = next((o for o in open_orders if str(o.get('id')) == str(order_id) or str(o.get('clientOrderId')) == str(order_id)), None)
                    
                    if found_o:
                        self._missing_order_counts[order_id] = 0
                        return order_id, found_o
                    else:
                        # If not in open orders and too old for Bybit's fetchOrder, we "ghost" it
                        # as it's likely gone. But we do it only if we've seen this error.
                        self.logger.info(f"[SYNC] Order {order_id} not in open orders and too old for Bybit Fetch/History. Clearing.")
                        if order_id in self._missing_order_counts: del self._missing_order_counts[order_id]
                        return None, None
                        
                except Exception as fb_e:
                    self.logger.warning(f"[SYNC] Fallback search failed for {order_id}: {fb_e}")
            
            # FALLBACK: If standard fetch failed, try Algo Orders endpoint if it's a conditional order
            if 'not found' in err_str or 'order does not exist' in err_str:
                try:
                    # Check Algo orders directly for this symbol
                    algo_orders = await self._execute_with_timestamp_retry(self.exchange.fapiPrivateGetOpenAlgoOrders, {'symbol': self._normalize_symbol(symbol)})
                    found_algo = next((o for o in algo_orders if str(o.get('id')) == str(order_id)), None)
                    if found_algo:
                        self._missing_order_counts[order_id] = 0
                        return order_id, found_algo
                except Exception: pass # If algo check fails, proceed to grace period

                # 3. Grace Period path: Handle exchange indexing lag
                count = self._missing_order_counts.get(order_id, 0) + 1
                self._missing_order_counts[order_id] = count
                if count < 3:
                     self.logger.warning(f"[SYNC] {symbol} order {order_id} NOT FOUND (Cycle {count}/3). Keeping in Grace Period...")
                     return order_id, None # Assume ALIVE during grace period (No data)
                else:
                     self.logger.warning(f"[SYNC] {symbol} order {order_id} NOT FOUND after 3 cycles. Wiping ID.")
                     if order_id in self._missing_order_counts: del self._missing_order_counts[order_id]
                     return None, None
            else:
                # Network error or other API issue: Keep for safety
                self.logger.warning(f"[SYNC] Transient error verifying {order_id} for {symbol}: {e}")
                return order_id, None 

    def _normalize_symbol(self, symbol):
        """Standardize symbol format for reliable comparison (ARBUSDT style)."""
        if not symbol: return ""
        # Remove :USDT suffix (CCXT linear) then remove slashes
        return symbol.split(':')[0].replace('/', '').upper()

    def _clamp_leverage(self, lev):
        """Clamp leverage to allowed range (default 5-20)."""
        # Import dynamically to ensure we get latest config value
        from config import LEVERAGE as GLOBAL_MAX
        
        lv = self._safe_int(lev, default=None)
        if lv is None:
            lv = self._safe_int(self.default_leverage, default=5)
            
        # 1. Clamp to global max setting (User safety preference)
        if lv > GLOBAL_MAX:
            lv = GLOBAL_MAX
            
        # 2. Hard limits: ensure between 1 and 20 (Exchange limits)
        return max(1, min(20, lv))

    def _debug_log(self, *parts):
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'execution_debug.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f"{self.exchange.id if hasattr(self.exchange,'id') else 'exchange'} | {str(parts)}\n")
        except Exception:
            pass
    
    def _load_cooldowns(self):
        """Load SL cooldowns from file."""
        cooldown_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cooldowns.json')
        if os.path.exists(cooldown_file):
            try:
                import time
                with open(cooldown_file, 'r') as f:
                    cooldowns = json.load(f)
                # Filter out expired cooldowns
                now = time.time()
                return {k: v for k, v in cooldowns.items() if v > now}
            except Exception:
                pass
        return {}
    
    def _save_cooldowns(self):
        """Save SL cooldowns to file."""
        cooldown_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cooldowns.json')
        try:
            with open(cooldown_file, 'w') as f:
                json.dump(self._sl_cooldowns, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving cooldowns: {e}")

    def set_sl_cooldown(self, symbol):
        """Set cooldown after a stop loss hit for this symbol."""
        import time
        expiry = time.time() + SL_COOLDOWN_SECONDS
        self._sl_cooldowns[symbol] = expiry
        self._save_cooldowns()  # Persist to file
        hours = SL_COOLDOWN_SECONDS / 3600
        self.logger.info(f"[COOLDOWN] {symbol} blocked for {hours:.1f} hours after SL")
        print(f"⏸️ [{symbol}] Cooldown activated for {hours:.1f} hours after SL")

    def is_in_cooldown(self, symbol):
        """Check if symbol is in cooldown period after SL."""
        import time
        if symbol not in self._sl_cooldowns:
            return False
        
        if time.time() >= self._sl_cooldowns[symbol]:
            # Cooldown expired, remove it
            del self._sl_cooldowns[symbol]
            self._save_cooldowns()  # Persist change
            return False
        
        return True

    def get_cooldown_remaining(self, symbol):
        """Get remaining cooldown time in minutes."""
        import time
        if symbol not in self._sl_cooldowns:
            return 0
        remaining = self._sl_cooldowns[symbol] - time.time()
        return max(0, remaining / 60)  # Return minutes

    def check_pending_limit_fills(self, symbol, timeframe, current_price):
        """
        [DRY RUN] Check if a pending limit order should be filled based on current price.
        Returns True if the order got filled, False otherwise.
        """
        pos_key = f"{symbol}_{timeframe}"
        
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
            self._save_positions()
            print(f"✅ [DRY RUN] Limit order FILLED: {symbol} {side} @ {limit_price:.3f} (current: {current_price:.3f})")
            return True
        
        return False

    def get_pending_positions(self):
        """Get all pending limit orders."""
        return {k: v for k, v in self.active_positions.items() if v.get('status') == 'pending'}

    def get_filled_positions(self):
        """Get all filled positions."""
        return {k: v for k, v in self.active_positions.items() if v.get('status') == 'filled'}

    async def has_any_symbol_position(self, symbol):
        """Checks if ANY position for the symbol exists (across any timeframe)."""
        norm_target = self._normalize_symbol(symbol)
        
        # 1. Check local storage (normalized)
        for p in self.active_positions.values():
            if self._normalize_symbol(p.get('symbol')) == norm_target:
                return True
        
        # 2. Check Exchange (Account Level)
        live_positions = []
        if not self.dry_run and self.exchange.can_trade:
             try:
                 live_positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions)
             except Exception as e:
                self.logger.error(f"Error checking exchange for symbol {symbol}: {e}")
        for pos in live_positions:
            if self._normalize_symbol(pos.get('symbol')) == norm_target and float(pos.get('contracts', 0)) > 0:
                return True
        
        return False

    async def get_open_position(self, symbol, timeframe=None):
        """Checks if there's an open position for the symbol/timeframe."""
        exchange_name = getattr(self.exchange, 'name', 'BINANCE')
        # 1. Check if we have the position locally
        key = f"{exchange_name}_{symbol}_{timeframe}" if timeframe else f"{exchange_name}_{symbol}"
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
                        self._save_positions()
                    break
            
            if not found_on_exchange and key in self.active_positions:
                return False
                
            return found_on_exchange
        except Exception as e:
            self.logger.error(f"Error fetching position from exchange for {symbol}: {e}")
            return key in self.active_positions

    def _check_min_notional(self, symbol, price, qty):
        """
        Check if order meets exchange minimum notional/amount requirements.
        Returns (is_valid, reason, correct_qty)
        """
        # If no price (market order), assumes checking against recent price
        if not price or price <= 0:
            return True, "Price unknown", qty
            
        market = self.exchange.market(symbol)
        min_cost = 0
        min_amount = 0
        
        if 'limits' in market:
            min_cost = market['limits'].get('cost', {}).get('min', 0) or 0
            min_amount = market['limits'].get('amount', {}).get('min', 0) or 0
        
        # fallback defaults if exchange info missing
        if min_cost == 0: min_cost = 5.0 
            
        notional = price * qty
        
        # 1. Check Amount
        if qty < min_amount:
            return False, f"Qty {qty} < Min Amount {min_amount}", 0
            
        # 2. Check Cost (Notional)
        if notional < min_cost:
            return False, f"Value ${notional:.2f} < Min Cost ${min_cost:.2f}", 0
            
        return True, "OK", qty

    async def place_order(self, symbol, side, amount, price=None, order_type='LIMIT', reduce_only=False, params={}):
        """
        Place an order with permission checks.
        """
        # 1. Permission Check
        if not self.exchange.can_trade:
            self.logger.info(f"📢 [PUBLIC MODE] Simulation: {side} {symbol} {amount} @ {price or 'MARKET'}")
            # Return a dummy order structure so calling code doesn't crash
            return {
                'id': f'sim_{int(time.time()*1000)}',
                'symbol': symbol,
                'status': 'closed', # Instant fill for sim? or open? Let's say closed for now to avoid management overhead
                'price': price,
                'amount': amount,
                'filled': amount,
                'side': side,
                'type': order_type,
                'info': {'msg': 'Simulation Mode (Public Only)'}
            }

        # 2. Rate Limit / Cooldown Checks
        # (This section is empty in the provided diff, assuming it's a placeholder for future logic)
        # The original place_order method follows this.

    async def place_order(self, symbol, side, qty, timeframe=None, order_type='market', price=None, sl=None, tp=None, timeout=None, leverage=10, signals_used=None, entry_confidence=None, snapshot=None):
        """Places an order and updates persistent storage. For limit orders, monitors fill in background."""
        # Validate qty - reject invalid orders
        if qty is None or qty <= 0:
            self.logger.warning(f"Rejected order: invalid qty={qty} for {symbol}")
            return None
        
        # Dynamic decimal places based on exchange metadata
        try:
            # Use CCXT's built-in precision handling
            # CRITICAL FIX FOR BYBIT: 
            # CCXT keys 'ADA/USDT' as Spot (prec 0.01) and 'ADA/USDT:USDT' as Swap (prec 1.0).
            # We must use the SWAP key for precision if we are trading perps.
            precision_symbol = symbol
            if self.exchange_name == 'BYBIT':
                swap_key = f"{symbol}:USDT"
                if swap_key in self.exchange.markets:
                    precision_symbol = swap_key
            
            # amount_to_precision returns a string
            qty_str = self.exchange.amount_to_precision(precision_symbol, qty)
            # CCXT expects float for amount, but creating it from the precision string is safest
            qty_rounded = float(qty_str)
            
            # [DEBUG] Check if rounding changed it significantly
            if abs(qty - qty_rounded) > (qty * 0.001):
                self.logger.info(f"Qty rounded: {qty} -> {qty_rounded} (str: {qty_str}) using {precision_symbol} rules")
                
        except Exception as e:
            self.logger.warning(f"amount_to_precision failed for {symbol}: {e}. Fallback to naive rounding.")
            # Fallback
            qty_decimals = 6 if price and price > 1000 else 3
            qty_rounded = round(qty, qty_decimals)
            
        # Validate minimum qty after rounding
        if qty_rounded <= 0:
            self.logger.warning(f"Rejected order: qty too small after rounding ({qty} -> {qty_rounded}) for {symbol}")
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
                self.logger.warning(f"Rejected order {symbol}: {reason}")
                print(f"⚠️ [{symbol}] Order rejected: {reason} (Notional: ${notional:.2f})")
                return None
        
        exchange_name = getattr(self.exchange, 'name', 'BINANCE')
        pos_key = f"{exchange_name}_{symbol}_{timeframe}" if timeframe else f"{exchange_name}_{symbol}"
        signals = signals_used or []
        confidence = entry_confidence or 0.5
        
        # Use string quantity for API (if available) to ensure exact precision
        # Falls back to float `qty` if string generation failed
        api_qty = qty_str if 'qty_str' in locals() and qty_str else qty
        print(f"🔧 [{exchange_name}] Sending Order: {side} {symbol} Qty={api_qty} (Type={api_qty.__class__.__name__}) @ {price or 'MARKET'}")

        if self.dry_run:
            self.logger.info(f"[DRY RUN] {side} {symbol} ({timeframe}): Qty={qty}, SL={sl}, TP={tp}")
            
            # Determine status based on order type
            is_limit = order_type == 'limit'
            status = 'pending' if is_limit else 'filled'
            
            print(f"[DRY RUN] Placed {side} {symbol} {timeframe} {qty} [{status.upper()}]")
            
            self.active_positions[pos_key] = {
                "symbol": symbol,
                "side": side.upper(),
                "qty": qty_rounded,
                "entry_price": round(price, 3) if isinstance(price, (int, float)) else price,
                "sl": round(sl, 3) if sl else None,
                "tp": round(tp, 3) if tp else None,
                "timeframe": timeframe,
                "order_type": order_type,
                "status": status,
                "leverage": leverage,
                "signals_used": signals,
                "entry_confidence": confidence,
                "snapshot": snapshot,
                "timestamp": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
            }
            self._save_positions()
            return {'id': 'dry_run_id', 'status': 'closed' if not is_limit else 'open', 'filled': qty if not is_limit else 0}

        # LIVE LOGIC
        params = {}
        if sl: params['stopLoss'] = str(sl)
        if tp: params['takeProfit'] = str(tp)

        # BYBIT: Force 'linear' category for USDT Perpetuals
        if self.exchange_name == 'BYBIT':
            params['category'] = 'linear'
            # Bybit V5 requires tpslMode if SL/TP are provided
            if sl or tp:
                params['tpslMode'] = 'Full'
                # Recommendation: Use MarkPrice for triggers to avoid wicks
                params['tpTriggerBy'] = 'MarkPrice'
                params['slTriggerBy'] = 'MarkPrice'

            # Force One-Way Mode (positionIdx=0) by default for simplicity
            # If user is in Hedge Mode, this might fail or require 1/2.
            # Assuming One-Way for this bot architecture.
            params['positionIdx'] = 0

        # Determine and clamp leverage to allowed range
        use_leverage = self._clamp_leverage(leverage)

        try:
            # Ensure margin mode & leverage set on exchange for LIVE orders
            if not self.dry_run:
                margin_params = {}
                lev_params = {}
                if self.exchange_name == 'BYBIT':
                    margin_params = {'category': 'linear'}
                    lev_params = {'category': 'linear'}

                if not self.dry_run:
                    # Double check if we can trade
                    if self.exchange.can_trade:
                        # Re-verify position on exchange before closing
                        # BYBIT: Skip set_margin_mode, as set_leverage handles it or it's account-level
                        if self.exchange_name != 'BYBIT':
                            try:
                                await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, symbol, 'isolated', margin_params)
                            except Exception:
                                pass # Often fails if already set, ignore

                        # 2. Set Leverage (CRITICAL - Retry Loop for Bybit)
                        lev_success = False
                        if self.exchange_name == 'BYBIT':
                            # Bybit sometimes needs a retry or specific category
                            for i in range(3):
                                try:
                                    print(f"🔧 [BYBIT] Setting Leverage to x{use_leverage} for {symbol} (Attempt {i+1})...")
                                    await self._execute_with_timestamp_retry(self.exchange.set_leverage, symbol, use_leverage, lev_params)
                                    lev_success = True
                                    break
                                except Exception as e:
                                    if "not modified" in str(e).lower():
                                        lev_success = True
                                        break
                                    print(f"⚠️ [BYBIT] Failed to set leverage: {e}")
                                    import asyncio
                                    await asyncio.sleep(0.5)
                        else:
                            # Standard for others
                            lev_success = True
                            await self._execute_with_timestamp_retry(self.exchange.set_leverage, symbol, use_leverage, lev_params)

        except Exception as e:
            # reduce log spam if "not modified"
            if "not modified" not in str(e).lower():
                self.logger.warning(f"Failed to set leverage for {symbol}: {e}")

        try:
            if order_type == 'market':
                # Generate Client Order ID for recovery
                import time
                client_id = f"bot_{symbol.replace('/', '')}_{side}_{int(time.time()*1000)}"
                params['newClientOrderId'] = client_id
                
                # Debug log: attempting market order
                try:
                    self._debug_log('place_order:market', {'symbol': symbol, 'side': side, 'qty': qty, 'params': params, 'leverage': use_leverage})
                except Exception:
                    pass
                
                order = None
                try:
                    # ---------------------------------------------------------
                    # [DEBUG] LOG REQUEST FOR USER
                    # ---------------------------------------------------------
                    print(f"\n🚀 [API REQUEST] create_order")
                    print(f"   Symbol: {symbol}")
                    print(f"   Type:   {order_type}")
                    print(f"   Side:   {side}")
                    print(f"   Qty:    {qty}")
                    print(f"   Price:  {price}")
                    print(f"   Params: {params}  <-- Check 'category' here")
                    # ---------------------------------------------------------

                    order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, order_type, side, qty, price, params=params
                    )

                    # ---------------------------------------------------------
                    # [DEBUG] LOG RESPONSE FOR USER
                    # ---------------------------------------------------------
                    print(f"✅ [API RESPONSE] Order ID: {order.get('id')}")
                    print(f"   Full Response: {order}\n")
                    # ---------------------------------------------------------
                except Exception as e:
                    # TIMEOUT RECOVERY: If request timed out, check if order was actually placed using client_id
                    self.logger.warning(f"Order creation failed/timed out for {client_id}. Attempting recovery... Error: {e}")
                    print(f"⚠️ Order request failed/timed out. verifying {client_id}...")
                    
                    # Wait briefly before verifying
                    import asyncio
                    await asyncio.sleep(1)
                    
                    try:
                        # Try to fetch by Client ID
                        # Note: fetch_order by client_id usually works on Binance if we pass param
                        # Or fetch_open_orders and filter? fetch_order is better if supported.
                        # CCXT: fetch_order(id, symbol, params={'clientOrderId': ...}) depending on exchange.
                        # For Binance: fetch_order(id, symbol) where id can be clientOrderId usually requires logic.
                        # Safest: fetch_all_orders or fetch_open_orders?
                        # Binance allows fetching by origClientOrderId.
                        
                        # Try fetch_order with implicit ID logic or fetch_open_orders scan
                        # Let's try explicit fetch using params if needed, or just iterate active orders
                        # Actually, ccxt.binance fetch_order implementation: if id is not passed, it tries params['origClientOrderId']
                        
                        found_order = None
                        try:
                            # Attempt 1: Specific fetch using client id param (Binance specific)
                            found_order = await self.exchange.fetch_order(client_id, symbol, params={'origClientOrderId': client_id})
                        except Exception:
                            # Attempt 2: Scan open orders
                            open_orders = await self.exchange.fetch_open_orders(symbol)
                            for o in open_orders:
                                if o.get('clientOrderId') == client_id or o.get('info', {}).get('clientOrderId') == client_id:
                                    found_order = o
                                    break
                        
                        if found_order:
                            self.logger.info(f"✅ RECOVERED order {client_id} from timeout! ID: {found_order['id']}")
                            print(f"✅ RECOVERED order {client_id} from timeout!")
                            order = found_order
                        else:
                            raise e # Re-raise original error if not found (failed for real)
                            
                    except Exception as recovery_error:
                        self.logger.error(f"Recovery failed for {client_id}: {recovery_error}")
                        # If insufficient balance, log current balance to help debug
                        if "insufficient balance" in str(e).lower() or "170131" in str(e):
                            try:
                                # CALL ADAPTER fetch_balance
                                bal = await self.exchange.fetch_balance()
                                curr_bal = bal.get('total', {}).get('USDT', 'N/A')
                                # Calculate approximate required margin
                                req_margin = "N/A"
                                try:
                                    # qty * price / leverage
                                    # price might be None if market order fails at different stage, using current price if possible
                                    req_margin = (qty * (price or 0)) / (leverage or 1)
                                    req_margin = f"${req_margin:.2f}"
                                except: pass
                                
                                self.logger.error(f"❌ [INSUFFICIENT BALANCE] Exchange: {self.exchange_name} | Required Margin: ~{req_margin} | Available Equity: {curr_bal} USDT")
                                print(f"❌ [{self.exchange_name}] Insufficient balance: Need ~{req_margin}, have {curr_bal} USDT")
                            except Exception as bal_err:
                                self.logger.error(f"Diagnostics: fetch_balance failed: {bal_err}")
                        raise e # Re-raise original error
                
                try:
                    self._debug_log('place_order:market:response', order)
                except Exception:
                    pass

                # Save filled position immediately (use the leverage we enforced)
                self.active_positions[pos_key] = {
                    "symbol": symbol,
                    "side": side.upper(),
                    "qty": round(qty, 3),
                    "entry_price": round(order.get('average', price), 3),
                    "sl": round(sl, 3) if sl else None,
                    "tp": round(tp, 3) if tp else None,
                    "timeframe": timeframe,
                    "order_type": "market",
                    "status": "filled",
                    "leverage": use_leverage,
                    "signals_used": signals,
                    "entry_confidence": confidence,
                    "snapshot": snapshot,
                    "timestamp": order.get('timestamp', 0),
                    "order_id": order.get('id') # Ensure order_id is saved
                }
                self._save_positions()
                # Create SL/TP reduce-only orders for the freshly opened position
                try:
                    await self._create_sl_tp_orders_for_position(pos_key)
                except Exception as e:
                    self.logger.warning(f"Failed to auto-create SL/TP for market entry {pos_key}: {e}")

            else:
                # Limit order - place and track as pending
                
                # Generate Client Order ID for recovery
                import time
                client_id = f"bot_{symbol.replace('/', '')}_{side}_{int(time.time()*1000)}"
                params['newClientOrderId'] = client_id
                
                try:
                    self._debug_log('place_order:limit', {'symbol': symbol, 'side': side, 'qty': qty, 'price': price, 'params': params, 'leverage': use_leverage})
                except Exception:
                    pass
                    
                order = None
                try:
                    order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, order_type, side, qty, price, params=params
                    )
                except Exception as e:
                    # TIMEOUT RECOVERY for Limit Order
                    self.logger.warning(f"Limit Order creation failed/timed out for {client_id}. Attempting recovery... Error: {e}")
                    print(f"⚠️ Limit Order request failed/timed out. Verifying {client_id}...")
                    
                    import asyncio
                    await asyncio.sleep(1)
                    
                    try:
                        found_order = None
                        try:
                            found_order = await self.exchange.fetch_order(client_id, symbol, params={'origClientOrderId': client_id})
                        except Exception:
                            open_orders = await self.exchange.fetch_open_orders(symbol)
                            for o in open_orders:
                                if o.get('clientOrderId') == client_id or o.get('info', {}).get('clientOrderId') == client_id:
                                    found_order = o
                                    break
                        
                        if found_order:
                            self.logger.info(f"✅ RECOVERED limit order {client_id} from timeout! ID: {found_order['id']}")
                            print(f"✅ RECOVERED limit order {client_id} from timeout!")
                            order = found_order
                        else:
                            raise e 
                    except Exception as recovery_error:
                        self.logger.error(f"Recovery failed for {client_id}: {recovery_error}")
                        raise e

                try:
                    self._debug_log('place_order:limit:response', order)
                except Exception:
                    pass
                    
                # SAFETY CHECK: Ensure we have a valid order ID
                if not order or not order.get('id'):
                    self.logger.error(f"❌ Failed to get valid order ID for {symbol} limit order. Request may have failed silently.")
                    return None

                order_id = order['id']

                # Save to pending orders (in-memory)
                self.pending_orders[pos_key] = {
                    'order_id': order_id,
                    'symbol': symbol,
                    'side': side.upper(),
                    'qty': qty,
                    'price': price,
                    'sl': sl,
                    'tp': tp,
                    'timeframe': timeframe,
                    'leverage': use_leverage,
                    'signals_used': signals,
                    'entry_confidence': confidence,
                    'snapshot': snapshot,
                    'timestamp': order.get('timestamp', 0)
                }

                # Also save to active_positions with status='pending' AND persist order_id
                self.active_positions[pos_key] = {
                    "symbol": symbol,
                    "side": side.upper(),
                    "qty": round(qty, 3),
                    "entry_price": round(price, 3) if isinstance(price, (int, float)) else price,
                    "sl": round(sl, 3) if sl else None,
                    "tp": round(tp, 3) if tp else None,
                    "timeframe": timeframe,
                    "order_type": "limit",
                    "status": "pending",
                    "leverage": use_leverage,
                    "order_id": order_id,
                    "signals_used": signals,
                    "entry_confidence": confidence,
                    "snapshot": snapshot,
                    "timestamp": order.get('timestamp', 0)
                }
                self._save_positions()
                print(f"📋 Limit order placed: {order_id} | {side} {symbol} @ {price:.3f} (waiting for fill...)")
                self.logger.info(f"Limit order {order_id} placed, monitoring for fill")

                # Start background task to monitor fill
                import asyncio
                asyncio.create_task(self._monitor_limit_order_fill(pos_key, order_id, symbol))
                
                # Create SL/TP for pending order (with proper ID tracking)
                # This allows cancel_pending_order() to clean up SL/TP when cancelling entry
                try:
                    asyncio.create_task(self.setup_sl_tp_for_pending(symbol, timeframe))
                except Exception as e:
                    self.logger.warning(f"Failed to setup SL/TP for pending {pos_key}: {e}")

            self.logger.info(f"Order placed: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            try:
                self._debug_log('place_order:error', str(e))
            except Exception:
                pass
            return None

    async def _monitor_limit_order_fill(self, pos_key, order_id, symbol):
        """Background task to monitor a limit order fill; can resume using persisted order_id."""
        import asyncio
        fill_check_interval = 3  # seconds
        local_order_id = order_id

        try:
            while True:
                await asyncio.sleep(fill_check_interval)

                # Refresh order_id if missing in memory but present in active_positions
                if pos_key in self.pending_orders:
                    local_order_id = self.pending_orders[pos_key].get('order_id', local_order_id)
                else:
                    active = self.active_positions.get(pos_key, {})
                    if not active or active.get('status') != 'pending':
                        # No longer pending on disk -> stop monitoring
                        break
                    local_order_id = active.get('order_id', local_order_id)

                if not local_order_id:
                    break

                try:
                    order_status = await self._execute_with_timestamp_retry(
                        self.exchange.fetch_order, local_order_id, symbol
                    )
                except Exception as e:
                    self.logger.error(f"Error fetching order {local_order_id}: {e}")
                    continue

                # Determine filled qty and compare
                filled_qty = order_status.get('filled', 0) or order_status.get('amount', 0)
                expected_qty = None
                if pos_key in self.pending_orders:
                    expected_qty = self.pending_orders[pos_key].get('qty')
                else:
                    # fallback to active_positions stored qty
                    active = self.active_positions.get(pos_key, {})
                    expected_qty = active.get('qty')

                # STRICT FILLED CHECK - Prevent false positives
                # Must satisfy ALL conditions:
                # 1. Status is explicitly 'closed' or 'filled'
                # 2. filled_qty > 0 (actually filled something)
                # 3. filled_qty >= 99% of expected (allow small rounding)
                status = order_status.get('status', '').lower()
                is_filled_status = status in ('closed', 'filled')
                has_filled_qty = filled_qty and float(filled_qty) > 0
                is_fully_filled = False
                fill_ratio = 0.0  # Initialize to avoid NameError
                
                if has_filled_qty and expected_qty:
                    fill_ratio = float(filled_qty) / float(expected_qty)
                    is_fully_filled = fill_ratio >= 0.99  # 99% threshold for rounding tolerance
                
                # Log for debugging
                if pos_key:
                    self.logger.debug(f"[FILL CHECK] {pos_key} | Status: {status} | Filled: {filled_qty}/{expected_qty} | Ratio: {fill_ratio:.2f}")
                
                # ONLY mark as filled if ALL conditions met
                if is_filled_status and has_filled_qty and is_fully_filled:
                    # Choose pending details if available, else read from active_positions
                    pending = self.pending_orders.get(pos_key) or self.active_positions.get(pos_key, {})
                    fill_price = order_status.get('average') or pending.get('price') or pending.get('entry_price')
                    # Move to filled
                    self.active_positions[pos_key] = {
                        "symbol": pending.get('symbol', symbol),
                        "side": pending.get('side', 'BUY'),
                        "qty": round(pending.get('qty', 0), 3),
                        "entry_price": round(fill_price, 3) if isinstance(fill_price, (int, float)) else fill_price,
                        "sl": round(pending.get('sl', 3), 3) if pending.get('sl') else None,
                        "tp": round(pending.get('tp', 3), 3) if pending.get('tp') else None,
                        "timeframe": pending.get('timeframe'),
                        "order_type": "limit",
                        "status": "filled",
                        "leverage": pending.get('leverage', self.default_leverage),
                        "signals_used": pending.get('signals_used', []),
                        "entry_confidence": pending.get('entry_confidence', 0.5),
                        "snapshot": pending.get('snapshot'),
                        "timestamp": order_status.get('timestamp', pending.get('timestamp'))
                    }
                    # Persist and cleanup
                    self._save_positions()
                    # After marking filled, ensure SL/TP conditional orders are placed
                    try:
                        await self._create_sl_tp_orders_for_position(pos_key)
                    except Exception as e:
                        self.logger.warning(f"Failed to create SL/TP after fill for {pos_key}: {e}")
                    if pos_key in self.pending_orders:
                        del self.pending_orders[pos_key]

                    print(f"✅ Limit order FILLED: {symbol} {self.active_positions[pos_key]['side']} @ {self.active_positions[pos_key]['entry_price']:.3f}")
                    self.logger.info(f"Limit order {local_order_id} filled at {self.active_positions[pos_key]['entry_price']}")
                    break

                # If cancelled externally
                if order_status.get('status') in ('canceled', 'cancelled'):
                    if pos_key in self.pending_orders:
                        del self.pending_orders[pos_key]
                    # Mark as cancelled in active_positions (or remove)
                    if pos_key in self.active_positions:
                        self.active_positions[pos_key]['status'] = 'cancelled'
                        self._save_positions()
                    self.logger.info(f"Limit order {local_order_id} was cancelled")
                    break

        except Exception as e:
            self.logger.error(f"Error in limit order monitor for {pos_key}: {e}")

    async def cancel_pending_order(self, pos_key, reason="Technical invalidation"):
        """Cancels a pending limit order AND its associated TP/SL orders."""
        if pos_key not in self.pending_orders and pos_key not in self.active_positions:
            return False
        
        # Get order info from either source
        pending = self.pending_orders.get(pos_key)
        if pending:
            order_id = pending['order_id']
            symbol = pending['symbol']
            sl_order_id = pending.get('sl_order_id')
            tp_order_id = pending.get('tp_order_id')
        else:
            # Dry run: position is in active_positions with status='pending'
            active_pos = self.active_positions.get(pos_key)
            if not active_pos or active_pos.get('status') != 'pending':
                return False
            order_id = 'dry_run_id'
            symbol = active_pos['symbol']
            sl_order_id = active_pos.get('sl_order_id')
            tp_order_id = active_pos.get('tp_order_id')
        
        try:
            if not self.dry_run and pending:
                # Cancel limit order
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order, order_id, symbol
                )
                
                # Cancel SL order if exists
                if sl_order_id:
                    try:
                        await self._execute_with_timestamp_retry(
                            self.exchange.cancel_order, sl_order_id, symbol
                        )
                        print(f"  ✓ Cancelled SL order {sl_order_id}")
                    except Exception as e:
                        self.logger.warning(f"Failed to cancel SL {sl_order_id}: {e}")
                
                # Cancel TP order if exists
                if tp_order_id:
                    try:
                        await self._execute_with_timestamp_retry(
                            self.exchange.cancel_order, tp_order_id, symbol
                        )
                        print(f"  ✓ Cancelled TP order {tp_order_id}")
                    except Exception as e:
                        self.logger.warning(f"Failed to cancel TP {tp_order_id}: {e}")
            
            # Clean up from both sources
            if pos_key in self.pending_orders:
                del self.pending_orders[pos_key]
            
            print(f"❌ Cancelled pending order: {symbol} | Reason: {reason}")
            self.logger.info(f"Cancelled limit order {order_id}: {reason}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_order(self, order_id, symbol, params={}):
        if not self.exchange.can_trade:
             self.logger.info(f"📢 [PUBLIC MODE] Simulation: Cancel Order {order_id} on {symbol}")
             return {'status': 'canceled', 'id': order_id}

        try:
            return await self.exchange.cancel_order(order_id, symbol, params)
        except Exception as e:
            self.logger.error(f"❌ Cancel failed for {symbol}: {e}")
            return None

    async def log_trade(self, pos_key, exit_price, exit_reason):
        """Logs a closed trade to the history file."""
        pos = self.active_positions.get(pos_key)
        if not pos:
            return

        symbol = pos.get('symbol', pos_key.split('_')[0])
        entry_price = pos.get('entry_price')
        side = pos.get('side')
        qty = pos.get('qty')
        
        # Calculate PnL (Simplified)
        pnl = 0
        if isinstance(entry_price, (int, float)):
            if side == 'BUY':
                pnl = (exit_price - entry_price) * qty
            else:
                pnl = (entry_price - exit_price) * qty

        trade_record = {
            "symbol": symbol,
            "side": side,
            "entry_price": round(entry_price, 3) if isinstance(entry_price, (int, float)) else entry_price,
            "exit_price": round(exit_price, 3),
            "qty": round(qty, 3),
            "pnl_usdt": round(pnl, 3),
            "exit_reason": exit_reason,
            "entry_time": pos.get('timestamp'),
            "exit_time": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
        }

        # Load existing history (tolerate corrupted or legacy formats)
        history = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    history = loaded
                elif isinstance(loaded, dict):
                    for k in ('history', 'trades', 'trade_history', 'records', 'data'):
                        if k in loaded and isinstance(loaded[k], list):
                            history = loaded[k]
                            break
                    else:
                        history = []
                else:
                    history = []
            except Exception:
                history = []

        history.append(trade_record)
        
        # Keep last 100 trades to avoid file bloat
        if len(history) > 100:
            history = history[-100:]

        try:
            with open(self.history_file, 'w') as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving history file: {e}")

    async def remove_position(self, symbol, timeframe=None, exit_price=None, exit_reason=None):
        """Removes a position and optionally logs it to history."""
        key = f"{symbol}_{timeframe}" if timeframe else symbol
        if key in self.active_positions:
            if exit_price is not None:
                await self.log_trade(key, exit_price, exit_reason)
            
            del self.active_positions[key]
            self._save_positions()
            self.logger.info(f"Position for {key} removed.")
            return True
        return False

    # ========== ADAPTIVE POSITION ADJUSTMENT (v2.0) ==========
    
    def tighten_sl(self, pos_key, factor=0.5):
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
        
        if not all([entry, old_sl, side]):
            return None
        
        # Calculate new SL
        sl_distance = abs(entry - old_sl)
        new_distance = sl_distance * (1 - factor)  # Reduce distance by factor
        
        if side == 'BUY':
            new_sl = entry - new_distance  # For long, SL below entry
        else:
            new_sl = entry + new_distance  # For short, SL above entry
        
        # Update position
        pos['sl'] = round(new_sl, 4)
        pos['sl_tightened'] = True
        self._save_positions()
        
        print(f"⚠️ [TIGHTEN SL] {pos_key}: {old_sl:.4f} → {new_sl:.4f} ({factor*100:.0f}% closer)")
        self.logger.info(f"[TIGHTEN SL] {pos_key}: {old_sl} → {new_sl}")
        
        return new_sl
    
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
        if self.exchange_name and not pos_key.startswith(self.exchange_name):
            return False
            
        if not ENABLE_PROFIT_LOCK:
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
            
        # 1. Avoid continuous price shifting if already locked
        if pos.get('profit_locked'):
            return False

        # 2. Calculate Progress
        total_dist = abs(tp - entry)
        if total_dist == 0: return False
        
        current_profit_dist = abs(current_price - entry)
        # Ensure we are actually in profit before calculating progress
        if side == 'BUY' and current_price < entry: return False
        if side == 'SELL' and current_price > entry: return False
        
        progress = current_profit_dist / total_dist
        
        # Only proceed if we reached 80% threshold
        if progress < PROFIT_LOCK_THRESHOLD:
            return False
            
        # 3. Calculate Positive SL (Lock in PROFIT_LOCK_LEVEL of target profit)
        lock_amount = total_dist * PROFIT_LOCK_LEVEL
        if side == 'BUY':
            new_sl = entry + lock_amount
        else:
            new_sl = entry - lock_amount
            
        # 4. TA-Based TP Extension
        new_tp = tp
        extension_count = pos.get('tp_extensions', 0)
        
        if extension_count < MAX_TP_EXTENSIONS:
            if side == 'BUY':
                # Use resistance if available and above current TP
                if resistance and resistance > tp:
                    # Cap extension to 1.5x original total distance from entry
                    max_tp = entry + (total_dist * 1.5)
                    new_tp = min(resistance, max_tp)
                elif atr:
                    # Fallback to ATR-based extension
                    new_tp = tp + (atr * ATR_EXT_MULTIPLIER)
            else:
                # Use support if available and below current TP
                if support and support < tp:
                    # Cap extension to 1.5x original total distance from entry
                    max_tp = entry - (total_dist * 1.5)
                    new_tp = max(support, max_tp)
                elif atr:
                    # Fallback to ATR-based extension
                    new_tp = tp - (atr * ATR_EXT_MULTIPLIER)
        
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
            self._save_positions()
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
            del self.active_positions[pos_key]
            self._save_positions()
            self.logger.info(f"[DRY RUN] Force closed {pos_key}: {reason}")
            return True
        
        # Live: Close at market
        try:
            close_side = 'sell' if side == 'BUY' else 'buy'
            order = await self._execute_with_timestamp_retry(
                self.exchange.create_order, symbol, 'market', close_side, qty,
                params={'reduceOnly': True}
            )
            
            del self.active_positions[pos_key]
            if pos_key in self.pending_orders:
                del self.pending_orders[pos_key]
            self._save_positions()
            self.logger.info(f"Force closed {pos_key}: {reason}, order={order['id']}")
            return True
            
        except Exception as e:
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
    
    def get_all_filled_positions(self):
        """Get all filled (active) positions for adjustment check."""
        return {
            k: v for k, v in self.active_positions.items() 
            if v.get('status', 'filled') == 'filled'
        }

    async def set_mode(self, symbol, leverage):
        """Sets leverage and margin mode."""
        if self.dry_run: return
        try:
            # Correct Adapter Order: (symbol, value)
            await self._execute_with_timestamp_retry(self.exchange.set_leverage, symbol, leverage)
            try:
                if self.exchange_name == 'BINANCE':
                    await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, symbol, 'isolated')
            except Exception:
                pass # Margin mode might fail if already set or account level, ignore
        except Exception as e:
            self.logger.warning(f"Failed to set mode for {symbol}: {e}")

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
        For all pending limit orders, setup SL/TP conditional orders (Binance Futures).
        """
        pos_key = f"{symbol}_{timeframe}" if timeframe else symbol
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
            
            if sl and not existing_sl_id:
                # Ensure we never place SL for a quantity larger than the stored position
                try:
                    stored_qty = float(self.active_positions.get(pos_key, {}).get('qty') or qty)
                except Exception:
                    stored_qty = qty
                qty_to_use = min(float(qty), stored_qty)
                # Use 'market' for Bybit SL/TP conditional orders (STOP_MARKET is invalid)
                order_type = 'market' if self.exchange_name == 'BYBIT' else 'STOP_MARKET'
                sl_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, close_side, qty_to_use,
                    params={
                        'stopPrice': sl,
                        'reduceOnly': True
                    }
                )
                
                # VERIFY RESPONSE: Only save ID if exchange confirms success
                sl_order_id = sl_order.get('id')
                sl_status = sl_order.get('status', '').upper()
                
                if not sl_order_id:
                    self.logger.error(f"[SL FAILED] No order ID in response for {symbol}")
                    print(f"❌ [SL FAILED] Exchange did not return order ID")
                elif sl_status in ('REJECTED', 'CANCELED', 'EXPIRED'):
                    self.logger.error(f"[SL FAILED] Order rejected by exchange: {sl_status} | Response: {sl_order}")
                    print(f"❌ [SL FAILED] Exchange rejected: {sl_status}")
                else:
                    # SUCCESS: Save ID
                    results['sl_order'] = sl_order
                    pending['sl_order_id'] = sl_order_id
                    # CRITICAL: Also save to self.pending_orders (in-memory) for cancel_pending_order()
                    if pos_key in self.pending_orders:
                        self.pending_orders[pos_key]['sl_order_id'] = sl_order_id
                    if pos_key in self.active_positions:
                        self.active_positions[pos_key]['sl_order_id'] = sl_order_id
                        self._save_positions()
                    print(f"[SETUP] SL order placed for {symbol} at {sl} (id={sl_order_id}, status={sl_status})")
                    self.logger.info(f"[SL SUCCESS] {symbol} SL order confirmed by exchange: {sl_order_id}")
            elif sl and existing_sl_id:
                print(f"[SKIP] SL already exists for {pos_key} (id={existing_sl_id})")
                
            if tp and not existing_tp_id:
                try:
                    stored_qty = float(self.active_positions.get(pos_key, {}).get('qty') or qty)
                except Exception:
                    stored_qty = qty
                qty_to_use = min(float(qty), stored_qty)
                # Use 'market' for Bybit SL/TP conditional orders (TAKE_PROFIT_MARKET is invalid)
                order_type = 'market' if self.exchange_name == 'BYBIT' else 'TAKE_PROFIT_MARKET'
                tp_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, close_side, qty_to_use,
                    params={
                        'stopPrice': tp,
                        'reduceOnly': True
                    }
                )
                
                # VERIFY RESPONSE: Only save ID if exchange confirms success
                tp_order_id = tp_order.get('id')
                tp_status = tp_order.get('status', '').upper()
                
                if not tp_order_id:
                    self.logger.error(f"[TP FAILED] No order ID in response for {symbol}")
                    print(f"❌ [TP FAILED] Exchange did not return order ID")
                elif tp_status in ('REJECTED', 'CANCELED', 'EXPIRED'):
                    self.logger.error(f"[TP FAILED] Order rejected by exchange: {tp_status} | Response: {tp_order}")
                    print(f"❌ [TP FAILED] Exchange rejected: {tp_status}")
                else:
                    # SUCCESS: Save ID
                    results['tp_order'] = tp_order
                    pending['tp_order_id'] = tp_order_id
                    # CRITICAL: Also save to self.pending_orders (in-memory) for cancel_pending_order()
                    if pos_key in self.pending_orders:
                        self.pending_orders[pos_key]['tp_order_id'] = tp_order_id
                    if pos_key in self.active_positions:
                        self.active_positions[pos_key]['tp_order_id'] = tp_order_id
                        self._save_positions()
                    print(f"[SETUP] TP order placed for {symbol} at {tp} (id={tp_order_id}, status={tp_status})")
                    self.logger.info(f"[TP SUCCESS] {symbol} TP order confirmed by exchange: {tp_order_id}")
            elif tp and existing_tp_id:
                print(f"[SKIP] TP already exists for {pos_key} (id={existing_tp_id})")
                
            return results
        except Exception as e:
            print(f"[ERROR] Failed to setup SL/TP for {pos_key}: {e}")
            return False

    async def _ensure_isolated_and_leverage(self, symbol, leverage):
        """Ensure margin mode is isolated and leverage set for `symbol` on exchange (LIVE only)."""
        if self.dry_run:
            return
            
        try:
            # 1. MARGIN MODE SETUP
            # Try REST calls to Binance Futures only if it's Binance and we have keys
            is_real_binance = (self.exchange_name == 'BINANCE' and BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY)
            
            if is_real_binance:
                try:
                    await self._execute_with_timestamp_retry(self._set_margin_type_rest, symbol, 'ISOLATED')
                except Exception as e:
                    self.logger.debug(f"REST set_margin_type failed for {symbol}: {e}")
            
            # CCXT Adapter Call (for Bybit, or if Binance REST was skipped/failed)
            try:
                # Adapter signature is set_margin_mode(symbol, mode)
                await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, symbol, 'isolated')
            except Exception as e:
                err = str(e).lower()
                if "-4067" in err or "side cannot be changed" in err:
                    print(f"ℹ️  {symbol}: Margin mode preserved (open orders/positions exist)")
                elif any(s in err for s in ["no change", "already", "-4046", "not need to change", "no need to change"]):
                    pass # Already set, ignore
                else:
                    print(f"⚠️  Could not set margin mode for {symbol}: {e}")

            # 2. LEVERAGE SETUP
            safe_lev = self._safe_int(leverage, default=10)
            
            # Try REST calls to Binance first if appropriate
            if is_real_binance:
                try:
                    await self._execute_with_timestamp_retry(self._set_leverage_rest, symbol, safe_lev)
                except Exception as e:
                    err_str = str(e).lower()
                    if "-4161" in err_str or "leverage reduction is not supported" in err_str:
                        # Fallback to fetch current leverage for logging
                        cur_lev = "?"
                        try:
                            l_info = await self.exchange.fetch_leverage(symbol)
                            cur_lev = str(l_info.get('leverage', '?'))
                        except: pass
                        print(f"⚠️  {symbol}: Leverage reduction blocked (Open Position). Kept at {cur_lev}x")
                    else:
                        self.logger.warning(f"REST set_leverage failed for {symbol}: {e}")
            
            # CCXT Adapter Call (for Bybit, or if Binance REST was skipped/failed)
            # We skip this for Binance if we successfully did REST, but generally safe to try both or gate
            if self.exchange_name == 'BYBIT':
                try:
                    # Correct Adapter Order: (symbol, leverage)
                    await self._execute_with_timestamp_retry(self.exchange.set_leverage, symbol, safe_lev)
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        self.logger.warning(f"[Bybit] Set leverage failed for {symbol}: {e}")

        except Exception as e:
            self.logger.error(f"Error ensuring isolated/leverage for {symbol}: {e}")

    async def _set_margin_type_rest(self, symbol, margin_type='ISOLATED'):
        """Call Binance Futures API to change margin type (signed request)."""
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise RuntimeError('Binance API credentials missing')

        path = '/fapi/v1/marginType'
        base = 'https://fapi.binance.com'
        params = {
            'symbol': symbol.replace('/', ''),
            'marginType': margin_type,
            'recvWindow': 60000,
            'timestamp': self.data_manager.get_synced_timestamp()
        }
        return await self._binance_signed_post(base + path, params)

    async def _set_leverage_rest(self, symbol, leverage):
        """Call Binance Futures API to change leverage (signed request)."""
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise RuntimeError('Binance API credentials missing')

        path = '/fapi/v1/leverage'
        base = 'https://fapi.binance.com'
        params = {
            'symbol': symbol.replace('/', ''),
            'leverage': self._safe_int(leverage, default=10),
            'recvWindow': 60000,
            'timestamp': self.data_manager.get_synced_timestamp()
        }
        return await self._binance_signed_post(base + path, params)

    async def _binance_signed_post(self, url, params):
        """Perform signed POST to Binance Futures API using requests in thread to avoid blocking."""
        # Ensure timestamp is fresh if not provided
        if 'timestamp' not in params:
            params['timestamp'] = self.data_manager.get_synced_timestamp()
            
        def do_request(u, p):
            query = urlencode(p)
            signature = hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=10)
            if r.status_code >= 400:
                # Silence "No need to change margin type"
                if any(s in r.text.lower() for s in ["-4046", "no need to change", "not need to change", "already"]):
                    return {"code": 200, "msg": "No change needed"}
                raise Exception(f"Binance API Error {r.status_code}: {r.text}")
            return r.json()

        import asyncio
        return await asyncio.to_thread(do_request, url, params)

    async def _binance_batch_create(self, symbol, orders):
        """Create multiple orders atomically via Binance Futures `batchOrders` endpoint.

        `orders` should be a list of dicts matching Binance order schema, e.g.
        {"symbol":"BTCUSDT","side":"SELL","type":"TAKE_PROFIT_MARKET","stopPrice":"65000","closePosition":"true"}
        Returns parsed JSON list on success.
        """
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise RuntimeError('Binance API credentials missing')

        path = '/fapi/v1/batchOrders'
        base = 'https://fapi.binance.com'
        url = base + path

        body = {
            'batchOrders': json.dumps(orders),
            'recvWindow': 60000,
            'timestamp': self.data_manager.get_synced_timestamp()
        }

        def do_post(u, data):
            query = urlencode(data)
            signature = hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': BINANCE_API_KEY, 'Content-Type': 'application/x-www-form-urlencoded'}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=15)
            if r.status_code >= 400:
                raise Exception(f"Binance Batch API Error {r.status_code}: {r.text}")
            return r.json()

        import asyncio
        return await asyncio.to_thread(do_post, url, body)

    async def _create_sl_tp_orders_for_position(self, pos_key):
        """Create SL/TP reduce-only orders for a filled position and persist the order ids."""
        if self.dry_run or not self.exchange.can_trade:
            return {'skipped': True}
            
        # Respect the global flag: if auto-create disabled, skip creating orders here.
        if not AUTO_CREATE_SL_TP:
            self.logger.info(f"AUTO_CREATE_SL_TP disabled; skipping auto SL/TP for {pos_key}")
            return {'skipped': True}

        position = self.active_positions.get(pos_key)
        if not position:
            return None

        symbol = position.get('symbol')
        qty = position.get('qty')
        side = position.get('side')
        sl = position.get('sl')
        tp = position.get('tp')

        close_side = 'sell' if side == 'BUY' else 'buy'
        results = {}
        try:
            # Create SL then TP separately (no batch) as requested
            if sl:
                try:
                    try:
                        stored_qty = float(position.get('qty') or qty)
                    except Exception:
                        stored_qty = qty
                    qty_to_use = min(float(qty), stored_qty)
                    
                    # Bybit V5 requires triggerDirection for conditional orders
                    sl_params = {'stopPrice': sl, 'reduceOnly': True}
                    if self.exchange_name == 'BYBIT':
                        sl_params['triggerDirection'] = 'descending' if side == 'BUY' else 'ascending'

                    order_type = 'market' if self.exchange_name == 'BYBIT' else 'STOP_MARKET'
                    sl_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, order_type, close_side, qty_to_use,
                        params=sl_params
                    )
                    results['sl_order'] = sl_order
                    position['sl_order_id'] = str(sl_order.get('id'))
                    print(f"[AUTO-SL] Placed SL for {pos_key} id={position['sl_order_id']} @ {sl}")
                except Exception as e:
                    self.logger.error(f"Failed to place SL for {pos_key}: {e}")

            if tp:
                try:
                    try:
                        stored_qty = float(position.get('qty') or qty)
                    except Exception:
                        stored_qty = qty
                    qty_to_use = min(float(qty), stored_qty)
                    
                    # Bybit V5 requires triggerDirection for conditional orders
                    tp_params = {'stopPrice': tp, 'reduceOnly': True}
                    if self.exchange_name == 'BYBIT':
                        tp_params['triggerDirection'] = 'ascending' if side == 'BUY' else 'descending'

                    # FIX: Use market for Bybit TP
                    tp_order_type = 'market' if self.exchange_name == 'BYBIT' else 'TAKE_PROFIT_MARKET'
                    tp_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, tp_order_type, close_side, qty_to_use,
                        params=tp_params
                    )
                    results['tp_order'] = tp_order
                    position['tp_order_id'] = str(tp_order.get('id'))
                    print(f"[AUTO-TP] Placed TP for {pos_key} id={position['tp_order_id']} @ {tp}")
                except Exception as e:
                    self.logger.error(f"Failed to place TP for {pos_key}: {e}")

            # Persist order ids to positions
            self.active_positions[pos_key] = position
            self._save_positions()
            return results
        except Exception as e:
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

    async def reconcile_positions(self, auto_fix=True):
        """Attempt to synchronize local `active_positions` with exchange state."""
        if self.dry_run or self.exchange.is_public_only:
            return
            
        """
        - Recover missing order_ids for pending entries
        - Ensure filled positions have SL/TP orders on-exchange
        - AUTO-ADOPT: If position exists on exchange but not locally, add it.
        - AUTO-FIX: If SL/TP missing on exchange, recreate them.
        """
        summary = {'recovered_order_ids': 0, 'created_tp_sl': 0, 'errors': []}
        
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
            # Fetch ALL positions
            ex_positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions)
            active_ex_pos = {p['symbol']: p for p in ex_positions if self._safe_float(p.get('contracts'), 0) > 0}
            
            # 1. Fetch TOTAL Open Orders (Standard + Algo via Adapter)
            try:
                # Adapter handles both Std + Algo merging internally
                all_exchange_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders)
                
                if all_exchange_orders:
                    self.logger.info(f"[SYNC] Total visibility: {len(all_exchange_orders)} orders (Std + Algo)")
            except Exception as e:
                self.logger.warning(f"[SYNC] Total visibility fetch failed: {e}. Falling back to per-symbol fetch later.")
                all_exchange_orders = None

            fetch_success = True
        except Exception as e:
            self.logger.error(f"[SYNC] Critical fetch failed: {e}")
            summary['errors'].append(f"Sync failed (fetch error): {e}")

        # 2. ADOPT POSITIONS (Ex -> Local) - Only if fetch succeeded
        if fetch_success:
            for sym, p in active_ex_pos.items():
                # Check if we have this symbol in any timeframe
                norm_sym = self._normalize_symbol(sym)
                found = False
                for k, local_p in self.active_positions.items():
                    # STRICT PREFIX CHECK: local_p is already filtered to this exchange's prefix
                    if self._normalize_symbol(local_p.get('symbol')) == norm_sym and local_p.get('status') == 'filled':
                        found = True
                        break
                
                if not found:
                    # Bot found a position it didn't know about - Adopt it!
                    # Initialize prefix for adopted key
                    prefix = f"{self.exchange_name}_" if self.exchange_name else ""
                    pos_key = f"{prefix}{sym}_adopted"
                    self.logger.info(f"[ADOPT] Found unknown position for {sym} on exchange. Adopting as {pos_key}")
                    
                    # Fetch ACTUAL leverage setting from exchange
                    try:
                        lev_info = await self.exchange.fetch_leverage(sym)
                        actual_leverage = int(lev_info.get('leverage', 8))
                        self.logger.info(f"[ADOPT] Fetched actual leverage for {sym}: {actual_leverage}x")
                    except Exception as lev_err:
                        raw_leverage = p.get('leverage')
                        actual_leverage = int(raw_leverage) if raw_leverage is not None else 8
                        self.logger.warning(f"[ADOPT] Could not fetch leverage for {sym}, using fallback: {actual_leverage}x | Error: {lev_err}")
                    
                    # Calculate entry price
                    entry_price = self._safe_float(p.get('entryPrice'), default=0)
                    side = p['side'].upper()
                    
                    # Initialize SL/TP placeholders
                    auto_sl = None
                    auto_tp = None
                    sl_order_id = None
                    tp_order_id = None
                    
                    # CHECK EXISTING OPEN ORDERS FIRST (Filter from global list)
                    try:
                        symbol_orders = []
                        if all_exchange_orders is not None:
                            symbol_orders = [o for o in all_exchange_orders if o.get('symbol') == sym]
                        else:
                            # Fallback if global failed
                            symbol_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, sym)
                        
                        for o in symbol_orders:
                            o_type = str(o.get('type') or '').upper()
                            o_side = o.get('side').upper()
                            # Use stopPrice for conditional, price for basic if stopPrice missing
                            o_price = float(o.get('stopPrice') or o.get('price') or 0)
                            o_id = str(o.get('id'))
                            
                            expected_close_side = 'SELL' if side == 'LONG' else 'BUY'
                            
                            if o_side == expected_close_side:
                                # Logic from Gemini: STOP_MARKET or TAKE_PROFIT_MARKET
                                if 'STOP' in o_type:
                                    auto_sl = o_price
                                    sl_order_id = o_id
                                    self.logger.info(f"[ADOPT] Found existing SL for {sym}: {o_id} @ {o_price}")
                                elif 'TAKE' in o_type:
                                    auto_tp = o_price
                                    tp_order_id = o_id
                                    self.logger.info(f"[ADOPT] Found existing TP for {sym}: {o_id} @ {o_price}")
                    except Exception as e:
                        self.logger.warning(f"[ADOPT] Failed to check existing open orders for {sym}: {e}")

                    # Auto-calculate SL/TP only if NOT found
                    if not auto_sl:
                        if side == 'LONG':
                            auto_sl = round(entry_price * 0.97, 5)
                        elif side == 'SHORT':
                            auto_sl = round(entry_price * 1.03, 5)
                    
                    if not auto_tp:
                        if side == 'LONG':
                            auto_tp = round(entry_price * 1.03, 5)
                        elif side == 'SHORT':
                            auto_tp = round(entry_price * 0.97, 5)
                    
                    self.active_positions[pos_key] = {
                        "symbol": sym,
                        "side": side,
                        "qty": self._safe_float(p.get('contracts'), default=0),
                        "entry_price": entry_price,
                        "status": "filled",
                        "timeframe": "sync",
                        "leverage": actual_leverage,
                        "timestamp": p.get('timestamp') or self.exchange.milliseconds(),
                        "sl": auto_sl,
                        "tp": auto_tp,
                        "order_id": None, 
                        "sl_order_id": sl_order_id,
                        "tp_order_id": tp_order_id
                    }
                    self._save_positions()
                    print(f"[ADOPT] {pos_key} adopted with leverage {actual_leverage}x, auto SL={auto_sl} TP={auto_tp}")

        # 3. SYNC & REPAIR (Local <-> Ex)
        for pos_key, pos in list(self.active_positions.items()):
            # EXTRA GUARD: Skip if key does not match current exchange
            if self.exchange_name and not pos_key.startswith(self.exchange_name):
                continue

            try:
                symbol = pos.get('symbol')
                status = pos.get('status')
                qty = pos.get('qty')

                # Skip removal if fetch failed!
                if status == 'filled' and symbol not in active_ex_pos:
                    if fetch_success:
                        self.logger.info(f"[SYNC] Position {pos_key} no longer on exchange. Removing.")
                        del self.active_positions[pos_key]
                        self._save_positions()
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
                                    self._save_positions()
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
                            found_in_open = any(str(o.get('id') or o.get('orderId')) == str(order_id) for o in symbol_orders)
                            
                            if not found_in_open:
                                # Not open check if filled
                                if fetch_success and symbol in active_ex_pos:
                                    self.logger.info(f"[SYNC] Pending order {order_id} filled. Updating status.")
                                    pos['status'] = 'filled'
                                    ex_p = active_ex_pos[symbol]
                                    pos['entry_price'] = self._safe_float(ex_p.get('entryPrice'), pos['entry_price'])
                                    pos['qty'] = self._safe_float(ex_p.get('contracts'), pos['qty'])
                                    if 'leverage' in ex_p:
                                        pos['leverage'] = int(ex_p['leverage'])
                                    self.active_positions[pos_key] = pos
                                    self._save_positions()
                                else:
                                    # CANCELLED
                                    self.logger.info(f"[SYNC] Pending order {order_id} gone. Removing.")
                                    if pos_key in self.pending_orders:
                                        del self.pending_orders[pos_key]
                                    del self.active_positions[pos_key]
                                    self._save_positions()
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
                        last_creation = self._pos_action_timestamps.get(f"{pos_key}_recreation", 0)
                        if (self.exchange.milliseconds() - last_creation) < 20000: # 20 seconds trust period
                            # self.logger.info(f"[SYNC] Skipping verification for {pos_key} (In Grace Period)")
                            continue

                        # 1. VERIFY SL (With Grace Period)
                        # 1. VERIFY SL (With Grace Period)
                        found_sl, sl_order = await self._verify_or_clear_id(pos_key, found_sl, symbol, symbol_orders)
                        
                        # 2. VERIFY TP (With Grace Period)
                        found_tp, tp_order = await self._verify_or_clear_id(pos_key, found_tp, symbol, symbol_orders)

                        # SYNC PRICES FROM EXCHANGE (If verified)
                        prices_changed = False
                        if sl_order:
                            new_sl = float(sl_order.get('stopPrice') or sl_order.get('triggerPrice') or 0)
                            if new_sl > 0 and new_sl != pos.get('sl'):
                                self.logger.info(f"[SYNC] Updating SL price for {pos_key}: {pos.get('sl')} -> {new_sl}")
                                pos['sl'] = new_sl
                                prices_changed = True

                        if tp_order:
                            new_tp = float(tp_order.get('stopPrice') or tp_order.get('triggerPrice') or tp_order.get('price') or 0)
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
                            self._save_positions()

                        # C) AUTO-RECREATE IF TRULY MISSING
                        if auto_fix and (not found_sl or not found_tp):
                            print(f"🛠️ [REPAIR] {pos_key} is missing SL or TP on exchange. Recreating...")
                            
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
                    norm_symbol = self._normalize_symbol(o_symbol)
                    
                    # Check if this symbol belongs to our TRADING_SYMBOLS (normalized)
                    # UPDATED: Reaper now scans ALL account symbols to clear ghosts from previous runs
                    
                    # We only reaper STOP/TAKE orders (Conditional)
                    is_conditional = 'STOP' in o_type or 'TAKE' in o_type or o.get('algoType') in ('STOP_LOSS', 'TAKE_PROFIT')
                    
                    if is_conditional and o_id not in managed_ids:
                        self.logger.warning(f"🧹 [REAPER] Cancelling orphaned {o_type} order {o_id} for {o_symbol}")
                        try:
                            # Use appropriate cancel method for Algo orders if necessary
                            if o.get('algoType'):
                                # fapiPrivateDeleteOrder for algo orders
                                await self._execute_with_timestamp_retry(self.exchange.fapiPrivateDeleteOrder, {'symbol': o_symbol, 'orderId': o_id})
                            else:
                                await self._execute_with_timestamp_retry(self.exchange.cancel_order, o_id, o_symbol)
                            
                            summary['orphans_cancelled'] += 1
                            orphans_deleted += 1
                            # Rate limit protection: Increased to 0.5s + Batch limit
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            self.logger.warning(f"[REAPER] Failed to cancel {o_id}: {e}")

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
            self._save_positions()

        return res

    async def recreate_missing_sl_tp(self, pos_key, recreate_sl=True, recreate_tp=True, recreate_sl_force=False, recreate_tp_force=False):
        """Recreate missing SL/TP orders for a given position."""
        if self.dry_run or self.exchange.is_public_only:
            return {'sl_recreated': False, 'tp_recreated': False, 'status': 'skipped'}
            
        # Acquire per-position lock to prevent race conditions
        async with self._get_position_lock(pos_key):
            result = {'sl_recreated': False, 'tp_recreated': False, 'errors': []}
            pos = self.active_positions.get(pos_key)
            if not pos:
                result['errors'].append('position_not_found')
                return result

            # EXCHANGE-AWARE CHECK: Skip repairs if position belongs to a different exchange
            # This prevents Bybit bot from trying to repair Binance positions (causing errors/noise)
            if self.exchange_name and not pos_key.startswith(self.exchange_name):
                # self.logger.debug(f"[REPAIR] Skipping {pos_key} - belongs to different exchange")
                result['status'] = 'exchange_mismatch'
                return result
            
            # TRADING PERMISSION GUARD: Ensure we have private keys before attempting repair
            if not self.exchange.can_trade:
                self.logger.warning(f"[REPAIR] No trading permissions for {self.exchange_name}. Skipping repair for {pos_key}")
                result['status'] = 'permission_denied'
                return result

            # If global setting disables auto SL/TP creation, skip and return informational result
            if not AUTO_CREATE_SL_TP:
                result['errors'].append('auto_create_disabled')
                return result

            if self.dry_run:
                result['errors'].append('dry_run_mode')
                return result

            symbol = pos.get('symbol')
            side = pos.get('side')
            qty = pos.get('qty')
            sl = pos.get('sl')
            tp = pos.get('tp')
            close_side = 'sell' if side == 'BUY' else 'buy'

            # SL Recreation
            try:
                if recreate_sl and sl and (not pos.get('sl_order_id') or recreate_sl_force):
                    # SAFETY: Try to cancel old SL order "blindly" if ID exists in local state
                    old_sl = pos.get('sl_order_id')
                    if old_sl:
                        try:
                            cancel_params = {}
                            if self.exchange_name == 'BYBIT':
                                cancel_params['category'] = 'linear'
                            await self._execute_with_timestamp_retry(self.exchange.cancel_order, old_sl, symbol, params=cancel_params)
                            self.logger.info(f"[RECREATE] Cancelled old SL {old_sl} before creating new one")
                        except Exception: pass # Ignore if already gone

                    # SL Validation
                    current_price = (await self.exchange.fetch_ticker(symbol))['last']
                    is_valid_sl = False
                    if side == 'BUY': # Long: SL must be < Current
                        is_valid_sl = sl < current_price
                    else: # Short: SL must be > Current
                        is_valid_sl = sl > current_price
                    
                    if not is_valid_sl:
                        self.logger.warning(f"[SL SAFETY] Skipping SL for {pos_key}: SL {sl} vs Current {current_price} (Immediate Trigger Risk)")
                        # Do not return, just skip SL creation to verify TP
                    else:
                        qty_to_use = min(float(qty), float(pos.get('qty') or qty))
                        
                        # Bybit V5 requires triggerDirection for conditional orders
                        sl_params = {'stopPrice': sl, 'reduceOnly': True}
                        if self.exchange_name == 'BYBIT':
                            sl_params['triggerDirection'] = 'descending' if side == 'BUY' else 'ascending'

                        order_type = 'market' if self.exchange_name == 'BYBIT' else 'STOP_MARKET'
                        sl_order = await self._execute_with_timestamp_retry(
                            self.exchange.create_order, symbol, order_type, close_side, qty_to_use,
                            params=sl_params
                        )
                        pos['sl_order_id'] = str(sl_order.get('id'))
                        self.active_positions[pos_key] = pos
                        self._save_positions()
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
                            cancel_params = {}
                            if self.exchange_name == 'BYBIT':
                                cancel_params['category'] = 'linear'
                            await self._execute_with_timestamp_retry(self.exchange.cancel_order, old_tp, symbol, params=cancel_params)
                            self.logger.info(f"[RECREATE] Cancelled old TP {old_tp} before creating new one")
                        except Exception: pass # Ignore core errors if already gone

                    # Safety check: Verify TP price won't trigger immediately
                    try:
                        ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)
                        current_price = float(ticker.get('last') or ticker.get('close'))
                        
                        # Check if TP would trigger immediately (with 0.1% buffer)
                        # Ensure current_price and tp are not None
                        if current_price is None or tp is None:
                            raise Exception("TP_SAFETY_NO_PRICE")

                        if side == 'BUY':
                            # For LONG, TP should be above current price
                            if tp <= current_price * 1.001:
                                result['errors'].append(f'tp_safety_abort:TP {tp} too close to current {current_price}')
                                print(f"[TP SAFETY] Aborting TP creation for {pos_key}: TP {tp} <= current {current_price}")
                                # Skip TP creation but don't fail the entire operation
                                raise Exception("TP_SAFETY_ABORT")
                        else:
                            # For SHORT, TP should be below current price
                            if tp >= current_price * 0.999:
                                result['errors'].append(f'tp_safety_abort:TP {tp} too close to current {current_price}')
                                print(f"[TP SAFETY] Aborting TP creation for {pos_key}: TP {tp} >= current {current_price}")
                                raise Exception("TP_SAFETY_ABORT")
                    except Exception as safety_error:
                        if "TP_SAFETY_ABORT" in str(safety_error):
                            raise  # Re-raise to skip TP creation
                        # If ticker fetch fails, log but continue with TP creation
                        print(f"[TP SAFETY] Could not verify TP safety for {pos_key}: {safety_error}")
                    
                    # Proceed with TP creation
                    try:
                        stored_qty = float(pos.get('qty') or qty)
                    except Exception:
                        stored_qty = qty
                    qty_to_use = min(float(qty), stored_qty)
                    
                    # Bybit V5 requires triggerDirection for conditional orders
                    tp_params = {'stopPrice': tp, 'reduceOnly': True}
                    if self.exchange_name == 'BYBIT':
                        tp_params['triggerDirection'] = 'ascending' if side == 'BUY' else 'descending'

                    tp_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', close_side, qty_to_use,
                        params=tp_params
                    )
                    pos['tp_order_id'] = str(tp_order.get('id'))
                    self.active_positions[pos_key] = pos
                    self._save_positions()
                    result['tp_recreated'] = True
                    self._debug_log('recreated_tp', pos_key, pos['tp_order_id'])
            except Exception as e:
                if "TP_SAFETY_ABORT" not in str(e):
                    result['errors'].append(f'tp_recreate:{e}')

            # persist any changes
            self.active_positions[pos_key] = pos
            self._save_positions()
            return result

    async def enforce_isolated_on_startup(self, symbols=None):
        """Call at startup to enforce isolated mode + leverage for configured symbols (LIVE only).

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
        pos_key = f"{symbol}_{timeframe}" if timeframe else symbol
        position = self.active_positions.get(pos_key)
        if not position:
            print(f"[WARN] No active position found for {pos_key} to modify SL/TP")
            return False

        try:
            # 1. Targeted Cancel: Only cancel OUR specific SL/TP orders for this position
            old_sl_id = position.get('sl_order_id')
            old_tp_id = position.get('tp_order_id')
            
            if old_sl_id:
                try:
                    await self._execute_with_timestamp_retry(self.exchange.cancel_order, old_sl_id, symbol)
                    print(f"[MODIFY] Canceled old SL {old_sl_id} for {pos_key}")
                except Exception: pass
            
            if old_tp_id:
                try:
                    await self._execute_with_timestamp_retry(self.exchange.cancel_order, old_tp_id, symbol)
                    print(f"[MODIFY] Canceled old TP {old_tp_id} for {pos_key}")
                except Exception: pass
            
            # Ensure price precision matching exchange (round to 4 or 5 decimals)
            if new_sl: new_sl = round(float(new_sl), 5)
            if new_tp: new_tp = round(float(new_tp), 5)

            # Get position details
            qty = position['qty']
            side = position['side']
            close_side = 'sell' if side == 'BUY' else 'buy'

            # Place new SL order if provided
            if new_sl:
                sl_order_params = {
                    'stopPrice': new_sl,
                    'reduceOnly': True
                }
                if self.exchange_name == 'BYBIT':
                    sl_order_params['category'] = 'linear'
                    sl_order_params['triggerDirection'] = 'descending' if side == 'BUY' else 'ascending'

                order_type = 'market' if self.exchange_name == 'BYBIT' else 'STOP_MARKET'
                sl_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, close_side, qty,
                    params=sl_order_params
                )
                print(f"[MODIFY] New SL order placed for {symbol} at {new_sl}")
                position['sl_order_id'] = sl_order.get('id')

            # Place new TP order if provided
            if new_tp:
                tp_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', close_side, qty,
                    params={
                        'stopPrice': new_tp,
                        'reduceOnly': True
                    }
                )
                print(f"[MODIFY] New TP order placed for {symbol} at {new_tp}")
                position['tp_order_id'] = tp_order.get('id')

            # Update position data
            if new_sl:
                position['sl'] = new_sl
            if new_tp:
                position['tp'] = new_tp
            self.active_positions[pos_key] = position
            self._save_positions()

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
