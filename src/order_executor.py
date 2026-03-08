import asyncio
import time
import json
import logging
from typing import Optional, Dict, List, Any
from src.infrastructure.notifications.notification import send_telegram_message, format_position_filled, format_pending_order, format_order_cancelled

class OrderExecutor:
    """
    Orchestrates the order lifecycle: placement, recovery, monitoring, and SL/TP setup.
    Extracted from Trader to reduce complexity.
    """
    
    def __init__(self, trader):
        self.trader = trader
        self.exchange = trader.exchange
        self.db = trader.db
        self.logger = trader.logger
        self.exchange_name = trader.exchange_name
        self.profile_id = trader.profile_id
        self.profile_name = trader.profile_name
        self.account_key = trader.account_key

    async def place_order(self, symbol: str, timeframe: str, side: str, qty: float, price: float, 
                          order_type: str = 'market', sl: float = None, tp: float = None, 
                          confidence: float = 0.5, signals: List[str] = None, 
                          snapshot: Dict = None, params: Dict = None, 
                          reduce_only: bool = False, leverage: int = 1,
                          qty_rounded: float = 0):
        """
        Primary entry point for placing market/limit orders.
        
        Orchestrates:
        1. API symbol normalization and parameter preparation.
        2. Client Order ID generation for idempotency/recovery.
        3. Execution via exchange adapter with timestamp retry logic.
        4. Timeout recovery: automatic polling of exchange for client ID if API call fails.
        5. State persistence to memory (active_positions) and SQLite DB.
        6. Background task initialization for limit order monitoring.
        7. Automated SL/TP protection order creation (if not natively supported by adapter).
        8. Standardized Telegram notifications.

        Args:
            symbol: Trading pair (e.g. 'BTC/USDT').
            timeframe: Strategy timeframe.
            side: 'BUY' or 'SELL'.
            qty: Order amount.
            price: Entry price (limit) or current price (market).
            order_type: 'market' or 'limit'.
            sl: Stop loss price level.
            tp: Take profit price level.
            confidence: Normalized signal confidence (0-1).
            signals: List of signal names triggered.
            snapshot: Dictionary of market indicators at entry time.
            params: Raw exchange-specific override parameters.
            reduce_only: Whether the order should only reduce existing position.
            leverage: Desired leverage.
            qty_rounded: Pre-calculated rounded quantity (optional).
        """
        params = params or {}
        pos_key = self.trader._get_pos_key(symbol, timeframe)
        
        # LIVE LOGIC START
        tpsl_attached = self.trader.exchange.is_tpsl_attached_supported() and (sl or tp)

        if sl: params['stopLoss'] = sl
        if tp: params['takeProfit'] = tp
        if reduce_only: 
            params['reduceOnly'] = True

        use_leverage = leverage # Assumed pre-clamped by Trader
        api_symbol = self.trader._normalize_symbol(symbol)
        
        try:
            # Generate Client Order ID for recovery
            prefix = f"P{self.profile_id}_"
            client_id = f"{prefix}{api_symbol}_{side.upper()}_{int(time.time()*1000)}"
            params['newClientOrderId'] = client_id
            
            self.logger.debug(f"[ORDER REQ] {symbol} {order_type} {side} {qty} {price} {params}")

            order = None
            try:
                order = await self.trader._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, order_type, side.lower(), qty, price, params=params
                )
            except Exception as e:
                # TIMEOUT RECOVERY
                self.logger.warning(f"Order failed/timed out for {client_id}. Attempting recovery... Error: {e}")
                await asyncio.sleep(1)
                
                err_str = str(e).lower()
                is_logic_error = any(x in err_str for x in ["10001", "side invalid", "insufficient", "170131", "403", "401"])
                if is_logic_error:
                    await self.trader.check_margin_error(e, new_confidence=confidence)
                    raise e
                try:
                    order = await self.exchange.fetch_order(client_id, symbol)
                except Exception:
                    open_orders = await self.exchange.fetch_open_orders(symbol)
                    order = next((o for o in open_orders if o.get('clientOrderId') == client_id or o.get('info', {}).get('clientOrderId') == client_id), None)
                
                if order:
                    self.logger.info(f"✅ RECOVERED order {client_id} from timeout! ID: {order['id']}")
                else:
                    await self.trader.check_margin_error(e, new_confidence=confidence)
                    raise e
            # Update Internal State
            is_limit = (order_type == 'limit')
            status = 'pending' if is_limit else 'filled'
            
            entry_price = order.get('average', price)
            timestamp = order.get('timestamp', 0)
            
            pos_data = {
                "symbol": symbol,
                "side": side.upper(),
                "qty": round(qty_rounded or qty, 3),
                "entry_price": round(entry_price, 3) if isinstance(entry_price, (int, float)) else entry_price,
                "sl": round(sl, 3) if sl else None,
                "tp": round(tp, 3) if tp else None,
                "timeframe": timeframe,
                "order_type": order_type,
                "status": status,
                "leverage": use_leverage,
                "signals_used": signals or [],
                "entry_confidence": confidence,
                "snapshot": snapshot,
                "timestamp": timestamp,
                "order_id": order.get('id'),
                "sl_order_id": 'attached' if tpsl_attached else None,
                "tp_order_id": 'attached' if tpsl_attached else None
            }
            
            self.trader.active_positions[pos_key] = pos_data
            if is_limit:
                self.trader.pending_orders[pos_key] = pos_data
                
            await self.trader._update_db_position(pos_key)
            
            # Update Shared Cache
            shared = self.trader.__class__._shared_account_cache.get(self.account_key, {'pos_symbols': set(), 'open_order_symbols': set()})
            cache_key = 'open_order_symbols' if is_limit else 'pos_symbols'
            shared[cache_key].add(api_symbol)
            shared['timestamp'] = time.time()
            self.trader.__class__._shared_account_cache[self.account_key] = shared
            # Notifications & Post-Processing
            if is_limit:
                print(f"📋 [{self.exchange_name}] Limit order placed: {order['id']} | {side} {symbol} @ {price:.3f}")
                _, tg_msg = format_pending_order(symbol, timeframe, side, price, sl, tp, confidence, use_leverage, False, exchange_name=self.exchange_name, profile_label=self.profile_name)
                asyncio.create_task(self.monitor_limit_order_fill(pos_key, order['id'], symbol))
                if not tpsl_attached:
                    asyncio.create_task(self.setup_sl_tp_for_pending(symbol, timeframe))
            else:
                print(f"✅ [{self.exchange_name}] Market order filled: {symbol} {side} @ {entry_price:.3f}")
                _, tg_msg = format_position_filled(symbol, timeframe, side, entry_price, qty, entry_price * qty, sl, tp, confidence, use_leverage, False, exchange_name=self.exchange_name, profile_label=self.profile_name)
                if not tpsl_attached:
                    await self.create_sl_tp_orders_for_position(pos_key)
            asyncio.create_task(send_telegram_message(tg_msg))
            return order
        except Exception as e:
            self.logger.error(f"Failed to place {order_type} order for {symbol}: {e}")
            return None
    async def monitor_limit_order_fill(self, pos_key: str, order_id: str, symbol: str):
        """
        Polls the exchange to track the fill status of a limit order.
        
        Logic:
        1. Checks every X seconds if the order is still active in memory.
        2. Fetches order status from exchange (closed, filled, canceled).
        3. If filled (>=99%), transitions local state to 'filled' and triggers SL/TP setup.
        4. Clears shared account cache identifiers when filled or cancelled.
        5. Handles 'Order Not Found' errors by cleaning up local state.
        
        Args:
            pos_key: Unique position identifier (Profile_Exchange_Symbol_Timeframe).
            order_id: Exchange-assigned order ID.
            symbol: Trading pair.
        """
        fill_check_interval = 3
        local_order_id = order_id
        try:
            while True:
                await asyncio.sleep(fill_check_interval)
                
                # Check if still pending in memory
                active = self.trader.active_positions.get(pos_key, {})
                if not active or active.get('status') != 'pending':
                    break
                
                local_order_id = active.get('order_id', local_order_id)
                if not local_order_id: break
                try:
                    order_status = await self.trader._execute_with_timestamp_retry(self.exchange.fetch_order, local_order_id, symbol)
                except Exception as e:
                    err_str = str(e).lower()
                    if "order does not exist" in err_str or "-2013" in err_str or "ordernotfound" in err_str:
                         print(f"🗑️ [{self.exchange_name}] [{symbol}] Order {local_order_id} no longer exists. Clearing position.")
                         await self.trader.cancel_pending_order(pos_key, reason="order_not_found")
                         break
                    self.logger.warning(f"Error fetching order {local_order_id} for {symbol}: {e}")
                    continue
                status = order_status.get('status', '').lower()
                filled_qty = float(order_status.get('filled', 0) or 0)
                expected_qty = float(active.get('qty', 0))
                if status in ('closed', 'filled') and filled_qty > 0 and (filled_qty / expected_qty >= 0.99):
                    # Transition to filled
                    active['status'] = 'filled'
                    fill_price = order_status.get('average') or active.get('entry_price')
                    active['entry_price'] = round(fill_price, 3)
                    active['timestamp'] = order_status.get('timestamp', active.get('timestamp'))
                    
                    await self.trader._update_db_position(pos_key)
                    self.trader.pending_orders.pop(pos_key, None)
                    # Update shared cache
                    shared = self.trader.__class__._shared_account_cache.get(self.account_key, {})
                    api_sym = self.trader._normalize_symbol(symbol)
                    if 'open_order_symbols' in shared: shared['open_order_symbols'].discard(api_sym)
                    if 'pos_symbols' in shared: shared['pos_symbols'].add(api_sym)
                    # Notifications
                    _, tg_msg = format_position_filled(symbol, active.get('timeframe'), active.get('side'), fill_price, active.get('qty'), fill_price * active.get('qty'), active.get('sl'), active.get('tp'), active.get('entry_confidence'), active.get('leverage'), False, exchange_name=self.exchange_name, profile_label=self.profile_name)
                    asyncio.create_task(send_telegram_message(tg_msg))
                    
                    await self.create_sl_tp_orders_for_position(pos_key)
                    print(f"✅ [{self.exchange_name}] Limit order FILLED: {symbol} {active['side']} @ {fill_price:.3f}")
                    break
                if status in ('canceled', 'cancelled'):
                    self.logger.info(f"Limit order {local_order_id} for {symbol} was cancelled. Purging.")
                    await self.trader.cancel_pending_order(pos_key, reason="Cancelled on Exchange")
                    break
        except Exception as e:
            self.logger.error(f"Error in limit order monitor for {pos_key}: {e}")
    async def setup_sl_tp_for_pending(self, symbol: str, timeframe: str):
        """
        Creates SL and/or TP conditional orders for a pending limit order.
        These are typically 'is_pending' triggers that only activate on-exchange 
        when the main entry order is filled.
        """
        pos_key = self.trader._get_pos_key(symbol, timeframe)
        pos = self.trader.active_positions.get(pos_key)
        if not pos or pos.get('status') != 'pending': return
        
        # This mirrors Trader.setup_sl_tp_for_pending implementation
        sl = pos.get('sl')
        tp = pos.get('tp')
        side = pos.get('side')
        qty = pos.get('qty')
        
        if sl or tp:
            try:
                ids = await self.exchange.place_stop_orders(symbol, side, qty, sl=sl, tp=tp, is_pending=True)
                if ids.get('sl_id'): pos['sl_order_id'] = ids['sl_id']
                if ids.get('tp_id'): pos['tp_order_id'] = ids['tp_id']
                await self.trader._update_db_position(pos_key)
            except Exception as e:
                self.logger.warning(f"Failed to setup SL/TP for pending {symbol}: {e}")
    async def create_sl_tp_orders_for_position(self, pos_key: str):
        """
        Creates 'reduce-only' SL/TP orders for a filled position.
        This is used for exchanges or order types where SL/TP wasn't attached at entry.
        """
        pos = self.trader.active_positions.get(pos_key)
        if not pos or pos.get('status') != 'filled': return
        
        symbol = pos.get('symbol')
        side = pos.get('side')
        qty = pos.get('qty')
        sl = pos.get('sl')
        tp = pos.get('tp')
        
        try:
             ids = await self.exchange.place_stop_orders(symbol, side, qty, sl=sl, tp=tp)
             if ids.get('sl_id'): pos['sl_order_id'] = ids['sl_id']
             if ids.get('tp_id'): pos['tp_order_id'] = ids['tp_id']
             await self.trader._update_db_position(pos_key)
        except Exception as e:
             self.logger.error(f"Failed to create SL/TP for position {pos_key}: {e}")
    async def cancel_pending_order(self, pos_key: str, reason: str = "Technical invalidation"):
        """
        Comprehensive cleanup of a pending trade.
        
        Actions:
        1. Cancels the main entry limit order.
        2. Cancels any associated SL/TP conditional orders.
        3. Clears local memory (active_positions, pending_orders).
        4. Updates DB trade record as CANCELLED.
        5. Sends Telegram notification.
        """
        # Mirroring Trader.cancel_pending_order logic
        pending = self.trader.pending_orders.get(pos_key)
        active = self.trader.active_positions.get(pos_key)
        pos_info = pending or active
        
        if not pos_info: return False
        
        order_id = pos_info.get('order_id')
        symbol = pos_info.get('symbol')
        sl_id = pos_info.get('sl_order_id')
        tp_id = pos_info.get('tp_order_id')
        
        try:
            # 1. Cancel main entry
            if order_id and order_id != 'dry_run_id':
                try:
                    await self.exchange.cancel_order(order_id, symbol)
                except Exception as e:
                    self.logger.warning(f"Failed to cancel entry {order_id}: {e}")
            
            # 2. Cancel protectors
            for protector_id in [sl_id, tp_id]:
                if protector_id and protector_id != 'attached':
                    try:
                        await self.exchange.cancel_order(protector_id, symbol, params={'trigger': True, 'is_algo': True})
                    except Exception: pass
            
            # 3. Cleanup DB & Memory
            await self.trader._clear_db_position(pos_key, exit_reason=reason)
            self.trader.pending_orders.pop(pos_key, None)
            self.trader.active_positions.pop(pos_key, None)
            
            # Notification
            _, tg_msg = format_order_cancelled(symbol, pos_info.get('timeframe', '1h'), pos_info.get('side', 'BUY'), pos_info.get('price', 0), reason, False, exchange_name=self.exchange_name)
            asyncio.create_task(send_telegram_message(tg_msg))
            return True
        except Exception as e:
            self.logger.error(f"Cancellation failed for {pos_key}: {e}")
            return False
