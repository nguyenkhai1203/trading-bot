import ccxt.async_support as ccxt
import logging
import json
import os
import tempfile
import asyncio

from config import LEVERAGE, BINANCE_API_KEY, BINANCE_API_SECRET, AUTO_CREATE_SL_TP
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

# Cooldown after SL (in seconds)
SL_COOLDOWN_SECONDS = 2 * 3600  # 2 hours cooldown after stop loss

class Trader:
    def __init__(self, exchange, dry_run=True, data_manager=None):
        self.exchange = exchange
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        
        # Use shared MarketDataManager for time synchronization
        if data_manager is None:
            from data_manager import MarketDataManager
            self.data_manager = MarketDataManager()
        else:
            self.data_manager = data_manager
            
        # Persistent storage for positions
        self.positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'positions.json')
        self.history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
        self.active_positions = self._load_positions() 
        self.pending_orders = {}  # Track pending limit orders: {pos_key: {'order_id': id, 'symbol': symbol, 'side': side, 'price': price}}
        self._symbol_locks = {}  # Per-symbol locks to prevent entry race conditions
        self._position_locks = {}  # Per-position locks for SL/TP recreation
        self._sl_cooldowns = self._load_cooldowns()  # Persist cooldowns across restarts
        self.default_leverage = LEVERAGE

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

    async def _execute_with_timestamp_retry(self, api_call, *args, **kwargs):
        """Execute exchange API call with timestamp error retry using shared data_manager."""
        return await self.data_manager._execute_with_timestamp_retry(api_call, *args, **kwargs)

    def _load_positions(self):
        """Loads positions from the JSON file."""
        if os.path.exists(self.positions_file):
            try:
                with open(self.positions_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    # Handle empty file gracefully
                    if not content:
                        self.logger.warning("[WARN] positions.json is empty, initializing with {}")
                        self.active_positions = {}
                        self._save_positions()  # Write {} to file
                        return self.active_positions
                    self.active_positions = json.loads(content)
            except json.JSONDecodeError as e:
                self.logger.error(f"Error loading positions file: {e}")
                self.logger.warning("[WARN] Initializing with empty positions")
                self.active_positions = {}
                self._save_positions()  # Write {} to file
            except Exception as e:
                self.logger.error(f"Error loading positions file: {e}")
                self.active_positions = {}
        else:
            self.active_positions = {}
            self._save_positions()  # Create file with {}
        return self.active_positions

    def _save_positions(self):
        """Saves current active positions to the JSON file."""
        import time
        max_retries = 3
        for attempt in range(max_retries):
            try:
                tmp = self.positions_file + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.active_positions, f, indent=4)
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

    def _clamp_leverage(self, lev):
        """Clamp leverage to allowed range (default 8-12)."""
        try:
            lv = int(lev)
        except Exception:
            lv = int(self.default_leverage or 8)
        # Allow floor 5 if default is lower; clamp to [5,12] to be safe
        return max(5, min(12, lv))

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
        # 1. Check local storage for any key starting with symbol_
        for key in self.active_positions.keys():
            if key.startswith(f"{symbol}_"):
                return True
        
        # 2. Check Exchange (Account Level)
        if not self.dry_run:
            try:
                positions = await self._execute_with_timestamp_retry(
                    self.exchange.fetch_positions, [symbol]
                )
                for pos in positions:
                    if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                        return True
            except Exception as e:
                self.logger.error(f"Error checking exchange for symbol {symbol}: {e}")
        
        return False

    async def get_open_position(self, symbol, timeframe=None):
        """Checks if there's an open position for the symbol/timeframe."""
        key = f"{symbol}_{timeframe}" if timeframe else symbol
        
        if self.dry_run:
            return key in self.active_positions

        try:
            # LIVE Mode: Always check exchange 
            positions = await self._execute_with_timestamp_retry(
                self.exchange.fetch_positions, [symbol]
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

    async def place_order(self, symbol, side, qty, timeframe=None, order_type='market', price=None, sl=None, tp=None, timeout=None, leverage=10, signals_used=None, entry_confidence=None):
        """Places an order and updates persistent storage. For limit orders, monitors fill in background."""
        # Validate qty - reject invalid orders
        if qty is None or qty <= 0:
            self.logger.warning(f"Rejected order: invalid qty={qty} for {symbol}")
            return None
        
        # Dynamic decimal places based on price (high-price coins need more decimals)
        qty_decimals = 6 if price and price > 1000 else 3
        qty_rounded = round(qty, qty_decimals)
        
        # Validate minimum qty after rounding
        if qty_rounded <= 0:
            self.logger.warning(f"Rejected order: qty too small after rounding ({qty} -> {qty_rounded}) for {symbol}")
            return None
        
        pos_key = f"{symbol}_{timeframe}" if timeframe else symbol
        signals = signals_used or []
        confidence = entry_confidence or 0.5
        
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
                "timestamp": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
            }
            self._save_positions()
            return {'id': 'dry_run_id', 'status': 'closed' if not is_limit else 'open', 'filled': qty if not is_limit else 0}

        # LIVE LOGIC
        params = {}
        if sl: params['stopLoss'] = str(sl)
        if tp: params['takeProfit'] = str(tp)

        # Determine and clamp leverage to allowed range
        use_leverage = self._clamp_leverage(leverage)

        try:
            # Ensure margin mode & leverage set on exchange for LIVE orders
            if not self.dry_run:
                try:
                    await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, 'isolated', symbol)
                except Exception:
                    # fallback signature variation handled elsewhere
                    pass
                try:
                    await self._execute_with_timestamp_retry(self.exchange.set_leverage, use_leverage, symbol)
                except Exception as e:
                    self.logger.warning(f"Failed to set leverage for {symbol}: {e}")

            if order_type == 'market':
                # Debug log: attempting market order
                try:
                    self._debug_log('place_order:market', {'symbol': symbol, 'side': side, 'qty': qty, 'params': params, 'leverage': use_leverage})
                except Exception:
                    pass
                order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, side, qty, params=params
                )
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
                    "timestamp": order.get('timestamp', 0)
                }
                self._save_positions()
                # Create SL/TP reduce-only orders for the freshly opened position
                try:
                    await self._create_sl_tp_orders_for_position(pos_key)
                except Exception as e:
                    self.logger.warning(f"Failed to auto-create SL/TP for market entry {pos_key}: {e}")

            else:
                # Limit order - place and track as pending
                try:
                    self._debug_log('place_order:limit', {'symbol': symbol, 'side': side, 'qty': qty, 'price': price, 'params': params, 'leverage': use_leverage})
                except Exception:
                    pass
                order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, side, qty, price, params=params
                )
                try:
                    self._debug_log('place_order:limit:response', order)
                except Exception:
                    pass
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
                    "timestamp": order.get('timestamp', 0)
                }
                self._save_positions()
                print(f"📋 Limit order placed: {order_id} | {side} {symbol} @ {price:.3f} (waiting for fill...)")
                self.logger.info(f"Limit order {order_id} placed, monitoring for fill")

                # Start background task to monitor fill
                import asyncio
                asyncio.create_task(self._monitor_limit_order_fill(pos_key, order_id, symbol))
                # Attempt to create SL/TP reduce-only orders for this pending order
                try:
                    asyncio.create_task(self.setup_sl_tp_for_pending(symbol, timeframe))
                except Exception:
                    pass

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

                # If closed/filled
                if order_status.get('status') in ('closed', 'filled') or (expected_qty and float(filled_qty) >= float(expected_qty)):
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
        """Cancels a pending limit order."""
        if pos_key not in self.pending_orders and pos_key not in self.active_positions:
            return False
        
        # Get order info from either source
        pending = self.pending_orders.get(pos_key)
        if pending:
            order_id = pending['order_id']
            symbol = pending['symbol']
        else:
            # Dry run: position is in active_positions with status='pending'
            active_pos = self.active_positions.get(pos_key)
            if not active_pos or active_pos.get('status') != 'pending':
                return False
            order_id = 'dry_run_id'
            symbol = active_pos['symbol']
        
        try:
            if not self.dry_run and pending:
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order, order_id, symbol
                )
            
            # Clean up from both sources
            if pos_key in self.pending_orders:
                del self.pending_orders[pos_key]
            if pos_key in self.active_positions:
                del self.active_positions[pos_key]
                self._save_positions()
            
            print(f"❌ Cancelled pending order: {symbol} | Reason: {reason}")
            self.logger.info(f"Cancelled limit order {order_id}: {reason}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

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
            await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, symbol)
            try:
                await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, 'isolated', symbol)
            except Exception: pass
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
                sl_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'STOP_MARKET', close_side, qty_to_use,
                    params={
                        'stopPrice': sl,
                        'reduceOnly': True
                    }
                )
                results['sl_order'] = sl_order
                # Persist id
                pending['sl_order_id'] = sl_order.get('id')
                if pos_key in self.active_positions:
                    self.active_positions[pos_key]['sl_order_id'] = sl_order.get('id')
                    self._save_positions()
                print(f"[SETUP] SL order placed for {symbol} at {sl} (id={sl_order.get('id')})")
            elif sl and existing_sl_id:
                print(f"[SKIP] SL already exists for {pos_key} (id={existing_sl_id})")
                
            if tp and not existing_tp_id:
                try:
                    stored_qty = float(self.active_positions.get(pos_key, {}).get('qty') or qty)
                except Exception:
                    stored_qty = qty
                qty_to_use = min(float(qty), stored_qty)
                tp_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', close_side, qty_to_use,
                    params={
                        'stopPrice': tp,
                        'reduceOnly': True
                    }
                )
                results['tp_order'] = tp_order
                pending['tp_order_id'] = tp_order.get('id')
                if pos_key in self.active_positions:
                    self.active_positions[pos_key]['tp_order_id'] = tp_order.get('id')
                    self._save_positions()
                print(f"[SETUP] TP order placed for {symbol} at {tp} (id={tp_order.get('id')})")
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
            # Try REST calls to Binance Futures first (signed requests)
            try:
                await self._set_margin_type_rest(symbol, 'ISOLATED')
            except Exception as e:
                self.logger.debug(f"REST set_margin_type failed for {symbol}: {e}")
                # Fallback to CCXT
                try:
                    await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, 'isolated', symbol)
                except Exception:
                    try:
                        await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, symbol, 'isolated')
                    except Exception as e:
                        self.logger.warning(f"set_margin_mode fallback failed for {symbol}: {e}")

            try:
                await self._set_leverage_rest(symbol, int(leverage))
            except Exception as e:
                self.logger.debug(f"REST set_leverage failed for {symbol}: {e}")
                try:
                    await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, symbol)
                except Exception as e:
                    self.logger.warning(f"Failed to set leverage {leverage}x for {symbol}: {e}")
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
            'timestamp': int(time.time() * 1000)
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
            'leverage': int(leverage),
            'timestamp': int(time.time() * 1000)
        }
        return await self._binance_signed_post(base + path, params)

    async def _binance_signed_post(self, url, params):
        """Perform signed POST to Binance Futures API using requests in thread to avoid blocking."""
        def do_request(u, p):
            query = urlencode(p)
            signature = hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=10)
            r.raise_for_status()
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
            'timestamp': int(time.time() * 1000)
        }

        def do_post(u, data):
            query = urlencode(data)
            signature = hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': BINANCE_API_KEY, 'Content-Type': 'application/x-www-form-urlencoded'}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()

        import asyncio
        return await asyncio.to_thread(do_post, url, body)

    async def _create_sl_tp_orders_for_position(self, pos_key):
        """Create SL/TP reduce-only orders for a filled position and persist the order ids."""
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
                    sl_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, 'STOP_MARKET', close_side, qty_to_use,
                        params={'stopPrice': sl, 'reduceOnly': True}
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
                    tp_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', close_side, qty_to_use,
                        params={'stopPrice': tp, 'reduceOnly': True}
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

    async def reconcile_positions(self):
        """Attempt to synchronize local `active_positions` with exchange state.
        - Recover missing order_ids for pending entries by matching open orders
        - Ensure filled positions have SL/TP orders on-exchange and persist their ids
        Returns a summary dict.
        """
        summary = {'recovered_order_ids': 0, 'created_tp_sl': 0, 'errors': []}
        for pos_key, pos in list(self.active_positions.items()):
            try:
                symbol = pos.get('symbol')
                status = pos.get('status')
                qty = pos.get('qty')

                # 1) Recover missing order_id for pending
                if status == 'pending' and not pos.get('order_id'):
                    try:
                        open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                    except Exception:
                        open_orders = []
                    matched = None
                    for o in open_orders or []:
                        # try match by amount and price (tolerance)
                        try:
                            o_price = float(o.get('price') or o.get('info', {}).get('price') or 0)
                            o_amt = float(o.get('amount') or o.get('info', {}).get('origQty') or 0)
                        except Exception:
                            continue
                        if abs(o_amt - float(qty)) < max(1e-6, 0.01 * float(qty)):
                            matched = o
                            break
                    if matched:
                        pos['order_id'] = matched.get('id')
                        self.active_positions[pos_key] = pos
                        self._save_positions()
                        summary['recovered_order_ids'] += 1

                # 2) For filled positions, ensure SL/TP orders exist
                if status == 'filled':
                    # If SL/TP ids missing, try to find open reduce-only orders for symbol
                    missing = False
                    if not pos.get('sl_order_id') or not pos.get('tp_order_id'):
                        try:
                            open_orders = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)
                        except Exception:
                            open_orders = []
                        # Find orders with reduceOnly True matching qty
                        for o in open_orders or []:
                            info = o.get('info', {})
                            try:
                                o_amt = float(o.get('amount') or info.get('origQty') or 0)
                            except Exception:
                                o_amt = 0
                            if abs(o_amt - float(qty)) > max(1e-6, 0.01 * float(qty)):
                                continue
                            o_type = o.get('type') or info.get('type')
                            if (not pos.get('sl_order_id')) and (str(o_type).upper().find('STOP') != -1):
                                pos['sl_order_id'] = o.get('id')
                                missing = True
                            if (not pos.get('tp_order_id')) and (str(o_type).upper().find('TAKE') != -1):
                                pos['tp_order_id'] = o.get('id')
                                missing = True
                        if missing:
                            self.active_positions[pos_key] = pos
                            self._save_positions()
                            summary['created_tp_sl'] += 1
            except Exception as e:
                summary['errors'].append(str(e))
                self.logger.error(f"Reconcile error for {pos_key}: {e}")

        return summary

    async def check_missing_sl_tp(self, pos_key):
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

        # Check SL
        if sl_id:
            try:
                await self._execute_with_timestamp_retry(self.exchange.fetch_order, sl_id, symbol)
                res['sl_exists'] = True
            except Exception as e:
                res['errors'].append(f'sl_fetch:{e}')

        # Check TP
        if tp_id:
            try:
                await self._execute_with_timestamp_retry(self.exchange.fetch_order, tp_id, symbol)
                res['tp_exists'] = True
            except Exception as e:
                res['errors'].append(f'tp_fetch:{e}')

        return res

    async def recreate_missing_sl_tp(self, pos_key, recreate_sl=True, recreate_tp=True, recreate_sl_force=False, recreate_tp_force=False):
        """Recreate missing SL/TP orders for a given position.
        This will actually place orders on exchange and persist new ids.
        Only runs when `self.dry_run` is False. Returns dict of results.
        Uses per-position locking to prevent concurrent recreation.
        """
        # Acquire per-position lock to prevent race conditions
        async with self._get_position_lock(pos_key):
            result = {'sl_recreated': False, 'tp_recreated': False, 'errors': []}
            pos = self.active_positions.get(pos_key)
            if not pos:
                result['errors'].append('position_not_found')
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
                    try:
                        stored_qty = float(pos.get('qty') or qty)
                    except Exception:
                        stored_qty = qty
                    qty_to_use = min(float(qty), stored_qty)
                    sl_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, 'STOP_MARKET', close_side, qty_to_use,
                        params={'stopPrice': sl, 'reduceOnly': True}
                    )
                    pos['sl_order_id'] = str(sl_order.get('id'))
                    result['sl_recreated'] = True
                    self._debug_log('recreated_sl', pos_key, pos['sl_order_id'])
            except Exception as e:
                result['errors'].append(f'sl_recreate:{e}')

            # TP Recreation with Safety Check
            try:
                if recreate_tp and tp and (not pos.get('tp_order_id') or recreate_tp_force):
                    # Safety check: Verify TP price won't trigger immediately
                    try:
                        ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)
                        current_price = float(ticker.get('last') or ticker.get('close'))
                        
                        # Check if TP would trigger immediately (with 0.1% buffer)
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
                    tp_order = await self._execute_with_timestamp_retry(
                        self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', close_side, qty_to_use,
                        params={'stopPrice': tp, 'reduceOnly': True}
                    )
                    pos['tp_order_id'] = str(tp_order.get('id'))
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
        if symbols is None:
            symbols = {v.get('symbol') for v in self.active_positions.values() if v.get('symbol')}
        else:
            symbols = set(symbols)

        for sym in symbols:
            lev = int(self.default_leverage or 8)
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
            # Cancel existing SL/TP orders for this symbol
            await self.cancel_all_orders(symbol)
            print(f"[MODIFY] Canceled existing orders for {symbol}")

            # Get position details
            qty = position['qty']
            side = position['side']
            close_side = 'sell' if side == 'BUY' else 'buy'

            # Place new SL order if provided
            if new_sl:
                sl_order = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'STOP_MARKET', close_side, qty,
                    params={
                        'stopPrice': new_sl,
                        'reduceOnly': True
                    }
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
            print(f"[ERROR] Failed to modify SL/TP for {pos_key}: {e}")
            return False

    async def close(self):
        """Close underlying exchange connector to avoid unclosed connector warnings."""
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
