import ccxt.async_support as ccxt
import asyncio
import logging
from typing import Dict, List, Optional, Any
from .base_adapter import BaseAdapter
from base_exchange_client import BaseExchangeClient
from config import BYBIT_API_KEY, BYBIT_API_SECRET

class BybitAdapter(BaseExchangeClient, BaseAdapter):
    """
    Bybit Adapter implementation using CCXT.
    Focuses on USDT Perpetual Futures (Linear).
    BaseExchangeClient provides time synchronization and retry logic.
    """

    def __init__(self, exchange_client=None):
        """
        Initialize Bybit adapter.
        If exchange_client is provided, use it. Otherwise create new ccxt.bybit instance.
        """
        # Initialize BaseAdapter (wrapper)
        BaseAdapter.__init__(self, exchange_client)
        self.name = 'BYBIT'

        client = exchange_client
        if not client:
            # Initialize CCXT Bybit instance
            options = {
                'defaultType': 'swap',  # USDT Perpetual
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
            client = ccxt.bybit({
                'apiKey': BYBIT_API_KEY,
                'secret': BYBIT_API_SECRET,
                'options': options,
                'enableRateLimit': True,
            })
            # Update BaseAdapter's exchange ref
            self.exchange = client
        
        # Initialize BaseExchangeClient (functionality)
        BaseExchangeClient.__init__(self, client)
        
        self.logger = logging.getLogger(__name__)

    def __getattr__(self, name):
        """Proxy unknown attributes to the underlying exchange object (ccxt)."""
        return getattr(self.exchange, name)

    async def sync_time(self) -> bool:
        """Sync time and load markets."""
        try:
            # BaseExchangeClient.sync_server_time handles the heavy lifting
            await self.sync_server_time()
            await self.exchange.load_markets()
            return True
        except Exception as e:
            self.logger.error(f"[Bybit] Sync time/markets failed: {e}")
            return False

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        """Fetch OHLCV klines with Bybit-specific mapping."""
        # Bybit doesn't support '8h'. Map to something else or skip.
        # Bybit V5 supports: 1,3,5,15,30,60,120,240,360,720,D,M,W
        mapping = {
            '8h': '4h', # closest supported
        }
        target_tf = mapping.get(timeframe, timeframe)
        
        try:
            return await self._execute_with_timestamp_retry(
                self.exchange.fetch_ohlcv, 
                symbol, 
                target_tf, 
                None, 
                limit, 
                {'category': 'linear'}
            )
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch OHLCV failed for {symbol} ({target_tf}): {e}")
            return []

    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch current ticker data."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol, params={'category': 'linear'})

    async def fetch_tickers(self, symbols: List[str]) -> Dict:
        """Fetch multiple tickers."""
        try:
            # Bybit supports fetching all or specific
            return await self._execute_with_timestamp_retry(self.exchange.fetch_tickers, symbols, params={'category': 'linear'})
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch tickers failed: {e}")
            return {}

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Fetch open orders (all or specific symbol)."""
        try:
            # For Bybit V5, category is crucial.
            params = {'category': 'linear'}
            return await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol, params=params)
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch open orders failed: {e}")
            return []

    async def fetch_positions(self, params: Dict = {}) -> List[Dict]:
        """
        Fetch active positions.
        Normalizes Bybit response to standard CCXT structure.
        """
        try:
            # For Bybit V5, category is crucial. Linear = USDT Perp.
            merged_params = {'category': 'linear'}
            merged_params.update(params)
            positions = await self._execute_with_timestamp_retry(self.exchange.fetch_positions, params=merged_params)
            # CCXT usually normalizes this well, but we ensure 'contracts' > 0
            active_positions = [p for p in positions if float(p.get('contracts', 0) or p.get('info', {}).get('size', 0)) > 0]
            # Double check category if info is available to prevent Spot contamination
            return [p for p in active_positions if p.get('info', {}).get('category', 'linear') == 'linear']
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch positions failed: {e}")
            return []

    async def fetch_leverage(self, symbol: str, params: Dict = {}):
        """Fetch current leverage settings for a symbol."""
        extra = {'category': 'linear'}
        extra.update(params)
        return await self._execute_with_timestamp_retry(self.exchange.fetch_leverage, symbol, extra)

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        """Create a new order."""
        try:
            extra_params = {'category': 'linear', 'positionIdx': 0}
            
            # Bybit V5 SL/TP Parameter Enrichment
            if 'stopLoss' in params or 'takeProfit' in params:
                extra_params['tpslMode'] = 'Full'
                extra_params['tpTriggerBy'] = 'MarkPrice'
                extra_params['slTriggerBy'] = 'MarkPrice'

            extra_params.update(params)
            
            return await self._execute_with_timestamp_retry(
                self.exchange.create_order,
                symbol, type, side, amount, price, extra_params
            )
        except Exception as e:
            self.logger.error(f"[Bybit] Create order failed for {symbol}: {e}")
            raise e

    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Cancel an order (standard or conditional trigger order)."""
        extra_params = {'category': 'linear'}
        extra_params.update(params)
        
        try:
            return await self._execute_with_timestamp_retry(
                self.exchange.cancel_order,
                order_id,
                symbol,
                extra_params
            )
        except Exception as e:
            err_str = str(e).lower()
            # If Bybit says order not found, it might be an SL/TP conditional order
            if "not found" in err_str and "trigger" not in err_str:
                self.logger.info(f"[Bybit] Order {order_id} not found in standard queue. Retrying cancel as conditional...")
                cond_params = extra_params.copy()
                cond_params['trigger'] = True
                return await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order,
                    order_id,
                    symbol,
                    cond_params
                )
            self.logger.error(f"[Bybit] Cancel order failed for {symbol}: {e}")
            raise e

    async def cancel_all_orders(self, symbol: str):
        """Cancel ALL orders for a symbol (Standard + Conditional) on Bybit V5."""
        self.logger.info(f"[Bybit] Purging all orders for {symbol}...")
        try:
            # Bybit V5 requires explicit orderFilter to cancel conditional orders
            # 1. Cancel Standard Orders
            try:
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_all_orders, symbol, params={'category': 'linear', 'orderFilter': 'Order'}
                )
            except Exception as e1:
                pass # Ignore if no standard orders exist

            # 2. Cancel Conditional Orders (SL/TP)
            try:
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_all_orders, symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'}
                )
            except Exception as e2:
                pass # Ignore if no conditional orders exist

            self.logger.info(f"[Bybit] All orders (Standard + Conditional) purged for {symbol}")
        except Exception as e:
            self.logger.warning(f"[Bybit] Cancel all orders failed: {e}")

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        """Set leverage for a symbol (V5 linear)."""
        try:
            # Resolve to native Bybit ID (e.g. LTCUSDT) for V5 API compatibility
            try:
                market = self.exchange.market(symbol)
                native_symbol = market.get('id', symbol)
            except:
                native_symbol = symbol.replace('/', '').replace(':USDT', '')

            # Merge with passed params if any
            extra = {'category': 'linear'}
            extra.update(params)
            
            self.logger.debug(f"[Bybit] Calling CCXT set_leverage({leverage}, {native_symbol}, {extra})")
            await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, native_symbol, params=extra)
        except Exception as e:
            # Bybit throws error if leverage is already set to that value
            if "not modified" not in str(e).lower() and "already" not in str(e).lower():
                self.logger.warning(f"[Bybit] Set leverage failed for {symbol}: {e}")

    async def fetch_balance(self, params: Dict = {}) -> Dict:
        """Fetch balance for UNIFIED account (V5 linear)."""
        try:
            extra = {'accountType': 'UNIFIED'}
            extra.update(params)
            res = await self._execute_with_timestamp_retry(self.exchange.fetch_balance, extra)
            
            # Diagnostic Log: See exactly what CCXT found
            total_usdt = res.get('total', {}).get('USDT', 0)
            free_usdt = res.get('free', {}).get('USDT', 0)
            self.logger.debug(f"[Bybit] Balance Fetched | Total USDT: {total_usdt} | Free USDT: {free_usdt}")
            
            return res
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch balance failed: {e}")
            return {}

    async def set_margin_mode(self, symbol: str, mode: str, params: Dict = {}):
        """Set margin mode (ISOLATED/CROSS)."""
        # Resolve to native Bybit ID (e.g. LTCUSDT)
        try:
            market = self.exchange.market(symbol)
            native_symbol = market.get('id', symbol)
        except:
            native_symbol = symbol.replace('/', '').replace(':USDT', '')

        # Try both lowercase and uppercase as Bybit V5 can be picky based on account type
        modes_to_try = [mode.lower(), mode.upper()]
        last_err = None
        
        for m in modes_to_try:
            try:
                extra = {'category': 'linear'}
                extra.update(params)
                # Bybit V5 set_margin_mode(margin_mode, symbol, params)
                self.logger.debug(f"[Bybit] Calling CCXT set_margin_mode({m}, {native_symbol}, {extra})")
                await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, m, native_symbol, params=extra)
                return # Success
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "not modified" in err_str or "already" in err_str:
                    return # Already set
                continue # Try next casing
        
        self.logger.warning(f"[Bybit] Set margin mode failed for {symbol}: {last_err}")

    async def fetch_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Fetch order with Bybit-specific acknowledgment and conditional order retry."""
        extra_params = {'acknowledged': True, 'category': 'linear'}
        extra_params.update(params)
        
        try:
            return await self._execute_with_timestamp_retry(
                self.exchange.fetch_order,
                order_id,
                symbol,
                extra_params
            )
        except Exception as e:
            err_str = str(e).lower()
            # If Bybit says order not found, it might be an SL/TP conditional order
            # The error usually contains 'not found' or retCode: 110001 / retMsg: Order does not exist
            is_not_found = "not found" in err_str or "does not exist" in err_str or "110001" in err_str
            # FIX: Only skip retry if 'trigger' was ALREADY in the params we just sent.
            # Don't check err_str for 'trigger' because Bybit's error message suggests it!
            if is_not_found and not extra_params.get('trigger'):
                self.logger.info(f"[Bybit] Order {order_id} not found in normal queue. Retrying as conditional order...")
                cond_params = extra_params.copy()
                cond_params['trigger'] = True
                try:
                    return await self._execute_with_timestamp_retry(
                        self.exchange.fetch_order,
                        order_id,
                        symbol,
                        cond_params
                    )
                except Exception as retry_e:
                     self.logger.warning(f"[Bybit] Conditional retry failed for {order_id}: {retry_e}")
                     raise retry_e
            raise e

    async def place_stop_orders(
        self, symbol: str, side: str, qty: float,
        sl: Optional[float] = None, tp: Optional[float] = None
    ) -> Dict:
        """
        Place SL and/or TP conditional orders for Bybit V5 linear futures.
        Returns {'sl_id': str|None, 'tp_id': str|None}.
        """
        is_spot = ':' not in symbol  # BTC/USDT vs BTC/USDT:USDT (swap)
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        ids = {'sl_id': None, 'tp_id': None}

        if sl is not None:
            try:
                params = {'stopPrice': sl, 'reduceOnly': True}
                if not is_spot:
                    params['category'] = 'linear'
                    params['triggerDirection'] = 'descending' if side.upper() == 'BUY' else 'ascending'
                else:
                    params['category'] = 'spot'
                o = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'market', close_side, qty, params=params
                )
                ids['sl_id'] = str(o.get('id')) if o.get('id') else None
                self.logger.info(f"[Bybit] SL placed for {symbol} @ {sl} → id={ids['sl_id']}")
            except Exception as e:
                self.logger.error(f"[Bybit] Failed to place SL for {symbol}: {e}")

        if tp is not None:
            try:
                params = {'stopPrice': tp, 'reduceOnly': True}
                if not is_spot:
                    params['category'] = 'linear'
                    params['triggerDirection'] = 'ascending' if side.upper() == 'BUY' else 'descending'
                else:
                    params['category'] = 'spot'
                o = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol, 'market', close_side, qty, params=params
                )
                ids['tp_id'] = str(o.get('id')) if o.get('id') else None
                self.logger.info(f"[Bybit] TP placed for {symbol} @ {tp} → id={ids['tp_id']}")
            except Exception as e:
                self.logger.error(f"[Bybit] Failed to place TP for {symbol}: {e}")

        return ids

    async def set_position_sl_tp(
        self, symbol: str, side: str,
        sl: Optional[float] = None, tp: Optional[float] = None
    ) -> Dict:
        """
        Set SL/TP on an existing Bybit position using set_trading_stop.
        This attaches SL/TP at the position level (not separate orders),
        consistent with how the initial order embeds SL/TP params.
        Returns {'sl_set': bool, 'tp_set': bool}.
        """
        result = {'sl_set': False, 'tp_set': False}
        try:
            # Direct Bybit V5 private endpoint: /v5/position/trading-stop
            normalized = self._normalize_symbol(symbol)
            pos_side = 'None'  # one-way mode
            body = {'category': 'linear', 'symbol': normalized, 'positionIdx': 0}
            if sl is not None:
                body['stopLoss'] = str(sl)
                body['slTriggerBy'] = 'MarkPrice'
            if tp is not None:
                body['takeProfit'] = str(tp)
                body['tpTriggerBy'] = 'MarkPrice'
            
            resp = await self.exchange.privatePostV5PositionTradingStop(body)
            ret_code = resp.get('retCode', -1)
            if ret_code == 0:
                result['sl_set'] = sl is not None
                result['tp_set'] = tp is not None
                self.logger.info(f"[Bybit] set_position_sl_tp OK for {symbol}: SL={sl} TP={tp}")
            else:
                self.logger.error(f"[Bybit] set_position_sl_tp failed: {resp.get('retMsg')}")
        except Exception as e:
            self.logger.error(f"[Bybit] set_position_sl_tp exception for {symbol}: {e}")
        
        return result

    async def cancel_stop_orders(
        self, symbol: str,
        sl_id: Optional[str] = None,
        tp_id: Optional[str] = None
    ):
        """
        Cancel existing SL and/or TP conditional orders on Bybit V5.
        Falls back to trigger=True if standard cancel returns 'not found'.
        """
        for oid in filter(None, [sl_id, tp_id]):
            try:
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order, oid, symbol,
                    params={'category': 'linear'}
                )
                self.logger.info(f"[Bybit] Cancelled stop order {oid} for {symbol}")
            except Exception as e:
                err = str(e).lower()
                if 'not found' in err or 'does not exist' in err or '110001' in err:
                    # Retry as conditional/trigger order
                    try:
                        await self._execute_with_timestamp_retry(
                            self.exchange.cancel_order, oid, symbol,
                            params={'category': 'linear', 'trigger': True}
                        )
                        self.logger.info(f"[Bybit] Cancelled conditional order {oid} (trigger fallback)")
                    except Exception as retry_e:
                        self.logger.warning(f"[Bybit] Could not cancel stop order {oid}: {retry_e}")
                else:
                    self.logger.warning(f"[Bybit] Cancel stop order {oid} failed: {e}")

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """
        Market-close an open position on Bybit V5 linear futures.
        Cancels all existing orders first to prevent conflicts.
        """
        # 1. cancel all open orders (including conditional) for this symbol
        await self.cancel_all_orders(symbol)

        # 2. market close with reduceOnly
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        is_spot = ':' not in symbol
        params = {'reduceOnly': True}
        if not is_spot:
            params['category'] = 'linear'
        else:
            params['category'] = 'spot'

        try:
            result = await self._execute_with_timestamp_retry(
                self.exchange.create_order, symbol, 'market', close_side, qty, params=params
            )
            self.logger.info(f"[Bybit] Closed position {symbol} {side} qty={qty} → {result.get('id')}")
            return result
        except Exception as e:
            self.logger.error(f"[Bybit] Close position failed for {symbol}: {e}")
            raise e

    def round_qty(self, symbol: str, qty: float) -> float:
        """
        Bybit-specific quantity rounding.
        Handles the Spot vs Swap key discrepancy in CCXT.
        """
        try:
            # Bybit Swap keys in CCXT have :USDT suffix
            precision_symbol = f"{symbol}:USDT" if ":USDT" not in symbol else symbol
            if precision_symbol not in self.exchange.markets:
                precision_symbol = symbol # Fallback to Spot or original
                
            qty_str = self.exchange.amount_to_precision(precision_symbol, qty)
            return float(qty_str)
        except Exception:
            # Safe naive fallback
            return round(qty, 3)

    def is_spot(self, symbol: str) -> bool:
        """Bybit Spot detection."""
        if ":USDT" in symbol: return False
        market = self.exchange.markets.get(symbol)
        if market and market.get('spot'): return True
        return False

    async def close(self):
        """Close exchange connection."""
        await self.exchange.close()
