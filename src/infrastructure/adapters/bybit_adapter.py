import ccxt.async_support as ccxt
import asyncio
import os
import logging
from typing import Dict, List, Optional, Any
from .base_adapter import BaseAdapter
from .base_exchange_client import BaseExchangeClient
from src.config import BYBIT_API_KEY, BYBIT_API_SECRET
from src.utils.symbol_helper import to_api_format, to_display_format

class BybitAdapter(BaseExchangeClient, BaseAdapter):
    """
    Bybit Adapter implementation using CCXT.
    Focuses on USDT Perpetual Futures (Linear).
    BaseExchangeClient provides time synchronization and retry logic.
    """

    def __init__(self, exchange_client=None, dry_run: bool = True):
        # Initialize BaseAdapter (wrapper)
        BaseAdapter.__init__(self, exchange_client, dry_run=dry_run)
        
        client = exchange_client
        if not client:
            options = {
                'defaultType': 'swap',
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
            client = ccxt.bybit({
                'apiKey': BYBIT_API_KEY,
                'secret': BYBIT_API_SECRET,
                'options': options,
                'enableRateLimit': True,
            })
            self.exchange = client
        
        BaseExchangeClient.__init__(self, client)
        self._position_mode = None
        self._mode_lock = asyncio.Lock() # Guard for position mode flipping

    def __getattr__(self, name):
        return getattr(self.exchange, name)

    async def sync_time(self) -> bool:
        try:
            await self.sync_server_time()
            await self._fetch_and_cache_position_mode(force=True)
            return True
        except Exception as e:
            self.logger.warning(f"Failed to sync time: {e}")
            return False

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        mapping = {'8h': '4h'}
        target_tf = mapping.get(timeframe, timeframe)
        try:
            return await self._execute_with_timestamp_retry(
                self.exchange.fetch_ohlcv, symbol, target_tf, None, limit, {'category': 'linear'}
            )
        except Exception as e:
            self.logger.error(f"Fetch OHLCV failed for {symbol}: {e}")
            return []

    async def fetch_ticker(self, symbol: str) -> Dict:
        ticker = await self._execute_with_timestamp_retry(self.exchange.fetch_ticker, symbol, params={'category': 'linear'})
        if 'info' in ticker and 'markPrice' in ticker['info']:
            ticker['mark'] = float(ticker['info']['markPrice'])
        return ticker

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        try:
            params = {'category': 'linear', 'limit': 50}
            std = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol, params={**params, 'orderFilter': 'Order'})
            cond = await self._execute_with_timestamp_retry(self.exchange.fetch_open_orders, symbol, params={**params, 'orderFilter': 'StopOrder'})
            all_orders = std + cond
            for o in all_orders:
                o['status'] = self.normalize_status(o.get('status'))
                # Normalize symbol to unified format 
                raw_sym = o.get('symbol', '')
                if raw_sym and '/' not in raw_sym:
                    o['symbol'] = f"{raw_sym[:-4]}/USDT:USDT" if raw_sym.endswith('USDT') else raw_sym
            return all_orders
        except Exception as e:
            self.logger.error(f"Fetch open orders failed: {e}")
            return []

    async def fetch_positions(self, params: Dict = {}) -> List[Dict]:
        try:
            merged = {'category': 'linear', 'settleCoin': 'USDT'}
            merged.update(params)
            res = await self._execute_with_timestamp_retry(self.exchange.privateGetV5PositionList, params=merged)
            raw_list = res.get('result', {}).get('list', [])
            active = []
            for p in raw_list:
                size = float(p.get('size', 0))
                if size > 0:
                    raw_sym = p.get('symbol', '')
                    # Normalize raw symbol (e.g. DOTUSDT) to CCXT standard (e.g. DOT/USDT:USDT)
                    norm_sym = f"{raw_sym[:-4]}/USDT:USDT" if raw_sym.endswith("USDT") else raw_sym
                    
                    active.append({
                        'symbol': norm_sym,
                        'contracts': size,
                        'side': p.get('side', '').upper(),
                        'entryPrice': float(p.get('avgPrice', 0)),
                        'markPrice': float(p.get('markPrice') or 0),
                        'leverage': float(p.get('leverage', 1)),
                        'stopLoss': float(p.get('stopLoss') or 0),
                        'takeProfit': float(p.get('takeProfit') or 0),
                        'info': p
                    })
            return active
        except Exception as e:
            self.logger.error(f"Fetch positions failed: {e}")
            return []

    async def _fetch_and_cache_position_mode(self, force: bool = False) -> str:
        if self._position_mode and not force:
            return self._position_mode
        try:
            res = await self._execute_with_timestamp_retry(self.exchange.fetch_position_mode, 'BTCUSDT', {'category': 'linear'})
            mode = str(res.get('mode', '')).lower()
            self._position_mode = 'BothSide' if 'hedge' in mode or res.get('hedged') else 'MergedSingle'
        except:
            self._position_mode = 'MergedSingle'
        return self._position_mode

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        mode = await self._fetch_and_cache_position_mode()
        extra = {'category': 'linear'}
        if mode == 'BothSide':
            reduce_only = params.get('reduceOnly', False)
            if not reduce_only:
                extra['positionIdx'] = 1 if side.upper() == 'BUY' else 2
            else:
                extra['positionIdx'] = 1 if side.upper() == 'SELL' else 2
        else:
            extra['positionIdx'] = 0
            
        if price:
            price = float(self.exchange.price_to_precision(symbol, price))
            type = 'limit' # Force limit if price is provided, safety check
        # SL/TP Parameter Enrichment (strictly typed for Bybit V5)
        if 'stopLoss' in params:
            params['stopLoss'] = str(self.exchange.price_to_precision(symbol, float(params['stopLoss'])))
            extra['tpslMode'] = 'Full'
            extra['slTriggerBy'] = 'MarkPrice'
        if 'takeProfit' in params:
            params['takeProfit'] = str(self.exchange.price_to_precision(symbol, float(params['takeProfit'])))
            extra['tpslMode'] = 'Full'
            extra['tpTriggerBy'] = 'MarkPrice'

        # Precision check for Qty (Amount)
        amount = self.round_qty(symbol, amount)
        
        combined = {**extra, **params}
        try:
            return await self._execute_with_timestamp_retry(self.exchange.create_order, symbol, type, side, amount, price, combined)
        except Exception as e:
            err = str(e).lower()
            if "10001" in err and ("side invalid" in err or "position mode" in err):
                async with self._mode_lock:
                    # Re-fetch mode inside lock to see if another task already flipped it
                    current_mode = self._position_mode
                    self.logger.info(f"Bybit 10001 detected. Flipping position mode to retry...")
                    # Flip mode and retry exactly once
                    self._position_mode = 'BothSide' if current_mode == 'MergedSingle' else 'MergedSingle'
                    
                    # Re-calculate extra params for the new mode
                    extra = {'category': 'linear'}
                    if self._position_mode == 'BothSide':
                        reduce_only = params.get('reduceOnly', False)
                        if not reduce_only:
                            extra['positionIdx'] = 1 if side.upper() == 'BUY' else 2
                        else:
                            extra['positionIdx'] = 1 if side.upper() == 'SELL' else 2
                    else:
                        extra['positionIdx'] = 0
                    combined = {**params, **extra}  # FIX: extra (fix) overrides params (potentially stale)
                    return await self._execute_with_timestamp_retry(self.exchange.create_order, symbol, type, side, amount, price, combined)
            raise e

    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        extra = {'category': 'linear'}
        extra.update(params)
        try:
            return await self._execute_with_timestamp_retry(self.exchange.cancel_order, order_id, symbol, extra)
        except Exception as e:
            if "not found" in str(e).lower():
                extra['trigger'] = True
                return await self._execute_with_timestamp_retry(self.exchange.cancel_order, order_id, symbol, extra)
            raise e

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        extra = {'category': 'linear'}
        extra.update(params)
        try:
            await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, symbol, params=extra)
        except Exception as e:
            if "not modified" not in str(e).lower():
                self.logger.warning(f"Set leverage failed: {e}")

    async def fetch_balance(self) -> Dict:
        return await self._execute_with_timestamp_retry(self.exchange.fetch_balance, {'accountType': 'UNIFIED'})

    async def place_stop_orders(self, symbol: str, side: str, qty: float, sl: Optional[float] = None, tp: Optional[float] = None, is_pending: bool = False, **kwargs) -> Dict:
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        ids = {'sl_id': None, 'tp_id': None}
        
        # If is_pending is True, these are often conditional orders on standard Bybit
        # but for V5 they can also be attached. Base logic here for separate orders:
        params = kwargs.copy()
        params.update({'reduceOnly': True})
        
        if sl:
            sl_str = str(self.exchange.price_to_precision(symbol, sl))
            extra = {**params, 'stopPrice': sl_str, 'triggerDirection': 'descending' if side.upper() == 'BUY' else 'ascending'}
            o = await self.create_order(symbol, 'market', close_side, qty, params=extra)
            ids['sl_id'] = str(o.get('id'))
        if tp:
            tp_str = str(self.exchange.price_to_precision(symbol, tp))
            extra = {**params, 'stopPrice': tp_str, 'triggerDirection': 'ascending' if side.upper() == 'BUY' else 'descending'}
            o = await self.create_order(symbol, 'market', close_side, qty, params=extra)
            ids['tp_id'] = str(o.get('id'))
        return ids

    async def cancel_stop_orders(self, symbol: str, sl_id: Optional[str] = None, tp_id: Optional[str] = None):
        for oid in filter(None, [sl_id, tp_id]):
            try:
                await self.cancel_order(oid, symbol)
            except:
                pass

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        await self.cancel_all_orders(symbol)
        close_side = 'sell' if side.upper() == 'BUY' else 'buy'
        return await self.create_order(symbol, 'market', close_side, qty, params={'reduceOnly': True})

    async def cancel_all_orders(self, symbol: str):
        try:
            await self._execute_with_timestamp_retry(self.exchange.cancel_all_orders, symbol, params={'category': 'linear', 'orderFilter': 'Order'})
            await self._execute_with_timestamp_retry(self.exchange.cancel_all_orders, symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        except:
            pass

    def round_qty(self, symbol: str, qty: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except:
            return round(qty, 3)

    def infer_exit_reason(self, close_trade: Dict, pos_data: Dict) -> str:
        info = close_trade.get('info') or {}
        stop_type = str(info.get('stopOrderType', '')).lower().replace('_', '').replace(' ', '')
        if stop_type in ('stoploss',):
            return 'SL'
        if stop_type in ('takeprofit',):
            return 'TP'
            
        exit_price = float(close_trade.get('price') or 0)
        sl_price = float(pos_data.get('sl') or 0)
        tp_price = float(pos_data.get('tp') or 0)
        side = pos_data.get('side', '').upper()
        entry_price = float(pos_data.get('entry_price') or 0)
        
        if exit_price > 0 and tp_price > 0 and abs(exit_price - tp_price) / tp_price < 0.01:
            return 'TP'
        if exit_price > 0 and sl_price > 0 and abs(exit_price - sl_price) / sl_price < 0.01:
            return 'SL'
            
        if exit_price > 0 and entry_price > 0:
            if side == 'BUY':
                return 'TP' if exit_price >= entry_price else 'SL'
            else:
                return 'TP' if exit_price <= entry_price else 'SL'
        return 'SYNC(Unknown)'

    async def ensure_isolated_and_leverage(self, symbol: str, leverage: int):
        try:
            try:
                await self._execute_with_timestamp_retry(
                    self.exchange.set_margin_mode, 'isolated', symbol, params={'category': 'linear', 'buyLeverage': str(leverage), 'sellLeverage': str(leverage)}
                )
            except Exception as e:
                err = str(e).lower()
                if "110026" in err or "already" in err or "no change" in err:
                    pass
                else:
                    self.logger.debug(f"Set margin mode failed for {symbol}: {e}")

            try:
                await self._execute_with_timestamp_retry(self.exchange.set_leverage, leverage, symbol, params={'category': 'linear'})
            except Exception as e:
                if "not modified" not in str(e).lower():
                    self.logger.warning(f"Bybit set_leverage failed for {symbol}: {e}")
        except Exception as e:
            self.logger.error(f"Bybit ensure_isolated_and_leverage error for {symbol}: {e}")

    def check_min_notional(self, symbol: str, price: float, qty: float) -> tuple[bool, str, float]:
        if not price or price <= 0:
            return True, "Price unknown", qty
        market = self.exchange.market(symbol)
        min_cost = market.get('limits', {}).get('cost', {}).get('min') or 1.0
        min_amount = market.get('limits', {}).get('amount', {}).get('min') or 0.0
        notional = price * qty
        if qty < min_amount:
            return False, f"Qty {qty} < Min Amount {min_amount}", notional
        if notional < min_cost:
            return False, f"Value ${notional:.2f} < Min Cost ${min_cost:.2f}", notional
        return True, "OK", qty

    def get_unified_symbol(self, symbol: str) -> str:
        try:
            if symbol in self.exchange.markets:
                return self.exchange.markets[symbol].get('symbol', symbol)
            # Try to find by ID (e.g., BTCUSDT -> BTC/USDT:USDT)
            for m in self.exchange.markets.values():
                if m.get('id') == symbol:
                    return m.get('symbol', symbol)
            return symbol
        except:
            return symbol

    def _get_bybit_symbol(self, symbol: str) -> str:
        """Internal helper to convert unified symbol to Bybit-native symbol (e.g. BTCUSDT)."""
        try:
            if symbol in self.exchange.markets:
                return self.exchange.markets[symbol].get('id', symbol.replace('/', '').split(':')[0])
            return symbol.replace('/', '').split(':')[0]
        except:
            return symbol.replace('/', '').split(':')[0]

    async def set_position_sl_tp(self, symbol: str, side: str, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict:
        """Bybit-specific: Update SL/TP of an existing position."""
        # Use native symbol format for private Post call
        native_symbol = self._get_bybit_symbol(symbol)
        params = {'category': 'linear', 'symbol': native_symbol}
        if sl: params['stopLoss'] = str(self.exchange.price_to_precision(symbol, sl))
        if tp: params['takeProfit'] = str(self.exchange.price_to_precision(symbol, tp))
        params['tpTriggerBy'] = 'MarkPrice'
        params['slTriggerBy'] = 'MarkPrice'
        params['tpslMode'] = 'Full'
        
        try:
            res = await self._execute_with_timestamp_retry(self.exchange.privatePostV5PositionTradingStop, params=params)
            if isinstance(res, dict) and res.get('retCode') == 10001:
                return {'sl_set': sl is not None, 'tp_set': tp is not None, 'info': 'already_passed'}
            return res
        except Exception as e:
            err = str(e).lower()
            if "already passed" in err or "base_price" in err or "10001" in err:
                return {'sl_set': sl is not None, 'tp_set': tp is not None, 'info': 'already_passed'}
            raise e

    def is_tpsl_attached_supported(self) -> bool:
        return True

    def is_spot(self, symbol: str) -> bool:
        return ':' not in symbol
