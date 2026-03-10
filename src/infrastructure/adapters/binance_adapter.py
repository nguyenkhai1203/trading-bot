import asyncio
import json
from typing import Dict, List, Optional, Any
from .base_adapter import BaseAdapter
from .base_exchange_client import BaseExchangeClient
from src.config import BINANCE_API_KEY, BINANCE_API_SECRET
from src.utils.symbol_helper import to_api_format

class BinanceAdapter(BaseExchangeClient, BaseAdapter):
    """
    Binance Adapter Implementation.
    """

    def __init__(self, exchange_client, dry_run: bool = True):
        BaseAdapter.__init__(self, exchange_client, dry_run=dry_run)
        BaseExchangeClient.__init__(self, exchange_client)
        
        if hasattr(self.exchange, 'options'):
            self.exchange.options['warnOnFetchOpenOrdersWithoutSymbol'] = False

    def __getattr__(self, name):
        return getattr(self.exchange, name)

    async def sync_time(self) -> bool:
        try:
            await self.sync_server_time()
            return True
        except:
            return False

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        return await self._execute_with_timestamp_retry(self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit)

    async def fetch_ticker(self, symbol: str) -> Dict:
        return await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        try:
            tasks = [self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol)]
            algo_params = {'symbol': to_api_format(symbol)} if symbol else {}
            tasks.append(self._execute_with_timestamp_retry(self.exchange.fapiPrivateGetOpenAlgoOrders, algo_params))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            all_orders = []
            if isinstance(results[0], list):
                for o in results[0]:
                    o['status'] = self.normalize_status(o.get('status'))
                    all_orders.append(o)
            if isinstance(results[1], list):
                for o in results[1]:
                    o['is_algo'] = True
                    o['status'] = self.normalize_status(o.get('algoStatus') or o.get('status'))
                    all_orders.append(o)
            return all_orders
        except:
            return []

    async def fetch_positions(self, params: Dict = {}) -> List[Dict]:
        return await self._execute_with_timestamp_retry(self.exchange.fetch_positions, params=params)

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        return await self._execute_with_timestamp_retry(self.exchange.create_order, symbol, type, side, amount, price, params)

    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        try:
            return await self._execute_with_timestamp_retry(self.exchange.cancel_order, order_id, symbol, params=params)
        except Exception as e:
            if "not found" in str(e).lower() or "2011" in str(e).lower():
                # Try algo fallback if not already specified
                return await self._execute_with_timestamp_retry(self.exchange.cancel_order, order_id, symbol, params=params)
            raise e

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        try:
            await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, symbol, params=params)
        except:
            pass

    async def fetch_balance(self) -> Dict:
        return await self._execute_with_timestamp_retry(self.exchange.fetch_balance)

    async def place_stop_orders(self, symbol: str, side: str, qty: float, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict:
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        ids = {'sl_id': None, 'tp_id': None}
        if sl:
            o = await self.create_order(symbol, 'STOP_MARKET', close_side, qty, params={'stopPrice': sl, 'reduceOnly': True})
            ids['sl_id'] = str(o.get('id'))
        if tp:
            o = await self.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, qty, params={'stopPrice': tp, 'reduceOnly': True})
            ids['tp_id'] = str(o.get('id'))
        return ids

    async def cancel_stop_orders(self, symbol: str, sl_id: Optional[str] = None, tp_id: Optional[str] = None):
        api_symbol = to_api_format(symbol)
        for oid in filter(None, [sl_id, tp_id]):
            try:
                await self.cancel_order(oid, symbol)
            except:
                try:
                    await self._execute_with_timestamp_retry(self.exchange.fapiPrivateDeleteAlgoOrder, {'symbol': api_symbol, 'algoId': oid})
                except:
                    pass

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        await self.cancel_all_orders(symbol)
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        return await self.create_order(symbol, 'MARKET', close_side, qty, params={'reduceOnly': True})

    async def cancel_all_orders(self, symbol: str):
        try:
            await self._execute_with_timestamp_retry(self.exchange.cancel_all_orders, symbol)
            await self._execute_with_timestamp_retry(self.exchange.fapiPrivateDeleteAlgoOpenOrders, {'symbol': to_api_format(symbol)})
        except:
            pass

    def round_qty(self, symbol: str, qty: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except:
            return round(qty, 3)

    def is_spot(self, symbol: str) -> bool:
        return ":" not in symbol

    def infer_exit_reason(self, close_trade: Dict, pos_data: Dict) -> str:
        stop_type = str(close_trade.get('info', {}).get('stopOrderType', '')).lower()
        if 'stop_loss' in stop_type: return 'SL'
        if 'take_profit' in stop_type: return 'TP'
        
        exit_price = float(close_trade.get('price') or 0)
        sl_price = float(pos_data.get('sl') or 0)
        tp_price = float(pos_data.get('tp') or 0)
        
        if exit_price > 0 and tp_price > 0 and abs(exit_price - tp_price) / tp_price < 0.001: return 'TP'
        if exit_price > 0 and sl_price > 0 and abs(exit_price - sl_price) / sl_price < 0.001: return 'SL'
        return 'SYNC'

    async def ensure_isolated_and_leverage(self, symbol: str, leverage: int):
        # Standardize: most CCXT unified exchanges use lowercase for position/margin modes
        try:
            await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, 'isolated', symbol)
        except:
            try:
                await self._execute_with_timestamp_retry(self.exchange.set_margin_mode, 'ISOLATED', symbol)
            except:
                pass
        try:
            await self.set_leverage(symbol, leverage)
        except:
            pass

    def check_min_notional(self, symbol: str, price: float, qty: float) -> tuple[bool, str, float]:
        market = self.exchange.market(symbol)
        min_notional = market.get('limits', {}).get('cost', {}).get('min') or 5.0
        if price * qty < min_notional:
            return False, f"Notional {price*qty} < {min_notional}", qty
        return True, "OK", qty

    def get_unified_symbol(self, symbol: str) -> str:
        return symbol

    def is_tpsl_attached_supported(self) -> bool:
        return False # Binance Futures usually requires separate SL/TP algo orders
