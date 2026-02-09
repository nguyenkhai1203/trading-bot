import ccxt.async_support as ccxt
import logging
import json
import os

class Trader:
    def __init__(self, exchange, dry_run=True):
        self.exchange = exchange
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        # Persistent storage for positions
        self.positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'positions.json')
        self.history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_history.json')
        self.active_positions = self._load_positions() 
        self._locks = {} # Per-symbol locks to prevent entry race conditions

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

    async def place_order(self, symbol, side, qty, timeframe=None, order_type='market', price=None, sl=None, tp=None):
        """Places an order and updates persistent storage."""
        pos_key = f"{symbol}_{timeframe}" if timeframe else symbol
        
        if self.dry_run:
            self.logger.info(f"[DRY RUN] {side} {symbol} ({timeframe}): Qty={qty}, SL={sl}, TP={tp}")
            print(f"[DRY RUN] Placed {side} {symbol} {timeframe} {qty}")
            
            self.active_positions[pos_key] = {
                "symbol": symbol,
                "side": side.upper(),
                "qty": round(qty, 3),
                "entry_price": round(price, 3) if isinstance(price, (int, float)) else price,
                "sl": round(sl, 3) if sl else None,
                "tp": round(tp, 3) if tp else None,
                "timeframe": timeframe,
                "timestamp": self.exchange.milliseconds() if hasattr(self.exchange, 'milliseconds') else 0
            }
            self._save_positions()
            return {'id': 'dry_run_id', 'status': 'closed', 'filled': qty}

        # LIVE LOGIC
        params = {}
        if sl: params['stopLoss'] = str(sl)
        if tp: params['takeProfit'] = str(tp)

        try:
            if order_type == 'market':
                order = await self.exchange.create_order(symbol, order_type, side, qty, params=params)
            else:
                order = await self.exchange.create_order(symbol, order_type, side, qty, price, params=params)
            
            # Save for persistence
            self.active_positions[pos_key] = {
                "symbol": symbol,
                "side": side.upper(),
                "qty": round(qty, 3),
                "entry_price": round(order.get('average', price), 3),
                "sl": round(sl, 3) if sl else None,
                "tp": round(tp, 3) if tp else None,
                "timeframe": timeframe,
                "timestamp": order.get('timestamp', 0)
            }
            self._save_positions()
            
            self.logger.info(f"Order placed: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            return None

    async def log_trade(self, symbol, exit_price, exit_reason):
        """Logs a closed trade to the history file."""
        pos = self.active_positions.get(symbol)
        if not pos:
            return

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
