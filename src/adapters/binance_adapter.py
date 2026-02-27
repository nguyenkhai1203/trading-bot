import asyncio
import json
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from typing import Dict, List, Optional, Any
import logging

from base_exchange_client import BaseExchangeClient
from .base_adapter import BaseAdapter
from config import BINANCE_API_KEY, BINANCE_API_SECRET

class BinanceAdapter(BaseExchangeClient, BaseAdapter):
    """
    Binance Adapter Implementation.
    Encapsulates all Binance-specific API logic, retry mechanisms, and unique endpoints (e.g., Algo orders).
    """

    def __init__(self, exchange_client):
        # Initialize BaseExchangeClient for time sync and retry logic
        BaseExchangeClient.__init__(self, exchange_client)
        # Initialize BaseAdapter for standard interface
        BaseAdapter.__init__(self, exchange_client)
        
        # Store API keys in instance for robust signing and easier testing
        self.api_key = BINANCE_API_KEY
        self.api_secret = BINANCE_API_SECRET

        # Explicitly suppress FetchOpenOrders warning for Binance
        if hasattr(self.exchange, 'options'):
            self.exchange.options['warnOnFetchOpenOrdersWithoutSymbol'] = False

    async def fetch_balance(self) -> Dict:
        """Fetch account balance."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_balance)

    async def sync_time(self) -> bool:
        """Sync time and load markets."""
        try:
            await self.sync_server_time()
            return True
        except Exception as e:
            print(f"⚠️ [BinanceAdapter] Failed to sync time or markets: {e}")
            return False

    def __getattr__(self, name):
        """Proxy unknown attributes to the underlying exchange object (ccxt)."""
        return getattr(self.exchange, name)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        """Fetch OHLCV klines with retry logic."""
        return await self._execute_with_timestamp_retry(
            self.exchange.fetch_ohlcv, 
            symbol, 
            timeframe, 
            limit=limit
        )


    def _get_api_symbol(self, symbol: str) -> str:
        """Standard help to normalize CCXT symbol to Binance native symbol (e.g. BTC/USDT:USDT -> BTCUSDT)."""
        if not symbol: return ""
        return symbol.split(':')[0].replace('/', '').upper()

    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch ticker with retry logic."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)

    async def fetch_tickers(self, symbols: List[str]) -> Dict:
        """Fetch multiple tickers using concurrent individual requests for better stability on Futures."""
        tasks = [self.fetch_ticker(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        tickers = {}
        for i, s in enumerate(symbols):
            if isinstance(results[i], dict):
                tickers[s] = results[i]
        return tickers

    async def fetch_algo_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Fetch algo orders (Stop Loss, Take Profit, Trailing Stop) via REST."""
        try:
            api_symbol = self._get_api_symbol(symbol)
            algo_params = {'symbol': api_symbol} if api_symbol else {}
            return await self._execute_with_timestamp_retry(self.exchange.fapiPrivateGetOpenAlgoOrders, algo_params)
        except Exception as e:
            self.logger.warning(f"[BinanceAdapter] Failed to fetch algo orders for {symbol}: {e}")
            return []

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        Fetch ALL open orders (Standard + Algo) for a symbol or globally.
        Merges results from `fetch_open_orders` and `fapiPrivateGetOpenAlgoOrders`.
        """
        try:
            tasks = []
            
            # 1. Standard Orders
            if symbol:
                tasks.append(self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol))
            else:
                tasks.append(self._execute_with_timestamp_retry(self.exchange.fetch_open_orders))

            # 2. Algo Orders (Binance Futures Specific)
            # fapiPrivateGetOpenAlgoOrders works globally or per symbol
            algo_params = {'symbol': self._get_api_symbol(symbol)} if symbol else {}
            tasks.append(self._execute_with_timestamp_retry(self.exchange.fapiPrivateGetOpenAlgoOrders, algo_params))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            std_orders_res = results[0]
            algo_orders_res = results[1]

            all_orders = []

            # Process Standard Orders
            if isinstance(std_orders_res, list):
                for o in std_orders_res:
                    o['status'] = self.normalize_status(o.get('status'))
                all_orders.extend(std_orders_res)
            elif isinstance(std_orders_res, Exception):
                raise std_orders_res # Re-raise if standard fetch fails

            # Process Algo Orders
            if isinstance(algo_orders_res, list):
                for o in algo_orders_res:
                    o['is_algo'] = True # Explicitly flag as algo for cancellation logic
                    o['algoType'] = o.get('algoType') 
                    if not o.get('symbol') and symbol:
                        o['symbol'] = symbol
                    # Normalize algo status
                    algo_raw_status = o.get('algoStatus') or o.get('status')
                    o['status'] = self.normalize_status(algo_raw_status)
                    all_orders.append(o)
            elif isinstance(algo_orders_res, Exception):
                 # For Binance, if Algo fetch fails, we MUST NOT return just partial list
                 # otherwise reconcile_positions might think stop orders are gone.
                 raise algo_orders_res 

            return all_orders

        except Exception as e:
            print(f"❌ [BinanceAdapter] Critical error fetching open orders: {e}")
            return []

    async def fetch_positions(self, params: Dict = {}) -> List[Dict]:
        """Fetch active positions."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_positions, params=params)

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        """Create a new order."""
        return await self._execute_with_timestamp_retry(
            self.exchange.create_order,
            symbol,
            type,
            side,
            amount,
            price,
            params
        )

    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Cancel an order (handles both Standard and Algo orders with automatic fallback)."""
        
        # Normalize symbol for Binance API (e.g. BTC/USDT:USDT -> BTCUSDT)
        # Use split(':') and then replace '/' to be safest
        api_symbol = self._get_api_symbol(symbol)
        
        is_algo = params.get('is_algo', False) or params.get('algoType') is not None or 'stopLoss' in params or 'takeProfit' in params or 'stopPrice' in params
        
        # Check if it looks like an SL/TP (usually passed as ALGO)
        try:
            if is_algo:
                self.logger.info(f"[Binance] Cancelling ALGO order {order_id} for {api_symbol}")
                # For Algo orders, we might need specific params
                algo_params = params.copy()
                algo_params.update({'symbol': api_symbol})
                # CCXT usually handles the routing, but we want to be sure
                return await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order,
                    order_id,
                    symbol, # CCXT wants the unified symbol usually
                    params=params
                )
            else:
                self.logger.info(f"[Binance] Cancelling Standard order {order_id} for {api_symbol}")
                # Map Client ID to origClientOrderId if not numeric
                if not str(order_id).isdigit() and 'origClientOrderId' not in params:
                    params['origClientOrderId'] = order_id
                return await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order,
                    order_id,
                    symbol,
                    params=params
                )
                
        except Exception as e:
            err_str = str(e).lower()
            # FALLBACK: If standard cancel fails, try as algo (and vice versa)
            if "not found" in err_str or "2011" in err_str or "orderid" in err_str:
                new_type = "Standard" if is_algo else "ALGO"
                self.logger.info(f"[Binance] Order {order_id} search failed as {'ALGO' if is_algo else 'Standard'}. Retrying as {new_type}...")
                
                # Flip the logic
                retry_params = params.copy()
                if not is_algo:
                    # Try as algo
                    return await self._execute_with_timestamp_retry(
                        self.exchange.cancel_order,
                        order_id,
                        symbol,
                        params=retry_params # CCXT might handle this if we just retry without specific flags
                    )
                else:
                    # Try as standard
                    return await self._execute_with_timestamp_retry(
                        self.exchange.cancel_order,
                        order_id,
                        symbol,
                        params=retry_params
                    )
            
            self.logger.error(f"[Binance] Cancel order failed for {api_symbol}: {e}")
            raise e

    async def cancel_all_orders(self, symbol: str):
        """Cancel ALL orders for a symbol, including standard and algo orders."""
        api_symbol = self._get_api_symbol(symbol)
        self.logger.info(f"[Binance] Purging all orders for {symbol} ({api_symbol})...")
        
        # 1. Cancel standard orders
        try:
            await self._execute_with_timestamp_retry(self.exchange.cancel_all_orders, symbol)
            self.logger.info(f"[Binance] Standard orders cancelled for {symbol}")
        except Exception as e:
            self.logger.warning(f"[Binance] No standard orders to cancel or fail: {e}")

        # 2. Cancel algo orders (Stop Loss / Take Profit)
        try:
            await self._execute_with_timestamp_retry(
                self.exchange.fapiPrivateDeleteAlgoOpenOrders,
                {'symbol': api_symbol}
            )
            self.logger.info(f"[Binance] Algo orders cancelled for {symbol}")
        except Exception as e:
            err_str = str(e).lower()
            if "not found" not in err_str and "2011" not in err_str:
                self.logger.warning(f"[Binance] Failed to cancel algo orders for {symbol}: {e}")

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        """Set leverage using signed POST."""
        path = '/fapi/v1/leverage'
        base = 'https://fapi.binance.com'
        # Merge with passed params if any
        payload = {
            'symbol': self._get_api_symbol(symbol),
            'leverage': int(leverage),
            'recvWindow': 60000,
            'timestamp': self.get_synced_timestamp()
        }
        payload.update(params)
        return await self._binance_signed_post(base + path, payload)

    async def set_margin_mode(self, symbol: str, mode: str, params: Dict = {}):
        """Set margin mode using signed POST."""
        path = '/fapi/v1/marginType'
        base = 'https://fapi.binance.com'
        payload = {
            'symbol': self._get_api_symbol(symbol),
            'marginType': mode.upper(), # ISOLATED or CROSSED
            'recvWindow': 60000,
            'timestamp': self.get_synced_timestamp()
        }
        # Merge with passed params if any
        payload.update(params)
        return await self._binance_signed_post(base + path, payload)

    async def batch_create_orders(self, orders: List[Dict]):
        """Create multiple orders atomically."""
        path = '/fapi/v1/batchOrders'
        base = 'https://fapi.binance.com'
        # orders is list of dicts
        body = {
            'batchOrders': json.dumps(orders),
            'recvWindow': 60000,
            'timestamp': self.get_synced_timestamp()
        }
        
        # Helper for batch post (different content type)
        def do_post_batch(u, data):
            query = urlencode(data)
            signature = hmac.new(self.api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': self.api_key, 'Content-Type': 'application/x-www-form-urlencoded'}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=15)
            if r.status_code >= 400:
                raise Exception(f"Binance Batch API Error {r.status_code}: {r.text}")
            return r.json()

        return await asyncio.to_thread(do_post_batch, base + path, body)

    async def _binance_signed_post(self, url, params):
        """Perform signed POST to Binance Futures API."""
        if not self.api_key or not self.api_secret:
            # Should probably raise or log error, but for now rely on caller check or config
            pass

        if 'timestamp' not in params:
            # Use our synced timestamp logic specifically for raw signed posts
            params['timestamp'] = self.get_synced_timestamp()
        
        if 'recvWindow' not in params:
            params['recvWindow'] = 60000
            
        def do_request(u, p):
            query = urlencode(p)
            signature = hmac.new(self.api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': self.api_key}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=10)
            if r.status_code >= 400:
                # Silence "No need to change margin type"
                if any(s in r.text.lower() for s in ["-4046", "no need to change", "not need to change", "already"]):
                    return {"code": 200, "msg": "No change needed"}
                raise Exception(f"Binance API Error {r.status_code}: {r.text}")
            return r.json()

        return await asyncio.to_thread(do_request, url, params)

    async def fetch_leverage(self, symbol: str) -> Optional[Dict]:
        """Fetch current leverage for a symbol."""
        try:
            # CCXT fetch_leverage on Binance Futures returns a single dict with leverage
            return await self._execute_with_timestamp_retry(self.exchange.fetch_leverage, symbol)
        except Exception as e:
            # Silently return None to allow fallback in execution.py
            return None

    async def fetch_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Fetch a specific order, checking both standard and algo endpoints."""
        try:
            # Map Client ID to origClientOrderId if not numeric
            if not str(order_id).isdigit() and 'origClientOrderId' not in params:
                params['origClientOrderId'] = order_id
            res = await self._execute_with_timestamp_retry(
                self.exchange.fetch_order,
                order_id,
                symbol,
                params
            )
            res['status'] = self.normalize_status(res.get('status'))
            return res
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "does not exist" in err_str or "2013" in err_str or "orderid" in err_str:
                # OPTIMIZATION: If we already know the order is likely standard or not algo,
                # querying fapiPrivateGetOpenAlgoOrders and fetch_my_trades EVERY single monitor cycle
                # for EVERY missing order causes massive rate limiting and API spam, leading to 
                # Timestamp errors (due to Binance rejecting our rate limit overhead).
                # We will only do the Algo check if the order might realistically be an ALGO order.
                
                is_algo_hint = params.get('is_algo', False) or params.get('type', '').upper() in ['STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET']
                
                if is_algo_hint:
                    self.logger.info(f"[Binance] Order {order_id} not found in standard queue. Checking Algo orders...")
                    try:
                        algo_params = {'symbol': self._get_api_symbol(symbol)} if symbol else {}
                        open_algos = await self._execute_with_timestamp_retry(
                            self.exchange.fapiPrivateGetOpenAlgoOrders, algo_params
                        )
                        if isinstance(open_algos, list):
                            for algo in open_algos:
                                if str(algo.get('algoId')) == str(order_id) or str(algo.get('clientOrderId')) == str(order_id):
                                    algo['is_algo'] = True
                                    algo['id'] = str(algo.get('algoId', order_id))
                                    algo['status'] = self.normalize_status(algo.get('algoStatus') or 'open')
                                    return algo
                    except Exception as algo_e:
                        self.logger.warning(f"[Binance] Algo check failed for {order_id}: {algo_e}")
                        
                # BUG-02 FIX: Check trade history to see if it was recently filled and dropped from cache
                # ONLY occasionally, or we rely on the broader reconcile_positions() sync loop
                # Since fetch_order is called every monitor tick (e.g. 5x a second), fetching trade history
                # here is disastrous for rate limits. We will ONLY check trade history IF this is a specific retry
                check_history = params.get('force_history_check', False)
                if check_history:
                    try:
                        self.logger.info(f"[Binance] Checking recent trades for order {order_id}...")
                        trades = await self._execute_with_timestamp_retry(
                            self.exchange.fetch_my_trades, symbol, limit=20
                        )
                        if isinstance(trades, list):
                            match = next((t for t in trades if str(t.get('order')) == str(order_id)), None)
                            if match:
                                self.logger.info(f"[Binance] Order {order_id} found in trade history! Returning as CLOSED.")
                                return {
                                    'id': order_id, 
                                    'status': 'CLOSED', 
                                    'filled': match.get('amount', 0), 
                                    'average': match.get('price', 0),
                                    'info': match
                                }
                    except Exception as trade_e:
                        self.logger.warning(f"[Binance] Trade history check failed for {order_id}: {trade_e}")
                    
            raise e

    async def place_stop_orders(
        self, symbol: str, side: str, qty: float,
        sl: Optional[float] = None, tp: Optional[float] = None
    ) -> Dict:
        """
        Place SL and/or TP orders for Binance Futures.
        Uses STOP_MARKET for SL and TAKE_PROFIT_MARKET for TP.
        Returns {'sl_id': str|None, 'tp_id': str|None}.
        """
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        ids = {'sl_id': None, 'tp_id': None}

        if sl is not None:
            try:
                o = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol,
                    'STOP_MARKET', close_side, qty,
                    params={'stopPrice': sl, 'reduceOnly': True}
                )
                ids['sl_id'] = str(o.get('id')) if o.get('id') else None
                self.logger.info(f"[Binance] SL placed for {symbol} @ {sl} → id={ids['sl_id']}")
            except Exception as e:
                self.logger.error(f"[Binance] Failed to place SL for {symbol}: {e}")

        if tp is not None:
            try:
                o = await self._execute_with_timestamp_retry(
                    self.exchange.create_order, symbol,
                    'TAKE_PROFIT_MARKET', close_side, qty,
                    params={'stopPrice': tp, 'reduceOnly': True}
                )
                ids['tp_id'] = str(o.get('id')) if o.get('id') else None
                self.logger.info(f"[Binance] TP placed for {symbol} @ {tp} → id={ids['tp_id']}")
            except Exception as e:
                self.logger.error(f"[Binance] Failed to place TP for {symbol}: {e}")

        return ids

    async def cancel_stop_orders(
        self, symbol: str,
        sl_id: Optional[str] = None,
        tp_id: Optional[str] = None
    ):
        """
        Cancel existing SL and/or TP orders on Binance Futures.
        Tries standard cancel first, then falls back to algo-order cancel.
        """
        api_symbol = self._get_api_symbol(symbol)

        for oid in filter(None, [sl_id, tp_id]):
            cancelled = False
            # Try standard cancel first
            try:
                await self._execute_with_timestamp_retry(
                    self.exchange.cancel_order, oid, symbol
                )
                self.logger.info(f"[Binance] Cancelled stop order {oid} for {symbol}")
                cancelled = True
            except Exception as e:
                err = str(e).lower()
                if 'not found' in err or '2011' in err or 'orderid' in err:
                    # Fallback: try as algo order
                    try:
                        await self._execute_with_timestamp_retry(
                            self.exchange.fapiPrivateDeleteAlgoOrder,
                            {'symbol': api_symbol, 'algoId': oid}
                        )
                        self.logger.info(f"[Binance] Cancelled algo order {oid} (fallback)")
                        cancelled = True
                    except Exception as algo_e:
                        self.logger.warning(f"[Binance] Could not cancel stop order {oid}: std={e} algo={algo_e}")
                else:
                    self.logger.warning(f"[Binance] Cancel stop order {oid} failed: {e}")

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """
        Market-close an open position on Binance Futures.
        Cancels all standard and algo orders first to prevent reduceOnly conflicts.
        """
        # 1. Cancel all orders (handles std + algo internally)
        await self.cancel_all_orders(symbol)

        # 2. Market close with reduceOnly
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        try:
            result = await self._execute_with_timestamp_retry(
                self.exchange.create_order, symbol,
                'MARKET', close_side, qty,
                params={'reduceOnly': True}
            )
            self.logger.info(f"[Binance] Closed position {symbol} {side} qty={qty} → {result.get('id')}")
            return result
        except Exception as e:
            self.logger.error(f"[Binance] Close position failed for {symbol}: {e}")
            raise e
    def round_qty(self, symbol: str, qty: float) -> float:
        """
        Binance-specific quantity rounding.
        Uses standard CCXT precision rules.
        """
        try:
            qty_str = self.exchange.amount_to_precision(symbol, qty)
            return float(qty_str)
        except Exception:
            return round(qty, 3)

    def is_spot(self, symbol: str) -> bool:
        """Binance Spot detection."""
        # Binance Futures symbols in CCXT typically have :USDT suffix
        if ":USDT" in symbol: return False
        market = self.exchange.markets.get(symbol)
        if market and market.get('spot'): return True
        return False
