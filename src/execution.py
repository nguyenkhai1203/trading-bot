import ccxt.async_support as ccxt
import logging
import json
import os

# Cooldown after SL (in seconds)
SL_COOLDOWN_SECONDS = 2 * 3600  # 2 hours cooldown after stop loss

class Trader:
    def __init__(self, exchange, dry_run=True):
        self.exchange = exchange
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        # Persistent storage for positions
        self.positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'positions.json')
        self.history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
        self.active_positions = self._load_positions() 
        self.pending_orders = {}  # Track pending limit orders: {pos_key: {'order_id': id, 'symbol': symbol, 'side': side, 'price': price}}
        self._locks = {} # Per-symbol locks to prevent entry race conditions
        self._sl_cooldowns = self._load_cooldowns()  # Persist cooldowns across restarts

    def _get_lock(self, symbol):
        import asyncio
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    def _load_positions(self):
        """Loads positions from the JSON file."""
        if os.path.exists(self.positions_file):
            try:
                with open(self.positions_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                self.logger.error(f"Error loading positions file: {e}")
        return {}

    def _save_positions(self):
        """Saves current active positions to the JSON file."""
        try:
            with open(self.positions_file, 'w') as f:
                json.dump(self.active_positions, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving positions file: {e}")
    
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
                positions = await self.exchange.fetch_positions([symbol])
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
            positions = await self.exchange.fetch_positions([symbol])
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

        try:
            if order_type == 'market':
                order = await self.exchange.create_order(symbol, order_type, side, qty, params=params)
                
                # Save filled position immediately
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
                    "leverage": leverage,
                    "signals_used": signals,
                    "entry_confidence": confidence,
                    "timestamp": order.get('timestamp', 0)
                }
                self._save_positions()
                
            else:
                # Limit order - place and track as pending
                order = await self.exchange.create_order(symbol, order_type, side, qty, price, params=params)
                order_id = order['id']
                
                # Save to pending orders (not in active_positions yet)
                self.pending_orders[pos_key] = {
                    'order_id': order_id,
                    'symbol': symbol,
                    'side': side.upper(),
                    'qty': qty,
                    'price': price,
                    'sl': sl,
                    'tp': tp,
                    'timeframe': timeframe,
                    'leverage': leverage,
                    'signals_used': signals,
                    'entry_confidence': confidence,
                    'timestamp': order.get('timestamp', 0)
                }
                
                print(f"📋 Limit order placed: {order_id} | {side} {symbol} @ {price:.3f} (waiting for fill...)")
                self.logger.info(f"Limit order {order_id} placed, monitoring for fill")
                
                # Start background task to monitor fill
                import asyncio
                asyncio.create_task(self._monitor_limit_order_fill(pos_key, order_id, symbol))
            
            self.logger.info(f"Order placed: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            return None

    async def _monitor_limit_order_fill(self, pos_key, order_id, symbol):
        """Background task to monitor limit order fill."""
        import asyncio
        fill_check_interval = 3  # Check every 3 seconds
        
        try:
            while pos_key in self.pending_orders:
                await asyncio.sleep(fill_check_interval)
                
                # Check if order was cancelled externally
                if pos_key not in self.pending_orders:
                    break
                
                # Fetch order status
                try:
                    order_status = await self.exchange.fetch_order(order_id, symbol)
                    
                    if order_status['status'] == 'closed' or order_status['filled'] == self.pending_orders[pos_key]['qty']:
                        # Order filled!
                        pending = self.pending_orders[pos_key]
                        fill_price = order_status.get('average', pending['price'])
                        
                        # Move from pending to active positions
                        self.active_positions[pos_key] = {
                            "symbol": pending['symbol'],
                            "side": pending['side'],
                            "qty": round(pending['qty'], 3),
                            "entry_price": round(fill_price, 3),
                            "sl": round(pending['sl'], 3) if pending['sl'] else None,
                            "tp": round(pending['tp'], 3) if pending['tp'] else None,
                            "timeframe": pending['timeframe'],
                            "order_type": "limit",
                            "status": "filled",
                            "leverage": pending.get('leverage', 10),
                            "signals_used": pending.get('signals_used', []),
                            "entry_confidence": pending.get('entry_confidence', 0.5),
                            "timestamp": order_status.get('timestamp', pending['timestamp'])
                        }
                        self._save_positions()
                        
                        del self.pending_orders[pos_key]
                        
                        print(f"✅ Limit order FILLED: {pending['symbol']} {pending['side']} @ {fill_price:.3f}")
                        self.logger.info(f"Limit order {order_id} filled at {fill_price}")
                        break
                        
                    elif order_status['status'] == 'canceled':
                        # Order was cancelled
                        if pos_key in self.pending_orders:
                            del self.pending_orders[pos_key]
                        self.logger.info(f"Limit order {order_id} was cancelled")
                        break
                        
                except Exception as e:
                    self.logger.error(f"Error checking limit order {order_id}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Error in limit order monitor: {e}")

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
                await self.exchange.cancel_order(order_id, symbol)
            
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

        # Load existing history
        history = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    history = json.load(f)
            except Exception:
                pass
        
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
            order = await self.exchange.create_order(
                symbol, 'market', close_side, qty,
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
            await self.exchange.set_leverage(leverage, symbol)
            try:
                await self.exchange.set_margin_mode('isolated', symbol)
            except Exception: pass
        except Exception as e:
            self.logger.warning(f"Failed to set mode for {symbol}: {e}")

    async def cancel_all_orders(self, symbol):
        """Cancels all active orders for a symbol."""
        if self.dry_run:
            self.logger.info(f"[DRY RUN] Cancelling all orders for {symbol}")
            return
        try:
            await self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            self.logger.error(f"Failed to cancel orders: {e}")

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
