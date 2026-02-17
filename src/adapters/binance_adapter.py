import asyncio
import json
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from typing import Dict, List, Optional, Any

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
        
        # Explicitly suppress FetchOpenOrders warning for Binance
        if hasattr(self.exchange, 'options'):
            self.exchange.options['warnOnFetchOpenOrdersWithoutSymbol'] = False

    async def fetch_balance(self) -> Dict:
        """Fetch account balance."""
        try:
            return await self.exchange.fetch_balance()
        except Exception as e:
            # Using print for consistency with other error messages in this file
            print(f"⚠️ [BinanceAdapter] Fetch balance failed: {e}")
            return {}

    async def sync_time(self) -> bool:
        """Sync time and load markets."""
        try:
            await self.sync_server_time()
            await self.exchange.load_markets()
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

    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch ticker with retry logic."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)

    async def fetch_tickers(self, symbols: List[str]) -> Dict:
        """Fetch multiple tickers at once."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_tickers, symbols)

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
            algo_params = {'symbol': symbol.replace('/', '')} if symbol else {}
            tasks.append(self._execute_with_timestamp_retry(self.exchange.fapiPrivateGetOpenAlgoOrders, algo_params))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            std_orders_res = results[0]
            algo_orders_res = results[1]

            all_orders = []

            # Process Standard Orders
            if isinstance(std_orders_res, list):
                all_orders.extend(std_orders_res)
            elif isinstance(std_orders_res, Exception):
                print(f"⚠️ [BinanceAdapter] Failed to fetch standard orders: {std_orders_res}")

            # Process Algo Orders
            if isinstance(algo_orders_res, list):
                # Algo orders from raw API need some normalization to match CCXT structure if used generically
                # But for now, we just pass them through as dicts, adding a flag
                for o in algo_orders_res:
                    o['algoType'] = o.get('algoType') # Ensure this field exists
                    all_orders.append(o)
            elif isinstance(algo_orders_res, Exception):
                 print(f"⚠️ [BinanceAdapter] Failed to fetch algo orders: {algo_orders_res}")

            return all_orders

        except Exception as e:
            print(f"❌ [BinanceAdapter] Critical error fetching open orders: {e}")
            return []

    async def fetch_positions(self) -> List[Dict]:
        """Fetch active positions."""
        return await self._execute_with_timestamp_retry(self.exchange.fetch_positions)

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
        """Cancel an order (handles both Standard and Algo if specified in params)."""
        # Check if it's an Algo order call
        if params.get('algoType') or params.get('stopPrice'): # Heuristic for Algo
             # For explicit Algo Cancel, we might need fapiPrivateDeleteOrder
             # But ccxt cancel_order usually handles it if 'type' is passed or params correctly set?
             # Actually, execution.py used fapiPrivateDeleteOrder explicitly for algo orders
             pass
        
        # If execution.py passed 'algoType' or we know it's algo, use specific endpoint
        # But for general compatibility, let's try standard cancel first, or rely on execution.py passing the right method?
        # Better: Implementation Plan said "Move specific Binance logic ... into this adapter".
        
        # So providing a specialized method for algo cancel might be good.
        # But 'cancel_order' signature is standard.
        # Let's inspect params.
        
        is_algo = params.pop('is_algo', False) 
        
        if is_algo:
             return await self._execute_with_timestamp_retry(
                 self.exchange.fapiPrivateDeleteOrder,
                 {'symbol': symbol.replace('/', ''), 'orderId': order_id}
             )
        else:
            return await self._execute_with_timestamp_retry(
                self.exchange.cancel_order,
                order_id,
                symbol,
                params
            )

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        """Set leverage using signed POST."""
        path = '/fapi/v1/leverage'
        base = 'https://fapi.binance.com'
        # Merge with passed params if any
        payload = {
            'symbol': symbol.replace('/', ''),
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
            'symbol': symbol.replace('/', ''),
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
            signature = hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': BINANCE_API_KEY, 'Content-Type': 'application/x-www-form-urlencoded'}
            full = f"{u}?{query}&signature={signature}"
            r = requests.post(full, headers=headers, timeout=15)
            if r.status_code >= 400:
                raise Exception(f"Binance Batch API Error {r.status_code}: {r.text}")
            return r.json()

        return await asyncio.to_thread(do_post_batch, base + path, body)

    async def _binance_signed_post(self, url, params):
        """Perform signed POST to Binance Futures API."""
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
             # Should probably raise or log error, but for now rely on caller check or config
             pass

        if 'timestamp' not in params:
            params['timestamp'] = self.get_synced_timestamp()
            
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

        return await asyncio.to_thread(do_request, url, params)

    async def fetch_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Fetch a specific order."""
        return await self._execute_with_timestamp_retry(
            self.exchange.fetch_order,
            order_id,
            symbol,
            params
        )
